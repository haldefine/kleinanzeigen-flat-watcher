#!/usr/bin/env python3
"""24/7 watcher: poll page 1 for NEW listings and contact the matches.

What actually wins a flat is noticing it early, not sending fast. So this polls
often and cheaply (one search page, detail pages only for ad ids never seen
before) and keeps normal human pacing between messages.

Env:
  COOKIE_HEADER   the `Cookie: ...` header for www.kleinanzeigen.de  (required)
  STATE_DIR       where state.json lives; use a Railway volume       (/data)
  POLL_SECONDS    base interval between scans                        (120)
  SEED_FIRST_RUN  "1" = mark current listings seen without messaging (1)
  WEBHOOK_URL     optional POST {"text": ...} on sends and auth loss
  DRY_RUN         "1" = never send, just log what would be sent
"""
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

import kleinanzeigen_bot as k

POLL = int(os.environ.get("POLL_SECONDS", "120"))
SEED = os.environ.get("SEED_FIRST_RUN", "1") == "1"
DRY = os.environ.get("DRY_RUN", "") == "1"
WEBHOOK = os.environ.get("WEBHOOK_URL", "")


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp}Z  {msg}", flush=True)


def notify(text):
    if not WEBHOOK:
        return
    try:
        requests.post(WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        log(f"webhook failed: {e}")


def scan_once(kl, cfg, template, state, seeding):
    """One poll. Returns (ads seen, new ads examined, messages sent)."""
    sent = seen = fresh = 0
    for url in kl.search_urls(1):
        seen += 1
        ad_id = k.re.search(r"/(\d+)-\d+-\d+$", url)
        if not ad_id:
            continue
        ad_id = ad_id.group(1)
        if ad_id in state["contacted"] or ad_id in state["skipped"]:
            continue
        fresh += 1

        if seeding:
            # first run on a fresh volume: remember what is already live so the
            # backlog is not blasted all at once
            state["skipped"][ad_id] = "seeded"
            continue

        try:
            ad = kl.parse_ad(url)
        except Exception as e:
            log(f"parse failed {ad_id}: {e}")
            continue

        reason = None
        if ad["warm"] is None:
            reason = "no price"
        elif ad["warm"] > cfg["max_warm_rent"]:
            reason = f"warm {ad['warm']:.0f} > {cfg['max_warm_rent']:.0f}"
        elif ad["warm_source"].startswith("unknown") and not cfg["include_unknown_warm"]:
            reason = "warm unknown"
        elif any(w.lower() in (ad["title"] + " " + ad["description"]).lower()
                 for w in cfg["exclude_keywords"]):
            reason = "excluded keyword"
        elif not ad["contactable"]:
            reason = "no contact form"

        if reason:
            state["skipped"][ad_id] = reason
            k.save_state(state)
            log(f"skip {ad_id} ({reason}) {ad['title'][:45]}")
            continue

        if DRY:
            log(f"DRY would send -> {ad_id} {ad['warm']:.0f} € {ad['title'][:45]}")
            state["skipped"][ad_id] = "dry-run"
            k.save_state(state)
            continue

        ok, code, body = kl.send_message(ad, k.build_message(cfg, ad, template))
        if ok:
            sent += 1
            state["contacted"][ad_id] = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "title": ad["title"], "warm": ad["warm"], "url": url,
            }
            log(f"SENT {ad_id} {ad['warm']:.0f} € {ad['title'][:45]}")
            notify(f"✉️ {ad['warm']:.0f} € — {ad['title'][:60]}\n{url}")
        else:
            log(f"send FAILED {ad_id} [{code}] {body[:160]}")
        k.save_state(state)
        time.sleep(random.uniform(*cfg["message_delay_seconds"]))
    return seen, fresh, sent


def main():
    cfg = k.load_config()
    template = k.load_template(cfg)
    log(f"watcher start | poll={POLL}s dry={DRY} state={k.STATE_FILE}")

    state = k.load_state()
    seeding = SEED and not state["contacted"] and not state["skipped"]
    if seeding:
        log("first run: seeding current listings as seen (no messages)")

    auth_ok_last = True
    while True:
        try:
            kl = k.Kleinanzeigen(cfg)
            ok, info = kl.check_auth()
            if not ok:
                if auth_ok_last:
                    log(f"AUTH LOST: {info}")
                    notify("⚠️ Kleinanzeigen session expired — refresh COOKIE_HEADER")
                auth_ok_last = False
                time.sleep(300)
                continue
            if not auth_ok_last:
                log("auth recovered")
            auth_ok_last = True

            seen, fresh, n = scan_once(kl, cfg, template, state, seeding)
            if seeding:
                k.save_state(state)
                log(f"seeded {len(state['skipped'])} listings; watching for new ones")
                seeding = False
            else:
                log(f"scan: {seen} ads, {fresh} new, {n} sent")
        except Exception:
            log("cycle error:\n" + traceback.format_exc())

        time.sleep(POLL * random.uniform(0.8, 1.3))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
