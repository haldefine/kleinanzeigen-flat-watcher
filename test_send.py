#!/usr/bin/env python3
"""Send ONE real message, to verify the contact endpoint end-to-end.

Run:  python3 test_send.py [ad_url] --yes
"""
import sys
import kleinanzeigen_bot as k

YES = "--yes" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("-")]
AD = args[0] if args else (
    "https://www.kleinanzeigen.de/s-anzeige/"
    "gepflegtes-helles-apartment-stadtnah-und-verkehrsguenstig/3496035669-203-7526")

cfg = k.load_config()
kl = k.Kleinanzeigen(cfg)
ok, info = kl.check_auth()
print("auth:", ok, info)
if not ok:
    sys.exit(1)

ad = kl.parse_ad(AD)
msg = k.build_message(cfg, ad, k.load_template(cfg))
print(f"\nad {ad['id']} | seller={ad['seller']!r} | warm={ad['warm']} ({ad['warm_source']})")
print("-" * 60); print(msg); print("-" * 60)
if not YES:
    try:
        if input("\nSend this to a REAL landlord? [y/N] ").strip().lower() != "y":
            sys.exit("aborted")
    except EOFError:
        sys.exit("\nNo TTY for the prompt — re-run with --yes to confirm the send.")

ok, code, body = kl.send_message(ad, msg)
print(f"\n{'✅ SENT' if ok else '❌ FAILED'} [{code}] {body[:250]}")
if ok:
    state = k.load_state()
    state["contacted"][ad["id"]] = {"at": "test_send", "title": ad["title"],
                                    "warm": ad["warm"], "url": AD}
    k.save_state(state)
    print("   recorded in state.json — this ad won't be contacted again")
