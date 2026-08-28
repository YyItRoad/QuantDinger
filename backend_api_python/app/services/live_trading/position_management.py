"""使用标准策略实例管理交易所当前整笔仓位。"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import uuid
from typing import Any, Dict, Iterator

from app.services.live_trading.account_positions import list_managed_positions_for_account
from app.services.live_trading.account_snapshot import fetch_account_snapshot
from app.services.live_trading.records import normalize_strategy_symbol, upsert_position
from app.services.strategy import get_strategy_service
from app.services.strategy_command_repository import StrategyCommandRepository
from app.utils.logger import get_logger


logger = get_logger(__name__)


class PositionManagementError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = int(status_code)


def _market_type(value: Any) -> str:
    market_type = str(value or "swap").strip().lower()
    if market_type in {"future", "futures", "perp", "perpetual"}:
        return "swap"
    return market_type


def _symbol(value: Any) -> str:
    raw = str(value or "").strip().split(":", 1)[0]
    return normalize_strategy_symbol(raw)


def _same_leg(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return (
        _symbol(left.get("symbol") or left.get("symbol_canonical"))
        == _symbol(right.get("symbol") or right.get("symbol_canonical"))
        and str(left.get("side") or "").strip().lower()
        == str(right.get("side") or "").strip().lower()
        and _market_type(left.get("market_type")) == _market_type(right.get("market_type"))
    )


def _fresh_position(snapshot: Dict[str, Any], position_ref: Dict[str, Any]) -> Dict[str, Any]:
    market_type = _market_type(position_ref.get("market_type"))
    rows = snapshot.get("swap_positions") if market_type == "swap" else snapshot.get("spot_positions")
    rows = rows if isinstance(rows, list) else []
    requested_inst_id = str(position_ref.get("inst_id") or "").strip()
    requested_side = str(position_ref.get("side") or "").strip().lower()

    # When the caller supplied an exchange instrument id, prefer it across the
    # whole target-market bucket before falling back to canonical leg matching.
    # This prevents an earlier same-symbol row from hiding a later exact match.
    if requested_inst_id:
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["market_type"] = _market_type(row.get("market_type") or market_type)
            if (
                str(row.get("side") or "").strip().lower() == requested_side
                and str(row.get("inst_id") or "").strip() == requested_inst_id
            ):
                return row

    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["market_type"] = _market_type(row.get("market_type") or market_type)
        if str(row.get("side") or "").strip().lower() != requested_side:
            continue
        if _same_leg(row, position_ref):
            return row

    # A warning from another market bucket must not invalidate a successfully
    # refreshed target position. Only surface the snapshot error when the target
    # leg itself could not be found.
    if snapshot.get("partial") is True or snapshot.get("error"):
        detail = str(snapshot.get("error") or "交易所目标市场仓位快照不完整")
        raise PositionManagementError(f"交易所仓位同步失败：{detail}", status_code=409)
    raise PositionManagementError("交易所中已找不到该仓位，请先同步持仓", status_code=409)


def _number_or_zero(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _spot_last_price(*, user_id: int, credential_id: int, symbol: str) -> float:
    """Read one fresh spot ticker without expanding the whole account snapshot."""
    if _symbol(symbol) == "USDT":
        return 1.0
    try:
        from app.services.exchange_execution import resolve_exchange_config
        from app.services.live_trading.factory import create_client
        from app.services.live_trading.spot_sizing import fetch_spot_last_price

        exchange_config = resolve_exchange_config(
            {"credential_id": int(credential_id)},
            user_id=int(user_id),
        )
        client = create_client(exchange_config, market_type="spot")
        return _number_or_zero(fetch_spot_last_price(client, symbol=_symbol(symbol)))
    except Exception as exc:
        logger.warning("读取现货最新价格失败 credential=%s symbol=%s: %s", credential_id, symbol, exc)
        return 0.0


def _management_lease_key(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"position-management:{digest}"


@contextmanager
def _management_leg_lock(
    *,
    credential_id: int,
    market_type: str,
    symbol: str,
    side: str,
) -> Iterator[None]:
    """Serialize adoption without holding a pooled DB connection while creating the strategy."""
    leg = "|".join((
        str(int(credential_id)),
        _market_type(market_type),
        _symbol(symbol),
        str(side or "").strip().lower(),
    ))
    lease_key = _management_lease_key(leg)
    owner_id = uuid.uuid4().hex
    repository = StrategyCommandRepository()
    acquired = repository.acquire_process_lease(
        lease_key=lease_key,
        owner_id=owner_id,
        lease_seconds=120,
    )
    if not acquired:
        raise PositionManagementError("该仓位正在被接管，请稍后重试", status_code=409)
    try:
        yield
    finally:
        try:
            repository.release_process_lease(lease_key=lease_key, owner_id=owner_id)
        except Exception as exc:
            # The lease expires automatically; cleanup failure must not hide the
            # adoption result or hold a database connection open.
            logger.warning("释放持仓接管租约失败 lease=%s: %s", lease_key, exc)


def _complete_fresh_position_price(
    *,
    user_id: int,
    credential_id: int,
    fresh: Dict[str, Any],
) -> Dict[str, Any]:
    """Complete spot takeover prices before entering the database-only critical section."""
    resolved = dict(fresh)
    if _market_type(resolved.get("market_type")) != "spot":
        return resolved
    symbol = _symbol(resolved.get("symbol"))
    mark_price = _number_or_zero(resolved.get("mark_price"))
    if mark_price <= 0:
        mark_price = _spot_last_price(
            user_id=int(user_id),
            credential_id=int(credential_id),
            symbol=symbol,
        )
    if mark_price <= 0:
        mark_price = _number_or_zero(resolved.get("entry_price"))
    if _number_or_zero(resolved.get("entry_price")) <= 0 and mark_price > 0:
        # Spot wallet APIs usually do not expose cost basis. Management PnL
        # therefore starts from the fresh takeover price.
        resolved["entry_price"] = mark_price
    resolved["mark_price"] = mark_price
    return resolved


def _delete_created_strategy(
    service: Any,
    *,
    strategy_id: int,
    user_id: int,
    reason: str,
) -> bool:
    """Best-effort compensation that never hides the original creation error."""
    try:
        deleted = bool(service.delete_strategy(int(strategy_id), user_id=int(user_id)))
    except Exception as exc:
        logger.error(
            "清理创建失败的持仓管理策略异常 strategy=%s reason=%s: %s",
            strategy_id,
            reason,
            exc,
            exc_info=True,
        )
        return False
    if not deleted:
        logger.error(
            "清理创建失败的持仓管理策略未删除任何记录 strategy=%s reason=%s",
            strategy_id,
            reason,
        )
        return False
    return True


def _cleanup_failure_suffix(*, cleaned: bool, strategy_id: int) -> str:
    if cleaned:
        return ""
    return f"；自动清理失败，残留策略 ID：{int(strategy_id)}，请在策略列表中删除"


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PositionManagementError(f"交易所返回的{label}无效，请先同步持仓", status_code=409) from exc
    if number <= 0:
        raise PositionManagementError(f"交易所返回的{label}无效，请先同步持仓", status_code=409)
    return number


def _object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _strategy_monitors_position(strategy: Dict[str, Any], symbol: str, market_type: str) -> bool:
    config = _object(strategy.get("trading_config"))
    manifest = _object(config.get("strategy_manifest"))
    universe = _object(manifest.get("universe"))
    if universe.get("reference"):
        return True
    instruments = universe.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        return True
    for item in instruments:
        if not isinstance(item, dict):
            continue
        item_market_type = _market_type(item.get("market_type"))
        if _symbol(item.get("symbol")) == symbol and item_market_type == market_type:
            return True
    return False


def create_managed_strategy(
    *,
    user_id: int,
    position_ref: Dict[str, Any],
    strategy_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """重新读取交易所仓位，创建标准停止策略实例并登记整笔仓位。"""
    uid = int(user_id or 0)
    position_ref = dict(position_ref or {})
    strategy_payload = dict(strategy_payload or {})
    credential_id = int(position_ref.get("credential_id") or 0)
    if uid <= 0 or credential_id <= 0:
        raise PositionManagementError("缺少有效的账户凭证")

    # Refresh exchange state before taking a DB-backed adoption lock. Network
    # latency must never occupy a connection from the application DB pool.
    snapshot = fetch_account_snapshot(user_id=uid, credential_id=credential_id)
    fresh = _fresh_position(snapshot, position_ref)
    fresh = _complete_fresh_position_price(
        user_id=uid,
        credential_id=credential_id,
        fresh=fresh,
    )

    with _management_leg_lock(
        credential_id=credential_id,
        market_type=_market_type(fresh.get("market_type") or position_ref.get("market_type")),
        symbol=_symbol(fresh.get("symbol") or fresh.get("symbol_canonical")),
        side=str(fresh.get("side") or "").strip().lower(),
    ):
        return _create_managed_strategy_locked(
            user_id=uid,
            credential_id=credential_id,
            position_ref=position_ref,
            strategy_payload=strategy_payload,
            fresh=fresh,
        )


def _create_managed_strategy_locked(
    *,
    user_id: int,
    credential_id: int,
    position_ref: Dict[str, Any],
    strategy_payload: Dict[str, Any],
    fresh: Dict[str, Any],
) -> Dict[str, Any]:
    uid = int(user_id)
    existing = list_managed_positions_for_account(user_id=uid, credential_id=credential_id)
    if any(_same_leg(row, fresh) for row in existing):
        raise PositionManagementError("该仓位已经由策略管理，请先同步持仓", status_code=409)

    market_type = _market_type(fresh.get("market_type") or position_ref.get("market_type"))
    symbol = _symbol(fresh.get("symbol"))
    size = _positive_number(fresh.get("size"), "持仓数量")
    entry_price = _positive_number(fresh.get("entry_price"), "开仓价")
    mark_price = _positive_number(fresh.get("mark_price"), "最新价格")
    leverage = _positive_number(fresh.get("leverage") or 1, "杠杆倍数")
    side = str(fresh.get("side") or "").strip().lower()
    inst_id = str(fresh.get("inst_id") or position_ref.get("inst_id") or "").strip()

    payload = dict(strategy_payload)
    params = _object(payload.get("params"))
    params["leverage"] = leverage
    payload.update({
        "user_id": uid,
        "executionMode": "live",
        "credentialId": credential_id,
        "leverageEnabled": leverage > 1,
        "leverage": leverage,
        "params": params,
        "positionManagement": {
            "enabled": True,
            "auto_stop_when_flat": True,
        },
    })
    service = get_strategy_service()
    strategy_id = int(service.create_strategy(payload))
    strategy = service.get_strategy(strategy_id, user_id=uid) or {}
    if not _strategy_monitors_position(strategy, symbol, market_type):
        cleaned = _delete_created_strategy(
            service,
            strategy_id=strategy_id,
            user_id=uid,
            reason="strategy_source_mismatch",
        )
        suffix = _cleanup_failure_suffix(cleaned=cleaned, strategy_id=strategy_id)
        raise PositionManagementError(
            f"策略源码没有订阅当前仓位品种，请选择对应品种的源码{suffix}",
            status_code=409 if cleaned else 500,
        )
    try:
        upsert_position(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            current_price=mark_price,
            highest_price=mark_price,
            lowest_price=mark_price,
            user_id=uid,
            market_type=market_type,
            credential_id=credential_id,
            inst_id=inst_id,
        )
    except Exception as exc:
        cleaned = _delete_created_strategy(
            service,
            strategy_id=strategy_id,
            user_id=uid,
            reason="position_registration_failed",
        )
        suffix = _cleanup_failure_suffix(cleaned=cleaned, strategy_id=strategy_id)
        raise PositionManagementError(f"创建策略后登记仓位失败{suffix}", status_code=500) from exc

    return {
        "strategy_id": strategy_id,
        "strategy_name": str(strategy.get("strategy_name") or payload.get("name") or ""),
        "status": str(strategy.get("status") or "stopped"),
        "timeframe": str(strategy.get("timeframe") or ""),
        "symbol": symbol,
        "side": side,
        "size": str(fresh.get("size") or "0"),
        "entry_price": str(fresh.get("entry_price") or "0"),
        "mark_price": str(fresh.get("mark_price") or "0"),
        "leverage": str(fresh.get("leverage") or "1"),
    }
