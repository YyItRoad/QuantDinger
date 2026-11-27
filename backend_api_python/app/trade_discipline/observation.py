"""Combine the approved read-only discipline checks into an hourly report."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from app.trade_discipline.position_evidence import read_position_evidence
from app.services.live_trading.position_management import read_account_position_inventory
from app.services.live_trading.symbols import to_binance_futures_symbol
from app.utils.logger import get_logger

logger = get_logger(__name__)
_ONE_HOUR = timedelta(hours=1)


def _entry_orders(evidence):
    # 一笔订单可以分多次成交，按订单号去重后再查计划和 EMA。
    # 此处允许回放工具传入加仓事件，Scheduler 的手动开仓判定只传当前开仓事件。
    seen = set()
    for event in evidence["events"]:
        if event["action"] not in {"open", "add", "reverse"} or event["order_id"] in seen:
            continue
        seen.add(event["order_id"])
        yield event["order_id"], "long" if event["side"] == "buy" else "short", event["submitted_at"]


def check_plans(inventory, lookback_hours):
    from app.trade_plans.service import PlanService

    evidence = inventory["evidence"]
    # 计划按真实下单时刻和成交回放确认的原生产品查询，不能跨结算币匹配。
    instrument = evidence.get("product")
    if not instrument:
        return []
    service = PlanService()
    checks = []
    for order_id, direction, submitted_at in _entry_orders(evidence):
        match = service.match_at_order_time(
            inventory["user_id"], instrument=instrument, direction=direction,
            submitted_at=submitted_at, lookback_hours=lookback_hours,
        )
        checks.append(dict(match, order_id=order_id, direction=direction, submitted_at=submitted_at))
    return checks


def check_ema(inventory):
    from app.trade_discipline.ema import check_ema25_at_order_time

    evidence = inventory["evidence"]
    product = evidence.get("product")
    if not product:
        return []
    return [
        dict(check_ema25_at_order_time(
            market=product["market"], symbol=product["symbol"], exchange_id=product["exchange_id"],
            market_type=product["market_type"], direction=direction, submitted_at=submitted_at,
        ), order_id=order_id)
        for order_id, direction, submitted_at in _entry_orders(evidence)
    ]


def _observe_position(inventory, position, evidence, observed_at, lookback_hours, *, include_ema=True, manual_only=False):
    side = position["side"]
    opened_at = evidence["opened_at"].get(side)
    report = {
        "symbol": position["symbol"],
        "instrument": evidence["instrument"],
        "product": evidence.get("product"),
        "side": side,
        "size": position["size"],
        "management_status": position["management_status"],
        "registrations": position.get("registrations", []),
        "evidence_status": evidence["status"],
        "opened_at": opened_at,
    }
    if manual_only and position["management_status"] != "unregistered":
        return dict(report, outcome="already_managed" if position["management_status"] == "registered"
                    else "needs_review", reason="management_registration")
    # 缺少当前仓位的开仓证据时无法判断该查哪个时刻的计划，留给人工复核。
    if not opened_at or evidence["status"] == "unverified":
        return dict(report, outcome="needs_review", reason="opening_evidence_unavailable",
                    evidence_issues=evidence["issues"])

    opened = datetime.fromisoformat(opened_at)
    due = opened + _ONE_HOUR
    if not manual_only:
        report["adopt_eligible_at"] = due.isoformat()
    book = [event for event in evidence["events"] if event["book"] in {"both", side}]
    entry_side = "buy" if side == "long" else "sell"
    # 从后往前定位当前这段仓位的开仓成交，避免同一毫秒多笔成交时取错。
    start = next((index for index in range(len(book) - 1, -1, -1)
                  if book[index]["action"] in {"open", "reverse"} and book[index]["side"] == entry_side), None)
    if opened > observed_at or start is None or book[start]["filled_at"] != opened_at:
        return dict(report, outcome="needs_review", reason="opening_evidence_unavailable")

    report.update(opening_fill_id=book[start].get("fill_id"), last_fill_id=book[-1].get("fill_id"))
    if manual_only:
        from app.trade_discipline.manual import assess_manual_opening

        return assess_manual_opening(inventory, position, book[start:], report, observed_at, lookback_hours)
    evidence = dict(evidence, events=book[start:])
    inputs = dict(inventory, evidence=evidence)
    plans = check_plans(inputs, lookback_hours)
    report["plan_checks"] = plans
    if include_ema:
        report["ema_checks"] = check_ema(inputs)

    if not plans or any(plan["status"] == "unavailable" for plan in plans):
        return dict(report, outcome="needs_review", reason="plan_evidence_unavailable")
    without_plan = [plan for plan in plans if plan["status"] == "no_plan"]
    if without_plan:
        if len(without_plan) != len(plans):
            return dict(report, outcome="needs_review", reason="mixed_plan_attribution")
        return dict(report, outcome="proposed_exit", reason="no_plan", proposed_quantity=position["size"])
    if position["management_status"] == "registered":
        return dict(report, outcome="already_managed")
    if position["management_status"] != "unregistered":
        return dict(report, outcome="needs_review", reason="management_registration_unavailable")
    if observed_at < due:
        return dict(report, outcome="waiting_for_adoption")
    return dict(report, outcome="proposed_adoption", pending_entry_orders_checked=False)


def observe_account(*, user_id, credential_id, observed_at, lookback_hours=None, symbols=(), manual_only=False):
    """Observe current positions and report proposed outcomes without executing them."""
    # 复用持仓页使用的快照服务，随快照读取管理归属。此函数只生成判定结果。
    inventory = read_account_position_inventory(user_id=user_id, credential_id=credential_id, market_type="swap")
    report = {
        "user_id": user_id, "credential_id": credential_id, "observed_at": observed_at.isoformat(),
        "inventory_status": inventory["status"], "snapshot_fetched_at": inventory["fetched_at"],
        "issues": inventory["issues"], "positions": [],
    }
    if manual_only:
        from app.trade_discipline.manual import audit

        audit("snapshot_read", user_id=user_id, credential_id=credential_id, round_at=observed_at,
              status=inventory["status"], position_count=len(inventory["positions"]), issues=inventory["issues"],
              fetched_at=inventory["fetched_at"], exchange_id=inventory["exchange_id"])
        if inventory["exchange_id"] not in {"binance", "binanceusdm", "binancefutures"}:
            return dict(report, outcome="unsupported_account")
    if inventory["status"] != "ok":
        return dict(report, outcome="needs_review", reason="inventory_unavailable")

    by_symbol = {}
    for position in inventory["positions"]:
        symbol = to_binance_futures_symbol(position.get("inst_id") or position["symbol"])
        if symbols and symbol not in symbols:
            continue
        if manual_only and position["management_status"] == "registered":
            from decimal import Decimal

            allocated = sum(Decimal(str(row["size"])) for row in position.get("registrations", [])
                            if row.get("execution_mode") == "live")
            surplus = allocated > 0 and Decimal(str(position["size"])) > allocated
            report["positions"].append(dict(position, instrument=symbol,
                outcome="notify_only" if surplus else "already_managed",
                reason="unallocated_surplus" if surplus else "management_registration"))
            continue
        by_symbol.setdefault(symbol, []).append(position)

    for symbol, positions in by_symbol.items():
        try:
            evidence = read_position_evidence(inventory, symbol)
            if manual_only:
                audit("fill_replay", user_id=user_id, credential_id=credential_id, round_at=observed_at,
                      instrument=symbol, product=evidence.get("product"), status=evidence["status"], issues=evidence["issues"],
                      fill_count=len(evidence["events"]))
            results = [_observe_position(inventory, position, evidence, observed_at, lookback_hours, manual_only=manual_only)
                       for position in positions]
        except Exception:
            logger.warning("Trade discipline could not observe credential=%s instrument=%s", credential_id, symbol)
            results = [{
                "symbol": position["symbol"], "side": position["side"], "size": position["size"],
                "outcome": "needs_review", "reason": "position_evidence_unavailable",
            } for position in positions]
        report["positions"].extend(results)
    return report


def _next_hour(now):
    return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + _ONE_HOUR


class HourlyDisciplineObserver:
    """Trigger at an hour boundary without holding up Scheduler lease renewal."""

    def __init__(self, *, now, adoption=None, exit_from=None, **account):
        self._account = account
        self._adoption = adoption
        self._exit_from = exit_from
        self._next_run = _next_hour(now)
        self._stopped = Event()
        self._thread = None

    def tick(self, now):
        # 停止中、整点未到或上一轮仍在跑时跳过，避免重叠执行。
        if self._stopped.is_set() or now < self._next_run or (self._thread and self._thread.is_alive()):
            return False
        self._next_run = _next_hour(now)
        # 持仓和成交查询可能耗时，放到后台线程以免阻塞 Scheduler 续租。
        self._thread = Thread(target=self._run, args=(now,), name="TradeDisciplineObservation", daemon=True)
        self._thread.start()
        return True

    def _run(self, now):
        try:
            if self._account.get("credential_id") is None:
                from app.services.live_trading.position_management import list_position_inventory_accounts

                from app.trade_discipline.manual import audit

                accounts = list_position_inventory_accounts(user_id=self._account["user_id"])
                audit("round_started", user_id=self._account["user_id"], round_at=now, account_count=len(accounts),
                      external_openings_as_manual=os.getenv("TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL", "false"))
                for account in accounts:
                    if self._stopped.is_set():
                        break
                    # 单个凭证查询失败时继续扫描该用户的其他账户。
                    try:
                        self._observe(now, dict(self._account, credential_id=account["id"], manual_only=True))
                    except Exception:
                        audit("account_failed", user_id=self._account["user_id"], credential_id=account["id"],
                              round_at=now, reason="account_observation_failed")
                        logger.exception("Trade discipline account observation failed credential=%s", account["id"])
                audit("round_finished", user_id=self._account["user_id"], round_at=now,
                      stopped=self._stopped.is_set())
            else:
                self._observe(now, self._account)
        except Exception:
            logger.exception("Trade discipline observation failed")

    def _observe(self, now, account):
        # 顺序是读取和判定、记录首轮通过或发送通知，再按开关执行后续动作。
        report = observe_account(observed_at=now, **account)
        if account.get("manual_only"):
            from app.trade_discipline.manual import apply_poll_window, audit
            from app.trade_discipline.notifications import notify_management_start_failures

            # 已管理仓位仍跳过纪律重判。沿用原管理列表返回值，通知查询内部再限定
            # 本模块的接管标记，避免为通知改动持仓页和 Agent API 的返回字段。
            failed_candidates = {row["strategy_id"] for position in report["positions"]
                                 for row in position.get("registrations", [])}
            notify_management_start_failures(user_id=report["user_id"], credential_id=report["credential_id"],
                                             strategy_ids=failed_candidates)

            for position in report["positions"]:
                if self._stopped.is_set():
                    return
                apply_poll_window(report, position, now, adoption=self._adoption)
                audit("position_decision", user_id=report["user_id"], credential_id=report["credential_id"],
                      round_at=now, instrument=position.get("instrument") or position["symbol"],
                      side=position["side"], outcome=position["outcome"], reason=position.get("reason"),
                      opening_fill_id=position.get("opening_fill_id"))

        # Scheduler 只把拟退出写入快捷交易请求，实际退出和回查由 Trading Worker 处理。
        if self._exit_from is not None:
            from app.trade_discipline.exit import queue_observed_exit

            for position in report["positions"]:
                if self._stopped.is_set():
                    break
                if position["outcome"] == "proposed_exit":
                    position["exit"] = queue_observed_exit(
                        user_id=report["user_id"], credential_id=report["credential_id"],
                        position=position, observed_at=now, exit_from=self._exit_from,
                    )
        # 拟接管交给现有整仓接管流程创建实例，策略启动沿用原有命令链路。
        if self._adoption is not None:
            from app.trade_discipline.adoption import adopt_observed_position

            for position in report["positions"]:
                if self._stopped.is_set():
                    break
                if position["outcome"] == "proposed_adoption":
                    result = adopt_observed_position(
                        user_id=report["user_id"], credential_id=report["credential_id"],
                        position=position, now=now, settings=self._adoption,
                        still_active=lambda: not self._stopped.is_set(),
                    )
                    position["pending_entry_orders_checked"] = result.pop("pending_entry_orders_checked", False)
                    position["adoption"] = result
        if account.get("manual_only"):
            for position in report["positions"]:
                for action in ("exit", "adoption"):
                    if action in position:
                        result = position[action]
                        audit(action, user_id=report["user_id"], credential_id=report["credential_id"],
                              round_at=now, instrument=position["instrument"], side=position["side"],
                              status=result.get("status"), reason=result.get("reason"),
                              request_id=result.get("request_id"), strategy_id=result.get("strategy_id"))
        if not self._stopped.is_set():
            # 完整客户端号码留在现有业务请求中用于恢复，汇总日志仅展示分支与来源线索。
            logged = dict(report, positions=[
                {key: value for key, value in position.items()
                 if key not in {"entry_client_ids", "opening_client_order_id"}}
                for position in report["positions"]
            ])
            logger.info("Trade discipline observation %s", json.dumps(logged, ensure_ascii=False))

    def stop(self):
        self._stopped.set()


def hourly_observer_from_env(now):
    if os.getenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    if os.getenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap").strip().lower() != "swap":
        raise ValueError("Hourly discipline observation requires TRADE_DISCIPLINE_MARKET_TYPE=swap")
    try:
        user_id = int(os.getenv("TRADE_DISCIPLINE_USER_ID", ""))
        # 正常定时任务从指定用户的全部凭证开始，各凭证分别查询当前仓位。
        credential_id = None
        lookback = os.getenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS")
        lookback_hours = int(lookback) if lookback else None
        if user_id <= 0 or (lookback_hours is not None and lookback_hours <= 0):
            raise ValueError()
    except ValueError:
        raise ValueError("Trade discipline user ID and optional plan lookback must be positive integers") from None
    symbols = tuple(to_binance_futures_symbol(symbol) for symbol in
                    os.getenv("TRADE_DISCIPLINE_SYMBOLS", "").split(",") if symbol.strip())
    from app.trade_discipline.adoption import adoption_settings_from_env

    try:
        adoption = adoption_settings_from_env()
    except ValueError as exc:
        logger.error("Trade discipline auto adoption configuration invalid: %s", exc)
        adoption = None
    from app.trade_discipline.exit import exit_start_from_env

    exit_from = exit_start_from_env()
    return HourlyDisciplineObserver(now=now, user_id=user_id, credential_id=credential_id,
                                   lookback_hours=lookback_hours, symbols=symbols, adoption=adoption, exit_from=exit_from)
