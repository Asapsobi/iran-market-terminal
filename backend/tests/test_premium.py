"""Premium engine: live method prices and historical series joins."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db
from app.models import MarketSnapshot
from app.premium import BUCKET, live_method_price, live_premium, premium_series


def snap(exchange, mid, bid, ask, vol=0.0):
    return MarketSnapshot(exchange=exchange, base="BTC", quote="TMN",
                          best_bid=bid, best_ask=ask, mid=mid,
                          volume_24h_quote=vol, timestamp=time.time(),
                          status="connected")


def test_live_methods():
    snaps = [snap("A", 100, 99, 101, vol=1000),
             snap("B", 104, 103, 105, vol=3000)]
    assert live_method_price(snaps, "composite") == 102          # (100+104)/2
    assert live_method_price(snaps, "best_mid") == 102           # (103+101)/2
    assert live_method_price(snaps, "vwap") == 103               # (100*1k+104*3k)/4k
    assert live_method_price(snaps, "exchange", "B") == 104
    assert live_method_price(snaps, "exchange", "missing") is None


def test_live_premium():
    asset = [snap("A", 11_550_000_000, 11_549e6, 11_551e6)]
    usdt = [MarketSnapshot(exchange="A", base="USDT", quote="TMN",
                           best_bid=109_990, best_ask=110_010, mid=110_000,
                           timestamp=time.time(), status="connected")]
    # implied $105k vs global $100k = +5%
    p = live_premium(asset, usdt, 100_000, "composite")
    assert abs(p - 5.0) < 0.01


def test_premium_series_join():
    now = time.time()
    t0 = now - now % BUCKET - 10 * BUCKET
    comp_rows, ref_rows = [], []
    for i in range(10):
        ts = t0 + i * BUCKET
        comp_rows.append(("BTC", "TMN", 11_550_000_000, 11_549e6, 11_551e6, 0, None, ts))
        comp_rows.append(("USDT", "TMN", 110_000, 109_990, 110_010, 0, None, ts))
        ref_rows.append(("BTC", 100_000, ts))
    db.insert_composites(comp_rows)
    db.insert_reference_prices(ref_rows)
    series = premium_series("BTC", "TMN", "composite", t0 - BUCKET)
    assert len(series) >= 9
    assert all(abs(p["premium_pct"] - 5.0) < 0.05 for p in series)
    assert abs(series[0]["implied_usd"] - 105_000) < 1


def test_premium_sanity_guard_hides_absurd_values():
    from app.premium import sane_premium
    assert sane_premium(3.2, "BTC") == 3.2
    assert sane_premium(-19.9, "BTC") == -19.9
    assert sane_premium(258956.0, "GRAM") is None
    assert sane_premium(None, "X") is None


def test_clean_base_strips_quote():
    from app.aggregator import clean_base
    assert clean_base("USOON/TMN") == "USOON"
    assert clean_base("btc-irt") == "BTC"
    assert clean_base("DOGEIRT") == "DOGE"
    assert clean_base("USDT") == "USDT"
    assert clean_base("../x") == ""
