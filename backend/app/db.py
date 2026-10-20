"""PostgreSQL storage layer.

All runtime state lives in Postgres (see alembic migrations). No SQLite and
no settings.json. Connection string: DATABASE_URL or POSTGRES_* env vars.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("terminal.db")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def database_url() -> str:
    """Return a libpq/psycopg connection URI."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Normalize common variants
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    user = os.environ.get("POSTGRES_USER", "terminal")
    password = os.environ.get("POSTGRES_PASSWORD", "terminal")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "terminal")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def database_info() -> Dict[str, Any]:
    """Safe summary for diagnostics (no password)."""
    url = database_url()
    # postgresql://user:pass@host:port/db
    host, db = "?", "?"
    try:
        rest = url.split("://", 1)[1]
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        hostport, _, dbname = rest.partition("/")
        host = hostport.split(":")[0]
        db = dbname.split("?")[0]
    except Exception:
        pass
    return {"host": host, "database": db, "driver": "postgresql"}


_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def _pool_is_open(pool: Optional[ConnectionPool]) -> bool:
    if pool is None:
        return False
    try:
        return not bool(getattr(pool, "closed", False))
    except Exception:
        return False


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool_is_open(_pool):
        return _pool  # type: ignore[return-value]
    with _pool_lock:
        if _pool_is_open(_pool):
            return _pool  # type: ignore[return-value]
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
        url = database_url()
        log.info("Opening Postgres pool → %s", database_info())
        # kwargs vary slightly across psycopg_pool versions — keep core options only
        _pool = ConnectionPool(
            conninfo=url,
            min_size=2,
            max_size=int(os.environ.get("PG_POOL_MAX", "16")),
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            timeout=30.0,
        )
    return _pool


@contextmanager
def _cursor(commit: bool = False):
    """Yield a dict-row cursor; commit on success when commit=True.
    One automatic reconnect attempt if the pool connection is dead."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            pool = _get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    try:
                        yield cur
                        if commit:
                            conn.commit()
                        return
                    except Exception:
                        conn.rollback()
                        raise
        except Exception as exc:
            last_exc = exc
            log.warning("DB cursor failed (attempt %d): %s", attempt + 1, exc)
            # force pool rebuild on next try
            close_pool()
            if attempt == 0:
                time.sleep(0.3)
                continue
            raise
    if last_exc:
        raise last_exc


def wait_for_db(timeout_sec: float = 60.0) -> None:
    """Block until Postgres accepts connections (used by entrypoint + lifespan)."""
    import psycopg
    url = database_url()
    deadline = time.time() + timeout_sec
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"Postgres not ready after {timeout_sec:.0f}s: {last}")


def ensure_schema(retries: int = 5) -> None:
    """Run Alembic migrations to head. Retries if Postgres is still warming up."""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    backend_root = Path(__file__).resolve().parent.parent
    ini = backend_root / "alembic.ini"
    if not ini.exists():
        raise RuntimeError(f"alembic.ini not found at {ini}")
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            wait_for_db(timeout_sec=30.0 if attempt == 1 else 15.0)
            log.info("Running database migrations (attempt %d)…", attempt)
            command.upgrade(cfg, "head")
            log.info("Database schema is up to date")
            # warm the pool
            with _cursor() as cur:
                cur.execute("SELECT 1")
            return
        except Exception as exc:
            last = exc
            log.warning("Migration attempt %d failed: %s", attempt, exc)
            close_pool()
            time.sleep(min(2.0 * attempt, 8.0))
    raise RuntimeError(f"Database migration failed after {retries} attempts: {last}")


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# Back-compat names used by diagnostics / older code
DATA_DIR = None  # no longer a filesystem data dir
DB_PATH = "postgresql"  # label only


# ---------------------------------------------------------------- snapshots

def insert_snapshots(rows: Iterable[tuple]) -> None:
    """rows: (exchange, base, quote, bid, ask, mid, spread_pct,
              bid_depth, ask_depth, vol_base, vol_quote, ts)"""
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO price_snapshots (exchange, base, quote, bid, ask, mid, spread_pct,"
            " bid_depth, ask_depth, volume_24h_base, volume_24h_quote, ts)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )


def insert_composites(rows: Iterable[tuple]) -> None:
    """rows: (base, quote, mid, best_bid, best_ask, total_volume_quote, premium_pct, ts)"""
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO composite_snapshots (base, quote, mid, best_bid, best_ask,"
            " total_volume_quote, premium_pct, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )


def get_composite_history(base: str, quote: str, since_ts: float,
                          limit: int = 20000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT ts, mid, best_bid, best_ask, premium_pct FROM composite_snapshots"
            " WHERE base=%s AND quote=%s AND ts>=%s ORDER BY ts ASC LIMIT %s",
            (base, quote, since_ts, limit),
        )
        return list(cur.fetchall())


def get_exchange_history(exchange: str, base: str, quote: str, since_ts: float,
                         limit: int = 20000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT ts, bid, ask, mid, spread_pct, bid_depth, ask_depth,"
            " volume_24h_quote FROM price_snapshots"
            " WHERE exchange=%s AND base=%s AND quote=%s AND ts>=%s"
            " ORDER BY ts ASC LIMIT %s",
            (exchange, base, quote, since_ts, limit),
        )
        return list(cur.fetchall())


def get_spread_history(base: str, quote: str, since_ts: float,
                       exchange: Optional[str] = None) -> List[Dict[str, Any]]:
    # ~0 = locked/crossed or empty book — not a real spread (see metrics.MIN_REAL_SPREAD_PCT)
    q = ("SELECT exchange, ts, spread_pct FROM price_snapshots"
         " WHERE base=%s AND quote=%s AND ts>=%s AND spread_pct >= 0.0001")
    args: list = [base, quote, since_ts]
    if exchange:
        q += " AND exchange=%s"
        args.append(exchange)
    q += " ORDER BY ts ASC LIMIT 50000"
    with _cursor() as cur:
        cur.execute(q, args)
        return list(cur.fetchall())


def get_mid_at(base: str, quote: str, ts: float, tolerance: float = 900.0) -> Optional[float]:
    with _cursor() as cur:
        cur.execute(
            "SELECT mid FROM composite_snapshots WHERE base=%s AND quote=%s"
            " AND ts BETWEEN %s AND %s ORDER BY ABS(ts-%s) ASC LIMIT 1",
            (base, quote, ts - tolerance, ts + tolerance, ts),
        )
        row = cur.fetchone()
        return row["mid"] if row else None


def get_composite_open_at(base: str, quote: str, start_ts: float,
                          end_ts: float) -> Optional[float]:
    """Open of the first stored composite candle in [start_ts, end_ts)."""
    with _cursor() as cur:
        cur.execute(
            "SELECT open FROM candles WHERE exchange='composite' AND base=%s"
            " AND quote=%s AND ts>=%s AND ts<%s ORDER BY ts ASC, resolution ASC LIMIT 1",
            (base, quote, start_ts, end_ts),
        )
        row = cur.fetchone()
        return row["open"] if row else None


def get_pair_snapshots(base: str, quote: str, since_ts: float,
                       limit: int = 100_000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT exchange, ts, bid, ask, mid, volume_24h_quote FROM price_snapshots"
            " WHERE base=%s AND quote=%s AND ts>=%s ORDER BY ts ASC LIMIT %s",
            (base, quote, since_ts, limit),
        )
        return list(cur.fetchall())


# --------------------------------------------------------- USD reference ---

def insert_reference_prices(rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO reference_prices (asset, usd, ts) VALUES (%s,%s,%s)", rows)


def get_reference_history(asset: str, since_ts: float,
                          limit: int = 50_000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT ts, usd FROM reference_prices WHERE asset=%s AND ts>=%s"
            " ORDER BY ts ASC LIMIT %s", (asset, since_ts, limit))
        return list(cur.fetchall())


def upsert_reference_id(asset: str, cg_id: str) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO reference_ids (asset, cg_id, created_ts) VALUES (%s,%s,%s)"
            " ON CONFLICT (asset) DO UPDATE SET cg_id=EXCLUDED.cg_id",
            (asset.upper(), cg_id, time.time()))


def get_reference_ids() -> Dict[str, str]:
    with _cursor() as cur:
        cur.execute("SELECT asset, cg_id FROM reference_ids")
        return {r["asset"]: r["cg_id"] for r in cur.fetchall()}


# ------------------------------------------------------------------ candles

def upsert_candles(rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO candles (exchange, base, quote, resolution, ts,"
            " open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (exchange, base, quote, resolution, ts) DO UPDATE SET"
            " open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,"
            " close=EXCLUDED.close, volume=EXCLUDED.volume",
            rows,
        )


def get_candles(exchange: str, base: str, quote: str, resolution: int,
                since_ts: float, limit: int = 5000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM candles"
            " WHERE exchange=%s AND base=%s AND quote=%s AND resolution=%s AND ts>=%s"
            " ORDER BY ts ASC LIMIT %s",
            (exchange, base, quote, resolution, since_ts, limit),
        )
        return list(cur.fetchall())


# ----------------------------------------------------------------- calendar

def upsert_calendar_events(rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO calendar_events (title, country, impact, forecast, previous,"
            " actual, surprise_pct, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (title, country, ts) DO UPDATE SET"
            " actual=EXCLUDED.actual, forecast=EXCLUDED.forecast,"
            " previous=EXCLUDED.previous, surprise_pct=EXCLUDED.surprise_pct,"
            " impact=EXCLUDED.impact",
            rows,
        )


def get_calendar_events(since_ts: float, until_ts: float) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM calendar_events WHERE ts BETWEEN %s AND %s ORDER BY ts ASC",
            (since_ts, until_ts),
        )
        return list(cur.fetchall())


def get_event_surprise_history(title: str, country: str, limit: int = 24) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT ts, forecast, previous, actual, surprise_pct FROM calendar_events"
            " WHERE title=%s AND country=%s AND actual IS NOT NULL AND actual != ''"
            " ORDER BY ts DESC LIMIT %s",
            (title, country, limit),
        )
        return list(cur.fetchall())


# ------------------------------------------------------------------- alerts

def insert_alert_rule(name: str, rule_type: str, base: Optional[str],
                      exchange: Optional[str], threshold: float,
                      window_sec: float, cooldown_sec: float) -> int:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO alert_rules (name, rule_type, base, exchange, threshold,"
            " window_sec, cooldown_sec, enabled, created_ts)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
            (name, rule_type, base, exchange, threshold, window_sec, cooldown_sec,
             time.time()),
        )
        return int(cur.fetchone()["id"])


def get_alert_rules(enabled_only: bool = False) -> List[Dict[str, Any]]:
    q = "SELECT * FROM alert_rules"
    if enabled_only:
        q += " WHERE enabled=1"
    with _cursor() as cur:
        cur.execute(q)
        return list(cur.fetchall())


def set_alert_rule_enabled(rule_id: int, enabled: bool) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("UPDATE alert_rules SET enabled=%s WHERE id=%s",
                    (1 if enabled else 0, rule_id))


def delete_alert_rule(rule_id: int) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM alert_rules WHERE id=%s", (rule_id,))


def insert_alert_event(rule_id: Optional[int], rule_type: str, message: str,
                       severity: str, ts: float) -> int:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO alert_events (rule_id, rule_type, message, severity, ts)"
            " VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (rule_id, rule_type, message, severity, ts),
        )
        return int(cur.fetchone()["id"])


def get_alert_events(since_ts: float, limit: int = 200) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM alert_events WHERE ts>=%s ORDER BY ts DESC LIMIT %s",
            (since_ts, limit),
        )
        return list(cur.fetchall())


def ack_alert_event(event_id: int) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("UPDATE alert_events SET acknowledged=1 WHERE id=%s", (event_id,))


# ----------------------------------------------------- runtime config (admin)

def upsert_custom_exchange(name: str, spec: Dict[str, Any], enabled: bool = True) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO custom_exchanges (name, spec, enabled, created_ts)"
            " VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec,"
            " enabled=EXCLUDED.enabled",
            (name, json.dumps(spec), 1 if enabled else 0, time.time()),
        )


def get_custom_exchanges() -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM custom_exchanges")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["spec"] = json.loads(d["spec"]) if isinstance(d["spec"], str) else d["spec"]
            out.append(d)
        return out


def delete_custom_exchange(name: str) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM custom_exchanges WHERE name=%s", (name,))


def upsert_custom_pair(base: str, quote: str, enabled: bool = True) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO custom_pairs (base, quote, enabled, created_ts)"
            " VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (base, quote) DO UPDATE SET enabled=EXCLUDED.enabled",
            (base.upper(), quote.upper(), 1 if enabled else 0, time.time()),
        )


def get_custom_pairs() -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM custom_pairs")
        return list(cur.fetchall())


def delete_custom_pair(base: str, quote: str) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM custom_pairs WHERE base=%s AND quote=%s",
                    (base.upper(), quote.upper()))


# ------------------------------------------------------------- app settings

def get_app_settings() -> Dict[str, float]:
    with _cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings")
        return {r["key"]: float(r["value"]) for r in cur.fetchall()}


def set_app_settings(values: Dict[str, float]) -> None:
    if not values:
        return
    now = time.time()
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO app_settings (key, value, updated_ts) VALUES (%s,%s,%s)"
            " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,"
            " updated_ts=EXCLUDED.updated_ts",
            [(k, float(v), now) for k, v in values.items()],
        )


# --------------------------------------------------------------------- auth

def create_user(username: str, password_hash: str, role: str,
                must_change: bool = False) -> int:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, role,"
            " must_change_password, created_ts) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (username.strip(), password_hash, role, 1 if must_change else 0, time.time()))
        return int(cur.fetchone()["id"])


def get_user_by_name(username: str) -> Optional[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(%s)",
            (username.strip(),))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT id, username, role, must_change_password, created_ts"
            " FROM users ORDER BY id")
        return list(cur.fetchall())


def count_users() -> int:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM users")
        return int(cur.fetchone()["c"])


def set_user_password(user_id: int, password_hash: str,
                      must_change: bool = False) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE users SET password_hash=%s, must_change_password=%s WHERE id=%s",
            (password_hash, 1 if must_change else 0, user_id))


def delete_user(user_id: int) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))


def create_session(token: str, user_id: int, ttl_sec: float) -> None:
    now = time.time()
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO auth_sessions (token, user_id, created_ts, expires_ts)"
            " VALUES (%s,%s,%s,%s)", (token, user_id, now, now + ttl_sec))


def get_session(token: str) -> Optional[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT s.token, s.user_id, s.expires_ts, u.username, u.role,"
            " u.must_change_password FROM auth_sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token=%s", (token,))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_session(token: str) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM auth_sessions WHERE token=%s", (token,))


def delete_user_sessions(user_id: int) -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))


def prune_sessions() -> None:
    with _cursor(commit=True) as cur:
        cur.execute("DELETE FROM auth_sessions WHERE expires_ts < %s", (time.time(),))


# ----------------------------------------------------- trade volume buckets

def bump_trade_volumes(rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO trade_volumes (exchange, base, hour_ts, base_vol, quote_vol)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (exchange, base, hour_ts) DO UPDATE SET"
            " base_vol = trade_volumes.base_vol + EXCLUDED.base_vol,"
            " quote_vol = trade_volumes.quote_vol + EXCLUDED.quote_vol",
            rows,
        )


def get_trade_volumes(since_ts: float) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT exchange, base, hour_ts, base_vol, quote_vol FROM trade_volumes"
            " WHERE hour_ts>=%s", (since_ts,))
        return list(cur.fetchall())


# ------------------------------------------------------------ intelligence

def bump_tob_hours(rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO tob_share (exchange, base, side, hour_ts, seconds_best, seconds_total)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (exchange, base, side, hour_ts) DO UPDATE SET"
            " seconds_best = tob_share.seconds_best + EXCLUDED.seconds_best,"
            " seconds_total = tob_share.seconds_total + EXCLUDED.seconds_total",
            rows,
        )


def get_tob_share(base: str, since_ts: float) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT exchange, side, hour_ts, seconds_best, seconds_total FROM tob_share"
            " WHERE base=%s AND hour_ts>=%s ORDER BY hour_ts ASC", (base, since_ts))
        return list(cur.fetchall())


def open_arb_window(base: str, quote: str, buy_ex: str, sell_ex: str,
                    ts: float) -> int:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO arb_windows (base, quote, buy_exchange, sell_exchange,"
            " opened_ts) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (base, quote, buy_ex, sell_ex, ts))
        return int(cur.fetchone()["id"])


def update_arb_window(window_id: int, net_pct: float, size_base: float,
                      profit_quote: float, cost_quote: float) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE arb_windows SET"
            " peak_net_pct = GREATEST(peak_net_pct, %s),"
            " avg_net_pct = (avg_net_pct * samples + %s) / (samples + 1),"
            " samples = samples + 1,"
            " max_size_base = GREATEST(max_size_base, %s),"
            " peak_profit_quote = GREATEST(peak_profit_quote, %s),"
            " max_cost_quote = GREATEST(max_cost_quote, %s)"
            " WHERE id=%s",
            (net_pct, net_pct, size_base, profit_quote, cost_quote, window_id))


def close_arb_window(window_id: int, ts: float) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE arb_windows SET closed_ts=%s WHERE id=%s AND closed_ts IS NULL",
            (ts, window_id))


def get_arb_windows(since_ts: float, limit: int = 2000) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM arb_windows WHERE opened_ts>=%s OR closed_ts IS NULL"
            " ORDER BY (closed_ts IS NULL) DESC, opened_ts DESC LIMIT %s",
            (since_ts, limit))
        return list(cur.fetchall())


# ---------------------------------------------------------------- retention

def prune(retention: Dict[str, int]) -> Dict[str, int]:
    now = time.time()
    plans = [
        ("price_snapshots", "ts", retention.get("snapshots_days", 90)),
        ("composite_snapshots", "ts", retention.get("snapshots_days", 90)),
        ("reference_prices", "ts", retention.get("snapshots_days", 90)),
        ("candles", "ts", retention.get("candles_days", 365)),
        ("alert_events", "ts", retention.get("alerts_days", 30)),
        ("calendar_events", "ts", retention.get("calendar_days", 365)),
        ("tob_share", "hour_ts", retention.get("ledger_days", 180)),
        ("arb_windows", "opened_ts", retention.get("ledger_days", 180)),
        ("trade_volumes", "hour_ts", 7),
    ]
    deleted: Dict[str, int] = {}
    with _cursor(commit=True) as cur:
        for table, col, days in plans:
            cur.execute(f"DELETE FROM {table} WHERE {col} < %s",
                        (now - days * 86400,))
            deleted[table] = cur.rowcount
    return deleted


def table_counts() -> Dict[str, int]:
    tables = (
        "price_snapshots", "composite_snapshots", "candles",
        "calendar_events", "arb_windows", "users", "app_settings",
    )
    out: Dict[str, int] = {}
    with _cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
            out[t] = int(cur.fetchone()["c"])
    return out
