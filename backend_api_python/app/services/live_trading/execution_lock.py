"""Cross-process serialization for orders that mutate one exchange position."""

from __future__ import annotations

import time
import json
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, Iterator

from app.services.live_trading.records import normalize_strategy_symbol
from app.utils.db import get_db_connection


class TradeExecutionBusy(RuntimeError):
    pass


def release_busy_pending_order(order_id: int) -> None:
    """Release a claimed order without consuming its retry budget."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE pending_orders
            SET status = 'pending',
                attempts = GREATEST(0, COALESCE(attempts, 0) - 1),
                last_error = 'strategyRuntime.tradeExecutionBusy',
                dispatch_note = 'trade_execution_lock_busy',
                updated_at = NOW()
            WHERE id = %s AND status = 'processing'
            """,
            (int(order_id),),
        )
        db.commit()
        cur.close()


def serialized_live_dispatch(func, *, strategy_loader: Callable[[int], Dict[str, Any]]):
    """Serialize one live strategy mutation without affecting signal mode."""
    @wraps(func)
    def wrapped(worker, order_row: Dict[str, Any], *args, **kwargs):
        try:
            payload = json.loads(str(order_row.get("payload_json") or "{}")) or {}
        except Exception:
            payload = {}
        mode = str(
            order_row.get("execution_mode") or payload.get("execution_mode") or "signal"
        ).strip().lower()
        if mode != "live":
            return func(worker, order_row, *args, **kwargs)
        strategy_id = int(order_row.get("strategy_id") or payload.get("strategy_id") or 0)
        try:
            strategy = strategy_loader(strategy_id) or {} if strategy_id > 0 else {}
        except Exception:
            strategy = {}
        exchange_config = strategy.get("exchange_config")
        trading_config = strategy.get("trading_config")
        exchange_config = exchange_config if isinstance(exchange_config, dict) else {}
        trading_config = trading_config if isinstance(trading_config, dict) else {}
        credential_id = int(
            order_row.get("credential_id")
            or payload.get("credential_id")
            or exchange_config.get("credential_id")
            or exchange_config.get("credentials_id")
            or trading_config.get("credential_id")
            or trading_config.get("credentials_id")
            or 0
        )
        if credential_id <= 0:
            return func(worker, order_row, *args, **kwargs)
        key = trade_execution_key(
            user_id=int(
                order_row.get("user_id")
                or payload.get("user_id")
                or strategy.get("user_id")
                or 0
            ),
            credential_id=credential_id,
            market_type=str(order_row.get("market_type") or payload.get("market_type") or "swap"),
            symbol=str(order_row.get("symbol") or payload.get("symbol") or ""),
        )
        try:
            with trade_execution_lock(key, timeout_sec=3.0):
                return func(worker, order_row, *args, **kwargs)
        except TradeExecutionBusy:
            release_busy_pending_order(int(order_row.get("id") or 0))
            return None

    return wrapped


def trade_execution_key(
    *,
    user_id: int,
    credential_id: int,
    market_type: str,
    symbol: str,
) -> str:
    market = str(market_type or "swap").strip().lower()
    if market in {"future", "futures", "perp", "perpetual"}:
        market = "swap"
    normalized_symbol = normalize_strategy_symbol(str(symbol or "")) or str(symbol or "").strip().upper()
    return (
        f"trade-execution:{int(user_id or 0)}:{int(credential_id or 0)}:"
        f"{market}:{normalized_symbol.upper()}"
    )


@contextmanager
def trade_execution_lock(key: str, *, timeout_sec: float = 3.0) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock for one account/instrument mutation."""
    lock_key = str(key or "").strip()
    if not lock_key:
        raise ValueError("trade execution lock key is required")
    deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
    acquired = False
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            while True:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                    (lock_key,),
                )
                row = cur.fetchone() or {}
                acquired = bool(row.get("acquired") if isinstance(row, dict) else row[0])
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise TradeExecutionBusy(lock_key)
                time.sleep(0.05)
            yield
        finally:
            if acquired:
                try:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                except Exception:
                    pass
            cur.close()
