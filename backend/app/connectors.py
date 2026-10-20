"""Exchange connectors.

Design goals vs v1:
- Full order-book DEPTH (top-N levels), not just best bid/ask.
- 24h ticker stats (last price, base/quote volume) per exchange.
- Global USD reference prices (CoinGecko) → Iran premium computation.
- GenericRestConnector: add a new exchange with a JSON spec at runtime, no code.
- DemoConnector: realistic synthetic markets for offline development/testing.

All prices are normalized to TOMAN (TMN). Exchanges quoting Rial are divided by 10.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from .models import OrderBook, TickerStats

log = logging.getLogger("terminal.connectors")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
}

# Bounded pool + aggressive client recycle.
#
# Failure mode we hit in production (18 assets × 6 venues = 108 concurrent
# fetches): asyncio.wait_for cancels leave pooled connections "in use", the
# pool fills, and every subsequent request stalls → TimeoutError cascade and
# Admin chips go red after a few minutes. Fix is three-part:
#   1. Cap the pool well above the aggregator semaphore (see MAX_INFLIGHT).
#   2. Split timeouts so a slow pool acquire does not burn the whole budget.
#   3. Recycle the shared client every cycle (and at least every 60s) so leaks
#      cannot accumulate across minutes.
MAX_INFLIGHT = 20               # must match aggregator global semaphore
_LIMITS = httpx.Limits(max_connections=max(60, MAX_INFLIGHT * 3),
                       max_keepalive_connections=20,
                       keepalive_expiry=20.0)
_CLIENT_MAX_AGE = 60.0          # hard ceiling even if cycle recycle is skipped

_client: Optional[httpx.AsyncClient] = None
_client_ts: float = 0.0
_client_timeout: float = 12.0

# Shared rate-limit cooldowns: exchange_name -> unix ts until which we skip
_rate_limited_until: Dict[str, float] = {}


class UnsupportedPair(ValueError):
    """Pair is not listed on this venue — not a network failure."""


class RateLimited(RuntimeError):
    """Venue returned 429; caller should soft-fail without thrashing."""


def get_client(timeout: float = 12.0) -> httpx.AsyncClient:
    global _client, _client_ts, _client_timeout
    # Rebuild if closed OR if the caller's timeout budget changed meaningfully
    if (_client is None or _client.is_closed
            or abs(timeout - _client_timeout) >= 1.0):
        if _client is not None and not _client.is_closed:
            try:
                asyncio.get_running_loop().create_task(_aclose_later(_client, 2.0))
            except RuntimeError:
                pass
        # connect a bit generous for flaky IR routes; read gets the request budget
        t = httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0)
        _client = httpx.AsyncClient(timeout=t, headers=HEADERS,
                                    follow_redirects=True, limits=_LIMITS)
        _client_ts = time.time()
        _client_timeout = timeout
    return _client


def maybe_recycle_client(force: bool = False) -> None:
    """Drop and rebuild the shared client. Call BETWEEN cycles only — never
    while requests on the current client are still in flight."""
    global _client, _client_ts
    if _client is None or _client.is_closed:
        return
    if not force and time.time() - _client_ts <= _CLIENT_MAX_AGE:
        return
    old, _client = _client, None
    try:
        asyncio.get_running_loop().create_task(_aclose_later(old, 5.0))
    except RuntimeError:
        pass


def force_recycle_client() -> None:
    """Always rebuild after a poll cycle to shed cancelled-request leaks."""
    maybe_recycle_client(force=True)


def is_rate_limited(exchange: str) -> bool:
    until = _rate_limited_until.get(exchange, 0.0)
    return time.time() < until


def mark_rate_limited(exchange: str, seconds: float = 45.0) -> None:
    # Do not shorten an existing longer cooldown.
    until = time.time() + max(5.0, seconds)
    _rate_limited_until[exchange] = max(_rate_limited_until.get(exchange, 0.0), until)
    log.warning("%s rate-limited — cooling down until +%.0fs", exchange, seconds)


async def _aclose_later(client: httpx.AsyncClient, delay: float = 10.0) -> None:
    try:
        await asyncio.sleep(delay)         # let in-flight requests drain first
        await client.aclose()
    except Exception:
        pass


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def request_get(url: str, *, exchange: str = "",
                      timeout: Optional[float] = None, **kwargs) -> httpx.Response:
    """GET with 429 → RateLimited. Prefer this for venue calls that rate-limit."""
    if exchange and is_rate_limited(exchange):
        raise RateLimited(f"{exchange} in cooldown")
    client = get_client(timeout if timeout is not None else _client_timeout)
    r = await client.get(url, **kwargs)
    if r.status_code == 429:
        # Exir in particular needs a long quiet window or every cycle re-trips 429.
        cool = 180.0 if exchange == "Exir" else 90.0
        if exchange:
            mark_rate_limited(exchange, cool)
        raise RateLimited(f"{exchange or url}: HTTP 429")
    return r

def first_positive(item: Dict[str, Any], keys: tuple) -> float:
    """Scan a payload dict for the first positive numeric value among keys.
    Iranian exchange APIs are inconsistent about volume field names."""
    for key in keys:
        v = safe_float(item.get(key))
        if v > 0:
            return v
    return 0.0


BASE_VOL_KEYS = ("volume", "daily_volume", "volume_24h", "base_volume",
                 "daily_volume_base", "vol", "amount", "baseVolume")
QUOTE_VOL_KEYS = ("quoteVolume", "quote_volume", "daily_volume_quote",
                  "volume_quote", "value", "quote_volume_24h")


def _normalize_trades(raw: Any, price_scale: float = 1.0) -> List[Dict[str, Any]]:
    """Convert heterogeneous trade payloads to [{id, ts, price, amount}]."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    now = time.time()
    for item in raw:
        tid, ts, price, amount = None, None, 0.0, 0.0
        if isinstance(item, dict):
            tid = item.get("id", item.get("match_id", item.get("trade_id")))
            ts = safe_float(item.get("time", item.get("timestamp",
                            item.get("created_at", item.get("date", 0)))))
            price = safe_float(item.get("price", item.get("rate")))
            amount = safe_float(item.get("amount", item.get("qty",
                                item.get("match_amount", item.get("quantity",
                                item.get("base_amount"))))))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, amount = safe_float(item[0]), safe_float(item[1])
            for v in item[2:]:
                fv = safe_float(v)
                if fv > 1e9:            # looks like a timestamp (s or ms)
                    ts = fv
                    break
        if ts and ts > 2e10:            # ms → s
            ts /= 1000
        if not ts or ts <= 0 or ts > now + 60:
            ts = now
        if price > 0 and amount > 0:
            out.append({"id": tid, "ts": ts, "price": price * price_scale,
                        "amount": amount})
    return out


def safe_float(value: Any) -> float:
    """Defensively convert exchange payload values to float."""
    try:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.replace(",", "").strip() or 0)
        if isinstance(value, (list, tuple)) and value:
            return safe_float(value[0])
        if isinstance(value, dict):
            for key in ("price", "value", "amount"):
                if key in value:
                    return safe_float(value[key])
    except (TypeError, ValueError):
        pass
    return 0.0


def _normalize_levels(raw: Any, price_scale: float = 1.0,
                      price_key: str = "price", qty_key: str = "quantity",
                      descending: bool = False) -> List[List[float]]:
    """Convert heterogeneous level formats to [[price, qty], ...] sorted best-first."""
    levels: List[List[float]] = []
    if not isinstance(raw, list):
        return levels
    for item in raw:
        price, qty = 0.0, 0.0
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            price, qty = safe_float(item[0]), safe_float(item[1])
        elif isinstance(item, dict):
            price = safe_float(item.get(price_key, item.get("price")))
            qty = safe_float(item.get(qty_key, item.get("amount", item.get("qty"))))
        if price > 0 and qty > 0:
            levels.append([price * price_scale, qty])
    levels.sort(key=lambda x: x[0], reverse=descending)
    return levels


class BaseExchangeConnector:
    exchange_name: str = "Base"
    supports_candles: bool = False

    def __init__(self, depth_levels: int = 20) -> None:
        self.depth_levels = depth_levels

    def supports_pair(self, base: str) -> bool:
        """Return False for known-unsupported pairs so the aggregator skips
        them without burning concurrency or spamming error logs."""
        return True

    # -- required ------------------------------------------------------------
    async def fetch_order_book(self, base: str) -> OrderBook:
        raise NotImplementedError

    # -- optional ------------------------------------------------------------
    async def fetch_stats(self, base: str) -> TickerStats:
        return TickerStats()

    async def fetch_candles(self, base: str, resolution_sec: int,
                            since_ts: float, until_ts: float) -> List[Dict[str, float]]:
        """Return [{ts, open, high, low, close, volume}, ...]."""
        return []

    supports_trades: bool = False

    async def fetch_trades(self, base: str) -> List[Dict[str, Any]]:
        """Recent public trades: [{id?, ts, price, amount}] — used to build a
        24h volume estimate for venues that don't report volume."""
        return []

    def _book(self, bids: List[List[float]], asks: List[List[float]],
              ts: Optional[float] = None) -> OrderBook:
        return OrderBook(bids=bids[: self.depth_levels],
                         asks=asks[: self.depth_levels],
                         timestamp=ts or time.time())


# =============================================================== Nobitex ====

class NobitexConnector(BaseExchangeConnector):
    exchange_name = "Nobitex"
    supports_candles = True
    BASE_URL = "https://apiv2.nobitex.ir"
    RIAL_SCALE = 0.1  # Rial -> Toman

    def _symbol(self, base: str) -> str:
        return f"{base.upper()}IRT"

    async def fetch_order_book(self, base: str) -> OrderBook:
        r = await get_client().get(f"{self.BASE_URL}/v3/orderbook/{self._symbol(base)}")
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise ValueError(f"Nobitex status: {data.get('status')}")
        ts = safe_float(data.get("lastUpdate")) / 1000 or time.time()
        bids = _normalize_levels(data.get("bids"), self.RIAL_SCALE, descending=True)
        asks = _normalize_levels(data.get("asks"), self.RIAL_SCALE)
        return self._book(bids, asks, ts)

    async def fetch_stats(self, base: str) -> TickerStats:
        r = await get_client().get(
            f"{self.BASE_URL}/market/stats",
            params={"srcCurrency": base.lower(), "dstCurrency": "rls"},
        )
        r.raise_for_status()
        stats = r.json().get("stats", {}).get(f"{base.lower()}-rls", {})
        return TickerStats(
            last_price=safe_float(stats.get("latest")) * self.RIAL_SCALE,
            volume_24h_base=safe_float(stats.get("volumeSrc")),
            volume_24h_quote=safe_float(stats.get("volumeDst")) * self.RIAL_SCALE,
            change_24h_pct=safe_float(stats.get("dayChange")) or None,
        )

    async def fetch_candles(self, base: str, resolution_sec: int,
                            since_ts: float, until_ts: float) -> List[Dict[str, float]]:
        res_map = {60: "1", 300: "5", 900: "15", 3600: "60", 14400: "240", 86400: "D"}
        resolution = res_map.get(resolution_sec, "5")
        r = await get_client().get(
            f"{self.BASE_URL}/market/udf/history",
            params={"symbol": self._symbol(base), "resolution": resolution,
                    "from": int(since_ts), "to": int(until_ts)},
        )
        r.raise_for_status()
        d = r.json()
        if d.get("s") != "ok":
            return []
        out = []
        for i, t in enumerate(d.get("t", [])):
            out.append({
                "ts": float(t),
                "open": safe_float(d["o"][i]) * self.RIAL_SCALE,
                "high": safe_float(d["h"][i]) * self.RIAL_SCALE,
                "low": safe_float(d["l"][i]) * self.RIAL_SCALE,
                "close": safe_float(d["c"][i]) * self.RIAL_SCALE,
                "volume": safe_float(d.get("v", [0] * len(d["t"]))[i]),
            })
        return out


# ================================================================ Wallex ====

class WallexConnector(BaseExchangeConnector):
    exchange_name = "Wallex"
    supports_candles = True
    BASE_URL = "https://api.wallex.ir"

    def _symbol(self, base: str) -> str:
        return f"{base.upper()}TMN"

    async def fetch_order_book(self, base: str) -> OrderBook:
        r = await get_client().get(f"{self.BASE_URL}/v1/depth",
                                   params={"symbol": self._symbol(base)})
        r.raise_for_status()
        result = r.json().get("result", {})
        bids = _normalize_levels(result.get("bid"), descending=True)
        asks = _normalize_levels(result.get("ask"))
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        r = await get_client().get(f"{self.BASE_URL}/v1/markets")
        r.raise_for_status()
        sym = r.json().get("result", {}).get("symbols", {}).get(self._symbol(base), {})
        stats = sym.get("stats", {})
        return TickerStats(
            last_price=safe_float(stats.get("lastPrice")),
            volume_24h_base=safe_float(stats.get("24h_volume")),
            volume_24h_quote=safe_float(stats.get("24h_quoteVolume")),
            change_24h_pct=safe_float(stats.get("24h_ch")) or None,
        )

    async def fetch_candles(self, base: str, resolution_sec: int,
                            since_ts: float, until_ts: float) -> List[Dict[str, float]]:
        res_map = {60: "1", 300: "5", 900: "15", 3600: "60", 14400: "240", 86400: "D"}
        r = await get_client().get(
            f"{self.BASE_URL}/v1/udf/history",
            params={"symbol": self._symbol(base),
                    "resolution": res_map.get(resolution_sec, "5"),
                    "from": int(since_ts), "to": int(until_ts)},
        )
        r.raise_for_status()
        d = r.json()
        if d.get("s") != "ok":
            return []
        return [{
            "ts": float(t),
            "open": safe_float(d["o"][i]), "high": safe_float(d["h"][i]),
            "low": safe_float(d["l"][i]), "close": safe_float(d["c"][i]),
            "volume": safe_float(d.get("v", [0] * len(d["t"]))[i]),
        } for i, t in enumerate(d.get("t", []))]


# ================================================================ Bitpin ====

class BitpinConnector(BaseExchangeConnector):
    exchange_name = "Bitpin"
    supports_trades = True
    BASE_URL = "https://api.bitpin.org"

    async def fetch_trades(self, base: str) -> List[Dict[str, Any]]:
        r = await get_client().get(
            f"{self.BASE_URL}/api/v1/mth/matches/{self._symbol(base)}/")
        r.raise_for_status()
        payload = r.json()
        raw = payload if isinstance(payload, list) else payload.get("results", [])
        return _normalize_trades(raw)

    def _symbol(self, base: str) -> str:
        return f"{base.upper()}_IRT"

    async def fetch_order_book(self, base: str) -> OrderBook:
        r = await get_client().get(
            f"{self.BASE_URL}/api/v1/mth/orderbook/{self._symbol(base)}/")
        r.raise_for_status()
        data = r.json()
        bids = _normalize_levels(data.get("bids"), descending=True)
        asks = _normalize_levels(data.get("asks"))
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        sym = self._symbol(base)
        # 1st source: tickers endpoint
        try:
            r = await get_client().get(f"{self.BASE_URL}/api/v1/mkt/tickers/")
            r.raise_for_status()
            payload = r.json()
            items = payload if isinstance(payload, list) else payload.get("results", [])
            for item in items:
                if isinstance(item, dict) and str(item.get("symbol", "")).upper() == sym:
                    stats = TickerStats(
                        last_price=safe_float(item.get("price")),
                        volume_24h_base=first_positive(item, BASE_VOL_KEYS),
                        volume_24h_quote=first_positive(item, QUOTE_VOL_KEYS),
                        change_24h_pct=safe_float(item.get("daily_change_price")) or None,
                    )
                    if stats.volume_24h_base or stats.volume_24h_quote:
                        return stats
                    log.debug("Bitpin tickers has no volume for %s; keys=%s",
                              sym, list(item.keys()))
                    break
        except Exception as exc:
            log.debug("Bitpin tickers failed: %s", exc)
        # 2nd source: markets endpoint (paginated, includes volume info)
        try:
            r = await get_client().get(f"{self.BASE_URL}/api/v1/mkt/markets/")
            r.raise_for_status()
            payload = r.json()
            items = payload if isinstance(payload, list) else payload.get("results", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code", item.get("symbol", ""))).upper()
                if code != sym:
                    continue
                info = item.get("price_info", item)
                if not isinstance(info, dict):
                    info = item
                return TickerStats(
                    last_price=safe_float(info.get("price", item.get("price"))),
                    volume_24h_base=first_positive({**item, **info}, BASE_VOL_KEYS),
                    volume_24h_quote=first_positive({**item, **info}, QUOTE_VOL_KEYS),
                )
        except Exception as exc:
            log.debug("Bitpin markets failed: %s", exc)
        return TickerStats()


# ================================================================== Exir ====

class ExirConnector(BaseExchangeConnector):
    exchange_name = "Exir"
    supports_candles = True
    BASE_URL = "https://api.exir.io"
    # Exir 429s hard under multi-pair fan-out. Poll only the three desk majors
    # and keep per-exchange concurrency at 1 (see EXCHANGE_INFLIGHT).
    SUPPORTED = {"BTC", "ETH", "USDT"}

    def _symbol(self, base: str) -> str:
        return f"{base.lower()}-irt"

    def supports_pair(self, base: str) -> bool:
        return base.upper() in self.SUPPORTED

    async def fetch_order_book(self, base: str) -> OrderBook:
        if not self.supports_pair(base):
            raise UnsupportedPair(f"Exir: pair not polled for {base}")
        if is_rate_limited(self.exchange_name):
            raise RateLimited("Exir in cooldown")
        sym = self._symbol(base)
        r = await request_get(f"{self.BASE_URL}/v2/orderbook",
                              exchange=self.exchange_name, params={"symbol": sym})
        r.raise_for_status()
        book = r.json().get(sym, {})
        bids = _normalize_levels(book.get("bids"), descending=True)
        asks = _normalize_levels(book.get("asks"))
        # Tiny pause so back-to-back Exir calls in one cycle do not trip 429.
        await asyncio.sleep(0.35)
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        if not self.supports_pair(base) or is_rate_limited(self.exchange_name):
            return TickerStats()
        r = await request_get(f"{self.BASE_URL}/v2/ticker",
                              exchange=self.exchange_name,
                              params={"symbol": self._symbol(base)})
        r.raise_for_status()
        d = r.json()
        return TickerStats(
            last_price=safe_float(d.get("last", d.get("close"))),
            volume_24h_base=safe_float(d.get("volume")),
            volume_24h_quote=0.0,
        )

    async def fetch_candles(self, base: str, resolution_sec: int,
                            since_ts: float, until_ts: float) -> List[Dict[str, float]]:
        res_map = {60: "1", 300: "5", 900: "15", 3600: "60", 86400: "1D"}
        r = await get_client().get(
            f"{self.BASE_URL}/v2/chart",
            params={"symbol": self._symbol(base),
                    "resolution": res_map.get(resolution_sec, "60"),
                    "from": int(since_ts), "to": int(until_ts)},
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            ts = row.get("time")
            if isinstance(ts, str):
                from datetime import datetime
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
            out.append({"ts": float(ts), "open": safe_float(row.get("open")),
                        "high": safe_float(row.get("high")), "low": safe_float(row.get("low")),
                        "close": safe_float(row.get("close")),
                        "volume": safe_float(row.get("volume"))})
        return out


# =============================================================== Tabdeal ====

class TabdealConnector(BaseExchangeConnector):
    exchange_name = "Tabdeal"
    supports_trades = True
    BASE_URL = "https://api1.tabdeal.org"

    async def fetch_trades(self, base: str) -> List[Dict[str, Any]]:
        r = await get_client().get(f"{self.BASE_URL}/r/api/v1/trades",
                                   params={"symbol": self._symbol(base), "limit": 500})
        r.raise_for_status()
        return _normalize_trades(r.json())

    def _symbol(self, base: str) -> str:
        return f"{base.upper()}IRT"

    async def fetch_order_book(self, base: str) -> OrderBook:
        r = await get_client().get(
            f"{self.BASE_URL}/r/api/v1/depth",
            params={"symbol": self._symbol(base), "limit": self.depth_levels},
        )
        r.raise_for_status()
        data = r.json()
        bids = _normalize_levels(data.get("bids"), descending=True)
        asks = _normalize_levels(data.get("asks"))
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        sym = self._symbol(base)

        def parse(d: Dict[str, Any]) -> TickerStats:
            return TickerStats(
                last_price=safe_float(d.get("lastPrice", d.get("last"))),
                volume_24h_base=first_positive(d, BASE_VOL_KEYS),
                volume_24h_quote=first_positive(d, QUOTE_VOL_KEYS),
                change_24h_pct=safe_float(d.get("priceChangePercent")) or None,
            )

        # try the symbol-scoped call, then fall back to the full list
        for params in ({"symbol": sym}, None):
            try:
                r = await get_client().get(f"{self.BASE_URL}/r/api/v1/ticker/24hr",
                                           params=params)
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:
                log.debug("Tabdeal 24hr (%s) failed: %s", params, exc)
                continue
            if isinstance(payload, dict) and payload.get("symbol", sym).upper() in (sym, ""):
                stats = parse(payload)
                if stats.volume_24h_base or stats.volume_24h_quote or stats.last_price:
                    return stats
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and str(item.get("symbol", "")).upper() == sym:
                        return parse(item)
        return TickerStats()


# ============================================================== Ramzinex ====

class RamzinexConnector(BaseExchangeConnector):
    exchange_name = "Ramzinex"
    supports_trades = True
    # publicapi often times out on some ISP/CDN edges — try multiple bases.
    BASE_URLS = (
        os.environ.get("RAMZINEX_API_BASE", "").strip() or None,
        "https://publicapi.ramzinex.com",
        "https://api.ramzinex.com",
    )
    RIAL_SCALE = 0.1
    # Numeric pair ids (IRR-quoted) for markets we poll.
    PAIR_IDS: Dict[str, int] = {
        "BTC": 2, "ETH": 3, "USDT": 11,
        # common extras (best-effort; unknown ids are skipped via supports_pair).
        # LTC=4 / BNB=13 were wrong guesses (4 quoted an XRP-level price, 13 a
        # few hundred TMN for BNB) — removed until the real ids are confirmed.
        "XRP": 9,
    }

    def supports_pair(self, base: str) -> bool:
        return base.upper() in self.PAIR_IDS

    def _bases(self) -> List[str]:
        return [b for b in self.BASE_URLS if b]

    async def _get_json(self, path: str, timeout: float = 25.0) -> Any:
        last_exc: Optional[Exception] = None
        client = get_client(timeout=timeout)
        for base in self._bases():
            url = f"{base.rstrip('/')}{path}"
            for attempt in range(2):
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    return r.json()
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError(f"Ramzinex failed: {path}")

    async def fetch_trades(self, base: str) -> List[Dict[str, Any]]:
        pair_id = self.PAIR_IDS.get(base.upper())
        if pair_id is None:
            return []
        try:
            payload = await self._get_json(
                f"/exchange/api/v1.0/exchange/trades/{pair_id}")
        except Exception:
            return []
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        return _normalize_trades(raw, self.RIAL_SCALE)

    async def fetch_order_book(self, base: str) -> OrderBook:
        pair_id = self.PAIR_IDS.get(base.upper())
        if pair_id is None:
            raise UnsupportedPair(f"Ramzinex: unknown pair for {base}")
        data = await self._get_json(
            f"/exchange/api/v1.0/exchange/orderbooks/{pair_id}/buys_sells",
            timeout=30.0)
        book = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(book, dict):
            book = {}
        bids = _normalize_levels(book.get("buys"), self.RIAL_SCALE, descending=True)
        asks = _normalize_levels(book.get("sells"), self.RIAL_SCALE)
        if not bids or not asks:
            raise ValueError("Ramzinex empty book")
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        pair_id = self.PAIR_IDS.get(base.upper())
        if pair_id is None:
            return TickerStats()
        try:
            data = await self._get_json(
                f"/exchange/api/v1.0/exchange/pairs/{pair_id}")
        except Exception:
            return TickerStats()
        data = data.get("data", data) if isinstance(data, dict) else {}
        if isinstance(data, list):
            data = data[0] if data else {}
        pair = data.get("pair", data) if isinstance(data, dict) else {}
        fin = pair.get("financial", data.get("financial", {})) or {}
        last24 = fin.get("last24h", fin) or {}
        if not isinstance(last24, dict):
            return TickerStats()
        return TickerStats(
            last_price=safe_float(last24.get("close")) * self.RIAL_SCALE,
            volume_24h_base=first_positive(last24, BASE_VOL_KEYS),
            volume_24h_quote=first_positive(last24, QUOTE_VOL_KEYS) * self.RIAL_SCALE,
            change_24h_pct=safe_float(last24.get("change_percent")) or None,
        )


# =========================================================== Generic REST ===

class GenericRestConnector(BaseExchangeConnector):
    """Declarative connector: add an exchange with JSON, no code, no rebuild.

    Spec example (POST /api/admin/exchanges):
    {
      "name": "MyExchange",
      "orderbook_url": "https://api.example.com/depth?symbol={symbol}",
      "symbol_template": "{base}{quote}",           // {base}/{quote} placeholders
      "symbol_overrides": {"BTC": "XBTIRT"},        // optional per-asset override
      "quote_name": "IRT",                          // what the venue calls Toman
      "bids_path": "result.bids",                   // dot path; {symbol} allowed
      "asks_path": "result.asks",
      "price_key": "price", "qty_key": "quantity",  // when levels are objects
      "price_scale": 1.0,                           // 0.1 to convert Rial->Toman
      "stats_url": "https://api.example.com/ticker?symbol={symbol}",   // optional
      "last_path": "lastPrice",
      "volume_base_path": "volume",
      "volume_quote_path": "quoteVolume",
      "taker_fee_pct": 0.25
    }
    """

    def __init__(self, spec: Dict[str, Any], depth_levels: int = 20) -> None:
        super().__init__(depth_levels)
        self.spec = spec
        self.exchange_name = spec.get("name", "Custom")

    def _symbol(self, base: str) -> str:
        overrides = self.spec.get("symbol_overrides", {})
        if base.upper() in overrides:
            return overrides[base.upper()]
        template = self.spec.get("symbol_template", "{base}{quote}")
        return template.format(base=base.upper(),
                               quote=self.spec.get("quote_name", "IRT"))

    @staticmethod
    def _resolve(data: Any, path: str, symbol: str = "") -> Any:
        if not path:
            return None
        node = data
        for part in path.replace("{symbol}", symbol).split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return node

    async def fetch_order_book(self, base: str) -> OrderBook:
        sym = self._symbol(base)
        url = self.spec["orderbook_url"].format(symbol=sym, base=base.upper())
        r = await get_client().get(url)
        r.raise_for_status()
        data = r.json()
        scale = float(self.spec.get("price_scale", 1.0))
        pk = self.spec.get("price_key", "price")
        qk = self.spec.get("qty_key", "quantity")
        bids = _normalize_levels(self._resolve(data, self.spec.get("bids_path", "bids"), sym),
                                 scale, pk, qk, descending=True)
        asks = _normalize_levels(self._resolve(data, self.spec.get("asks_path", "asks"), sym),
                                 scale, pk, qk)
        return self._book(bids, asks)

    async def fetch_stats(self, base: str) -> TickerStats:
        url = self.spec.get("stats_url")
        if not url:
            return TickerStats()
        sym = self._symbol(base)
        r = await get_client().get(url.format(symbol=sym, base=base.upper()))
        r.raise_for_status()
        data = r.json()
        scale = float(self.spec.get("price_scale", 1.0))
        return TickerStats(
            last_price=safe_float(self._resolve(data, self.spec.get("last_path", ""), sym)) * scale,
            volume_24h_base=safe_float(self._resolve(data, self.spec.get("volume_base_path", ""), sym)),
            volume_24h_quote=safe_float(self._resolve(data, self.spec.get("volume_quote_path", ""), sym)) * scale,
        )


# ======================================================= Global reference ===

class ReferenceConnector:
    """Global USD prices (CoinGecko, keyless) → used for the Iran premium.

    Any asset can be referenced: unknown symbols are auto-resolved through the
    CoinGecko search API and cached, so pairs added at runtime through the
    admin panel get a premium too. A manual coingecko_id override is supported.
    """

    URL = "https://api.coingecko.com/api/v3/simple/price"
    SEARCH_URL = "https://api.coingecko.com/api/v3/search"

    def __init__(self, id_map: Dict[str, str]) -> None:
        self.id_map = dict(id_map)  # {"BTC": "bitcoin", ...}

    def set_id(self, asset: str, cg_id: str) -> None:
        self.id_map[asset.upper()] = cg_id

    async def resolve_id(self, symbol: str) -> Optional[str]:
        """Find the CoinGecko id for a ticker symbol (exact symbol match wins)."""
        try:
            r = await get_client().get(self.SEARCH_URL, params={"query": symbol})
            r.raise_for_status()
            coins = r.json().get("coins", [])
        except Exception as exc:
            log.debug("reference resolve %s failed: %s", symbol, exc)
            return None
        sym = symbol.lower()
        for coin in coins:
            if str(coin.get("symbol", "")).lower() == sym:
                return coin.get("id")
        return coins[0].get("id") if coins else None

    async def fetch_usd_prices(self) -> Dict[str, float]:
        ids = ",".join(v for v in self.id_map.values() if v)
        if not ids:
            return {}
        r = await get_client().get(self.URL, params={"ids": ids, "vs_currencies": "usd"})
        r.raise_for_status()
        data = r.json()
        out: Dict[str, float] = {}
        for asset, cg_id in self.id_map.items():
            price = safe_float(data.get(cg_id, {}).get("usd"))
            if price > 0:
                out[asset.upper()] = price
        return out


# ================================================================== Demo ====

class DemoConnector(BaseExchangeConnector):
    """Synthetic market generator for offline development and demos.

    Random-walk mid prices per exchange with realistic spreads, depth ladders,
    volumes and occasional liquidity shocks so alerts/anomalies fire.
    """

    _shared_state: Dict[str, Dict[str, float]] = {}   # {asset: {"mid": x}}
    _usd_state: Dict[str, float] = {}

    BASE_USD = {"BTC": 108_000.0, "ETH": 3_900.0, "USDT": 1.0}
    USDT_TMN = 112_000.0

    def __init__(self, name: str, bias_pct: float = 0.0,
                 spread_bps: float = 12.0, depth_levels: int = 20) -> None:
        super().__init__(depth_levels)
        self.exchange_name = name
        self.bias_pct = bias_pct          # persistent premium/discount vs composite
        self.spread_bps = spread_bps
        self._rng = random.Random(hash(name) & 0xFFFF)
        self._shock_until = 0.0

    @classmethod
    def _composite_mid(cls, base: str) -> float:
        base = base.upper()
        state = cls._shared_state.setdefault(base, {})
        if "mid" not in state:
            usd = cls.BASE_USD.get(base, 100.0)
            state["mid"] = usd * cls.USDT_TMN * 1.025  # ~2.5% Iran premium
        # global random walk (shared across exchanges so they stay correlated)
        state["mid"] *= 1 + random.gauss(0, 0.0006)
        return state["mid"]

    @classmethod
    def _pseudo_usd(cls, asset: str) -> float:
        """Stable pseudo USD price for assets outside BASE_USD (demo mode)."""
        return (abs(hash(asset.upper())) % 49_000) / 100 + 10   # $10 – $500

    @classmethod
    def usd_reference(cls, assets: Optional[List[str]] = None) -> Dict[str, float]:
        symbols = set(cls.BASE_USD) | {a.upper() for a in (assets or [])}
        out = {}
        for asset in symbols:
            base = cls.BASE_USD.get(asset, cls._pseudo_usd(asset))
            cur = cls._usd_state.setdefault(asset, base)
            cls._usd_state[asset] = cur * (1 + random.gauss(0, 0.0004))
            out[asset] = cls._usd_state[asset]
        return out

    async def fetch_order_book(self, base: str) -> OrderBook:
        await asyncio.sleep(self._rng.uniform(0.01, 0.06))  # simulated latency
        now = time.time()
        mid = self._composite_mid(base) * (1 + self.bias_pct / 100
                                           + self._rng.gauss(0, 0.0008))
        spread_bps = self.spread_bps * (1 + abs(self._rng.gauss(0, 0.35)))
        # occasional liquidity shock (~0.5% of cycles, lasts 2 minutes)
        if self._rng.random() < 0.005:
            self._shock_until = now + 120
        shock = 0.15 if now < self._shock_until else 1.0
        half_spread = mid * spread_bps / 20000
        bids, asks = [], []
        unit = {"BTC": 0.05, "ETH": 0.8, "USDT": 40_000}.get(base.upper(), 1.0)
        for i in range(self.depth_levels):
            step = mid * 0.0004 * (i + 1)
            qty_b = unit * self._rng.uniform(0.3, 1.8) * shock * (1 + i * 0.15)
            qty_a = unit * self._rng.uniform(0.3, 1.8) * shock * (1 + i * 0.15)
            bids.append([mid - half_spread - step, qty_b])
            asks.append([mid + half_spread + step, qty_a])
        return OrderBook(bids=bids, asks=asks, timestamp=now)

    async def fetch_stats(self, base: str) -> TickerStats:
        # Ramzinex/Bitpin/Tabdeal simulate "no reported volume" in demo mode so
        # the trade-tape estimator path is exercised end-to-end.
        if self.exchange_name in ("Ramzinex", "Bitpin", "Tabdeal"):
            mid = self._shared_state.get(base.upper(), {}).get("mid", 0.0)
            return TickerStats(last_price=mid * (1 + self.bias_pct / 100))
        mid = self._shared_state.get(base.upper(), {}).get("mid", 0.0)
        unit = {"BTC": 0.05, "ETH": 0.8, "USDT": 40_000}.get(base.upper(), 1.0)
        vol_base = unit * self._rng.uniform(300, 1500)
        return TickerStats(
            last_price=mid * (1 + self.bias_pct / 100),
            volume_24h_base=vol_base,
            volume_24h_quote=vol_base * mid,
            change_24h_pct=self._rng.gauss(0.5, 2.0),
        )

    supports_trades = True

    async def fetch_trades(self, base: str) -> List[Dict[str, Any]]:
        mid = self._shared_state.get(base.upper(), {}).get("mid", 0.0)
        if mid <= 0:
            return []
        unit = {"BTC": 0.05, "ETH": 0.8, "USDT": 40_000}.get(base.upper(), 1.0)
        now = time.time()
        return [{"id": f"{self.exchange_name}-{base}-{int(now)}-{i}",
                 "ts": now - self._rng.uniform(0, 55),
                 "price": mid * (1 + self._rng.gauss(0, 0.0006)),
                 "amount": unit * self._rng.uniform(0.05, 0.9)}
                for i in range(self._rng.randint(2, 9))]


# =============================================================== Registry ===

CONNECTOR_REGISTRY: Dict[str, type] = {
    "Nobitex": NobitexConnector,
    "Wallex": WallexConnector,
    "Bitpin": BitpinConnector,
    "Exir": ExirConnector,
    "Tabdeal": TabdealConnector,
    "Ramzinex": RamzinexConnector,
}

DEMO_PROFILES = [
    # (name, bias_pct vs composite, typical spread bps)
    ("Nobitex", -0.15, 8.0),
    ("Wallex", 0.10, 12.0),
    ("Bitpin", 0.30, 10.0),
    ("Exir", -0.35, 18.0),
    ("Tabdeal", 0.05, 14.0),
    ("Ramzinex", 0.45, 22.0),
]


def build_connectors(config: Dict[str, Any],
                     custom_specs: List[Dict[str, Any]]) -> List[BaseExchangeConnector]:
    depth = int(config.get("depth_levels", 20))
    if config.get("demo_mode"):
        enabled = {n for n, s in config["exchanges"].items() if s.get("enabled")}
        return [DemoConnector(name, bias, spread, depth)
                for name, bias, spread in DEMO_PROFILES if name in enabled]
    connectors: List[BaseExchangeConnector] = []
    for name, spec in config["exchanges"].items():
        if spec.get("enabled") and name in CONNECTOR_REGISTRY:
            connectors.append(CONNECTOR_REGISTRY[name](depth_levels=depth))
    for spec in custom_specs:
        if spec.get("enabled", True):
            payload = spec.get("spec", spec)
            try:
                connectors.append(GenericRestConnector(payload, depth_levels=depth))
            except Exception as exc:
                log.error("Bad custom exchange spec %s: %s", spec.get("name"), exc)
    return connectors
