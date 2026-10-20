"""Rule-based alert engine.

Rule types (threshold semantics in parentheses):
- spread_above        : any venue spread_pct > threshold (%)
- arb_net_above       : best fee-adjusted arbitrage net_pct > threshold (%)
- deviation_above     : |venue mid vs cross-exchange median| > threshold (%)
- liquidity_drop      : depth below window average by > threshold (%)
- change_above        : |composite change over window_sec| > threshold (%)
- premium_above       : Iran premium > threshold (%)
- premium_below       : Iran premium < threshold (%)
- calendar_high_impact: HIGH-impact event within threshold minutes

Rules can be scoped to a base asset and/or exchange. Each rule has a cooldown
so it doesn't refire every cycle. Fired events persist to SQLite and are pushed
to WebSocket clients.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any, Callable, Dict, List, Optional

from . import db
from .metrics import metrics_engine
from .models import MarketSnapshot, is_usable

log = logging.getLogger("terminal.alerts")

DEFAULT_RULES = [
    # (name, rule_type, base, exchange, threshold, window_sec, cooldown_sec)
    ("Wide spread > 1%", "spread_above", None, None, 1.0, 3600, 900),
    ("Net arbitrage > 0.5%", "arb_net_above", None, None, 0.5, 3600, 600),
    ("Venue deviates > 2%", "deviation_above", None, None, 2.0, 3600, 900),
    ("Liquidity down 40% (1h)", "liquidity_drop", None, None, 40.0, 3600, 1800),
    ("BTC moves 3% in 1h", "change_above", "BTC", None, 3.0, 3600, 1800),
    ("USDT premium > 5%", "premium_above", "BTC", None, 5.0, 3600, 3600),
    ("High-impact event in 30m", "calendar_high_impact", None, None, 30.0, 3600, 3600),
]


class AlertEngine:
    def __init__(self) -> None:
        self._last_fired: Dict[int, float] = {}
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        if db.get_alert_rules():
            return
        for name, rtype, base, ex, thr, win, cd in DEFAULT_RULES:
            db.insert_alert_rule(name, rtype, base, ex, thr, win, cd)
        log.info("Seeded %d default alert rules", len(DEFAULT_RULES))

    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self.listeners.append(fn)

    # ---------------------------------------------------------------- fire --
    def _fire(self, rule: Dict[str, Any], message: str, severity: str) -> None:
        rule_id = rule["id"]
        now = time.time()
        if now - self._last_fired.get(rule_id, 0) < rule.get("cooldown_sec", 900):
            return
        self._last_fired[rule_id] = now
        event_id = db.insert_alert_event(rule_id, rule["rule_type"], message, severity, now)
        payload = {"type": "alert", "data": {"id": event_id, "rule_id": rule_id,
                                             "rule_type": rule["rule_type"],
                                             "message": message,
                                             "severity": severity, "ts": now}}
        for fn in list(self.listeners):
            try:
                fn(payload)
            except Exception:
                pass
        log.info("ALERT [%s] %s", severity, message)

    # ------------------------------------------------------------ evaluate --
    def evaluate(self, aggregator: Any,
                 calendar_events: Optional[List[Dict[str, Any]]] = None) -> None:
        rules = db.get_alert_rules(enabled_only=True)
        if not rules:
            return
        by_asset = aggregator.snapshots_by_asset()
        for rule in rules:
            try:
                self._evaluate_rule(rule, aggregator, by_asset, calendar_events or [])
            except Exception as exc:
                log.debug("rule %s failed: %s", rule.get("name"), exc)

    def _matches(self, rule: Dict[str, Any], base: str, exchange: str = "") -> bool:
        if rule.get("base") and rule["base"].upper() != base.upper():
            return False
        if rule.get("exchange") and exchange and rule["exchange"] != exchange:
            return False
        return True

    def _evaluate_rule(self, rule: Dict[str, Any], aggregator: Any,
                       by_asset: Dict[str, List[MarketSnapshot]],
                       calendar: List[Dict[str, Any]]) -> None:
        rtype = rule["rule_type"]
        thr = float(rule["threshold"])
        win = float(rule.get("window_sec") or 3600)

        if rtype == "spread_above":
            for base, snaps in by_asset.items():
                for s in snaps:
                    if s.mid > 0 and self._matches(rule, base, s.exchange) \
                            and s.spread_pct > thr:
                        self._fire(rule,
                                   f"{s.exchange} {base} spread {s.spread_pct:.2f}% "
                                   f"exceeds {thr:g}%", "warning")

        elif rtype == "arb_net_above":
            for base, quote in aggregator.pairs():
                if not self._matches(rule, base):
                    continue
                ops = metrics_engine.arbitrage(base, quote, aggregator.usable_books(base))
                if ops and ops[0].net_pct > thr:
                    o = ops[0]
                    self._fire(rule,
                               f"{base}: buy {o.buy_exchange} / sell {o.sell_exchange} "
                               f"nets {o.net_pct:.2f}% after fees "
                               f"(size {o.max_size_base:g} {base})", "critical")

        elif rtype == "deviation_above":
            for base, snaps in by_asset.items():
                # offline last-good quotes and flagged outliers are not markets
                live = [s for s in snaps if is_usable(s)]
                if len(live) < 3:
                    continue
                med = statistics.median(s.mid for s in live)
                for s in live:
                    dev = abs(s.mid - med) / med * 100
                    if self._matches(rule, base, s.exchange) and dev > thr:
                        self._fire(rule,
                                   f"{s.exchange} {base} deviates {dev:.2f}% from "
                                   f"median across venues", "critical")

        elif rtype == "liquidity_drop":
            for base, snaps in by_asset.items():
                for s in snaps:
                    if not self._matches(rule, base, s.exchange):
                        continue
                    drop = metrics_engine.depth_drop_pct(s.exchange, base, win)
                    if drop is not None and drop > thr:
                        self._fire(rule,
                                   f"{s.exchange} {base} liquidity down {drop:.0f}% "
                                   f"vs {int(win / 60)}m average", "warning")

        elif rtype == "change_above":
            for base, quote in aggregator.pairs():
                if not self._matches(rule, base):
                    continue
                chg = metrics_engine.change_pct(base, quote, win)
                if chg is not None and abs(chg) > thr:
                    self._fire(rule,
                               f"{base} moved {chg:+.2f}% in the last "
                               f"{int(win / 60)} minutes", "warning")

        elif rtype in ("premium_above", "premium_below"):
            for base, _quote in aggregator.pairs():
                if base == "USDT" or not self._matches(rule, base):
                    continue
                prem = aggregator.premium_pct(base)
                if prem is None:
                    continue
                if rtype == "premium_above" and prem > thr:
                    self._fire(rule, f"{base} Iran premium at {prem:+.2f}% "
                                     f"(> {thr:g}%)", "info")
                elif rtype == "premium_below" and prem < thr:
                    self._fire(rule, f"{base} Iran premium at {prem:+.2f}% "
                                     f"(< {thr:g}%)", "info")

        elif rtype == "calendar_high_impact":
            now = time.time()
            horizon = thr * 60
            for ev in calendar:
                if str(ev.get("impact", "")).upper() != "HIGH":
                    continue
                dt = ev.get("ts", ev.get("timestamp", 0)) - now
                if 0 < dt <= horizon:
                    self._fire(rule,
                               f"High-impact event in {int(dt / 60)}m: "
                               f"{ev.get('country', '')} {ev.get('title', '')}",
                               "info")


alert_engine = AlertEngine()
