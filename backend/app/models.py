"""Core data models for the Iran Market Terminal backend."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any


class ImpactLevel(int, Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class MarketStatus(str, Enum):
    CONNECTED = "connected"
    DELAYED = "delayed"
    STALE = "stale"        # responded, but slower than the staleness threshold
    OUTLIER = "outlier"    # price far off the cross-venue median (bad mapping/feed)
    OFFLINE = "offline"


# Only these feed composites, premium, arbitrage and top-of-book share.
USABLE_STATUSES = (MarketStatus.CONNECTED.value, MarketStatus.DELAYED.value)


def is_usable(snap: "MarketSnapshot") -> bool:
    return snap.mid > 0 and snap.status in USABLE_STATUSES


@dataclass
class OrderBook:
    """Full-depth order book. bids/asks: list of [price, quantity], best first."""
    bids: List[List[float]] = field(default_factory=list)
    asks: List[List[float]] = field(default_factory=list)
    timestamp: float = 0.0  # epoch seconds

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    def depth_notional(self, side: str, levels: int = 20) -> float:
        """Total quote-currency notional in the top N levels."""
        book = self.bids if side == "bid" else self.asks
        return sum(p * q for p, q in book[:levels])


@dataclass
class TickerStats:
    """24h rolling stats reported by an exchange."""
    last_price: float = 0.0
    volume_24h_base: float = 0.0   # volume in base asset units
    volume_24h_quote: float = 0.0  # volume in quote currency (TMN)
    change_24h_pct: Optional[float] = None


@dataclass
class MarketSnapshot:
    """One exchange x pair observation produced by the aggregator each cycle."""
    exchange: str
    base: str
    quote: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    bid_depth_quote: float = 0.0   # notional TMN in top-20 bid levels
    ask_depth_quote: float = 0.0
    depth_imbalance: float = 0.0   # (bid-ask)/(bid+ask) in [-1, 1]
    volume_24h_base: float = 0.0
    volume_24h_quote: float = 0.0
    volume_estimated: bool = False   # True = built from observed trades
    last_price: float = 0.0
    latency_ms: float = 0.0
    timestamp: float = 0.0
    status: str = MarketStatus.OFFLINE.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class NewsItem:
    title: str
    source: str
    impact: ImpactLevel
    related_coins: List[str]
    timestamp: float
    url: str
    category: str = "GENERAL"


@dataclass
class EconomicEvent:
    title: str
    country: str            # currency code, e.g. USD / EUR
    impact: str             # Low / Medium / High / Holiday
    forecast: str
    previous: str
    actual: str
    timestamp: float
    surprise_pct: Optional[float] = None  # actual - forecast when both numeric
    revised: str = ""                     # revised previous, when the feed provides it

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArbitrageOpportunity:
    base: str
    quote: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float          # ask on buy side
    sell_price: float         # bid on sell side
    gross_pct: float
    net_pct: float            # after taker fees on both legs
    max_size_base: float      # depth-limited executable size (top levels)
    est_profit_quote: float   # net profit for max_size at these prices
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Anomaly:
    kind: str        # price_deviation | volume_spike | liquidity_drop | stale_feed
    exchange: str
    base: str
    message: str
    severity: str    # info | warning | critical
    value: float
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AppSettings:
    market_interval: float = 20.0       # seconds between polling cycles
    snapshot_interval: float = 120.0    # seconds between DB snapshot writes
    candle_interval: float = 300.0      # seconds between candle refreshes
    news_interval: float = 600.0
    calendar_interval: float = 900.0
    request_timeout: float = 12.0       # per-request budget (connect+read)
    ui_refresh_interval: float = 5.0
    arb_min_edge_pct: float = 0.1       # opportunity-ledger entry threshold (net %)
    stale_latency_ms: float = 2000.0    # slower venue responses are flagged "stale"

    BOUNDS = {
        "market_interval": (1.0, 60.0),
        "snapshot_interval": (60.0, 3600.0),
        "candle_interval": (60.0, 3600.0),
        "news_interval": (60.0, 3600.0),
        "calendar_interval": (120.0, 3600.0),
        "request_timeout": (1.0, 30.0),
        "ui_refresh_interval": (2.0, 120.0),
        "arb_min_edge_pct": (0.01, 5.0),
        "stale_latency_ms": (200.0, 30000.0),
    }
