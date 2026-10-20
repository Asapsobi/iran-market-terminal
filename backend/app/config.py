"""Application config: app_config.json + env overrides + runtime additions from DB.

Adding an exchange or pair does NOT require a rebuild:
- Built-in exchanges are toggled in app_config.json.
- New exchanges can be added at runtime through POST /api/admin/exchanges with a
  declarative JSON spec (see connectors.GenericRestConnector) — stored in SQLite.
- New pairs via POST /api/admin/pairs.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("terminal.config")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "app_config.json"

_DEFAULTS: Dict[str, Any] = {
    "quote_currency": "TMN",
    "assets": [
        "BTC", "ETH", "USDT", "TRX", "SOL", "GRAM", "BNB", "XRP",
        "ADA", "LTC", "LINK", "SUI", "XLM", "AVAX", "NEAR", "UNI", "AAVE", "DOT",
    ],
    "exchanges": {
        "Nobitex": {"enabled": True, "taker_fee_pct": 0.25, "color": "#4A9EFF"},
        "Wallex": {"enabled": True, "taker_fee_pct": 0.25, "color": "#9C6BFF"},
        "Bitpin": {"enabled": True, "taker_fee_pct": 0.30, "color": "#00D68F"},
        "Exir": {"enabled": True, "taker_fee_pct": 0.20, "color": "#FFB020"},
        "Tabdeal": {"enabled": True, "taker_fee_pct": 0.25, "color": "#FF6B9D"},
        # publicapi.ramzinex.com is often TCP-blocked on some hosts; enable via
        # app_config or env RAMZINEX_FORCE=1 when reachable.
        "Ramzinex": {"enabled": True, "taker_fee_pct": 0.35, "color": "#38C6D9"},
    },
    "reference": {
        "enabled": True,
        "provider": "coingecko",
        "coingecko_ids": {
            "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether",
            "TRX": "tron", "SOL": "solana", "GRAM": "the-open-network", "BNB": "binancecoin",
            "XRP": "ripple", "ADA": "cardano", "LTC": "litecoin",
            "LINK": "chainlink", "SUI": "sui", "XLM": "stellar",
            "AVAX": "avalanche-2", "NEAR": "near", "UNI": "uniswap",
            "AAVE": "aave", "DOT": "polkadot",
        },
    },
    "retention": {"snapshots_days": 90, "candles_days": 365,
                  "alerts_days": 30, "calendar_days": 730},
    "depth_levels": 20,
    "demo_mode": False,
    "admin_token": "",
    "auth_enabled": True,
    "server": {"host": "127.0.0.1", "port": 4000},
}


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    try:
        if CONFIG_PATH.exists():
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except Exception as exc:  # pragma: no cover - defensive
        log.error("Failed to read %s (%s); using defaults", CONFIG_PATH, exc)

    # Environment overrides
    if os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes"):
        cfg["demo_mode"] = True
    if os.environ.get("ADMIN_TOKEN"):
        cfg["admin_token"] = os.environ["ADMIN_TOKEN"]
    if os.environ.get("PORT"):
        cfg["server"]["port"] = int(os.environ["PORT"])
    if os.environ.get("HOST"):
        cfg["server"]["host"] = os.environ["HOST"]
    # Some hosts cannot reach Ramzinex CDN (TCP timeout). Disable with env.
    if os.environ.get("RAMZINEX_DISABLE", "").lower() in ("1", "true", "yes"):
        cfg.setdefault("exchanges", {}).setdefault("Ramzinex", {})["enabled"] = False
    if os.environ.get("RAMZINEX_FORCE", "").lower() in ("1", "true", "yes"):
        cfg.setdefault("exchanges", {}).setdefault("Ramzinex", {})["enabled"] = True
    return cfg


CONFIG = load_config()


def enabled_exchanges() -> List[str]:
    return [name for name, spec in CONFIG["exchanges"].items() if spec.get("enabled")]


def taker_fee_pct(exchange: str) -> float:
    spec = CONFIG["exchanges"].get(exchange, {})
    return float(spec.get("taker_fee_pct", 0.25))


def exchange_color(exchange: str) -> str:
    return CONFIG["exchanges"].get(exchange, {}).get("color", "#8A93A6")
