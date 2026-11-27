"""Opt-in handoff from a discipline observation to existing whole-position management."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta
from decimal import Decimal

from app.services.exchange_execution import resolve_exchange_config
from app.services.live_trading.binance import BinanceFuturesClient
from app.services.live_trading.factory import create_client
from app.trade_discipline.position_evidence import read_position_evidence
from app.services.live_trading.position_management import (
    create_managed_strategy,
    find_discipline_adoption,
)
from app.utils.logger import get_logger
from app.services.settings.capabilities import is_live_trading_enabled

logger = get_logger(__name__)


class AdoptionDeferred(ValueError):
    pass


def _opening_order_pending(client, instrument):
    try:
        orders = client.get_open_orders(symbol=instrument, strict=True) + client.get_open_algo_orders(symbol=instrument)
    except Exception as exc:
        raise AdoptionDeferred("open_orders_unavailable") from exc
    for order in orders:
        if order.get("symbol") != instrument:
            raise AdoptionDeferred("open_orders_unavailable")
        if order.get("reduceOnly") is True or order.get("closePosition") is True:
            continue
        if (order.get("positionSide"), order.get("side")) in {("LONG", "SELL"), ("SHORT", "BUY")}:
            continue
        return True
    return False


def adopt_observed_position(*, user_id, credential_id, position, now, settings, still_active):
    """Recheck one current leg and create at most one manager for its opening fill."""
    if position.get("outcome") != "proposed_adoption" or not still_active():
        return {"status": "deferred", "reason": "adoption_not_eligible"}
    # 复用原系统全局执行权限。已有观察结果不能绕过当前关闭的执行开关。
    if not is_live_trading_enabled():
        return {"status": "deferred", "reason": "live_trading_disabled"}
    side = position["side"]
    template = settings["templates"].get(side)
    if not template:
        return {"status": "deferred", "reason": "template_not_configured"}
    opened = datetime.fromisoformat(position["opened_at"])
    if opened < settings["from"]:
        return {"status": "deferred", "reason": "opened_before_adoption_enabled"}
    if not position.get("approved_plan") and now < opened + timedelta(hours=1):
        return {"status": "deferred", "reason": "adoption_not_due"}
    instrument = position["instrument"]
    opening_fill = position.get("opening_fill_id")
    if not opening_fill or not position.get("last_fill_id"):
        return {"status": "deferred", "reason": "position_evidence_unavailable"}
    # 同一账户、合约、方向和开仓成交生成同一个接管键，重复触发可找回原实例。
    key = hashlib.sha256(
        f"{credential_id}|binance|swap|{instrument}|{side}|{opening_fill}".encode()
    ).hexdigest()
    orders_checked = False

    # 创建实例前重新核对仓位、成交和未成交开仓单，避免接管已经变化的仓位。
    def verify_snapshot(snapshot, fresh):
        nonlocal orders_checked
        if (snapshot.get("error") or snapshot.get("partial") or snapshot.get("warnings")
                or fresh.get("inst_id") != instrument or fresh.get("side") != side
                or Decimal(str(fresh["size"])) != Decimal(str(position["size"]))):
            raise AdoptionDeferred("position_changed")
        current = read_position_evidence(
            {"user_id": user_id, "credential_id": credential_id, "market_type": "swap",
             "exchange_id": snapshot["exchange_id"], "status": "ok", "fetched_at": snapshot["fetched_at"],
             "positions": snapshot["swap_positions"]},
            instrument, verify_current=True,
        )
        book = [event for event in current["events"] if event["book"] in {"both", side}]
        first = next((event for event in reversed(book)
                      if event["action"] in {"open", "reverse"}
                      and event["side"] == ("buy" if side == "long" else "sell")), None)
        if (current["status"] != "reconciled" or not first or first["fill_id"] != opening_fill
                or current["opened_at"].get(side) != position["opened_at"]
                or book[-1]["fill_id"] != position["last_fill_id"]):
            raise AdoptionDeferred("position_evidence_changed")
        try:
            config = resolve_exchange_config({"credential_id": credential_id}, user_id=user_id)
            client = create_client(config, market_type="swap")
            if not isinstance(client, BinanceFuturesClient):
                raise ValueError("Unsupported adoption account")
            client.timeout_sec = min(client.timeout_sec, 6.0)
        except Exception as exc:
            raise AdoptionDeferred("open_orders_unavailable") from exc
        # 未成交开仓单仍可能改变仓位数量和归属，这种情况本轮暂缓接管。
        pending = _opening_order_pending(client, instrument)
        orders_checked = True
        if pending:
            raise AdoptionDeferred("entry_order_pending")
        if not still_active() or not is_live_trading_enabled():
            raise AdoptionDeferred("scheduler_stopped")

    # 使用开仓判定通过时记录的计划版本生成并校验接管参数。
    params = template.get("params", {})
    if position.get("approved_plan"):
        from app.trade_discipline.manual import load_plan_parameters

        from app.trade_discipline.manual import audit

        context = dict(user_id=user_id, credential_id=credential_id, instrument=instrument, side=side,
                       round_at=now, opening_fill_id=opening_fill,
                       plan_version_id=position["approved_plan"]["version_id"])
        audit("adoption_parameter_start", **context)
        try:
            params = load_plan_parameters(user_id, position["approved_plan"], template, side)
        except Exception as exc:
            audit("adoption_parameter_failed", **context, error_type=type(exc).__name__)
            return {"status": "needs_review", "reason": "plan_parameters_unavailable"}
        audit("adoption_parameter_validated", **context, fixed_stop_price=params.get("fixed_stop_price"))
    try:
        # 复用整仓接管，以既定模板版本创建管理实例。实际启动交给现有策略命令。
        result = create_managed_strategy(
            user_id=user_id,
            position_ref={"credential_id": credential_id, "market_type": "swap",
                          "symbol": position["symbol"], "inst_id": instrument, "side": side},
            strategy_payload={
                "sourceId": template["source_id"], "initialCapital": template["initial_capital"],
                "name": f"Discipline {instrument} {side} {opening_fill}",
                "params": params, "timeframe": template.get("timeframe"),
                "directionMode": f"{side}_only",
            },
            discipline_adoption_key=key,
            expected_source_version_id=template["source_version_id"],
            verify_snapshot=verify_snapshot,
        )
    except AdoptionDeferred as exc:
        return {"status": "deferred", "reason": str(exc), **({"pending_entry_orders_checked": True} if orders_checked else {})}
    except Exception:
        logger.warning("Discipline adoption could not finish credential=%s instrument=%s", credential_id, instrument)
        # 实例可能已经创建成功。异常后用接管键找回原结果，避免重复创建。
        try:
            existing = find_discipline_adoption(user_id=user_id, adoption_key=key)
        except Exception:
            existing = None
        if existing:
            return dict(existing, adoption_key=key)
        return {"status": "needs_review", "reason": "adoption_failed", "adoption_key": key}
    if result.get("status") == "stopped" and result.get("strategy_id"):
        from app.trade_discipline.notifications import notify_management_start_failures

        notify_management_start_failures(user_id=user_id, credential_id=credential_id,
                                         strategy_ids=[result["strategy_id"]])
    return dict(result, adoption_key=key, **({"pending_entry_orders_checked": True} if orders_checked else {}))


def adoption_settings_from_env():
    if os.getenv("TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        begins = datetime.fromisoformat(os.getenv("TRADE_DISCIPLINE_ADOPTION_FROM", ""))
        templates = json.loads(os.getenv("TRADE_DISCIPLINE_ADOPTION_TEMPLATES_JSON", ""))
        if begins.tzinfo is None or not isinstance(templates, dict) or not templates:
            raise ValueError()
        for side, template in templates.items():
            if side not in {"long", "short"} or not isinstance(template, dict):
                raise ValueError()
            for name in ("source_id", "source_version_id"):
                if type(template.get(name)) is not int or template[name] <= 0:
                    raise ValueError()
            capital = template.get("initial_capital")
            if type(capital) not in {int, float} or not math.isfinite(capital) or capital <= 0:
                raise ValueError()
            if not isinstance(template.get("params", {}), dict):
                raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Auto adoption requires a timezone-aware start and approved, versioned templates") from None
    return {"from": begins, "templates": templates}
