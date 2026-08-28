#!/usr/bin/env python3
"""Turn auth.txt into the one-line COOKIE_HEADER value for Railway.

Pasting the whole `Copy as cURL` blob into an env var does not work: it is
multi-line and full of quotes. This extracts just the cookie string.

    python3 make_env.py        # writes cookie_header.txt
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "auth.txt")
OUT = os.path.join(HERE, "cookie_header.txt")

raw = open(SRC, encoding="utf-8").read()
m = re.search(r"-H 'Cookie: ([^']*)'", raw) or \
    re.search(r"^Cookie:\s*(.+)$", raw, re.M | re.I)
cookie = (m.group(1) if m else raw).strip()
cookie = " ".join(cookie.split())  # collapse any newlines into one line

jar = dict(p.split("=", 1) for p in
           (x.strip() for x in cookie.split(";")) if "=" in p)
missing = [c for c in ("access_token", "refresh_token") if c not in jar]
if missing:
    sys.exit(f"❌ auth.txt is missing {', '.join(missing)} — re-capture it "
             "(DevTools → Network → www.kleinanzeigen.de → Copy as cURL)")

open(OUT, "w", encoding="utf-8").write(cookie)
print(f"✅ wrote {OUT}")
print(f"   {len(jar)} cookies, {len(cookie)} chars, single line")
print(f"   contains: access_token, refresh_token, "
      f"{'CSRF-TOKEN' if 'CSRF-TOKEN' in jar else 'no CSRF-TOKEN'}")
print()
print("Paste the ENTIRE contents of cookie_header.txt as COOKIE_HEADER in")
print("Railway. Use the variable editor's raw/multi-line paste so nothing is")
print("truncated, and make sure no trailing newline or quotes sneak in.")
