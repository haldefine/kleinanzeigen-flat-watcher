# Kleinanzeigen Wohnungs-Bot (Augsburg, Warmmiete ≤ 800 €)

Scans `kleinanzeigen.de` rental ads, computes the **warm rent** from each ad's
detail page, and optionally sends a contact message to the matching ones.

## Setup

```bash
pip install requests beautifulsoup4
```

1. **Session** — log into kleinanzeigen.de, open DevTools → Network, right-click
   any request to **www.kleinanzeigen.de** → Copy → **Copy as cURL**, and save it
   as `auth.txt`. It must be a `www` request: a capture from
   `gateway.kleinanzeigen.de` carries different cookies and will not authenticate.
   Only the `Cookie:` header is read.

   To stop copying it by hand, set `"use_browser_cookies": true` in
   `config.json` and `pip install browser-cookie3` — the session is then read
   live from Firefox/Chrome on every run, staying valid as long as you remain
   logged in.
2. **Message** — `message.txt` holds the text (it overrides `message_template`
   in `config.json`). Placeholders: `{seller}`, `{title}`, `{rooms}`, `{size}`,
   `{address}`, `{price}`, `{warm}`, `{name}`. `{seller}` is used only when the
   profile looks like a person's name; agencies and slogans fall back to a plain
   "Guten Tag,".
3. **Applicant** — `config.json` → `applicant`. Some ads use an extended tenant
   form; these values fill it. `income` and `salutation` default to
   `NOT_SPECIFIED` — set them, since landlords filter on income.
   - `salutation`: `NOT_SPECIFIED` `FEMALE` `MALE` `DIVERS`
   - `schufa`: `AVAILABLE` `NOT_AVAILABLE` `NOT_SPECIFIED`
   - `household`: `ONE_PERSON_HOUSEHOLD` `TWO_PERSON_HOUSEHOLD` `FAMILY` `FLAT_SHARE`
   - `employment`: `EMPLOYEE` `FREELANCER` `OFFICIAL` `APPRENTICE` `STUDENT`
     `PH_D_STUDENT` `HOMEMAKER` `JOBSEEKER` `PENSIONER` `OTHER`
   - `income`: `LESS_THAN_500_EUR` `EUR_500_TO_1000` `EUR_1000_TO_1500`
     `EUR_1500_TO_2000` `EUR_2000_TO_3000` `EUR_3000_TO_4000` `EUR_4000_TO_5000`
     `MORE_THAN_5000_EUR`

Check the session before a run:

```bash
python3 kleinanzeigen_bot.py --check-auth
python3 test_send.py <ad_url> --yes    # send exactly one message
```

## How messaging works

`POST /s-anbieter-kontaktieren.json`, form-encoded, with the `X-CSRF-TOKEN`
header read fresh from each ad page. Ads carry **one of two contact forms** — a
simple `contactName` + `phoneNumber` one, or an extended tenant application
(Schufa, employment, income, household) — so the payload is built from the
fields that ad actually has. Each ad page also embeds its own
`contactPosterWenkseSessionId` anti-bot token, which is read per ad.

A rejected message still returns HTTP 200 with
`{"status":"ERROR","fieldErrors":[...]}`, so the body is checked, not the code.

## Usage

```bash
python3 kleinanzeigen_bot.py                 # dry run, 3 pages -> results.csv
python3 kleinanzeigen_bot.py --pages 5       # scan more pages
python3 kleinanzeigen_bot.py --limit 750     # different warm-rent ceiling
python3 kleinanzeigen_bot.py --send          # actually send messages
```

**Dry run is the default.** Review `results.csv` before using `--send`.

## How the warm rent is determined

The search URL filters on the *listed* price, which for rentals is usually the
**Kaltmiete** — so the price filter alone produces false positives (a real
Augsburg ad listed at 750 € has a Warmmiete of 1.000 €). Each ad's detail page
is therefore fetched and the warm rent resolved in this order:

| # | Source | `warm_source` |
|---|--------|---------------|
| 1 | Structured `Warmmiete` attribute | `attr:Warmmiete` |
| 2 | Price + `Nebenkosten` + `Heizkosten` attributes | `attr:kalt+nk` |
| 3 | "Warmmiete: 750 €" in the description | `desc:Warmmiete` |
| 4 | "750 € warm" in the description | `desc:warm` |
| 5 | "inkl. NK" / "all-in" → price counts as warm | `desc:inkl.NK` |
| 6 | Price + "NK 120 €" from the description | `desc:kalt+nk` |
| 7 | Nothing found → listed price used as-is | `unknown(price only)` |

Roughly half of real ads carry no structured cost data, so case 7 is common.
`include_unknown_warm: true` keeps those ads (they may exceed 800 € warm); set
it to `false` for confirmed matches only. Always check `warm_source` in the CSV.

## Files

- `state.json` — ads already contacted; never messaged twice, safe to re-run.
- `results.csv` — every ad scanned this run, with warm rent, source, status.
- `cookies.txt` — your session (gitignored; do not share it).

## Rate limiting & account safety

Automated scraping/messaging is against Kleinanzeigen's ToS, and accounts that
send many near-identical messages can get throttled or banned. Defaults are kept
deliberately slow and small: 3–7 s between page loads, 25–60 s between messages,
`max_messages_per_run: 10`. Keep them low, and personalise `message_template`
rather than blasting the same text at every landlord. The script aborts if it
hits a captcha.

## 24/7 on Railway

`watch.py` polls search page 1 and messages new matches as they appear.

**Speed:** what wins a flat is *noticing it early*, not sending fast. The watcher
fetches one cheap search page per cycle and only opens detail pages for ad ids it
has never seen, so a new listing is contacted roughly `POLL_SECONDS` after it goes
live. `POLL_SECONDS=120` is a good default; 60 is fine. Below that you gain almost
nothing — listings do not appear that often — while looking far more like a bot.
Sending stays human-paced (`message_delay_seconds`) because new matches arrive a
few per hour at most, so fast sending buys no speed and is the thing that gets
accounts blocked.

### Deploy

1. Push this folder to a Git repo (`.gitignore` already excludes `auth.txt`,
   `cookies.txt` and `state.json` — the session must never be committed).
2. Railway → New Project → Deploy from repo. Nixpacks picks up
   `requirements.txt`; `railway.json` sets the start command and always-restart.
3. Add a **Volume** mounted at `/data` — without it `state.json` is lost on every
   redeploy.
4. Set variables (see `.env.example`):

   | Variable | Value |
   |---|---|
   | `COOKIE_HEADER` | contents of `cookie_header.txt` — see below |
   | `STATE_DIR` | `/data` |
   | `POLL_SECONDS` | `120` |
   | `SEED_FIRST_RUN` | `1` |
   | `WEBHOOK_URL` | optional, POSTs `{"text": ...}` per send and on session loss |
   | `DRY_RUN` | `1` for the first deploy, then `0` |

**Do not paste `auth.txt` into `COOKIE_HEADER`.** It is a multi-line cURL blob
and env vars truncate at the first line. Run:

```bash
python3 make_env.py     # -> cookie_header.txt, one line, session cookies only
```

and paste that file's contents instead. It refuses to write the file if the
login cookies are missing, so a bad capture is caught before you deploy.

**First run seeds.** With `SEED_FIRST_RUN=1` the first cycle records everything
currently listed as *seen* without messaging it, so a fresh deploy never blasts
the whole backlog. Only listings that appear afterwards get contacted.

### The one thing that will break

The session in `COOKIE_HEADER` eventually expires, and a server has no browser to
re-capture it from (`use_browser_cookies` cannot work there). When that happens
the watcher logs `AUTH LOST`, fires the webhook, and retries every 5 minutes
without sending — it will not silently do nothing. Fix by pasting a fresh
`Cookie:` header into the Railway variable; the service restarts itself.

Set `WEBHOOK_URL` (any endpoint taking `{"text": ...}` — a Discord/Slack webhook
or ntfy) or you will not find out the session died until you check the logs.
