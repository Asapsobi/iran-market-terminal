"""Iran Market Terminal — FastAPI backend.

Run:  python3 main.py            (serves API + built frontend on :4000)
Demo: DEMO_MODE=1 python3 main.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.auth import auth_service
from app.aggregator import clean_base, market_aggregator
from app.alerts import alert_engine
from app.candles import candle_service, HISTORY_WINDOWS, TF_SPEC
from app.config import CONFIG, exchange_color, taker_fee_pct
from app.connectors import close_client
from app.demo_seed import seed_if_needed
from app.diagnostics import (connectivity_test, environment_report,
                             install_log_capture, ring_handler)
from app.downtime_alert import downtime_alerter
from app.intelligence import arb_ledger, inventory_requirements, tob_tracker
from app.metrics import MIN_REAL_SPREAD_PCT, metrics_engine
from app.news import news_service
from app.premium import METHODS, premium_series
from app.settings import settings_store

# Migrations run at import so tests / CLI that touch db work after ensure_schema()
# is called from lifespan (and from the docker entrypoint).
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_capture()   # every log line also goes to the in-app ring buffer
log = logging.getLogger("terminal")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


# ------------------------------------------------------------- WebSocket ---

class WSManager:
    """Thread-safe broadcaster: alerts fire from worker threads, so sends are
    marshalled onto the main event loop via call_soon_threadsafe."""

    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.clients:
            self.clients.remove(ws)

    def broadcast(self, payload: Dict[str, Any]) -> None:
        loop = self.loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        for ws in list(self.clients):
            def _send(ws=ws):
                async def _deliver():
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        self.disconnect(ws)   # drop dead clients so the list can't grow
                asyncio.ensure_future(_deliver())
            try:
                loop.call_soon_threadsafe(_send)
            except RuntimeError:
                pass


ws_manager = WSManager()
market_aggregator.add_listener(ws_manager.broadcast)
alert_engine.add_listener(ws_manager.broadcast)


# ------------------------------------------------------- background loops ---
#
# Every background loop runs through _run_loop, which fixes the class of bug
# where a single stuck network/DB call silently freezes collection forever
# ("UI is up but no new data, nothing in the logs"). It guarantees:
#   1. No cycle can hang — each is bounded by asyncio.wait_for, so a stuck call
#      aborts the cycle (logged loudly) instead of freezing the whole loop.
#   2. Health is observable — last-success time per loop, surfaced on /api/meta
#      and in a periodic heartbeat log line (+ a watchdog warning on stalls).
#   3. A loop that somehow exits is automatically restarted (see _spawn_loop).

# loop name -> health counters (seconds / counts)
LOOP_HEALTH: Dict[str, Dict[str, float]] = {}
_loop_tasks: Dict[str, asyncio.Task] = {}
_shutting_down = False
_STARTED_AT = time.time()

APP_VERSION = "3.2.0"


def _resolve_build_info() -> Dict[str, str]:
    """Version + build stamp, so you can confirm which build is actually running
    (shown on the Admin page). git_sha/build_time are baked into the Docker image
    at build time; outside Docker they fall back to 'dev'/'unknown'."""
    here = Path(__file__).resolve().parent
    sha = os.environ.get("APP_GIT_SHA", "").strip()
    build_time = os.environ.get("APP_BUILD_TIME", "").strip()
    if not build_time:
        try:
            build_time = (here / ".build_time").read_text(encoding="utf-8").strip()
        except OSError:
            build_time = ""
    return {"version": APP_VERSION, "git_sha": sha or "dev",
            "build_time": build_time or "unknown"}


BUILD_INFO = _resolve_build_info()


async def _run_loop(name: str, work, interval_getter, work_timeout: float) -> None:
    health = LOOP_HEALTH.setdefault(
        name, {"last_success": 0.0, "last_attempt": 0.0, "timeouts": 0.0, "fails": 0.0})
    cycle = 0
    while True:
        health["last_attempt"] = time.time()
        try:
            await asyncio.wait_for(work(cycle), timeout=work_timeout)
            health["last_success"] = time.time()
            health["fails"] = 0.0
            cycle += 1
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            health["timeouts"] += 1
            health["fails"] += 1
            log.error("%s loop: cycle exceeded %.0fs and was aborted — a network "
                      "or DB call is stuck; recovering and continuing "
                      "(timeouts=%d)", name, work_timeout, int(health["timeouts"]))
        except Exception as exc:
            health["fails"] += 1
            log.exception("%s loop error: %s", name, exc)
        try:
            await asyncio.sleep(max(0.5, interval_getter()))
        except asyncio.CancelledError:
            raise


def _spawn_loop(name: str, work, interval_getter, work_timeout: float) -> None:
    """Start a supervised loop; if it ever exits unexpectedly, restart it."""
    task = asyncio.create_task(
        _run_loop(name, work, interval_getter, work_timeout), name=f"loop:{name}")
    _loop_tasks[name] = task

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled() or _shutting_down:
            return
        exc = t.exception()
        log.error("loop %s exited unexpectedly (%s) — restarting in 2s", name, exc)
        try:
            asyncio.get_running_loop().call_later(
                2.0, lambda: _spawn_loop(name, work, interval_getter, work_timeout))
        except RuntimeError:
            pass

    task.add_done_callback(_on_done)


# ---- per-loop work bodies (one cycle each) --------------------------------

async def _market_work(cycle: int) -> None:
    await market_aggregator.update_markets()
    # competitive intelligence: TOB scoreboard + opportunity ledger
    tob_tracker.record(market_aggregator.snapshots_by_asset())
    await asyncio.wait_for(
        asyncio.to_thread(arb_ledger.update, market_aggregator), timeout=20)
    if cycle % 5 == 0:  # evaluate alerts every ~5 cycles
        calendar = news_service.get_calendar()
        await asyncio.wait_for(
            asyncio.to_thread(alert_engine.evaluate, market_aggregator, calendar),
            timeout=20)


async def _candle_work(_cycle: int) -> None:
    await candle_service.refresh(market_aggregator)


async def _news_work(_cycle: int) -> None:
    await news_service.refresh_news()


async def _calendar_work(_cycle: int) -> None:
    await news_service.refresh_calendar()


async def _prune_work(_cycle: int) -> None:
    deleted = await asyncio.wait_for(
        asyncio.to_thread(db.prune, CONFIG.get("retention", {})), timeout=45)
    if any(deleted.values()):
        log.info("Pruned: %s", deleted)


async def _heartbeat_loop() -> None:
    """Periodic health line + stall warning, so a frozen loop is never silent."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            parts = []
            for nm, h in LOOP_HEALTH.items():
                age = now - h["last_success"] if h["last_success"] else None
                parts.append(f"{nm}={('%.0fs' % age) if age is not None else 'never'}")
            log.info("heartbeat — last update: %s", ", ".join(parts))
            mh = LOOP_HEALTH.get("market", {})
            if mh.get("last_success") and now - mh["last_success"] > 120:
                log.error("WATCHDOG: market data has not updated in %.0fs — the "
                          "poll loop is stalling; check exchange connectivity "
                          "from this host", now - mh["last_success"])
            # Telegram alert when data goes stale (UI up but feeds dead) / recovers
            await downtime_alerter.check(market_aggregator)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


# ------------------------------------------------------------- collector ---
# Whether THIS process runs the continuous background collector. Long-lived
# servers (Docker, VPS, `python main.py`) MUST run it; only true serverless
# platforms (Vercel), where each request is a short-lived function, skip it.
# An explicit RUN_COLLECTOR always wins, so a container can never *silently*
# end up with polling disabled (the "UI up but no new data" failure).
def _collector_enabled() -> bool:
    override = os.environ.get("RUN_COLLECTOR")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return not bool(os.environ.get("VERCEL") or os.environ.get("SERVERLESS"))


RUN_COLLECTOR = _collector_enabled()

# Hard self-restart watchdog. If the market collector stops producing fresh data
# for this long, the process is wedged (stuck thread / exhausted sockets or
# memory) in a way in-process recovery cannot fix. The watchdog runs on its own
# OS thread — immune to event-loop hangs — and exits the process so the
# container's restart policy brings up a clean one. This is what makes "the UI
# is up but the data is frozen and I have to restart Docker by hand" impossible.
WATCHDOG_STALE_EXIT = float(os.environ.get("WATCHDOG_STALE_EXIT_SEC", "300"))


def _watchdog_thread() -> None:
    while not _shutting_down:
        time.sleep(15)
        last = LOOP_HEALTH.get("market", {}).get("last_success", 0.0)
        if not last:
            continue                       # never succeeded yet: startup grace
        age = time.time() - last
        if age > WATCHDOG_STALE_EXIT:
            log.critical("WATCHDOG: market data stale for %.0fs (> %.0fs) — the "
                         "process is wedged; exiting for a clean container restart",
                         age, WATCHDOG_STALE_EXIT)
            os._exit(1)


def _start_watchdog() -> None:
    if os.environ.get("WATCHDOG_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        log.warning("watchdog disabled via WATCHDOG_DISABLED")
        return
    threading.Thread(target=_watchdog_thread, name="watchdog", daemon=True).start()
    log.info("watchdog active — will restart if market data goes stale > %.0fs",
             WATCHDOG_STALE_EXIT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shutting_down
    _shutting_down = False
    ws_manager.loop = asyncio.get_running_loop()
    # Schema must exist before auth bootstrap / settings / collector writes.
    try:
        db.ensure_schema()
    except Exception as exc:
        log.error("Database migration failed: %s", exc)
        raise
    if AUTH_ENABLED and auth_service.default_creds:
        log.warning("Using default credentials admin/admin — set AUTH_USERNAME/"
                    "AUTH_PASSWORD_HASH/AUTH_TOKEN_SECRET env variables")
    auth_service.bootstrap()   # seed the env admin as the first DB user if none exist
    seed_if_needed()
    if not RUN_COLLECTOR:
        log.warning("COLLECTOR DISABLED (serverless mode) — no background polling "
                    "in this process. Set RUN_COLLECTOR=1 to force it on.")
        asyncio.create_task(_safe_first_poll())   # best-effort, non-blocking
    else:
        log.info("COLLECTOR ENABLED — starting supervised background loops")
        try:  # prime state once so the first requests aren't empty (bounded)
            await asyncio.wait_for(market_aggregator.update_markets(), timeout=120)
        except Exception as exc:
            log.warning("initial market poll failed (will retry in loop): %s", exc)
        # Market cycle can take ~30-60s under semaphore + slow venues; give it room
        # so wait_for does not abort a healthy but slow poll (which leaks sockets).
        _spawn_loop("market", _market_work,
                    lambda: settings_store.get().market_interval, 120)
        _spawn_loop("candle", _candle_work,
                    lambda: settings_store.get().candle_interval, 90)
        _spawn_loop("news", _news_work,
                    lambda: settings_store.get().news_interval, 60)
        _spawn_loop("calendar", _calendar_work,
                    lambda: (60 if not news_service.calendar_cache
                             else settings_store.get().calendar_interval), 60)
        _spawn_loop("prune", _prune_work, lambda: 3600.0, 60)
        _loop_tasks["heartbeat"] = asyncio.create_task(
            _heartbeat_loop(), name="loop:heartbeat")
        _start_watchdog()
    yield
    _shutting_down = True
    for t in _loop_tasks.values():
        t.cancel()
    await close_client()
    db.close_pool()


async def _safe_first_poll() -> None:
    try:
        await market_aggregator.update_markets()
        await news_service.refresh_calendar()
    except Exception as exc:
        log.warning("serverless first poll failed: %s", exc)


app = FastAPI(title="Iran Market Terminal", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)

AUTH_ENABLED = bool(CONFIG.get("auth_enabled", True))
AUTH_EXEMPT = ("/api/auth/login", "/api/health")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Every /api route (except login) requires a valid bearer token."""
    path = request.url.path
    if AUTH_ENABLED and path.startswith("/api") and path not in AUTH_EXEMPT:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        session = auth_service.validate(token)
        if not session:
            return JSONResponse(status_code=401, content={"detail": "not authenticated"})
        request.state.user = session
    else:
        request.state.user = None
    return await call_next(request)


# ----------------------------------------------------------------- guards ---

def require_admin(request: Request) -> None:
    """Mutating/config endpoints require the admin role (when auth is on)."""
    if not AUTH_ENABLED:
        return
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


# ----------------------------------------------------------------- models ---

class SettingsUpdate(BaseModel):
    market_interval: Optional[float] = None
    snapshot_interval: Optional[float] = None
    candle_interval: Optional[float] = None
    news_interval: Optional[float] = None
    calendar_interval: Optional[float] = None
    request_timeout: Optional[float] = None
    ui_refresh_interval: Optional[float] = None
    arb_min_edge_pct: Optional[float] = None


class AlertRuleIn(BaseModel):
    name: str
    rule_type: str
    base: Optional[str] = None
    exchange: Optional[str] = None
    threshold: float
    window_sec: float = 3600
    cooldown_sec: float = 900


class ExchangeSpecIn(BaseModel):
    name: str
    spec: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class PairIn(BaseModel):
    base: str
    quote: Optional[str] = None
    enabled: bool = True
    coingecko_id: Optional[str] = None   # manual global-reference override


# ------------------------------------------------------------------- auth ---

class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class CreateUserIn(BaseModel):
    username: str
    password: str
    role: str = "operator"


def _bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


@app.post("/api/auth/login")
def auth_login(body: LoginIn) -> Dict[str, Any]:
    if not AUTH_ENABLED:
        return {"token": "", "user": {"username": "anonymous", "role": "admin"}}
    result = auth_service.login(body.username, body.password)
    if result is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    if "error" in result:
        raise HTTPException(status_code=429,
                            detail=f"too many attempts — retry in {result['retry_in']}s")
    return result


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> Dict[str, Any]:
    auth_service.logout(_bearer(request))
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        return {"username": "anonymous", "role": "admin", "default_creds": False}
    return {"username": user["username"], "role": user.get("role", "operator"),
            "must_change_password": bool(user.get("must_change_password")),
            "default_creds": auth_service.default_creds,
            "env_managed": auth_service.env_managed}


@app.post("/api/auth/change-password")
def change_password(request: Request, body: ChangePasswordIn) -> Dict[str, Any]:
    """Any signed-in user can change their own password."""
    user = getattr(request.state, "user", None)
    if not user or not user.get("user_id"):
        raise HTTPException(status_code=401, detail="not authenticated")
    result = auth_service.change_password(user["user_id"], body.current_password,
                                          body.new_password)
    if result is None:
        raise HTTPException(status_code=422,
                            detail="new password must be at least 6 characters")
    if not result:
        raise HTTPException(status_code=403, detail="current password is incorrect")
    return {"ok": True}


# ------------------------------------------------------------ user management
# admin role only (require_admin). operators get 403.

@app.get("/api/users", dependencies=[Depends(require_admin)])
def list_users() -> List[Dict[str, Any]]:
    return auth_service.list_users()


@app.post("/api/users", dependencies=[Depends(require_admin)])
def create_user(body: CreateUserIn) -> Dict[str, Any]:
    result = auth_service.create_user(body.username, body.password, body.role)
    err = (result or {}).get("error")
    if err == "username":
        raise HTTPException(status_code=422, detail="username is required")
    if err == "weak":
        raise HTTPException(status_code=422, detail="password must be at least 6 characters")
    if err == "exists":
        raise HTTPException(status_code=409, detail="a user with that name already exists")
    return result


@app.delete("/api/users/{user_id}", dependencies=[Depends(require_admin)])
def remove_user(user_id: int, request: Request) -> Dict[str, Any]:
    acting = getattr(request.state, "user", None) or {}
    result = auth_service.delete_user(user_id, acting.get("user_id"))
    err = result.get("error")
    if err == "self":
        raise HTTPException(status_code=422, detail="you cannot delete your own account")
    if err == "last_admin":
        raise HTTPException(status_code=422, detail="cannot delete the last admin user")
    if err == "notfound":
        raise HTTPException(status_code=404, detail="user not found")
    return result


# ----------------------------------------------------------------- health ---

@app.get("/api/health")
def get_health() -> JSONResponse:
    """Unauthenticated liveness/readiness for the container health check.

    Healthy while the collector is producing fresh market data (or when the
    collector is intentionally disabled). Returns 503 when data is stale, so the
    orchestrator / Docker HEALTHCHECK notices and the operator can see it."""
    now = time.time()
    last = LOOP_HEALTH.get("market", {}).get("last_success", 0.0)
    age = (now - last) if last else None
    stale_after = float(os.environ.get("HEALTH_STALE_SEC", "120"))
    if not RUN_COLLECTOR:
        healthy = True                              # collector off by design
    elif age is None:
        healthy = (now - _STARTED_AT) < 120         # still warming up
    else:
        healthy = age <= stale_after
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "stale",
            "collector_enabled": RUN_COLLECTOR,
            "market_last_update_age_sec": round(age, 1) if age is not None else None,
            "loops": {k: (round(now - v["last_success"], 1)
                          if v.get("last_success") else None)
                      for k, v in LOOP_HEALTH.items()},
            "uptime_sec": round(now - _STARTED_AT, 1),
            "server_time": now,
        },
    )


# ------------------------------------------------------------ diagnostics ---

@app.get("/api/logs")
def get_logs(level: str = Query("INFO"),
             limit: int = Query(300, ge=1, le=2000),
             search: str = Query("")) -> List[Dict[str, Any]]:
    """Recent backend log records (in-memory ring buffer)."""
    return ring_handler.tail(level, limit, search)


@app.get("/api/diagnostics")
def get_diagnostics() -> Dict[str, Any]:
    """Instant health report: env, auth vars, disk, DB, feeds, connectors."""
    return environment_report(market_aggregator, news_service)


@app.post("/api/diagnostics/nettest")
async def run_nettest() -> List[Dict[str, Any]]:
    """Probe every upstream API from this server — reveals geo-blocks."""
    return await connectivity_test()


# ------------------------------------------------------------------- meta ---

@app.get("/api/meta")
def get_meta() -> Dict[str, Any]:
    return {
        "app": "Iran Market Terminal", "version": APP_VERSION,
        "build": BUILD_INFO,
        "collector_enabled": RUN_COLLECTOR,
        "demo_mode": bool(CONFIG.get("demo_mode")),
        "quote_currency": CONFIG.get("quote_currency", "TMN"),
        "exchanges": [
            {"name": name, "enabled": bool(spec.get("enabled")),
             "taker_fee_pct": spec.get("taker_fee_pct", 0.25),
             "color": spec.get("color", "#8A93A6")}
            for name, spec in CONFIG["exchanges"].items()
        ] + [
            {"name": row["name"], "enabled": bool(row["enabled"]),
             "taker_fee_pct": row["spec"].get("taker_fee_pct", 0.25),
             "color": "#8A93A6", "custom": True}
            for row in db.get_custom_exchanges()
        ],
        "assets": _selectable_assets(),
        "usd_reference": market_aggregator.usd_reference,
        "usd_reference_ts": market_aggregator.usd_reference_ts,
        "news_refreshed_at": news_service.news_refreshed_at,
        "calendar_refreshed_at": news_service.calendar_refreshed_at,
        "server_time": time.time(),
        "loops": {
            nm: {
                "last_update_age": (round(time.time() - h["last_success"], 1)
                                    if h["last_success"] else None),
                "timeouts": int(h["timeouts"]),
            } for nm, h in LOOP_HEALTH.items()
        },
    }


def _selectable_assets() -> List[str]:
    """Assets for the selector bars: hide ones no venue has ever quoted
    (misconfigured / unsupported symbols that would only show '0/N')."""
    by_asset = market_aggregator.snapshots_by_asset()
    return [b for b, _q in market_aggregator.pairs()
            if any(s.mid > 0 for s in by_asset.get(b, []))] \
        or [b for b, _q in market_aggregator.pairs()]   # before the first poll


def _usdt_peg_dev_pct() -> Optional[float]:
    """USDT has no Iran premium (it is the benchmark); show its global
    deviation from $1 instead."""
    usd = market_aggregator.usd_reference.get("USDT")
    return round((usd - 1.0) * 100, 3) if usd else None


# --------------------------------------------------------------- overview ---

@app.get("/api/overview")
def get_overview() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    by_asset = market_aggregator.snapshots_by_asset()
    for base, quote in market_aggregator.pairs():
        snaps = by_asset.get(base, [])
        live = market_aggregator.usable_snaps(base)
        price = market_aggregator.composite_mid(base)
        best_bid = max(live, key=lambda s: s.best_bid, default=None)
        best_ask = min((s for s in live if s.best_ask > 0),
                       key=lambda s: s.best_ask, default=None)
        scores = metrics_engine.liquidity_scores(snaps)
        out.append({
            "base": base, "quote": quote,
            "price": price,
            "change_1h": metrics_engine.change_pct(base, quote, 3600, price),
            "change_24h": metrics_engine.change_pct(base, quote, 86400, price),
            "change_7d": metrics_engine.change_pct(base, quote, 7 * 86400, price),
            "volume_24h_quote": sum(s.volume_24h_quote for s in live),
            "liquidity_score": round(statistics.fmean(scores.values()), 1) if scores else None,
            "min_spread_pct": min((s.spread_pct for s in live
                                   if s.spread_pct >= MIN_REAL_SPREAD_PCT), default=None),
            "best_bid": ({"exchange": best_bid.exchange, "price": best_bid.best_bid}
                         if best_bid else None),
            "best_ask": ({"exchange": best_ask.exchange, "price": best_ask.best_ask}
                         if best_ask else None),
            "premium_pct": market_aggregator.premium_pct(base),
            "peg_dev_pct": _usdt_peg_dev_pct() if base == "USDT" else None,
            "exchanges_live": len(live), "exchanges_total": len(snaps),
            "sparkline": metrics_engine.sparkline(base),
        })
    # rank by 24h volume
    out.sort(key=lambda r: r.get("volume_24h_quote") or 0, reverse=True)
    for i, row in enumerate(out):
        row["rank"] = i + 1
    return out


@app.get("/api/markets")
def get_markets() -> List[Dict[str, Any]]:
    return [s.to_dict() for s in market_aggregator.market_state.values()]


@app.get("/api/pair/{base}")
def get_pair_detail(base: str) -> Dict[str, Any]:
    base = base.upper()
    quote = CONFIG.get("quote_currency", "TMN")
    snaps = [s for (ex, b), s in market_aggregator.market_state.items() if b == base]
    if not snaps:
        raise HTTPException(status_code=404, detail=f"unknown pair {base}")
    scores = metrics_engine.liquidity_scores(snaps)
    rows = []
    for s in sorted(snaps, key=lambda x: x.exchange):
        d = s.to_dict()
        d["liquidity_score"] = scores.get(s.exchange)
        d["taker_fee_pct"] = taker_fee_pct(s.exchange)
        d["color"] = exchange_color(s.exchange)
        d["spread_stats_1h"] = metrics_engine.spread_stats(s.exchange, base, 3600)
        rows.append(d)
    ops = metrics_engine.arbitrage(base, quote, market_aggregator.usable_books(base))
    price = market_aggregator.composite_mid(base)
    return {
        "base": base, "quote": quote,
        "price": price,
        "premium_pct": market_aggregator.premium_pct(base),
        "peg_dev_pct": _usdt_peg_dev_pct() if base == "USDT" else None,
        "usd_reference": market_aggregator.usd_reference.get(base),
        "change_1h": metrics_engine.change_pct(base, quote, 3600, price),
        "change_24h": metrics_engine.change_pct(base, quote, 86400, price),
        "change_7d": metrics_engine.change_pct(base, quote, 7 * 86400, price),
        "stale_latency_ms": settings_store.get().stale_latency_ms,
        "exchanges": rows,
        # every venue pair, best net first (net < 0 included — the UI shows the gap)
        "arbitrage": [o.to_dict() for o in ops[:30]],
    }


@app.get("/api/depth/{exchange}/{base}")
def get_depth(exchange: str, base: str) -> Dict[str, Any]:
    book = market_aggregator.books.get((exchange, base.upper()))
    if not book:
        raise HTTPException(status_code=404, detail="no order book")
    return {"exchange": exchange, "base": base.upper(),
            "bids": book.bids, "asks": book.asks, "timestamp": book.timestamp}


# ---------------------------------------------------------------- history ---

@app.get("/api/history/{base}")
def get_history(base: str, range: str = Query("1d"),
                exchange: Optional[str] = None) -> List[Dict[str, Any]]:
    base = base.upper()
    quote = CONFIG.get("quote_currency", "TMN")
    since = time.time() - HISTORY_WINDOWS.get(range, 86400)
    if exchange and exchange != "composite":
        return db.get_exchange_history(exchange, base, quote, since)
    return db.get_composite_history(base, quote, since)


@app.get("/api/candles/{base}")
def get_candles(base: str, tf: Optional[str] = Query(None),
                range: Optional[str] = Query(None),      # legacy alias
                exchange: str = Query("composite")) -> List[Dict[str, Any]]:
    return candle_service.get(exchange, base.upper(), tf or range or "15min")


# ----------------------------------------------------------- intelligence ---

@app.get("/api/tob-share")
def get_tob_share(base: str = Query("BTC"),
                  range: str = Query("1d")) -> Dict[str, Any]:
    """Top-of-book time share: % of time each venue held the best bid/ask."""
    base = base.upper()
    tob_tracker.flush()   # include up-to-the-minute accumulation
    since = time.time() - HISTORY_WINDOWS.get(range, 86400)
    rows = db.get_tob_share(base, since)
    # Share = a venue's best-quote seconds / all venues' best-quote seconds, per
    # side. New data splits ties 1/n so this equals time share; normalizing at
    # read time also brings hours recorded before that fix back to 100%.
    best: Dict[str, Dict[str, float]] = {}
    side_total = {"bid": 0.0, "ask": 0.0}
    hour_best: Dict[float, Dict[str, Dict[str, float]]] = {}
    for r in rows:
        best.setdefault(r["exchange"], {"bid": 0.0, "ask": 0.0})[r["side"]] += r["seconds_best"]
        side_total[r["side"]] += r["seconds_best"]
        hour_best.setdefault(r["hour_ts"], {"bid": {}, "ask": {}})[r["side"]][r["exchange"]] = \
            r["seconds_best"]
    series: Dict[str, Dict[float, Dict[str, float]]] = {}
    for hour, sides in hour_best.items():
        for side, per_ex in sides.items():
            tot = sum(per_ex.values())
            for ex, secs in per_ex.items():
                series.setdefault(ex, {}).setdefault(hour, {})[side] = \
                    secs / tot * 100 if tot else 0
    board = []
    for ex, t in best.items():
        bid = t["bid"] / side_total["bid"] * 100 if side_total["bid"] else 0
        ask = t["ask"] / side_total["ask"] * 100 if side_total["ask"] else 0
        board.append({
            "exchange": ex,
            "bid_share_pct": round(bid, 2), "ask_share_pct": round(ask, 2),
            "combined_pct": round((bid + ask) / 2, 2),
            "series": [{"ts": h, "bid": round(v.get("bid", 0), 2),
                        "ask": round(v.get("ask", 0), 2)}
                       for h, v in sorted(series.get(ex, {}).items())],
        })
    board.sort(key=lambda r: r["combined_pct"], reverse=True)
    for i, r in enumerate(board):
        r["rank"] = i + 1
    return {
        "base": base, "range": range,
        "current_best_bid": tob_tracker.current.get((base, "bid"), []),
        "current_best_ask": tob_tracker.current.get((base, "ask"), []),
        "board": board,
    }


@app.get("/api/market-share")
def get_market_share(base: str = Query("ALL"),
                     range: str = Query("1w")) -> Dict[str, Any]:
    """Volume share per venue from persisted snapshots (reported 24h volumes)."""
    base = base.upper()
    quote = CONFIG.get("quote_currency", "TMN")
    since = time.time() - HISTORY_WINDOWS.get(range, 7 * 86400)
    bases = ([base] if base != "ALL"
             else [b for b, _q in market_aggregator.pairs()])
    # bucket -> exchange -> summed volume
    buckets: Dict[float, Dict[str, float]] = {}
    for b in bases:
        for r in db.get_pair_snapshots(b, quote, since):
            bts = r["ts"] - r["ts"] % 3600
            buckets.setdefault(bts, {})
            buckets[bts][r["exchange"]] = \
                buckets[bts].get(r["exchange"], 0.0) + (r["volume_24h_quote"] or 0.0)
    exchanges = sorted({ex for v in buckets.values() for ex in v})
    series = {ex: [] for ex in exchanges}
    for bts in sorted(buckets):
        total = sum(buckets[bts].values())
        for ex in exchanges:
            share = buckets[bts].get(ex, 0.0) / total * 100 if total else 0.0
            series[ex].append({"ts": bts, "share": round(share, 2)})
    # current share from live state
    live: Dict[str, float] = {}
    for (ex, b), s in market_aggregator.market_state.items():
        if b in bases and s.mid > 0:
            live[ex] = live.get(ex, 0.0) + s.volume_24h_quote
    live_total = sum(live.values())
    current = sorted(
        [{"exchange": ex, "share_pct": round(v / live_total * 100, 2) if live_total else 0,
          "volume_quote": v} for ex, v in live.items()],
        key=lambda r: r["share_pct"], reverse=True)
    # rank movement: first vs second half of the window
    half = since + (time.time() - since) / 2
    def avg_share(ex, pred):
        pts = [p["share"] for p in series.get(ex, []) if pred(p["ts"])]
        return sum(pts) / len(pts) if pts else 0.0
    for row in current:
        ex = row["exchange"]
        row["trend"] = round(avg_share(ex, lambda t: t >= half)
                             - avg_share(ex, lambda t: t < half), 2)
    # venues that are live but report zero volume (broken/absent ticker data),
    # and venues whose volume is estimated from the observed trade tape
    from app.volume_estimator import volume_estimator
    live_venues = {ex for (ex, b), s in market_aggregator.market_state.items()
                   if b in bases and s.mid > 0 and s.status != "offline"}
    estimated: Dict[str, float] = {}
    for (ex, b), s in market_aggregator.market_state.items():
        if b in bases and s.volume_estimated:
            _bv, _qv, cov = volume_estimator.volumes(ex, b)
            estimated[ex] = max(estimated.get(ex, 0.0), cov)
    not_reporting = sorted(live_venues
                           - {r["exchange"] for r in current if r["volume_quote"] > 0}
                           - set(estimated))
    return {"base": base, "range": range, "current": current, "series": series,
            "not_reporting": not_reporting, "estimated": estimated}


@app.get("/api/opportunities/summary")
def get_opportunities_summary(days: int = Query(7, ge=1, le=180)) -> Dict[str, Any]:
    since = time.time() - days * 86400
    windows = [w for w in db.get_arb_windows(since) if w["opened_ts"] >= since]
    now = time.time()
    day_ago = now - 86400
    def total(rows): return round(sum(w["peak_profit_quote"] for w in rows))
    durations = sorted((w["closed_ts"] or now) - w["opened_ts"] for w in windows)
    by_hour = [0.0] * 24
    by_route: Dict[str, Dict[str, Any]] = {}
    from datetime import datetime, timezone, timedelta
    tehran = timezone(timedelta(hours=3, minutes=30))
    for w in windows:
        hour = datetime.fromtimestamp(w["opened_ts"], tehran).hour
        by_hour[hour] += w["peak_profit_quote"]
        route = f"{w['base']}: {w['buy_exchange']}→{w['sell_exchange']}"
        r = by_route.setdefault(route, {"route": route, "count": 0, "profit": 0.0})
        r["count"] += 1
        r["profit"] = round(r["profit"] + w["peak_profit_quote"])
    return {
        "days": days,
        "open_now": sum(1 for w in windows if w["closed_ts"] is None),
        "windows_total": len(windows),
        "missed_profit_quote": total(windows),
        "missed_profit_24h": total([w for w in windows if w["opened_ts"] >= day_ago]),
        "median_duration_sec": round(durations[len(durations) // 2], 1) if durations else None,
        "by_hour": [round(v) for v in by_hour],
        "top_routes": sorted(by_route.values(), key=lambda r: r["profit"], reverse=True)[:8],
        "min_edge_pct": settings_store.get().arb_min_edge_pct,
    }


@app.get("/api/opportunities/windows")
def get_opportunities_windows(days: int = Query(7, ge=1, le=180),
                              limit: int = Query(100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    since = time.time() - days * 86400
    return db.get_arb_windows(since, limit)


@app.get("/api/opportunities/inventory")
def get_opportunities_inventory(days: int = Query(7, ge=1, le=180)) -> Dict[str, Any]:
    """Minimum per-venue balances (TMN + coins) to capture 100% / 95% of the
    period's windows without any transfers."""
    since = time.time() - days * 86400
    windows = [w for w in db.get_arb_windows(since, limit=10000)
               if w["opened_ts"] >= since]
    result = inventory_requirements(windows)
    result["days"] = days
    return result


# ---------------------------------------------------------------- premium ---

@app.get("/api/premium/methods")
def get_premium_methods() -> Dict[str, Any]:
    return {
        "methods": list(METHODS),
        "exchanges": sorted({ex for (ex, _b) in market_aggregator.market_state}),
        "assets": [b for b, _q in market_aggregator.pairs() if b != "USDT"],
    }


@app.get("/api/premium/{base}")
def get_premium(base: str, range: str = Query("1d"),
                method: str = Query("composite"),
                exchange: Optional[str] = None) -> Dict[str, Any]:
    base = base.upper()
    if base == "USDT":
        raise HTTPException(status_code=422,
                            detail="USDT is the local benchmark — no premium")
    if method not in METHODS:
        raise HTTPException(status_code=422, detail=f"method must be one of {METHODS}")
    if method == "exchange" and not exchange:
        raise HTTPException(status_code=422, detail="method=exchange requires ?exchange=")
    quote = CONFIG.get("quote_currency", "TMN")
    since = time.time() - HISTORY_WINDOWS.get(range, 86400)
    series = premium_series(base, quote, method, since, exchange)
    values = [p["premium_pct"] for p in series]
    return {
        "base": base, "quote": quote, "method": method, "exchange": exchange,
        "range": range,
        "current": market_aggregator.premium_pct(base, method, exchange),
        "usd_reference": market_aggregator.usd_reference.get(base),
        "stats": {
            "avg": round(statistics.fmean(values), 3) if values else None,
            "min": round(min(values), 3) if values else None,
            "max": round(max(values), 3) if values else None,
            "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else None,
        },
        "series": series,
    }


# -------------------------------------------------------------- analytics ---

@app.get("/api/analytics/spread/{base}")
def get_spread_analytics(base: str, window: int = Query(3600, ge=300, le=30 * 86400),
                         exchange: Optional[str] = None) -> Dict[str, Any]:
    base = base.upper()
    quote = CONFIG.get("quote_currency", "TMN")
    exchanges = ([exchange] if exchange else
                 sorted({ex for (ex, b) in market_aggregator.market_state if b == base}))
    stats = {ex: metrics_engine.spread_stats(ex, base, window) for ex in exchanges}
    history = db.get_spread_history(base, quote, time.time() - window, exchange)
    return {"base": base, "window_sec": window, "stats": stats, "history": history}


@app.get("/api/arbitrage")
def get_arbitrage() -> List[Dict[str, Any]]:
    out = []
    for base, quote in market_aggregator.pairs():
        books = market_aggregator.usable_books(base)
        out.extend(o.to_dict() for o in metrics_engine.arbitrage(base, quote, books))
    out.sort(key=lambda o: o["net_pct"], reverse=True)
    return out


@app.get("/api/anomalies")
def get_anomalies() -> List[Dict[str, Any]]:
    out = [a.to_dict() for a in
           metrics_engine.detect_anomalies(market_aggregator.snapshots_by_asset())]
    # actionable arbitrage windows surface here too (info severity)
    now = time.time()
    for base, quote in market_aggregator.pairs():
        books = market_aggregator.usable_books(base)
        for o in metrics_engine.arbitrage(base, quote, books)[:2]:
            if o.net_pct >= 0.25:
                out.append({
                    "kind": "arbitrage", "exchange": f"{o.buy_exchange}→{o.sell_exchange}",
                    "base": base, "severity": "info", "value": o.net_pct,
                    "message": (f"{base}: buy {o.buy_exchange} / sell {o.sell_exchange} "
                                f"nets {o.net_pct:.2f}% after fees "
                                f"(size {o.max_size_base:g} {base})"),
                    "timestamp": now,
                })
    sev_rank = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (sev_rank.get(a["severity"], 3), -abs(a.get("value", 0))))
    return out


# Standard clip sizes for the large-order impact simulation, in quote (TMN)
IMPACT_CLIP_QUOTE = 2_000_000_000   # ~2B TMN market order


@app.get("/api/impact/{base}")
def get_impact(base: str, notional: float = Query(2e9, gt=0, le=1e15)) -> List[Dict[str, Any]]:
    """Slippage calculator: cost of a market order of `notional` TMN on every
    venue, both directions, walked through the live order books."""
    base = base.upper()
    rows = []
    for (ex, b), book in market_aggregator.books.items():
        if b != base or not book.bids or not book.asks:
            continue
        mid = (book.best_bid + book.best_ask) / 2
        buy = metrics_engine.order_impact_pct(book, notional, "buy")
        sell = metrics_engine.order_impact_pct(book, notional, "sell")
        rows.append({
            "exchange": ex, "base": base, "mid": mid,
            "buy_impact_pct": buy,
            "sell_impact_pct": sell,
            "buy_price": round(mid * (1 + buy / 100), 2) if buy is not None else None,
            "sell_price": round(mid * (1 - sell / 100), 2) if sell is not None else None,
        })
    rows.sort(key=lambda r: r["buy_impact_pct"] if r["buy_impact_pct"] is not None else 1e9)
    return rows


@app.get("/api/liquidity")
def get_liquidity() -> List[Dict[str, Any]]:
    """Per venue x asset liquidity board with real-time warnings:
    spread vs its 1h average, top-20 depth, depth drop, liquidity score,
    and simulated slippage of a large market order."""
    rows: List[Dict[str, Any]] = []
    by_asset = market_aggregator.snapshots_by_asset()
    for base, snaps in by_asset.items():
        scores = metrics_engine.liquidity_scores(snaps)
        for s in snaps:
            if s.mid <= 0:
                continue
            book = market_aggregator.books.get((s.exchange, base))
            impact = (metrics_engine.order_impact_pct(book, IMPACT_CLIP_QUOTE)
                      if book else None)
            st = metrics_engine.spread_stats(s.exchange, base, 3600)
            drop = metrics_engine.depth_drop_pct(s.exchange, base)
            score = scores.get(s.exchange)
            warnings: List[str] = []
            if s.status in ("offline", "stale", "outlier"):
                warnings.append(s.status)
            if score is not None and score < 40:
                warnings.append("low_score")
            if st["avg"] and st["current"] and st["current"] >= 2 * st["avg"]:
                warnings.append("spread_widening")
            if drop is not None and drop >= 30:
                warnings.append("depth_drop")
            if impact is None:
                warnings.append("book_too_thin")
            elif impact >= 0.5:
                warnings.append("high_impact")
            rows.append({
                "exchange": s.exchange, "base": base,
                "status": s.status,
                "liquidity_score": score,
                "spread_pct": round(s.spread_pct, 4),
                "spread_avg_1h": st["avg"],
                "bid_depth_quote": s.bid_depth_quote,
                "ask_depth_quote": s.ask_depth_quote,
                "depth_imbalance": round(s.depth_imbalance, 4),
                "depth_drop_pct": drop,
                "impact_pct": impact,
                "impact_clip_quote": IMPACT_CLIP_QUOTE,
                "warnings": warnings,
            })
    rows.sort(key=lambda r: (-len(r["warnings"]), r["liquidity_score"] or 0))
    return rows


# ----------------------------------------------------------------- alerts ---

@app.get("/api/alerts/rules")
def list_alert_rules() -> List[Dict[str, Any]]:
    return db.get_alert_rules()


@app.post("/api/alerts/rules", dependencies=[Depends(require_admin)])
def create_alert_rule(rule: AlertRuleIn) -> Dict[str, Any]:
    rule_id = db.insert_alert_rule(rule.name, rule.rule_type, rule.base,
                                   rule.exchange, rule.threshold,
                                   rule.window_sec, rule.cooldown_sec)
    return {"id": rule_id}


@app.delete("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_admin)])
def remove_alert_rule(rule_id: int) -> Dict[str, Any]:
    db.delete_alert_rule(rule_id)
    return {"ok": True}


@app.patch("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_admin)])
def toggle_alert_rule(rule_id: int, enabled: bool = Query(...)) -> Dict[str, Any]:
    db.set_alert_rule_enabled(rule_id, enabled)
    return {"ok": True}


@app.get("/api/alerts/events")
def list_alert_events(hours: int = Query(24, ge=1, le=720)) -> List[Dict[str, Any]]:
    return db.get_alert_events(time.time() - hours * 3600)


@app.post("/api/alerts/events/{event_id}/ack")
def ack_alert(event_id: int) -> Dict[str, Any]:
    db.ack_alert_event(event_id)
    return {"ok": True}


# --------------------------------------------------------- calendar / news --

@app.get("/api/calendar")
def get_calendar(impact: str = "ALL", country: str = "ALL") -> List[Dict[str, Any]]:
    return news_service.get_calendar(impact, country)


@app.get("/api/calendar/history")
def get_calendar_history(title: str, country: str) -> List[Dict[str, Any]]:
    return db.get_event_surprise_history(title, country)


@app.get("/api/news")
def get_news(coin: str = "ALL", min_impact: int = Query(1, ge=1, le=3)) -> List[Dict[str, Any]]:
    return news_service.get_news(coin, min_impact)


# --------------------------------------------------------------- settings ---

@app.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(settings_store.get())


@app.post("/api/settings", dependencies=[Depends(require_admin)])
def update_settings(update: SettingsUpdate) -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(settings_store.update(update.model_dump(exclude_none=True)))


# ------------------------------------------------------------------ admin ---

@app.get("/api/admin/exchanges")
def list_custom_exchanges() -> List[Dict[str, Any]]:
    return db.get_custom_exchanges()


@app.post("/api/admin/exchanges", dependencies=[Depends(require_admin)])
def add_custom_exchange(body: ExchangeSpecIn) -> Dict[str, Any]:
    spec = dict(body.spec)
    spec.setdefault("name", body.name)
    if "orderbook_url" not in spec:
        raise HTTPException(status_code=422, detail="spec.orderbook_url is required")
    db.upsert_custom_exchange(body.name, spec, body.enabled)
    market_aggregator.reload_connectors()
    return {"ok": True, "active": [c.exchange_name for c in market_aggregator.connectors]}


@app.delete("/api/admin/exchanges/{name}", dependencies=[Depends(require_admin)])
def remove_custom_exchange(name: str) -> Dict[str, Any]:
    db.delete_custom_exchange(name)
    market_aggregator.reload_connectors()
    return {"ok": True}


@app.get("/api/admin/pairs")
def list_custom_pairs() -> List[Dict[str, Any]]:
    return db.get_custom_pairs()


@app.post("/api/admin/pairs", dependencies=[Depends(require_admin)])
def add_custom_pair(body: PairIn) -> Dict[str, Any]:
    quote = body.quote or CONFIG.get("quote_currency", "TMN")
    # "USOON/TMN" typed as the base used to produce "USOON/TMN/TMN" everywhere
    base = clean_base(body.base, quote)
    if not base:
        raise HTTPException(status_code=422,
                            detail="base must be a ticker like DOGE (no quote, 2-12 letters/digits)")
    db.upsert_custom_pair(base, quote, body.enabled)
    if body.coingecko_id:
        db.upsert_reference_id(base, body.coingecko_id.strip().lower())
        market_aggregator._reference.set_id(base, body.coingecko_id.strip().lower())
    market_aggregator._unresolvable.discard(base)
    market_aggregator.usd_reference_ts = 0.0   # refresh reference on next cycle
    return {"ok": True, "pairs": [f"{b}/{q}" for b, q in market_aggregator.pairs()]}


@app.delete("/api/admin/pairs/{base:path}", dependencies=[Depends(require_admin)])
def remove_custom_pair(base: str) -> Dict[str, Any]:
    quote = CONFIG.get("quote_currency", "TMN")
    target = clean_base(base, quote) or base.upper()
    # also removes rows stored un-normalized by older builds ("USOON/TMN")
    for row in db.get_custom_pairs():
        if row["base"] == base.upper() or clean_base(row["base"], quote) == target:
            db.delete_custom_pair(row["base"], row["quote"])
    return {"ok": True}


# -------------------------------------------------------------- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    if AUTH_ENABLED and not auth_service.validate(token):
        await ws.close(code=4401)
        return
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive pings from client
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)


# ----------------------------------------------------------------- static ---

if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    log.warning("frontend/dist not found — build it with: cd frontend && npm run build")


def _cli(args: List[str]) -> int:
    """Offline credential management (single-user auth).

    python3 main.py hash-password "<password>"   print AUTH_PASSWORD_HASH value
    python3 main.py generate-secret              print AUTH_TOKEN_SECRET value
    python3 main.py whoami                       show active username
    """
    cmd = args[0]
    if cmd == "hash-password" and len(args) >= 2:
        from app.auth import hash_password
        if len(args[1]) < 6:
            print("password must be at least 6 characters")
            return 1
        print("AUTH_PASSWORD_HASH=" + hash_password(args[1]))
        print("\n→ copy this line into backend/.env (local) or add the variable"
              "\n  in your hosting dashboard (Vercel → Settings → Environment"
              "\n  Variables), then restart/redeploy.")
        return 0
    if cmd == "generate-secret":
        from app.auth import generate_secret
        print("AUTH_TOKEN_SECRET=" + generate_secret())
        print("\n→ set alongside AUTH_PASSWORD_HASH; rotating it logs everyone out.")
        return 0
    if cmd == "whoami":
        suffix = " (default admin/admin — configure env!)" \
            if auth_service.default_creds else ""
        print(auth_service.username + suffix)
        return 0
    print(_cli.__doc__)
    return 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        raise SystemExit(_cli(sys.argv[1:]))
    server = CONFIG.get("server", {})
    uvicorn.run(app, host=server.get("host", "127.0.0.1"),
                port=int(server.get("port", 4000)))
