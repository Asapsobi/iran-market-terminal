"""Intelligence engines: inventory sweep math and window lifecycle."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db
from app.intelligence import _sweep_peak, inventory_requirements


def test_sweep_peak_overlap():
    # two overlapping windows -> requirement is the sum while both open
    ivs = [(0, 100, 5.0), (50, 150, 3.0), (200, 300, 10.0)]
    assert _sweep_peak(ivs) == 10.0          # max(5+3, 10)
    assert _sweep_peak(ivs[:2]) == 8.0       # overlap adds up


def test_inventory_requirements():
    now = time.time()
    windows = [
        {"base": "BTC", "buy_exchange": "Exir", "sell_exchange": "Nobitex",
         "opened_ts": now - 300, "closed_ts": now - 200,
         "max_cost_quote": 1_000_000, "max_size_base": 0.5},
        {"base": "BTC", "buy_exchange": "Exir", "sell_exchange": "Nobitex",
         "opened_ts": now - 250, "closed_ts": now - 100,   # overlaps the first
         "max_cost_quote": 2_000_000, "max_size_base": 0.7},
    ]
    req = inventory_requirements(windows, now=now)
    # both windows open simultaneously between -250 and -200
    assert req["full"]["Exir"]["tmn"] == 3_000_000
    assert abs(req["full"]["Nobitex"]["assets"]["BTC"] - 1.2) < 1e-9
    assert req["windows_counted"] == 2


def test_window_lifecycle_db():
    wid = db.open_arb_window("BTC", "TMN", "Exir", "Ramzinex", time.time() - 60)
    db.update_arb_window(wid, 0.3, 0.5, 15_000_000, 6_000_000_000)
    db.update_arb_window(wid, 0.5, 0.8, 25_000_000, 9_000_000_000)
    db.close_arb_window(wid, time.time())
    rows = [w for w in db.get_arb_windows(time.time() - 3600) if w["id"] == wid]
    assert rows, "window not found"
    w = rows[0]
    assert w["peak_net_pct"] == 0.5
    assert w["samples"] == 2
    assert abs(w["avg_net_pct"] - 0.4) < 1e-9
    assert w["max_size_base"] == 0.8
    assert w["peak_profit_quote"] == 25_000_000
    assert w["closed_ts"] is not None


def test_tob_ties_are_split():
    from app.intelligence import TobTracker
    from app.models import MarketSnapshot
    tr = TobTracker()
    snaps = [MarketSnapshot(exchange=ex, base="BTC", quote="TMN", best_bid=bid,
                            best_ask=ask, mid=(bid + ask) / 2, status="connected",
                            timestamp=time.time())
             for ex, bid, ask in (("A", 100, 101), ("B", 100, 102), ("C", 99, 101))]
    tr._last_cycle_ts = time.time() - 10
    tr._last_flush = time.time()   # keep it in memory (no DB)
    tr.record({"BTC": snaps})
    for side in ("bid", "ask"):
        best = sum(acc[0] for (ex, b, s, h), acc in tr._acc.items() if s == side)
        total = max(acc[1] for (ex, b, s, h), acc in tr._acc.items() if s == side)
        assert abs(best - total) < 1e-6   # shares across venues sum to 100%
