"""Candle service.

Two sources of OHLC data:
1. Native klines from exchanges that expose them (Nobitex, Wallex, Exir),
   fetched incrementally at 5-minute resolution.
2. Ring-built candles: for every exchange AND the composite index, 60-second
   candles are built from the in-memory mid-price rings each refresh — this
   covers venues with no public kline endpoint (Bitpin, Tabdeal, Ramzinex).

Reads resample stored candles to the requested timeframe.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from . import db
from .config import CONFIG
from .metrics import metrics_engine

log = logging.getLogger("terminal.candles")

NATIVE_RES = 300
RING_RES = 60
NATIVE_BACKFILL_SEC = 30 * 86400   # first fetch pulls 30 days of 5m klines

# Timeframe = the REAL candle interval shown on the chart.
# tf -> (lookback_window_sec, candle_resolution_sec)
TF_SPEC = {
    "1min": (6 * 3600, 60),
    "5min": (2 * 86400, 300),
    "15min": (5 * 86400, 900),
    "1h": (14 * 86400, 3600),
    "4h": (45 * 86400, 14400),
    "1d": (180 * 86400, 86400),
}

# Legacy range keys (v2.0 UI) -> equivalent timeframe
LEGACY_RANGE_TO_TF = {"1h": "1min", "4h": "5min", "1d": "15min",
                      "1w": "1h", "1m": "4h"}

# Lookback windows for the /api/history endpoint (line data, not candles)
HISTORY_WINDOWS = {"1h": 3600, "4h": 4 * 3600, "1d": 86400,
                   "1w": 7 * 86400, "1m": 30 * 86400}


class CandleService:
    def __init__(self) -> None:
        self._last_native_fetch: Dict[Tuple[str, str], float] = {}
        self._last_ring_build: float = 0.0

    # ------------------------------------------------------------ refresh --
    async def refresh(self, aggregator) -> None:
        self._build_ring_candles(aggregator)
        if not CONFIG.get("demo_mode"):
            await self._fetch_native(aggregator)

    def _build_ring_candles(self, aggregator) -> None:
        """Aggregate in-memory mid rings into 60s candles (last ~2h window)."""
        now = time.time()
        since = self._last_ring_build or now - 7200
        # Start at a bucket boundary: re-upserting a bucket from only its tail
        # used to overwrite its open/high/low with partial data.
        cutoff = since - since % RING_RES - RING_RES
        rows: List[tuple] = []
        quote = CONFIG.get("quote_currency", "TMN")

        def bucketize(points, name, base):
            buckets: Dict[float, List[float]] = {}
            for ts, mid in points:
                if ts < cutoff:
                    continue
                buckets.setdefault(ts - ts % RING_RES, []).append(mid)
            for bts, vals in buckets.items():
                rows.append((name, base, quote, RING_RES, bts,
                             vals[0], max(vals), min(vals), vals[-1], 0.0))

        for (exchange, base), ring in metrics_engine.mid_rings.items():
            bucketize(ring, exchange, base)
        for base, ring in metrics_engine.composite_rings.items():
            bucketize(ring, "composite", base)
        if rows:
            db.upsert_candles(rows)
        self._last_ring_build = now

    async def _fetch_native(self, aggregator) -> None:
        quote = CONFIG.get("quote_currency", "TMN")
        now = time.time()
        for connector in aggregator.connectors:
            if not getattr(connector, "supports_candles", False):
                continue
            for base, _q in aggregator.pairs():
                key = (connector.exchange_name, base)
                since = self._last_native_fetch.get(key, now - NATIVE_BACKFILL_SEC)
                try:
                    candles = await connector.fetch_candles(base, NATIVE_RES, since, now)
                except Exception as exc:
                    log.debug("candles %s %s failed: %s", key, base, exc)
                    continue
                if candles:
                    db.upsert_candles([
                        (connector.exchange_name, base, quote, NATIVE_RES,
                         c["ts"], c["open"], c["high"], c["low"], c["close"],
                         c.get("volume", 0.0))
                        for c in candles if c.get("ts")
                    ])
                    self._last_native_fetch[key] = max(c["ts"] for c in candles)

    # --------------------------------------------------------------- read --
    @staticmethod
    def _resample(candles: List[Dict], target_res: int) -> List[Dict]:
        if not candles:
            return []
        buckets: Dict[float, Dict] = {}
        for c in candles:
            bts = c["ts"] - c["ts"] % target_res
            b = buckets.get(bts)
            if b is None:
                buckets[bts] = {"ts": bts, "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"],
                                "volume": c.get("volume", 0.0)}
            else:
                b["high"] = max(b["high"], c["high"])
                b["low"] = min(b["low"], c["low"])
                b["close"] = c["close"]
                b["volume"] += c.get("volume", 0.0)
        return [buckets[k] for k in sorted(buckets)]

    def get(self, exchange: str, base: str, tf: str) -> List[Dict]:
        quote = CONFIG.get("quote_currency", "TMN")
        tf = LEGACY_RANGE_TO_TF.get(tf, tf) if tf not in TF_SPEC else tf
        window, target_res = TF_SPEC.get(tf, TF_SPEC["15min"])
        since = time.time() - window
        # prefer native 5m candles for coarser frames, 60s ring candles for 1min
        sources = ([RING_RES, NATIVE_RES] if target_res < NATIVE_RES
                   else [NATIVE_RES, RING_RES])
        for res in sources:
            rows = db.get_candles(exchange, base, quote, res, since)
            if len(rows) >= 2:
                return self._resample(self._with_live_tail(rows, exchange, base, res),
                                      target_res)
        return []

    def _with_live_tail(self, rows: List[Dict], exchange: str, base: str,
                        res: int) -> List[Dict]:
        """Ring candles are only persisted every candle_interval (5 min), so the
        last stored candle lags the live price. Append/patch the minutes since
        the last persisted build straight from the in-memory ring, so the last
        candle closes at the current composite (or venue) mid."""
        ring = (metrics_engine.composite_rings.get(base) if exchange == "composite"
                else metrics_engine.mid_rings.get((exchange, base)))
        if not ring or not rows:
            return rows
        last_ts = rows[-1]["ts"]
        tail_from = last_ts - last_ts % RING_RES
        tail: Dict[float, Dict] = {}
        for ts, mid in ring:
            if ts < tail_from:
                continue
            bts = ts - ts % RING_RES
            c = tail.get(bts)
            if c is None:
                tail[bts] = {"ts": bts, "open": mid, "high": mid, "low": mid,
                             "close": mid, "volume": 0.0}
            else:
                c["high"] = max(c["high"], mid)
                c["low"] = min(c["low"], mid)
                c["close"] = mid
        if not tail:
            return rows
        out = list(rows)
        if res == RING_RES:
            # the ring is authoritative for the minutes it covers
            out = [r for r in out if r["ts"] not in tail]
        else:
            # native 5m candles: keep them, only extend past the last native bar
            tail = {k: v for k, v in tail.items() if k >= last_ts + res}
        return out + [tail[k] for k in sorted(tail)]


candle_service = CandleService()
