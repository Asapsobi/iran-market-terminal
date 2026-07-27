"""Data-staleness → Telegram alerter ("system is down").

Watches whether the collector is actually producing *fresh market data* — at
least one venue connected with a live price. If nothing fresh arrives for
DOWNTIME_ALERT_SEC, it sends a Telegram "down" message; when data returns it
sends a "recovered" message. This catches the "UI is up but the feeds are
stale" case that a plain loop-health check misses (the loop can keep cycling
while every venue is offline).

IMPORTANT — this can only notify while the SERVER can still reach Telegram. If
the whole host loses internet (a common cause of stale data), it cannot send;
use an external monitor for that case. Telegram is filtered in Iran, so set
TELEGRAM_PROXY if api.telegram.org is not directly reachable from the server.

Env:
  TELEGRAM_BOT_TOKEN     bot token from @BotFather (required to enable)
  TELEGRAM_CHAT_ID       target chat / channel id  (required to enable)
  TELEGRAM_PROXY         optional proxy, e.g. socks5://127.0.0.1:1080 or http://host:port
  DOWNTIME_ALERT_SEC     stale-for this long → alert       (default 300)
  DOWNTIME_REMINDER_SEC  re-send interval while still down (default 0 = alert once)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .models import MarketStatus

log = logging.getLogger("terminal.downtime")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class DowntimeAlerter:
    def __init__(self) -> None:
        self._down = False
        self._last_fresh = time.time()   # last time data was flowing
        self._last_sent = 0.0            # last time we sent a "down" message
        self._down_since = 0.0
        self.reload()

    def reload(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.proxy = os.environ.get("TELEGRAM_PROXY", "").strip() or None
        try:
            self.threshold = float(os.environ.get("DOWNTIME_ALERT_SEC", "300") or 300)
        except ValueError:
            self.threshold = 300.0
        try:
            self.reminder = float(os.environ.get("DOWNTIME_REMINDER_SEC", "0") or 0)
        except ValueError:
            self.reminder = 0.0
        self.enabled = bool(self.token and self.chat_id)
        if self.enabled:
            log.info("downtime alerter enabled (threshold=%.0fs, proxy=%s)",
                     self.threshold, "yes" if self.proxy else "no")

    @staticmethod
    def _data_flowing(aggregator: Any) -> bool:
        """True if at least one venue is connected with a live price."""
        for s in aggregator.market_state.values():
            if s.mid > 0 and s.status != MarketStatus.OFFLINE.value:
                return True
        return False

    async def check(self, aggregator: Any) -> None:
        """Call periodically (e.g. every 60s from the heartbeat loop)."""
        if not self.enabled:
            return
        now = time.time()
        if self._data_flowing(aggregator):
            self._last_fresh = now
            if self._down:
                self._down = False
                down_for = int(now - self._down_since) if self._down_since else 0
                await self._send(f"✅ Iran Market Terminal — RECOVERED. Market data "
                                 f"is flowing again (was stale for ~{down_for}s).")
            return
        stale_for = now - self._last_fresh
        if stale_for < self.threshold:
            return
        if not self._down:
            self._down = True
            self._down_since = self._last_fresh
            self._last_sent = now
            await self._send(f"⚠️ Iran Market Terminal — DATA DOWN. No fresh market "
                             f"data for {int(stale_for)}s. The UI may still be up, but every "
                             f"venue is offline/stale. Check the server's connectivity and the "
                             f"collector.")
        elif self.reminder and (now - self._last_sent) >= self.reminder:
            self._last_sent = now
            await self._send(f"⚠️ Still down — no fresh market data for "
                             f"{int(stale_for)}s.")

    async def _send(self, text: str) -> None:
        url = TELEGRAM_API.format(token=self.token)
        kwargs: dict = {"timeout": 10.0}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                r = await client.post(url, json={"chat_id": self.chat_id, "text": text})
            if r.status_code == 200:
                log.info("telegram alert sent: %s", text[:60])
            else:
                log.warning("telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as exc:   # never let a send failure break the loop
            log.warning("telegram send error (server may not reach api.telegram.org — "
                        "set TELEGRAM_PROXY): %s", exc)


downtime_alerter = DowntimeAlerter()
