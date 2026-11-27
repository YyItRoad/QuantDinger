"""Scheduler 检查纪律接管策略的启动失败，复用现有站内通知，不重启策略。"""

import json

from app.services.notifications.email_templates import strategy_messages
from app.services.signal_notifier import SignalNotifier
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def notify_unconfirmed_exit(row):
    """退出提交结果不明时通知人工，同一逻辑请求通知一次并继续原单回查。"""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("""SELECT id FROM qd_strategy_notifications
                    WHERE user_id=%s AND signal_type='discipline_exit_unconfirmed'
                      AND NULLIF(payload_json,'')::jsonb->>'request_id'=%s LIMIT 1""",
                            (row["user_id"], str(row["id"])))
                if cur.fetchone():
                    return
            finally:
                cur.close()
        messages = strategy_messages()
        delivered, _ = SignalNotifier()._notify_browser(
            user_id=row["user_id"], symbol=row["symbol"], channels=["browser"],
            signal_type="discipline_exit_unconfirmed", title=messages["discipline_exit_unconfirmed_title"],
            message=messages["discipline_exit_unconfirmed_body"].format(symbol=row["symbol"], id=row["id"]),
            payload={"credential_id": row["credential_id"], "request_id": str(row["id"])},
        )
        logger.info("trade_discipline exit_unconfirmed user_id=%s request_id=%s delivered=%s",
                    row["user_id"], row["id"], delivered)
    except Exception:
        logger.exception("Trade discipline exit uncertainty notice unavailable request_id=%s", row["id"])


def notify_management_start_failures(*, user_id, credential_id, strategy_ids):
    """同一失败命令只通知一次，人工 stop 命令与普通原项目策略均不参与。"""
    if not strategy_ids:
        return
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("""
                    SELECT s.id, s.strategy_name, s.notification_config,
                           COALESCE(command.id::text, 'initial') AS failure_key
                    FROM qd_strategies_trading s
                    LEFT JOIN LATERAL (
                        SELECT id, command_type, status FROM qd_strategy_commands
                        WHERE strategy_id=s.id AND user_id=s.user_id ORDER BY id DESC LIMIT 1
                    ) command ON TRUE
                    WHERE s.user_id=%s AND s.id=ANY(%s)
                      AND s.trading_config->'position_management'->>'discipline_adoption_key' IS NOT NULL
                      AND ((command.command_type IN ('start', 'restart') AND command.status='failed')
                        OR (command.id IS NULL AND s.status='stopped'))
                      AND NOT EXISTS (
                        SELECT 1 FROM qd_strategy_notifications n
                        WHERE n.user_id=s.user_id AND n.strategy_id=s.id
                          AND n.signal_type='discipline_start_failed'
                          AND NULLIF(n.payload_json,'')::jsonb->>'failure_key'=COALESCE(command.id::text, 'initial'))
                    """, (user_id, list(strategy_ids)))
                failures = cur.fetchall()
            finally:
                cur.close()
        for failure in failures:
            config = failure.get("notification_config") or {}
            if isinstance(config, str):
                config = json.loads(config)
            messages = strategy_messages(config.get("language"))
            fallback = strategy_messages("en-US")
            title = messages.get("discipline_start_failed_title", fallback["discipline_start_failed_title"])
            body = messages.get("discipline_start_failed_body", fallback["discipline_start_failed_body"])
            delivered, _ = SignalNotifier()._notify_browser(
                user_id=user_id, strategy_id=failure["id"], symbol="", channels=["browser"],
                signal_type="discipline_start_failed", title=title,
                message=body.format(name=failure["strategy_name"], id=failure["id"]),
                payload={"credential_id": credential_id, "failure_key": failure["failure_key"]},
            )
            logger.info("trade_discipline management_start_failure user_id=%s credential_id=%s strategy_id=%s delivered=%s",
                        user_id, credential_id, failure["id"], delivered)
    except Exception:
        # 通知异常不改变管理归属，保留失败命令供下一轮重查。
        logger.exception("Trade discipline start failure notification unavailable user_id=%s credential_id=%s",
                         user_id, credential_id)
