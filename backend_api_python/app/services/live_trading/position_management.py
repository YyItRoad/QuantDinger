"""使用标准策略实例管理交易所当前整笔仓位。"""

from __future__ import annotations

import json
from typing import Any, Dict

from app.services.live_trading.account_positions import list_managed_positions_for_account
from app.services.live_trading.account_snapshot import fetch_account_snapshot
from app.services.live_trading.records import normalize_strategy_symbol, upsert_position
from app.services.strategy import get_strategy_service


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
    if snapshot.get("partial") is True or snapshot.get("error"):
        detail = str(snapshot.get("error") or "交易所账户快照不完整")
        raise PositionManagementError(f"交易所仓位同步失败：{detail}", status_code=409)

    market_type = _market_type(position_ref.get("market_type"))
    rows = snapshot.get("swap_positions") if market_type == "swap" else snapshot.get("spot_positions")
    rows = rows if isinstance(rows, list) else []
    requested_inst_id = str(position_ref.get("inst_id") or "").strip()
    requested_side = str(position_ref.get("side") or "").strip().lower()

    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["market_type"] = _market_type(row.get("market_type") or market_type)
        same_inst_id = requested_inst_id and str(row.get("inst_id") or "").strip() == requested_inst_id
        if str(row.get("side") or "").strip().lower() != requested_side:
            continue
        if same_inst_id or _same_leg(row, position_ref):
            return row
    raise PositionManagementError("交易所中已找不到该仓位，请先同步持仓", status_code=409)


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

    snapshot = fetch_account_snapshot(user_id=uid, credential_id=credential_id)
    fresh = _fresh_position(snapshot, position_ref)

    existing = list_managed_positions_for_account(user_id=uid, credential_id=credential_id)
    if any(_same_leg(row, fresh) for row in existing):
        raise PositionManagementError("该仓位已经由策略管理，请先同步持仓", status_code=409)

    size = _positive_number(fresh.get("size"), "持仓数量")
    entry_price = _positive_number(fresh.get("entry_price"), "开仓价")
    mark_price = _positive_number(fresh.get("mark_price"), "最新价格")
    leverage = _positive_number(fresh.get("leverage") or 1, "杠杆倍数")
    side = str(fresh.get("side") or "").strip().lower()
    market_type = _market_type(fresh.get("market_type") or position_ref.get("market_type"))
    symbol = _symbol(fresh.get("symbol"))
    inst_id = str(fresh.get("inst_id") or position_ref.get("inst_id") or "").strip()

    payload = dict(strategy_payload)
    payload.update({
        "user_id": uid,
        "executionMode": "live",
        "credentialId": credential_id,
        "leverageEnabled": leverage > 1,
        "leverage": leverage,
    })
    service = get_strategy_service()
    strategy_id = int(service.create_strategy(payload))
    strategy = service.get_strategy(strategy_id, user_id=uid) or {}
    if not _strategy_monitors_position(strategy, symbol, market_type):
        service.delete_strategy(strategy_id, user_id=uid)
        raise PositionManagementError("策略源码没有订阅当前仓位品种，请选择对应品种的源码", status_code=409)
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
        service.delete_strategy(strategy_id, user_id=uid)
        raise PositionManagementError("创建策略后登记仓位失败", status_code=500) from exc

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
