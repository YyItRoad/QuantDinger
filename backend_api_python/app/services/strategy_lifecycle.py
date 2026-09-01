"""
Unified auto-stop for live strategies after fatal exchange/auth/connectivity errors.

Position sync and order workers call `auto_stop_live_strategy` so DB status, executor
threads, and runtime logs stay consistent (avoids endless retry spam after restart).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Set

from app.services.strategy import get_strategy_service
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.utils.strategy_runtime_logs import append_strategy_log

logger = get_logger(__name__)

_quiet_lock = threading.Lock()
_quiet_sids: Set[int] = set()


def _config_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _strategy_has_open_positions(strategy_id: int) -> bool:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT 1 FROM qd_strategy_positions WHERE strategy_id = %s AND size > 0 LIMIT 1",
            (int(strategy_id),),
        )
        row = cur.fetchone()
        cur.close()
    return bool(row)


def maybe_stop_position_management_strategy(strategy_id: int) -> bool:
    """Stop a marked management instance after its final position is gone."""
    sid = int(strategy_id or 0)
    if sid <= 0:
        return False
    service = get_strategy_service()
    strategy = service.get_strategy(sid) or {}
    config = _config_object(strategy.get("trading_config"))
    marker = _config_object(config.get("position_management"))
    if marker.get("enabled") is not True or marker.get("auto_stop_when_flat") is not True:
        return False
    if str(strategy.get("status") or "").strip().lower() != "running":
        return False
    if _strategy_has_open_positions(sid):
        return False
    user_id = int(strategy.get("user_id") or 0)
    if not service.update_strategy_status(sid, "stopped", user_id=user_id):
        return False

    # Do not stop a process-local executor directly here. Position sync may run
    # on a different trading worker from the one that owns this strategy's
    # runtime lease. A durable stop command is claimed by the lease owner, which
    # then performs the normal executor cleanup and releases that lease.
    try:
        from app.services.strategy_command_repository import StrategyCommandRepository

        StrategyCommandRepository().enqueue(
            strategy_id=sid,
            user_id=user_id,
            command_type="stop",
            payload={"close_positions": False},
        )
    except Exception as exc:
        logger.error(
            "Failed to queue position-management stop command for strategy %s: %s",
            sid,
            exc,
            exc_info=True,
        )
        append_strategy_log(sid, "error", f"持仓已平仓，但自动停止命令创建失败：{exc}")
        return False

    append_strategy_log(sid, "info", "持仓已全部平仓，管理策略已自动停止")
    return True


def is_fatal_exchange_error(msg: str) -> bool:
    """Return True when the strategy should not keep retrying exchange/private APIs."""
    m = (msg or "").lower()
    if not m:
        return False
    if "unsupported market type" in m or "unsupported market" in m:
        return True
    tokens = (
        # Programming/adapter contract failures are deterministic. Retrying on
        # every candle can enqueue an entire robot order ladder without any
        # chance of recovery until the service is upgraded.
        "got an unexpected keyword argument",
        "missing 1 required positional argument",
        "binance http 401",
        '"code":-2015',
        "-2015",
        "okx http 401",
        '"code":"50111"',
        "50111",
        "invalid api-key",
        "invalid api key",
        "invalid ok-access-key",
        "invalid ip",
        "invalid_ip",
        "40018",
        "permissions for action",
        "unauthorized",
        "forbidden",
        " http 401",
        "authentication",
        "signature mismatch",
        "invalid_signature",
        "permission denied",
        "connection refused",
        "connect call failed",
        "errno 111",
        "make sure api port on tws",
        "failed to connect to ibkr",
        "ibkr connection failed",
        "live trading error",
        "single-asset collateral mode is temporarily unavailable",
        "disabled ibkr",
        "已关闭 ibkr",
        "missing okx",
        "api_key/secret_key",
        "secret_key/passphrase",
        "missing api_key",
        "missing secret",
        "missing passphrase",
        "missing credential",
        "no credential",
        "exchange credential",
        "unsupported client for grid",
    )
    return any(t in m for t in tokens)


def maybe_auto_stop_on_exchange_error(
    strategy_id: int,
    msg: str,
    *,
    source: str = "exchange",
    consecutive_failures: int = 0,
    consecutive_threshold: int = 5,
) -> bool:
    """
    Stop a live strategy after a fatal exchange/auth error or repeated failures.
    Returns True if auto-stop was triggered (or already quieted for this run).
    """
    sid = int(strategy_id or 0)
    if sid <= 0:
        return False
    reason = (msg or "").strip()
    if not reason:
        return False
    if is_fatal_exchange_error(reason):
        auto_stop_live_strategy(sid, reason, source=source)
        return True
    if consecutive_failures >= max(1, int(consecutive_threshold or 5)):
        auto_stop_live_strategy(
            sid,
            f"Repeated exchange errors ({consecutive_failures}): {reason}",
            source=source,
        )
        return True
    return False


def should_skip_position_sync(strategy_id: int) -> bool:
    """In-process guard: skip sync for strategies already auto-stopped this run."""
    with _quiet_lock:
        return int(strategy_id) in _quiet_sids


def auto_stop_live_strategy(
    strategy_id: int,
    reason: str,
    *,
    source: str = "position_sync",
) -> bool:
    """
    Mark strategy stopped in DB, stop executor thread if running, append runtime log.
    Safe to call multiple times for the same strategy_id.
    """
    sid = int(strategy_id)
    if sid <= 0:
        return False

    reason = (reason or "").strip() or "fatal exchange error"
    with _quiet_lock:
        already = sid in _quiet_sids
        _quiet_sids.add(sid)
    if already:
        return True

    log_msg = f"Auto-stopped ({source}): {reason}"
    logger.error("[Strategy %s] %s", sid, log_msg)
    try:
        append_strategy_log(sid, "error", log_msg)
    except Exception:
        pass

    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "UPDATE qd_strategies_trading SET status = 'stopped' WHERE id = %s",
                (sid,),
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("auto_stop: DB update failed for strategy %s: %s", sid, e)

    try:
        from app import get_trading_executor

        get_trading_executor().stop_strategy(sid)
    except Exception as e:
        logger.debug("auto_stop: executor stop_strategy(%s): %s", sid, e)

    return True
