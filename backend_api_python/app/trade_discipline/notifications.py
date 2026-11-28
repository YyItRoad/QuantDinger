"""纪律关键事件通知：使用原请求与原启动命令判定结果，不驱动任何交易。"""

import json
import os
from datetime import datetime, timezone

from app.trade_discipline.notice_delivery import record_notice, publish_pending_notices
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def _manager_results(user_id, strategy_ids=None):
    # 仅检查本用户的纪律接管实例，普通原策略和最近为人工 stop 的实例均不参与。
    # 实例先创建、命令后入库；无命令时必须有原流程明确的失败日志，不能把创建窗口当失败。
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute("""SELECT s.id,s.strategy_name,s.status AS strategy_status,s.exchange_config,
                    command.id AS command_id,command.command_type,command.status AS command_status
                FROM qd_strategies_trading s LEFT JOIN LATERAL (
                    SELECT id,command_type,status FROM qd_strategy_commands
                    WHERE strategy_id=s.id AND user_id=s.user_id ORDER BY id DESC LIMIT 1
                ) command ON TRUE
                WHERE s.user_id=%s AND s.trading_config->'position_management'->>'discipline_adoption_key' IS NOT NULL
                  AND (%s::int[] IS NULL OR s.id=ANY(%s::int[]))
                  AND ((command.command_type IN ('start','restart') AND command.status IN ('failed','succeeded'))
                    OR (command.id IS NULL AND s.status='stopped' AND EXISTS (
                        SELECT 1 FROM qd_strategy_logs startup_log WHERE startup_log.strategy_id=s.id
                          AND startup_log.level='error'
                          AND startup_log.message='策略已创建并登记仓位，但自动启动失败，请手动启动')))
                  AND (command.status='failed' OR (command.status='succeeded' AND s.status='running')
                    OR command.id IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM qd_strategy_notifications n WHERE n.user_id=s.user_id
                    AND NULLIF(n.payload_json,'')::jsonb->'_discipline_notice'->>'key'=
                        concat('manager:',s.id,':',COALESCE(command.id::text,'initial'),':',COALESCE(command.status,'failed')))
                ORDER BY s.id DESC LIMIT 50""", (user_id, strategy_ids, strategy_ids))
            return list(cur.fetchall())
        finally:
            cur.close()


def _manager_notice(user_id, row, credential_id=None):
    failed = row["command_status"] == "failed" or row["command_id"] is None
    if not failed and row["strategy_status"] != "running":
        return
    command_id = str(row["command_id"] or "initial")
    cfg = _json(row["exchange_config"])
    record_notice(user_id=user_id, strategy_id=row["id"], symbol="",
                  event_key=f"manager:{row['id']}:{command_id}:{'failed' if failed else 'succeeded'}",
                  kind="start_failed" if failed else "start_succeeded",
                  credential_id=credential_id or cfg.get("credential_id"), name=row["strategy_name"],
                  command_id=command_id, failure_key=command_id,
                  reason="strategy_start_failed" if failed else None)


def notify_management_start_failures(*, user_id, credential_id, strategy_ids):
    """保持原调用入口；是否成功启动仍以 Worker 已完成的启动命令为证据。"""
    if not strategy_ids:
        return
    try:
        for row in _manager_results(user_id, list(strategy_ids)):
            if row["command_status"] == "failed" or row["command_id"] is None:
                _manager_notice(user_id, row, credential_id)
    except Exception:
        logger.exception("trade_discipline management_notice_unavailable user_id=%s", user_id)


def notify_unconfirmed_exit(row):
    raw = _json(row.get("raw_result"))
    meta = raw.get("_discipline_exit", {})
    record_notice(user_id=row["user_id"], symbol=row["symbol"], kind="exit_unconfirmed",
                  event_key=f"exit-unknown:{row['id']}:{meta.get('submit_attempt', 0)}",
                  credential_id=row["credential_id"], request_id=row["id"],
                  side=raw.get("_quick_trade", {}).get("close_side"), reason="exit_result_unknown")


def _exit_notice(row, *, recovered=False):
    raw = _json(row.get("raw_result"))
    meta = raw.get("_discipline_exit", {})
    state = raw.get("status")
    filled = row.get("filled_amount") if row.get("filled_amount") is not None else 0
    if (not row.get("exchange_order_id") or str(raw.get("orderId")) != row["exchange_order_id"]
            or not meta.get("instrument") or raw.get("symbol") != meta["instrument"]):
        return
    if state not in {"FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}:
        return
    # 原成交列固定为 NUMERIC(24,8)，与恢复查询的 PostgreSQL 文本表达保持一致，包括零成交。
    marker = f"{meta.get('submit_attempt', 0)}:{state}:{filled:.8f}"
    if meta.get("notice_marker") == marker:
        return
    kind = "exit_filled" if state == "FILLED" else "exit_partial" if filled > 0 else "exit_failed"
    # 恢复时只补原已确认结果，不重新下单；文案标出补记，不伪装成原来已即时通知。
    notice_id = record_notice(
        user_id=row["user_id"], symbol=row["symbol"], kind=kind,
        event_key=f"exit:{row['id']}:{marker}", credential_id=row["credential_id"],
        request_id=row["id"], side=raw.get("_quick_trade", {}).get("close_side"), reason=meta.get("reason"),
        quantity=meta.get("submitted_qty"), filled=filled,
        price=row.get("avg_fill_price") if row.get("avg_fill_price") else None,
        exchange_status=state, recovered=True if recovered else None,
        occurred_at=(datetime.fromtimestamp(int(raw["updateTime"]) / 1000, timezone.utc).isoformat()
                     if raw.get("updateTime") else row.get("created_at")),
    )
    if notice_id:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("""UPDATE qd_quick_trades SET raw_result=jsonb_set(raw_result,
                    '{_discipline_exit,notice_marker}',to_jsonb(%s::text))
                    WHERE id=%s AND user_id=%s AND credential_id=%s AND source='trade_discipline'
                      AND raw_result->>'status'=%s AND filled_amount=%s""",
                            (marker, row["id"], row["user_id"], row["credential_id"], state, filled))
                db.commit()
            finally:
                cur.close()


def notify_exit_request(user_id, credential_id, request_id):
    """先读取实际已入库的原退出请求，拒绝凭调用方的预期数量宣称成交。"""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_quick_trades WHERE id=%s AND user_id=%s AND credential_id=%s AND source='trade_discipline'",
                            (request_id, user_id, credential_id))
                row = cur.fetchone()
            finally:
                cur.close()
        if row:
            _exit_notice(row)
    except Exception:
        logger.exception("trade_discipline exit_notice_unavailable user_id=%s request_id=%s", user_id, request_id)


def notify_observation(report, position):
    """只通知关键分支；常规已管理、系统单跳过和重复观察不逐轮刷消息。"""
    outcome = position.get("outcome")
    key = f"opening:{report['credential_id']}:{position.get('instrument', position['symbol'])}:{position['side']}:{position.get('opening_fill_id') or 'unverified'}"
    facts = dict(credential_id=report["credential_id"], side=position["side"],
                 reason=position.get("reason"), quantity=position.get("size"))
    common = dict(user_id=report["user_id"], symbol=position["symbol"])
    plans = position.get("plan_checks") or []
    plan = position.get("approved_plan") or (plans[0] if plans else {})
    facts["plan_version_id"] = plan.get("version_id")
    if outcome == "waiting_for_next_poll":
        record_notice(**common, **facts, event_key=f"{key}:passed", kind="opening_passed")
    elif outcome == "opening_passed":
        details = dict(facts, reason="adoption_not_configured")
        record_notice(**common, **details, event_key=f"{key}:adoption-not-configured", kind="needs_review")
    elif outcome in {"notify_only", "needs_review"}:
        # 身份尚未核对出的仓位按日、品种及原因去重，防止小时轮询刷屏。
        suffix = "" if position.get("opening_fill_id") else datetime.now(timezone.utc).date().isoformat()
        kind = "mixed_exposure" if outcome == "notify_only" else "needs_review"
        record_notice(**common, **facts, event_key=f"{key}:{kind}:{facts['reason']}:{suffix}", kind=kind)
    for action in ("exit", "adoption"):
        result = position.get(action) or {}
        if action == "exit" and result.get("status") == "queued":
            record_notice(**common, **facts, event_key=f"exit-queued:{result['request_id']}",
                          kind="exit_queued", request_id=result["request_id"])
        if action == "adoption" and result.get("strategy_id"):
            record_notice(**common, **facts, event_key=f"manager-queued:{result['strategy_id']}",
                          kind="adoption_queued", strategy_id=result["strategy_id"])
        if result.get("status") == "needs_review" or (
                result.get("status") == "deferred" and result.get("reason") not in
                {"scheduler_stopped", "adoption_not_due", "adoption_not_eligible", "already_managed"}):
            details = dict(facts, reason=result.get("reason") or "execution_unavailable")
            record_notice(**common, **details, event_key=f"{key}:{action}:{details['reason']}", kind="needs_review")


def notify_account_failure(user_id, credential_id, reason="inventory_unavailable"):
    """账户读取失败不能误当空仓；持续相同故障每天只登记一次人工处理通知。"""
    day = datetime.now(timezone.utc).date().isoformat()
    record_notice(user_id=user_id, event_key=f"account-review:{credential_id}:{reason}:{day}",
                  kind="needs_review", credential_id=credential_id, reason=reason)


def process_discipline_notices(*, still_active):
    """Worker 恢复漏通知并投递有限任务，独立于开平仓开关，不重新驱动交易。"""
    try:
        user_id = int(os.getenv("TRADE_DISCIPLINE_USER_ID", ""))
        if user_id <= 0 or not still_active():
            return
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("""SELECT * FROM qd_quick_trades WHERE user_id=%s AND source='trade_discipline'
                    AND raw_result->'_discipline_exit' IS NOT NULL AND created_at>=NOW()-INTERVAL '7 days'
                    AND raw_result->>'status' IN ('FILLED','PARTIALLY_FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED')
                    AND (raw_result->'_discipline_exit'->>'notice_marker') IS DISTINCT FROM
                        concat(COALESCE(raw_result->'_discipline_exit'->>'submit_attempt','0'),':',raw_result->>'status',':',filled_amount::text)
                    ORDER BY id LIMIT 10""", (user_id,))
                rows = list(cur.fetchall())
            finally:
                cur.close()
        for row in rows:
            if not still_active():
                return
            _exit_notice(row, recovered=True)
        for row in _manager_results(user_id):
            if not still_active():
                return
            _manager_notice(user_id, row)
        publish_pending_notices(user_id, still_active=still_active)
    except Exception:
        logger.exception("trade_discipline notice_poll_unavailable")
