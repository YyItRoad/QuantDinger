"""启动失败通知使用原有命令与站内通知记录，人工停止不自动重启。"""

import pytest

from app.trade_discipline.notifications import notify_management_start_failures
from app.trade_discipline.notifications import notify_unconfirmed_exit
from app.trade_discipline.exit import queue_observed_exit
from tests.test_trade_discipline_exit import CANDIDATE, ROUND, START
from datetime import timedelta
from tests.test_trade_discipline_origin import source_db
from tests.test_trade_plan_app import full_app


@pytest.mark.parametrize("command_type,status,expected", [
    ("start", "failed", 1), ("start", "succeeded", 0), ("stop", "succeeded", 0), (None, None, 1),
])
def test_failure_notice_is_scoped_and_deduplicated(source_db, command_type, status, expected):
    with source_db.cursor() as cur:
        cur.execute("""UPDATE qd_strategies_trading SET status='stopped',
            notification_config='{"language":"zh-CN"}',
            trading_config='{"position_management":{"discipline_adoption_key":"fixture"}}'
            WHERE id=42""")
        if command_type:
            cur.execute("""INSERT INTO qd_strategy_commands
                (user_id,strategy_id,command_type,status,idempotency_key)
                VALUES (1,42,%s,%s,'notice-fixture')""", (command_type, status))
    for _ in range(2):
        notify_management_start_failures(user_id=1, credential_id=901, strategy_ids=[42])
    notify_management_start_failures(user_id=2, credential_id=901, strategy_ids=[42])
    with source_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications WHERE signal_type='discipline_start_failed'")
        assert cur.fetchone()[0] == expected
        cur.execute("SELECT count(*) FROM qd_strategy_commands")
        assert cur.fetchone()[0] == (1 if command_type else 0)


def test_unknown_exit_notifies_once_without_submitting_any_command(source_db):
    row = dict(user_id=1, credential_id=901, id=701, symbol="BTC/USDT")
    notify_unconfirmed_exit(row)
    notify_unconfirmed_exit(row)
    with source_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications WHERE signal_type='discipline_exit_unconfirmed'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM qd_strategy_commands")
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize("status, expected", [("cancelled", "queued"), ("discipline_sent", "existing")])
def test_real_sql_requeues_only_confirmed_zero_fill_terminal(source_db, status, expected):
    def queue(now):
        return queue_observed_exit(user_id=1, credential_id=901, position=CANDIDATE,
                                   observed_at=now, exit_from=START)

    request_id = queue(ROUND)["request_id"]
    with source_db.cursor() as cur:
        cur.execute("""UPDATE qd_quick_trades SET status=%s, client_order_id='original-request',
            exchange_order_id='123', filled_amount=0,
            raw_result=jsonb_set(raw_result,'{status}','"EXPIRED"') WHERE id=%s""", (status, request_id))
    # 同轮不重试。下一轮也只有已确认终态可重新进入队列。
    assert queue(ROUND)["status"] == "existing"
    result = queue(ROUND + timedelta(hours=1))
    assert result["status"] == expected
    if expected == "queued":
        assert result["request_id"] == request_id
        with source_db.cursor() as cur:
            cur.execute("SELECT raw_result, client_order_id FROM qd_quick_trades WHERE id=%s", (request_id,))
            raw, client_id = cur.fetchone()
        assert raw["_discipline_exit"]["submit_attempt"] == 1 and client_id == ""
        assert raw["_discipline_exit"]["attempt_history"][0]["client_order_id"] == "original-request"
        assert queue(ROUND + timedelta(hours=1))["status"] == "existing"
