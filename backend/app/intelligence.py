"""Competitive intelligence engines.

1. TobTracker  — top-of-book time share: which venue holds the best bid/ask,
   time-weighted, persisted hourly. The "best exchange" scoreboard.
2. ArbLedger   — opportunity-cost ledger: every window where the fee-adjusted,
   depth-limited cross-venue edge exceeds the configurable threshold is
   recorded (peak edge, executable size, peak profit, duration). Model is
   INVENTORY-BASED: capital pre-positioned on both venues, so no transfer
   fees apply — book-walking already prices in spreads and taker fees.
3. inventory_requirements — per-venue TMN / coin balances needed to have
   captured 100% (and 95%) of the period's windows, via a peak-concurrency
   sweep over window intervals.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .metrics import metrics_engine
from .models import MarketSnapshot, is_usable
from .settings import settings_store

log = logging.getLogger("terminal.intelligence")

TIE_TOLERANCE = 1e-4   # 1 bp: quotes this close both count as "best"


class TobTracker:
    def __init__(self) -> None:
        self._acc: Dict[Tuple[str, str, str, float], List[float]] = {}
        self._last_flush = time.time()
        self._last_cycle_ts: Optional[float] = None
        self.current: Dict[Tuple[str, str], List[str]] = {}   # (base, side) -> holders

    def record(self, by_asset: Dict[str, List[MarketSnapshot]]) -> None:
        now = time.time()
        dt = min(now - self._last_cycle_ts, 60.0) if self._last_cycle_ts else 0.0
        self._last_cycle_ts = now
        if dt <= 0:
            return
        hour = now - now % 3600
        for base, snaps in by_asset.items():
            live = [s for s in snaps if is_usable(s)]
            if not live:
                continue
            best_bid = max(s.best_bid for s in live)
            best_ask = min(s.best_ask for s in live if s.best_ask > 0)
            bid_holders = [s.exchange for s in live
                           if s.best_bid >= best_bid * (1 - TIE_TOLERANCE)]
            ask_holders = [s.exchange for s in live
                           if 0 < s.best_ask <= best_ask * (1 + TIE_TOLERANCE)]
            self.current[(base, "bid")] = bid_holders
            self.current[(base, "ask")] = ask_holders
            for s in live:
                for side, holders in (("bid", bid_holders), ("ask", ask_holders)):
                    key = (s.exchange, base, side, hour)
                    acc = self._acc.setdefault(key, [0.0, 0.0])
                    acc[1] += dt
                    if s.exchange in holders:
                        # a tie at the best price is shared, not counted in full
                        # for every tied venue — keeps each column summing to 100%
                        acc[0] += dt / len(holders)
        if now - self._last_flush >= 60:
            self.flush()

    def flush(self) -> None:
        if not self._acc:
            return
        rows = [(ex, base, side, hour, acc[0], acc[1])
                for (ex, base, side, hour), acc in self._acc.items()]
        try:
            db.bump_tob_hours(rows)
            self._acc.clear()
            self._last_flush = time.time()
        except Exception as exc:
            log.error("tob flush failed: %s", exc)


class ArbLedger:
    def __init__(self) -> None:
        # (base, buy_ex, sell_ex) -> window row id
        self._open: Dict[Tuple[str, str, str], int] = {}

    def update(self, aggregator) -> None:
        threshold = settings_store.get().arb_min_edge_pct
        now = time.time()
        seen: set = set()
        for base, quote in aggregator.pairs():
            books = aggregator.usable_books(base)
            for op in metrics_engine.arbitrage(base, quote, books):
                if op.net_pct < threshold or op.max_size_base <= 0:
                    continue
                key = (base, op.buy_exchange, op.sell_exchange)
                seen.add(key)
                cost = op.max_size_base * op.buy_price
                try:
                    if key not in self._open:
                        self._open[key] = db.open_arb_window(
                            base, quote, op.buy_exchange, op.sell_exchange, now)
                    db.update_arb_window(self._open[key], op.net_pct,
                                         op.max_size_base, op.est_profit_quote, cost)
                except Exception as exc:
                    log.error("ledger update failed: %s", exc)
        # close windows whose edge died
        for key in [k for k in self._open if k not in seen]:
            try:
                db.close_arb_window(self._open.pop(key), now)
            except Exception as exc:
                log.error("ledger close failed: %s", exc)


def _sweep_peak(intervals: List[Tuple[float, float, float]]) -> float:
    """Peak concurrent sum of (start, end, amount) intervals."""
    events: List[Tuple[float, float]] = []
    for start, end, amount in intervals:
        events.append((start, amount))
        events.append((end, -amount))
    events.sort()
    peak = cur = 0.0
    for _ts, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def inventory_requirements(windows: List[Dict[str, Any]],
                           now: Optional[float] = None) -> Dict[str, Any]:
    """Minimum balances per venue to capture ALL windows in the period
    (peak concurrent requirement), plus a 95% variant that drops the
    heaviest 5% of windows."""
    now = now or time.time()

    def build(rows):
        tmn: Dict[str, List[tuple]] = {}     # buy venue  -> TMN intervals
        coin: Dict[Tuple[str, str], List[tuple]] = {}  # (sell venue, asset) -> coin intervals
        for w in rows:
            start = w["opened_ts"]
            end = w["closed_ts"] or now
            tmn.setdefault(w["buy_exchange"], []).append(
                (start, end, w["max_cost_quote"]))
            coin.setdefault((w["sell_exchange"], w["base"]), []).append(
                (start, end, w["max_size_base"]))
        out: Dict[str, Any] = {}
        for ex, ivs in tmn.items():
            out.setdefault(ex, {"tmn": 0.0, "assets": {}})["tmn"] = round(_sweep_peak(ivs))
        for (ex, base), ivs in coin.items():
            out.setdefault(ex, {"tmn": 0.0, "assets": {}})["assets"][base] = \
                round(_sweep_peak(ivs), 6)
        return out

    full = build(windows)
    # 95%: drop the top 5% of windows by TMN cost (the outliers that inflate
    # the requirement the most)
    by_cost = sorted(windows, key=lambda w: w["max_cost_quote"], reverse=True)
    drop = max(1, len(by_cost) // 20) if len(by_cost) >= 10 else 0
    p95 = build(by_cost[drop:]) if drop else full
    return {"full": full, "p95": p95, "windows_counted": len(windows)}


tob_tracker = TobTracker()
arb_ledger = ArbLedger()
