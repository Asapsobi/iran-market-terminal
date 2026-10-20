"""Market aggregator: polls all connectors, computes metrics, persists snapshots.

Cycle (every `market_interval` seconds):
  1. Bounded fan-out of order-book fetches (global + per-venue semaphores).
  2. Skip circuit-open / rate-limited / unsupported venue×pair slots.
  3. Build MarketSnapshot per live feed; soft-fail keeps last-good as delayed.
  4. Composite mid + Iran premium; batch-write snapshots to PostgreSQL.
  5. Recycle the shared HTTP client after every cycle (leak shed).
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db
from .circuit_breaker import breakers
from .config import CONFIG
from .connectors import (BaseExchangeConnector, DemoConnector, ReferenceConnector,
                         RateLimited, UnsupportedPair, build_connectors,
                         force_recycle_client, is_rate_limited, maybe_recycle_client,
                         MAX_INFLIGHT)
from .metrics import metrics_engine
from .models import MarketSnapshot, MarketStatus, OrderBook, TickerStats, is_usable
from .settings import settings_store
from .volume_estimator import volume_estimator

log = logging.getLogger("terminal.aggregator")

STALE_AFTER_MS = 15_000
# Keep last-good quote as "delayed" for this long before flipping to offline.
SOFT_FAIL_GRACE_SEC = 120.0
# A venue whose mid sits this far from the cross-venue median is a bad feed
# (wrong pair id / unit), not a market — flag it and keep it out of composites.
OUTLIER_DEV_PCT = 20.0
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def clean_base(raw: str, quote: str = "TMN") -> str:
    """Normalize a user-entered base symbol: 'usoon/tmn', 'BTC-IRT' -> 'USOON', 'BTC'.
    Returns '' when the result is still not a plain ticker."""
    base = (raw or "").strip().upper()
    for sep in ("/", "-", "_", " "):
        base = base.split(sep)[0]
    for q in {quote.upper(), "TMN", "IRT", "IRR", "TOMAN", "RLS"}:
        if base.endswith(q) and len(base) > len(q) + 1:
            base = base[: -len(q)]
    return base if _SYMBOL_RE.match(base) else ""
# Cap concurrent venue fetches so we never oversubscribe the httpx pool.
_GLOBAL_SEM: Optional[asyncio.Semaphore] = None
_EX_SEMS: Dict[str, asyncio.Semaphore] = {}
PER_EXCHANGE_INFLIGHT = 3
# Venues that 429 or time out under parallelism get a tighter slot budget.
EXCHANGE_INFLIGHT = {
    "Exir": 1,
    "Ramzinex": 1,
    "Bitpin": 2,
    "Tabdeal": 2,
}


def _global_sem() -> asyncio.Semaphore:
    global _GLOBAL_SEM
    if _GLOBAL_SEM is None:
        _GLOBAL_SEM = asyncio.Semaphore(MAX_INFLIGHT)
    return _GLOBAL_SEM


def _ex_sem(name: str) -> asyncio.Semaphore:
    sem = _EX_SEMS.get(name)
    if sem is None:
        n = EXCHANGE_INFLIGHT.get(name, PER_EXCHANGE_INFLIGHT)
        sem = asyncio.Semaphore(n)
        _EX_SEMS[name] = sem
    return sem


class MarketAggregator:
    def __init__(self) -> None:
        self.connectors: List[BaseExchangeConnector] = []
        self.market_state: Dict[Tuple[str, str], MarketSnapshot] = {}
        self.books: Dict[Tuple[str, str], OrderBook] = {}
        self.stats_cache: Dict[Tuple[str, str], TickerStats] = {}
        self.usd_reference: Dict[str, float] = {}
        self.usd_reference_ts: float = 0.0
        self.last_stats_refresh: float = 0.0
        self.last_snapshot_write: float = 0.0
        self.cycle_count: int = 0
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._reference = ReferenceConnector(
            CONFIG.get("reference", {}).get("coingecko_ids",
                                            {"BTC": "bitcoin", "ETH": "ethereum",
                                             "USDT": "tether"}))
        self._unresolvable: set = set()
        self._unresolvable_ts: float = time.time()
        self._ref_missing: List[str] = []
        self._vol_warned: set = set()
        self._fetch_warned: Dict[Tuple[str, str], float] = {}
        self.reload_connectors()

    # ------------------------------------------------------------- config --
    def reload_connectors(self) -> None:
        custom = db.get_custom_exchanges()
        self.connectors = build_connectors(CONFIG, custom)
        log.info("Active connectors: %s", [c.exchange_name for c in self.connectors])

    def pairs(self) -> List[Tuple[str, str]]:
        quote = CONFIG.get("quote_currency", "TMN")
        bases = [a.upper() for a in CONFIG.get("assets", [])]
        for row in db.get_custom_pairs():
            # Older builds stored whatever was typed (e.g. "USOON/TMN"); clean it
            # here so the quote never ends up inside the base symbol.
            base = clean_base(row["base"], quote)
            if row.get("enabled") and base and base not in bases:
                bases.append(base)
        return [(b, quote) for b in bases]

    # -------------------------------------------------------------- cycle --
    async def update_markets(self) -> None:
        maybe_recycle_client()   # shed leaks if client aged out
        settings = settings_store.get()
        pairs = self.pairs()
        # Drop unsupported venue×pair slots so Admin no longer shows fake reds
        # left over from older builds that stored offline placeholders.
        for connector in self.connectors:
            for base, _q in pairs:
                if not connector.supports_pair(base):
                    key = (connector.exchange_name, base)
                    self.market_state.pop(key, None)
                    self.books.pop(key, None)
        # Stats/trades are expensive (extra RTT per pair). Refresh at most a
        # rotating subset each minute so we never double the concurrent load.
        refresh_stats = time.time() - self.last_stats_refresh > 90
        stats_budget = 8
        stats_allowed: set = set()
        if refresh_stats:
            self.last_stats_refresh = time.time()
            all_keys = [(c.exchange_name, b)
                        for c in self.connectors for b, _q in pairs
                        if c.supports_pair(b)]
            if all_keys:
                start = (self.cycle_count * stats_budget) % len(all_keys)
                for i in range(stats_budget):
                    stats_allowed.add(all_keys[(start + i) % len(all_keys)])

        t0 = time.time()
        tasks = [
            self._process(connector, base, quote, settings.request_timeout,
                          refresh_stats=(connector.exchange_name, base) in stats_allowed)
            for connector in self.connectors
            for base, quote in pairs
            if connector.supports_pair(base)  # never schedule unsupported slots
        ]
        tasks.append(self._refresh_reference())
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Always rebuild the shared client after a cycle so cancelled
            # wait_for requests cannot leak sockets into the next wave.
            force_recycle_client()

        self._flag_outliers()
        self._compute_composites(pairs)
        self.cycle_count += 1
        elapsed = time.time() - t0
        live = sum(1 for s in self.market_state.values()
                   if s.mid > 0 and s.status != MarketStatus.OFFLINE.value)
        if self.cycle_count % 5 == 1 or elapsed > 25:
            log.info("market cycle #%d done in %.1fs — live_feeds=%d/%d open_circuits=%s",
                     self.cycle_count, elapsed, live, len(self.market_state),
                     [k for k, v in breakers.snapshot().items() if v["state"] == "open"])

        if time.time() - self.last_snapshot_write >= settings.snapshot_interval:
            try:
                await asyncio.to_thread(self._persist_snapshots, pairs)
                self.last_snapshot_write = time.time()
            except Exception as exc:
                log.error("snapshot persist failed (will retry next cycle): %s", exc)

        self._broadcast()

    async def _process(self, connector: BaseExchangeConnector, base: str, quote: str,
                       timeout: float, refresh_stats: bool) -> None:
        key = (connector.exchange_name, base)
        ex = connector.exchange_name

        # Skip known-unsupported pairs — do NOT leave offline chips in Admin.
        if not connector.supports_pair(base):
            self.market_state.pop(key, None)
            self.books.pop(key, None)
            return
        if not breakers.allow(ex):
            self._soft_fail(key, ex, base, quote,
                            RateLimited(f"{ex} circuit open"))
            return
        if is_rate_limited(ex):
            self._soft_fail(key, ex, base, quote,
                            RateLimited(f"{ex} cooldown"))
            return

        # Slow venues get a longer wait_for budget (retries live in the connector).
        eff_timeout = timeout
        if connector.exchange_name == "Ramzinex":
            eff_timeout = max(timeout, 22.0)
        elif connector.exchange_name == "Exir":
            eff_timeout = max(timeout, 15.0)

        try:
            async with _global_sem():
                async with _ex_sem(connector.exchange_name):
                    # Time the venue only — not the wait for a semaphore slot,
                    # which made every venue look multi-second slow.
                    started = time.time()
                    book = await asyncio.wait_for(
                        connector.fetch_order_book(base), timeout=eff_timeout)
                    latency_ms = (time.time() - started) * 1000
                    if refresh_stats:
                        try:
                            stats_result = await asyncio.wait_for(
                                connector.fetch_stats(base), timeout=eff_timeout)
                            self.stats_cache[key] = stats_result
                            if (not stats_result.volume_24h_base
                                    and not stats_result.volume_24h_quote
                                    and key not in self._vol_warned):
                                self._vol_warned.add(key)
                                if getattr(connector, "supports_trades", False):
                                    log.info("%s reports no 24h volume for %s — building"
                                             " an estimate from the public trade tape",
                                             connector.exchange_name, base)
                                else:
                                    log.warning("%s reports no 24h volume for %s and has"
                                                " no trades endpoint — share will read 0",
                                                connector.exchange_name, base)
                        except Exception:
                            pass
                        cached = self.stats_cache.get(key, TickerStats())
                        if (getattr(connector, "supports_trades", False)
                                and not cached.volume_24h_base
                                and not cached.volume_24h_quote):
                            try:
                                trades = await asyncio.wait_for(
                                    connector.fetch_trades(base), timeout=eff_timeout)
                                volume_estimator.ingest(
                                    connector.exchange_name, base, trades)
                            except Exception as exc:
                                log.debug("%s trades fetch failed: %s",
                                          connector.exchange_name, exc)
            stats = self.stats_cache.get(key, TickerStats())
            vol_estimated = False
            if not stats.volume_24h_base and not stats.volume_24h_quote:
                est_base, est_quote, coverage = volume_estimator.volumes(
                    connector.exchange_name, base)
                if est_quote > 0 or est_base > 0:
                    stats = TickerStats(last_price=stats.last_price,
                                        volume_24h_base=est_base,
                                        volume_24h_quote=est_quote,
                                        change_24h_pct=stats.change_24h_pct)
                    vol_estimated = True
            best_bid, best_ask = book.best_bid, book.best_ask
            if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask * 1.5:
                raise ValueError("degenerate order book")
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            bid_depth = book.depth_notional("bid")
            ask_depth = book.depth_notional("ask")
            total = bid_depth + ask_depth
            age_ms = (time.time() - book.timestamp) * 1000
            if latency_ms > settings_store.get().stale_latency_ms:
                status = MarketStatus.STALE.value
            elif age_ms > STALE_AFTER_MS:
                status = MarketStatus.DELAYED.value
            else:
                status = MarketStatus.CONNECTED.value
            snap = MarketSnapshot(
                exchange=connector.exchange_name, base=base, quote=quote,
                best_bid=best_bid, best_ask=best_ask, mid=mid,
                spread=spread, spread_pct=spread / best_ask * 100 if best_ask else 0.0,
                bid_depth_quote=bid_depth, ask_depth_quote=ask_depth,
                depth_imbalance=(bid_depth - ask_depth) / total if total else 0.0,
                volume_24h_base=stats.volume_24h_base,
                volume_24h_quote=stats.volume_24h_quote or stats.volume_24h_base * mid,
                volume_estimated=vol_estimated,
                last_price=stats.last_price or mid,
                latency_ms=round(latency_ms, 1),
                timestamp=book.timestamp or time.time(),
                status=status,
            )
            self.books[key] = book
            self.market_state[key] = snap
            metrics_engine.ingest(snap)
            breakers.record_success(ex)
        except UnsupportedPair:
            self.market_state.pop(key, None)
            self.books.pop(key, None)
        except Exception as exc:
            breakers.record_failure(ex, f"{type(exc).__name__}: {exc}")
            self._soft_fail(key, ex, base, quote, exc)

    def _soft_fail(self, key: Tuple[str, str], exchange: str,
                   base: str, quote: str, exc: Exception) -> None:
        """On transient errors keep last-good mid as delayed for a grace window
        so the Admin board does not flap red every failed cycle."""
        now_warn = time.time()
        # RateLimited / timeouts: log less often; Unsupported already quiet
        throttle = 120 if isinstance(exc, (RateLimited, asyncio.TimeoutError,
                                           TimeoutError)) else 300
        if now_warn - self._fetch_warned.get(key, 0) > throttle:
            self._fetch_warned[key] = now_warn
            log.warning("%s %s order-book fetch FAILED: %s: %s "
                        "(run Admin → connectivity test)",
                        exchange, base, type(exc).__name__, str(exc)[:200])
        else:
            log.debug("%s %s fetch failed: %s", exchange, base, exc)

        prev = self.market_state.get(key)
        if prev and prev.mid > 0:
            age = now_warn - (prev.timestamp or 0.0)
            if age >= SOFT_FAIL_GRACE_SEC:
                prev.status = MarketStatus.OFFLINE.value
            elif prev.status not in (MarketStatus.STALE.value, MarketStatus.OUTLIER.value):
                # a stale/outlier quote must not become usable just because
                # the next fetch failed
                prev.status = MarketStatus.DELAYED.value
        else:
            self.market_state[key] = MarketSnapshot(
                exchange=exchange, base=base, quote=quote,
                status=MarketStatus.OFFLINE.value, timestamp=0.0)

    async def _refresh_reference(self) -> None:
        if not CONFIG.get("reference", {}).get("enabled", True):
            return
        if time.time() - self.usd_reference_ts < 120:
            return
        bases = [b for b, _q in self.pairs()]
        try:
            if CONFIG.get("demo_mode"):
                self.usd_reference = DemoConnector.usd_reference(bases)
                self.usd_reference_ts = time.time()
                return
            # merge persisted id overrides / previously-resolved ids
            for asset, cg_id in db.get_reference_ids().items():
                if cg_id:
                    self._reference.set_id(asset, cg_id)
            # auto-resolve one unknown asset per refresh (rate-limit friendly)
            # A failed lookup (often a CoinGecko 429) used to blacklist the asset
            # for the life of the process; retry it after a cool-down instead.
            if time.time() - self._unresolvable_ts > 1800:
                self._unresolvable.clear()
                self._unresolvable_ts = time.time()
            unknown = [b for b in bases
                       if b not in self._reference.id_map and b not in self._unresolvable]
            if unknown:
                asset = unknown[0]
                cg_id = await self._reference.resolve_id(asset)
                if cg_id:
                    self._reference.set_id(asset, cg_id)
                    db.upsert_reference_id(asset, cg_id)
                    log.info("reference: resolved %s -> %s", asset, cg_id)
                else:
                    self._unresolvable.add(asset)
                    log.warning("reference: could not resolve %s (set a manual"
                                " coingecko_id in Admin)", asset)
            prices = await self._reference.fetch_usd_prices()
            missing = sorted(b for b in bases if b not in prices)
            if missing and missing != self._ref_missing:
                log.warning("reference: no global USD price for %s — premium will"
                            " show '—' (check the coingecko id in Admin)", missing)
            self._ref_missing = missing
            # keep the last good price for an asset CoinGecko skipped this time
            self.usd_reference = {**self.usd_reference, **prices}
            self.usd_reference_ts = time.time()
        except Exception as exc:
            log.debug("reference fetch failed: %s", exc)

    # --------------------------------------------------------- composites --
    def _flag_outliers(self) -> None:
        """Mark venues whose mid is >OUTLIER_DEV_PCT off the median of the other
        venues (needs 3+ quotes to know which one is wrong); clear the flag once
        the price is back in line."""
        now = time.time()
        for base, snaps in self.snapshots_by_asset().items():
            cands = [s for s in snaps if s.mid > 0 and s.status in (
                MarketStatus.CONNECTED.value, MarketStatus.DELAYED.value,
                MarketStatus.OUTLIER.value)]
            if len(cands) < 3:
                continue
            med = statistics.median(s.mid for s in cands)
            for s in cands:
                dev = abs(s.mid - med) / med * 100
                if dev > OUTLIER_DEV_PCT:
                    if s.status != MarketStatus.OUTLIER.value:
                        log.warning("%s %s mid %.6g is %.1f%% off the cross-venue median"
                                    " %.6g — excluded as an outlier (bad pair mapping?)",
                                    s.exchange, base, s.mid, dev, med)
                    s.status = MarketStatus.OUTLIER.value
                elif s.status == MarketStatus.OUTLIER.value:
                    s.status = (MarketStatus.DELAYED.value
                                if (now - s.timestamp) * 1000 > STALE_AFTER_MS
                                else MarketStatus.CONNECTED.value)

    def usable_snaps(self, base: str) -> List[MarketSnapshot]:
        return [s for (ex, b), s in self.market_state.items() if b == base and is_usable(s)]

    def usable_books(self, base: str) -> Dict[str, OrderBook]:
        """Order books of venues that are live, fast enough and not outliers."""
        ok = {s.exchange for s in self.usable_snaps(base)}
        return {ex: b for (ex, b_), b in self.books.items() if b_ == base and ex in ok}

    def composite_mid(self, base: str) -> Optional[float]:
        mids = [s.mid for s in self.usable_snaps(base)]
        return statistics.fmean(mids) if mids else None

    def _compute_composites(self, pairs: List[Tuple[str, str]]) -> None:
        now = time.time()
        for base, _quote in pairs:
            mid = self.composite_mid(base)
            if mid:
                metrics_engine.ingest_composite(base, now, mid)

    def premium_pct(self, base: str, method: str = "composite",
                    exchange: Optional[str] = None) -> Optional[float]:
        if base == "USDT":
            return None
        from .premium import live_premium
        by_asset = self.snapshots_by_asset()
        return live_premium(by_asset.get(base, []), by_asset.get("USDT", []),
                            self.usd_reference.get(base, 0.0), method, exchange)

    # -------------------------------------------------------- persistence --
    def _persist_snapshots(self, pairs: List[Tuple[str, str]]) -> None:
        now = time.time()
        snap_rows = []
        comp_rows = []
        for (ex, base), s in self.market_state.items():
            if not is_usable(s):
                continue
            snap_rows.append((ex, base, s.quote, s.best_bid, s.best_ask, s.mid,
                              s.spread_pct, s.bid_depth_quote, s.ask_depth_quote,
                              s.volume_24h_base, s.volume_24h_quote, now))
        for base, quote in pairs:
            mid = self.composite_mid(base)
            if not mid:
                continue
            live = self.usable_snaps(base)
            best_bid = max((s.best_bid for s in live), default=0.0)
            best_ask = min((s.best_ask for s in live if s.best_ask > 0), default=0.0)
            volume = sum(s.volume_24h_quote for s in live)
            comp_rows.append((base, quote, mid, best_bid, best_ask, volume,
                              self.premium_pct(base), now))
        volume_estimator.flush()
        ref_rows = [(asset, usd, now) for asset, usd in self.usd_reference.items()
                    if usd > 0]
        try:
            if snap_rows:
                db.insert_snapshots(snap_rows)
            if comp_rows:
                db.insert_composites(comp_rows)
            if ref_rows:
                db.insert_reference_prices(ref_rows)
        except Exception as exc:
            log.error("snapshot persist failed: %s", exc)

    # ----------------------------------------------------------- fan-out ---
    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self.listeners.append(fn)

    def _broadcast(self) -> None:
        if not self.listeners:
            return
        payload = {"type": "markets", "ts": time.time(),
                   "data": [s.to_dict() for s in self.market_state.values()]}
        for fn in list(self.listeners):
            try:
                fn(payload)
            except Exception:
                pass

    # -------------------------------------------------------------- views --
    def snapshots_by_asset(self) -> Dict[str, List[MarketSnapshot]]:
        out: Dict[str, List[MarketSnapshot]] = {}
        for (ex, base), s in self.market_state.items():
            out.setdefault(base, []).append(s)
        return out


market_aggregator = MarketAggregator()
