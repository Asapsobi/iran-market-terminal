#!/usr/bin/env python3
"""Standalone downtime monitor -> Telegram (no dependencies, stdlib only).

Runs OUTSIDE the app, on any always-on machine that can reach BOTH the
terminal's /api/health AND Telegram. It alerts when the app is unreachable,
returns a non-200, or its data has gone stale — and again when it recovers.
This needs no code merge and no rebuild of the main app.

Config via environment variables:
  MONITOR_URL        health URL           (default http://127.0.0.1:4000/api/health)
  TELEGRAM_BOT_TOKEN bot token            (if token/chat unset, alerts are PRINTED, not sent)
  TELEGRAM_CHAT_ID   chat id              (e.g. -1004418166892)
  CHECK_INTERVAL     seconds between checks           (default 60)
  FAIL_THRESHOLD     consecutive bad checks before DOWN alert (default 3)
  STALE_SEC          treat as down if market_last_update_age exceeds this (default 600; 0=off)
  HTTPS_PROXY        standard proxy env, if Telegram is filtered on this host
                     (also set NO_PROXY=127.0.0.1,<server-ip> so health checks stay direct)

Run (foreground):
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=-100... python3 downtime_monitor.py
"""
import json
import os
import time
import urllib.parse
import urllib.request

URL = os.environ.get("MONITOR_URL", "http://127.0.0.1:4000/api/health")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = float(os.environ.get("CHECK_INTERVAL", "60") or 60)
THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "3") or 3)
STALE = float(os.environ.get("STALE_SEC", "600") or 0)


def check():
    """Return (ok: bool, detail: str) for one health probe."""
    try:
        with urllib.request.urlopen(URL, timeout=10) as r:
            if r.status != 200:
                return False, f"HTTP {r.status}"
            data = json.load(r)
        age = data.get("market_last_update_age_sec")
        if STALE and isinstance(age, (int, float)) and age > STALE:
            return False, f"data stale {int(age)}s"
        return True, f"healthy (data age={age}s)"
    except Exception as exc:
        return False, f"unreachable ({type(exc).__name__})"


def send(text):
    """Send a Telegram message; if creds unset, just print (for testing)."""
    if not (TOKEN and CHAT):
        print("[would-send]", text, flush=True)
        return
    body = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        print("[sent]", text, flush=True)
    except Exception as exc:
        print("[send-failed]", type(exc).__name__, exc,
              "(set HTTPS_PROXY if Telegram is filtered on this host)", flush=True)


def main():
    print(f"downtime_monitor: {URL} every {INTERVAL:.0f}s; alert after {THRESHOLD} "
          f"consecutive fails; telegram={'ON' if TOKEN and CHAT else 'PRINT-ONLY'}",
          flush=True)
    fails = 0
    down = False
    down_since = 0.0
    while True:
        ok, detail = check()
        now = time.time()
        if ok:
            if down:
                down = False
                send(f"✅ Iran Market Terminal RECOVERED — {detail} "
                     f"(was down ~{int(now - down_since)}s).")
            fails = 0
        else:
            fails += 1
            print(f"check failed ({fails}/{THRESHOLD}): {detail}", flush=True)
            if fails >= THRESHOLD and not down:
                down = True
                down_since = now
                send(f"⚠️ Iran Market Terminal DOWN — {detail}. "
                     f"{fails} consecutive failed health checks at {URL}.")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
