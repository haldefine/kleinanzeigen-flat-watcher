#!/usr/bin/env python3
"""Scrape Kleinanzeigen rental ads (Augsburg, warm rent <= limit) and contact them.

Dry-run by default. Use --send to actually deliver messages.
"""
import argparse, csv, json, os, random, re, sys, time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://www.kleinanzeigen.de"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
STATE_DIR = os.environ.get("STATE_DIR", HERE)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
RESULTS_CSV = os.path.join(HERE, "results.csv")
COOKIE_FILE = os.path.join(HERE, "cookies.txt")
MESSAGE_FILE = os.path.join(HERE, "message.txt")
AUTH_FILE = os.path.join(HERE, "auth.txt")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


# ---------------------------------------------------------------- helpers
def money(text):
    """'1.830 €' -> 1830.0 ; '750,50' -> 750.5 ; None if no number."""
    if not text:
        return None
    m = re.search(r"\d[\d.]*(?:,\d+)?", text)
    if not m:
        return None
    return float(m.group(0).replace(".", "").replace(",", "."))


def load_cookies():
    """Accept a raw 'Cookie:' header string or a Cookie-Editor style JSON export."""
    if not os.path.exists(COOKIE_FILE):
        return {}
    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    if not raw:
        return {}
    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return {c["name"]: c["value"] for c in data if "name" in c}
    raw = re.sub(r"^\s*Cookie:\s*", "", raw, flags=re.I)
    jar = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE, encoding="utf-8"))
    return {"contacted": {}, "skipped": {}}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"Missing {CONFIG_FILE} — see README.md")
    return json.load(open(CONFIG_FILE, encoding="utf-8"))



def browser_cookies():
    """Read the live kleinanzeigen session straight out of the browser.

    Avoids re-copying auth.txt by hand: as long as you stay logged in, the
    browser keeps the session fresh and each run picks up current cookies.
    Needs `pip install browser-cookie3`.
    """
    try:
        import browser_cookie3
    except ImportError:
        sys.exit("use_browser_cookies is on but browser-cookie3 is not installed:\n"
                 "    pip install browser-cookie3")
    for loader in (browser_cookie3.firefox, browser_cookie3.chrome):
        try:
            jar = {c.name: c.value for c in loader(domain_name="kleinanzeigen.de")}
        except Exception:
            continue
        if "access_token" in jar:
            return jar
    return {}


def load_auth():
    """Read the session out of a `Copy as cURL` capture in auth.txt.

    Messaging needs the www.kleinanzeigen.de session cookies (access_token,
    refresh_token, CSRF-TOKEN ...). A capture taken from a request to
    gateway.kleinanzeigen.de does NOT contain them -- it must be a request to
    www.kleinanzeigen.de.
    """
    raw = os.environ.get("COOKIE_HEADER", "")
    if not raw:
        if not os.path.exists(AUTH_FILE):
            sys.exit(f"Missing {AUTH_FILE} and $COOKIE_HEADER — see README.md")
        raw = open(AUTH_FILE, encoding="utf-8").read()

    m = re.search(r"-H 'Cookie: ([^']*)'", raw) or \
        re.search(r"^Cookie:\s*(.+)$", raw, re.M | re.I)
    if not m:
        sys.exit(f"No Cookie header found in {AUTH_FILE}")
    jar = {}
    for part in m.group(1).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    if "access_token" not in jar:
        sys.exit("auth.txt has no 'access_token' cookie — capture a request to "
                 "www.kleinanzeigen.de (not gateway.kleinanzeigen.de).")

    # anti-bot token that the contact form submits alongside the message
    w = re.search(r"contactPosterWenkseSessionId=([^&'\s]+)", raw)
    return jar, (w.group(1) if w else "")


# profile names are often companies or slogans ("Ohne Makler - Privat vom
# Eigentümer"); greeting those by name reads worse than no name at all
NOT_A_PERSON = re.compile(
    r"makler|immobilien|hausverwaltung|verwaltung|vermietung|wohnen|wohnung|"
    r"gmbh|ug\b|\bag\b|e\.?k\.?|kg\b|mbh|privat|eigent(ü|ue)mer|service|"
    r"team|group|consult|invest|estate|realt|haus\b|apartment|zimmer|www\.|@|"
    r"\.de\b|\.com\b", re.I)


def personal_name(name):
    """Return the name only if it plausibly belongs to a person, else ''."""
    name = " ".join(name.split())
    if not name or len(name) > 30 or NOT_A_PERSON.search(name):
        return ""
    words = name.split()
    if len(words) > 3 or any(ch.isdigit() for ch in name):
        return ""
    return name


# ---------------------------------------------------------------- scraping
class Kleinanzeigen:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "de-DE,de;q=0.9",
        })
        self.jar, self.wenkse = {}, ""
        if cfg.get("use_browser_cookies"):
            self.jar = browser_cookies()
        if not self.jar and os.path.exists(AUTH_FILE):
            self.jar, self.wenkse = load_auth()
        self.s.cookies.update(self.jar or load_cookies())

    def get(self, url):
        delay = random.uniform(*self.cfg["delay_seconds"])
        time.sleep(delay)
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        if "captcha" in r.url.lower() or "Ich bin kein Roboter" in r.text:
            sys.exit("Blocked by captcha — stop, wait a while, and slow down delay_seconds.")
        return r

    # ---- search
    def search_urls(self, pages):
        """Yield ad URLs from the paginated search results."""
        c = self.cfg
        seen = []
        for page in range(1, pages + 1):
            seite = "" if page == 1 else f"seite:{page}/"
            # anzeige:angebote drops "Gesuche" (people looking for a flat);
            # preis:: filters the listed (usually cold) rent -- warm rent is
            # verified per ad in warm_rent().
            url = (f"{BASE}/s-{c['category_slug']}/{c['location_slug']}/"
                   f"anzeige:angebote/preis::{int(c['max_warm_rent'])}/{seite}"
                   f"c{c['category_id']}l{c['location_id']}")
            print(f"[search] page {page}: {url}")
            soup = BeautifulSoup(self.get(url).text, "html.parser")
            found = 0
            for a in soup.select('a[href^="/s-anzeige/"]'):
                full = BASE + a["href"].split("?")[0]
                if full not in seen:
                    seen.append(full)
                    found += 1
            print(f"          {found} new ads")
            if found == 0:
                break
        return seen

    # ---- detail
    def parse_ad(self, url):
        soup = BeautifulSoup(self.get(url).text, "html.parser")
        ad = {"url": url, "id": re.search(r"/(\d+)-\d+-\d+$", url).group(1)}

        t = soup.select_one("#viewad-title")
        ad["title"] = t.get_text(strip=True) if t else ""
        p = soup.select_one("#viewad-price")
        ad["price"] = money(p.get_text(strip=True)) if p else None

        attrs = {}
        for li in soup.select("li.addetailslist--detail"):
            v = li.select_one(".addetailslist--detail--value")
            if v:
                val = v.get_text(strip=True)
                key = li.get_text(strip=True).replace(val, "").strip()
                attrs[key] = val
        ad["attrs"] = attrs
        ad["rooms"] = attrs.get("Zimmer", "?")
        ad["size"] = attrs.get("Wohnfläche", "?")

        d = soup.select_one("#viewad-description-text")
        ad["description"] = d.get_text(" ", strip=True) if d else ""
        loc = soup.select_one("#viewad-locality")
        ad["address"] = loc.get_text(strip=True) if loc else ""

        # ads carry one of two contact forms: a simple name+phone one, or an
        # extended tenant application (Schufa, employment, income ...). Read the
        # actual fields so the payload matches whichever this ad uses.
        form = soup.find("form", action="/s-anbieter-kontaktieren.json")
        ad["contactable"] = form is not None
        ad["fields"], ad["hidden"] = set(), {}
        if form:
            for el in form.find_all(["input", "textarea", "select"]):
                name = el.get("name")
                if not name:
                    continue
                ad["fields"].add(name)
                if el.get("type") == "hidden":
                    ad["hidden"][name] = el.get("value") or ""

        seller = soup.select_one("#viewad-contact .userprofile-vip a") or \
            soup.select_one("#viewad-contact .userprofile-vip")
        ad["seller"] = personal_name(seller.get_text(" ", strip=True) if seller else "")

        meta = soup.find("meta", {"name": "_csrf"})
        ad["csrf"] = meta["content"] if meta else None
        hdr = soup.find("meta", {"name": "_csrf_header"})
        ad["csrf_header"] = hdr["content"] if hdr else "X-CSRF-TOKEN"

        ad["warm"], ad["warm_source"] = self.warm_rent(ad)
        return ad

    @staticmethod
    def warm_rent(ad):
        """Best-effort warm rent. Returns (value, source)."""
        a, price, desc = ad["attrs"], ad["price"], ad["description"]

        if a.get("Warmmiete"):
            return money(a["Warmmiete"]), "attr:Warmmiete"

        nk, hz = money(a.get("Nebenkosten")), money(a.get("Heizkosten"))
        if price is not None and (nk or hz):
            return price + (nk or 0) + (hz or 0), "attr:kalt+nk"

        m = re.search(r"warmmiete\D{0,20}(\d[\d.]*(?:,\d+)?)", desc, re.I)
        if m:
            return money(m.group(0)), "desc:Warmmiete"

        m = re.search(r"(\d[\d.]*(?:,\d+)?)\s*(?:€|EUR)?\s*warm\b", desc, re.I)
        if m:
            return money(m.group(1)), "desc:warm"

        if price is not None and re.search(
                r"(?:inkl(?:usive)?\.?\s*(?:aller\s*)?(?:nk|nebenkosten|betriebskosten)|"
                r"warmmiete|alles inklusive|all.?in)", desc, re.I):
            return price, "desc:inkl.NK"

        m = re.search(r"(?:nebenkosten|nk)\D{0,20}(\d[\d.]*(?:,\d+)?)", desc, re.I)
        if m and price is not None:
            return price + money(m.group(1)), "desc:kalt+nk"

        return price, "unknown(price only)"

    # ---- messaging
    def check_auth(self):
        r = self.s.get(f"{BASE}/m-meine-anzeigen.html", timeout=30,
                       allow_redirects=True)
        ok = "login" not in r.url.lower() and "m-meine-anzeigen" in r.url
        return ok, (f"session valid, wenkse token "
                    f"{'present' if self.wenkse else 'MISSING'}" if ok else r.url)

    def send_message(self, ad, text):
        """POST /s-anbieter-kontaktieren.json with this ad's own form fields."""
        app = self.cfg["applicant"]
        # hidden inputs (adId, adType, locationId and a fresh anti-bot token)
        payload = dict(ad["hidden"])
        payload["message"] = text
        if not payload.get("contactPosterWenkseSessionId"):
            payload["contactPosterWenkseSessionId"] = self.wenkse

        optional = {
            "contactName": f"{app['first_name']} {app['last_name']}".strip(),
            "contactFirstName": app["first_name"],
            "contactLastName": app["last_name"],
            "phoneNumber": self.cfg.get("phone_number", ""),
            "salutation": app["salutation"],
            "street": app["street"],
            "zipCode": app["zip_code"],
            "currentSchufaInformation": app["schufa"],
            "numberOfPersonsInHousehold": app["household"],
            "employment": app["employment"],
            "householdNetIncome": app["income"],
            "pets": app["pets"],
        }
        for key, value in optional.items():
            if key in ad["fields"]:
                payload[key] = value

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": ad.get("csrf") or self.jar.get("CSRF-TOKEN", ""),
            "Origin": BASE,
            "Referer": ad["url"],
        }
        r = self.s.post(f"{BASE}/s-anbieter-kontaktieren.json",
                        data=payload, headers=headers, timeout=30)
        body = " ".join(r.text.split())
        ok = r.status_code == 200 and '"ERROR"' not in body
        return ok, r.status_code, body[:600]


# ---------------------------------------------------------------- main
def load_template(cfg):
    """message.txt wins over config.json's message_template."""
    if os.path.exists(MESSAGE_FILE):
        text = open(MESSAGE_FILE, encoding="utf-8").read().strip()
        if text:
            return text
    return cfg["message_template"]


def build_message(cfg, ad, template):
    text = template.format(
        title=ad["title"], price=ad["price"], warm=ad["warm"],
        rooms=ad["rooms"], size=ad["size"], address=ad["address"],
        seller=ad.get("seller", ""), name=cfg["contact_name"],
    )
    # an unknown seller leaves "Guten Tag ," -- tidy the dangling space
    return re.sub(r" +([,.!?])", r"\1", text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true",
                    help="actually send messages (default: dry run)")
    ap.add_argument("--pages", type=int, default=1, help="search pages to scan")
    ap.add_argument("--limit", type=float, help="override max warm rent")
    ap.add_argument("--max-sends", type=int, help="override cap on messages per run")
    ap.add_argument("--check-auth", action="store_true",
                    help="verify cookies are valid, then exit")
    args = ap.parse_args()

    cfg = load_config()
    if args.limit:
        cfg["max_warm_rent"] = args.limit
    # 0 (or null) means no cap -- send to every match found
    max_sends = args.max_sends or cfg["max_messages_per_run"] or float("inf")

    kl = Kleinanzeigen(cfg)

    if args.check_auth:
        ok, url = kl.check_auth()
        print(f"{'✅ Logged in' if ok else '❌ NOT logged in'} (landed on {url})")
        sys.exit(0 if ok else 1)

    if args.send:
        ok, url = kl.check_auth()
        if not ok:
            sys.exit(f"❌ Cookies invalid/expired (redirected to {url}). "
                     "Refresh cookies.txt — nothing was sent.")
        print("✅ Authenticated")

    template = load_template(cfg)
    state = load_state()
    urls = kl.search_urls(args.pages)
    print(f"\n[found] {len(urls)} ads total\n")

    rows, sent = [], 0
    for url in urls:
        ad_id = re.search(r"/(\d+)-\d+-\d+$", url)
        if not ad_id:
            continue
        ad_id = ad_id.group(1)
        if ad_id in state["contacted"]:
            print(f"[skip] {ad_id} already contacted")
            continue

        try:
            ad = kl.parse_ad(url)
        except Exception as e:
            print(f"[warn] {url}: {e}")
            continue

        reason = None
        if ad["warm"] is None:
            reason = "no price found"
        elif ad["warm"] > cfg["max_warm_rent"]:
            reason = f"warm {ad['warm']:.0f} € > {cfg['max_warm_rent']:.0f} €"
        elif ad["warm_source"].startswith("unknown") and not cfg["include_unknown_warm"]:
            reason = "warm rent unknown"
        elif any(k.lower() in (ad["title"] + " " + ad["description"]).lower()
                 for k in cfg["exclude_keywords"]):
            reason = "excluded keyword"
        elif not ad["contactable"]:
            reason = "no contact form"

        status = reason or ("SEND" if args.send else "MATCH (dry run)")
        warm_str = f"{ad['warm']:.0f}" if ad["warm"] is not None else "?"
        print(f"[{'skip' if reason else 'hit '}] {warm_str:>5} € "
              f"({ad['warm_source']:<18}) {ad['title'][:55]}"
              + (f"  -> {reason}" if reason else ""))

        rows.append({
            "id": ad_id, "title": ad["title"], "warm": ad["warm"],
            "warm_source": ad["warm_source"], "price": ad["price"],
            "rooms": ad["rooms"], "size": ad["size"], "address": ad["address"],
            "url": url, "status": status,
        })

        if reason:
            state["skipped"][ad_id] = reason
            continue

        if args.send:
            if sent >= max_sends:
                print(f"[stop] reached max_messages_per_run={max_sends:.0f}")
                break
            msg = build_message(cfg, ad, template)
            ok, code, body = kl.send_message(ad, msg)
            if ok:
                sent += 1
                state["contacted"][ad_id] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "title": ad["title"], "warm": ad["warm"], "url": url,
                }
                print(f"       ✉️  sent ({sent}/{max_sends})")
            else:
                print(f"       ❌ send failed [{code}]: {body}")
            save_state(state)
            time.sleep(random.uniform(*cfg["message_delay_seconds"]))

    save_state(state)
    if rows:
        with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    hits = [r for r in rows if r["status"] in ("SEND", "MATCH (dry run)")]
    print(f"\n{'='*70}\nScanned {len(rows)} ads | {len(hits)} match | {sent} messaged")
    print(f"Report: {RESULTS_CSV}")
    if not args.send and hits:
        print("Dry run — review results.csv, then re-run with --send")


if __name__ == "__main__":
    main()
