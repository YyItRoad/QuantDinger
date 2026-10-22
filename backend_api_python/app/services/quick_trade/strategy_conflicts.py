"""Detect automated strategy activity that can conflict with Quick Trade."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.services.live_trading.gate import GateUsdtFuturesClient
from app.services.live_trading.symbols import to_gate_currency_pair
from app.utils.logger import get_logger
from app.utils.request_guard import cache_key, guarded_cached


_STRATEGY_CLIENT_ID = re.compile(r"^(?:t-)?qd_(\d+)_", re.IGNORECASE)
_QUICK_TRADE_CLIENT_ID = re.compile(r"^(?:t-)?qt", re.IGNORECASE)
logger = get_logger(__name__)


def recent_gate_strategy_activity(
    client: Any,
    symbol: str,
    *,
    lookback_seconds: float = 1800.0,
    manual_stability_seconds: float = 120.0,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return sanitized recent QuantDinger strategy activity for a Gate swap leg."""
    if not isinstance(client, GateUsdtFuturesClient):
        return None

    contract = to_gate_currency_pair(symbol)
    rows = client._signed_request(
        "GET",
        "/api/v4/futures/usdt/my_trades",
        params={"contract": contract, "limit": 100},
    )
    if not isinstance(rows, list):
        return None
    current = float(time.time() if now is None else now)
    newest = None
    newest_time = 0.0
    newest_manual_time = 0.0

    for row in rows or []:
        if str(row.get("contract") or "") != contract:
            continue
        client_text = str(row.get("text") or row.get("client_order_id") or "").strip()
        match = _STRATEGY_CLIENT_ID.match(client_text)
        try:
            occurred_at = float(row.get("create_time") or row.get("time") or 0)
        except (TypeError, ValueError):
            continue
        age = current - occurred_at
        if occurred_at <= 0 or age < -30 or age > float(lookback_seconds):
            continue
        if not match:
            if _QUICK_TRADE_CLIENT_ID.match(client_text):
                newest_manual_time = max(newest_manual_time, occurred_at)
            continue
        if occurred_at <= newest_time:
            continue
        newest_time = occurred_at
        newest = {
            "active": True,
            "source": "quantdinger_strategy",
            "strategy_id": match.group(1),
            "last_activity_at": datetime.fromtimestamp(occurred_at, timezone.utc).isoformat(),
            "age_seconds": max(0, round(age, 3)),
            "exchange_order_id": str(row.get("order_id") or ""),
        }

    if (
        newest is not None
        and newest_manual_time > newest_time
        and current - newest_manual_time >= float(manual_stability_seconds)
    ):
        return None
    return newest


def cached_recent_strategy_activity(
    client: Any,
    *,
    user_id: int,
    credential_id: int,
    symbol: str,
    market_type: str,
) -> Optional[Dict[str, Any]]:
    """Return a short-lived strategy conflict snapshot for swap trading."""
    if str(market_type or "").strip().lower() != "swap":
        return None

    try:
        return guarded_cached(
            cache_key("quick_trade_strategy_conflict", user_id, credential_id, market_type, symbol),
            lambda: recent_gate_strategy_activity(client, symbol),
            ttl_sec=12,
            stale_ttl_sec=30,
            timeout_sec=6,
            namespace="quick_trade_strategy_conflict",
            max_concurrent=4,
        )
    except Exception as exc:
        logger.warning("Quick Trade strategy-conflict check failed: %s", exc)
        return None
