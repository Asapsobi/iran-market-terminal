"""Premium engine — dynamic, configurable Iran-premium computation.

premium % = (asset_price_TMN / usdt_price_TMN) / asset_price_USD_global − 1

Benchmark METHODS decide which TMN price represents "the market":
- composite : average mid across live exchanges          (default, v1 behavior)
- best_mid  : (highest bid + lowest ask) / 2 across ALL venues — the tightest
              executable market price
- vwap      : venue mids weighted by 24h venue volume — where liquidity is
- exchange  : one specific venue's mid (pass exchange=<Name>) — e.g. compare
              only Nobitex against Binance/CoinGecko

Both real-time (from live market state) and historical (from persisted
snapshots + stored reference prices) computations share these definitions.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any, Dict, List, Optional

from . import db
from .models import MarketSnapshot, is_usable

log = logging.getLogger("terminal.premium")

METHODS = ("composite", "best_mid", "vwap", "exchange")
BUCKET = 300.0   # snapshot cadence — series are joined on 5-minute buckets
# A real Iran premium/discount is a few percent; anything beyond this means the
# global reference is mapped to the wrong coin or the units are off.
SANITY_MAX_PCT = 20.0
_insane_warned: Dict[str, float] = {}


def sane_premium(value: Optional[float], label: str = "") -> Optional[float]:
    """None (shown as '—') when |premium| is implausibly large; logs a throttled warning."""
    if value is None or abs(value) <= SANITY_MAX_PCT:
        return value
    now = time.time()
    if now - _insane_warned.get(label, 0.0) > 900:
        _insane_warned[label] = now
        log.warning("premium for %s = %+.1f%% exceeds ±%.0f%% — hidden; check the "
                    "global reference id / units for this asset", label or "?",
                    value, SANITY_MAX_PCT)
    return None


# ------------------------------------------------------------- real-time ---

def live_method_price(snaps: List[MarketSnapshot], method: str,
                      exchange: Optional[str] = None) -> Optional[float]:
    live = [s for s in snaps if is_usable(s)]
    if not live:
        return None
    if method == "exchange":
        s = next((x for x in live if x.exchange == exchange), None)
        return s.mid if s else None
    if method == "best_mid":
        best_bid = max(s.best_bid for s in live)
        best_ask = min(s.best_ask for s in live if s.best_ask > 0)
        return (best_bid + best_ask) / 2 if best_ask > 0 else None
    if method == "vwap":
        weights = [(s.mid, s.volume_24h_quote) for s in live]
        total = sum(w for _, w in weights)
        if total > 0:
            return sum(m * w for m, w in weights) / total
        # fall through to composite when venues report no volume
    return statistics.fmean(s.mid for s in live)   # composite


def live_premium(snaps_asset: List[MarketSnapshot], snaps_usdt: List[MarketSnapshot],
                 usd_ref: float, method: str = "composite",
                 exchange: Optional[str] = None) -> Optional[float]:
    asset_price = live_method_price(snaps_asset, method, exchange)
    usdt_price = live_method_price(snaps_usdt, method, exchange)
    if not asset_price or not usdt_price or not usd_ref:
        return None
    label = snaps_asset[0].base if snaps_asset else ""
    return sane_premium(round(((asset_price / usdt_price) / usd_ref - 1) * 100, 3), label)


# ------------------------------------------------------------ historical ---

def _bucketize(points: List[tuple]) -> Dict[float, float]:
    """[(ts, value)] -> {bucket_ts: last value in bucket}"""
    out: Dict[float, float] = {}
    for ts, value in points:
        if value:
            out[ts - ts % BUCKET] = value
    return out


def _method_series(base: str, quote: str, method: str, since: float,
                   exchange: Optional[str] = None) -> Dict[float, float]:
    if method == "composite":
        rows = db.get_composite_history(base, quote, since)
        return _bucketize([(r["ts"], r["mid"]) for r in rows])

    if method == "best_mid":
        rows = db.get_composite_history(base, quote, since)
        pts = []
        for r in rows:
            bid, ask = r.get("best_bid") or 0, r.get("best_ask") or 0
            pts.append((r["ts"], (bid + ask) / 2 if bid and ask else r["mid"]))
        return _bucketize(pts)

    # vwap / exchange need per-venue snapshots
    rows = db.get_pair_snapshots(base, quote, since)
    if method == "exchange":
        return _bucketize([(r["ts"], r["mid"]) for r in rows
                           if r["exchange"] == exchange])
    # vwap: weight venue mids by 24h volume inside each bucket
    buckets: Dict[float, List[tuple]] = {}
    for r in rows:
        if r["mid"]:
            buckets.setdefault(r["ts"] - r["ts"] % BUCKET, []).append(
                (r["mid"], r["volume_24h_quote"] or 0.0))
    out: Dict[float, float] = {}
    for bts, entries in buckets.items():
        total = sum(w for _, w in entries)
        out[bts] = (sum(m * w for m, w in entries) / total if total > 0
                    else statistics.fmean(m for m, _ in entries))
    return out


def premium_series(base: str, quote: str, method: str, since: float,
                   exchange: Optional[str] = None) -> List[Dict[str, Any]]:
    """Historical premium: joins asset series, USDT series and the stored
    global USD reference on 5-minute buckets."""
    asset = _method_series(base, quote, method, since, exchange)
    usdt = _method_series("USDT", quote, method, since, exchange)
    ref = _bucketize([(r["ts"], r["usd"]) for r in db.get_reference_history(base, since)])
    out: List[Dict[str, Any]] = []
    for bts in sorted(asset):
        a, u, r = asset.get(bts), usdt.get(bts), ref.get(bts)
        if not a or not u or not r:
            continue
        implied_usd = a / u
        pct = round((implied_usd / r - 1) * 100, 3)
        if abs(pct) > SANITY_MAX_PCT:   # history recorded under a wrong reference id
            continue
        out.append({
            "ts": bts,
            "premium_pct": pct,
            "implied_usd": round(implied_usd, 4),
            "asset_price": round(a, 2),
            "usdt_price": round(u, 2),
            "usd_ref": round(r, 4),
        })
    return out
