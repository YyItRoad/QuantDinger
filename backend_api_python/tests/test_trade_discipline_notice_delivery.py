"""使用真实隔离 PostgreSQL 核对通知事实、租户、并发与逐渠道恢复。"""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.trade_discipline import notifications
from app.trade_discipline.notice_delivery import record_notice, deliver_notice, publish_pending_notices
from app.services.signal_notifier import SignalNotifier
from tests.test_trade_discipline_origin import source_db
from tests.test_trade_plan_app import full_app


@pytest.fixture
def notice_db(source_db):
    with source_db.cursor() as cur:
        cur.execute("""UPDATE qd_users SET notification_settings=%s WHERE id=1""", (json.dumps({
            "language": "zh-CN", "default_channels": ["browser", "webhook", "email"],
            "webhook_url": "https://example.invalid/fixture", "webhook_token": "fixture-secret-never-in-notice",
            "email": "fixture@example.invalid",
        }),))
    return source_db


def create_notice(uid=1, key="fixture-close"):
    return record_notice(user_id=uid, event_key=key, kind="exit_filled", symbol="TRX/USDC",
                         credential_id=901, request_id=701, filled=Decimal("82"), price=Decimal("0.30"))


def read_notice(db, notice_id):
    with db.cursor() as cur:
        cur.execute("SELECT user_id,payload_json FROM qd_strategy_notifications WHERE id=%s", (notice_id,))
        uid, raw = cur.fetchone()
    return uid, json.loads(raw)


def make_due(db, notice_id):
    with db.cursor() as cur:
        cur.execute("""UPDATE qd_strategy_notifications SET payload_json=jsonb_set(payload_json::jsonb,
            '{_discipline_notice,next_at}','0'::jsonb)::text WHERE id=%s""", (notice_id,))


def test_concurrent_business_event_creates_one_record_without_secrets(notice_db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create_notice(), range(2)))
    assert ids[0] == ids[1] and ids[0]
    uid, raw = read_notice(notice_db, ids[0])
    assert uid == 1 and raw["filled"] == "82"
    assert raw["title"] == "纪律退出已全部成交"
    assert "fixture-secret" not in json.dumps(raw)
    assert "example.invalid" not in json.dumps(raw)
    with notice_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications")
        assert cur.fetchone()[0] == 1


def test_wrong_user_cannot_deliver_another_users_notice(notice_db, monkeypatch):
    notice_id = create_notice(uid=2)
    monkeypatch.setattr(SignalNotifier, "_deliver_channels", lambda *a, **k: pytest.fail("跨用户发送"))
    assert deliver_notice(1, notice_id)["status"] == "missing"
    assert read_notice(notice_db, notice_id)[0] == 2


def test_partial_channel_failure_retries_only_failed_channel(notice_db, monkeypatch):
    calls = []

    def transport(self, **kwargs):
        channel = kwargs["channels"][0]
        calls.append(channel)
        return {channel: {"ok": channel != "email" or calls.count("email") > 1,
                          "error": "fixture-secret-must-not-be-persisted"}}

    monkeypatch.setattr(SignalNotifier, "_deliver_channels", transport)
    notice_id = create_notice()
    assert deliver_notice(1, notice_id)["status"] == "pending"
    make_due(notice_db, notice_id)
    assert deliver_notice(1, notice_id)["status"] == "delivered"
    assert deliver_notice(1, notice_id)["status"] == "delivered"
    assert calls == ["webhook", "email", "email"]
    raw = read_notice(notice_db, notice_id)[1]
    assert raw["_discipline_notice"]["results"]["webhook"]["ok"] is True
    assert "fixture-secret" not in json.dumps(raw)


def test_delivery_has_retry_limit_and_preserves_in_app_record(notice_db, monkeypatch):
    calls = []
    monkeypatch.setattr(SignalNotifier, "_deliver_channels", lambda self, **kw:
                        calls.extend(kw["channels"]) or {kw["channels"][0]: {"ok": False}})
    notice_id = create_notice()
    for expected in ["pending", "pending", "failed", "failed"]:
        make_due(notice_db, notice_id)
        assert deliver_notice(1, notice_id)["status"] == expected
    assert calls.count("webhook") == 3 and calls.count("email") == 3
    assert read_notice(notice_db, notice_id)[1]["_discipline_notice"]["attempts"] == 3


def test_channel_opt_out_before_delivery_is_respected(notice_db, monkeypatch):
    notice_id = create_notice()
    with notice_db.cursor() as cur:
        cur.execute("UPDATE qd_users SET notification_settings='{}' WHERE id=1")
    monkeypatch.setattr(SignalNotifier, "_deliver_channels", lambda *a, **k: pytest.fail("退出订阅后仍发送"))
    assert deliver_notice(1, notice_id)["status"] == "skipped"
    assert read_notice(notice_db, notice_id)[1]["_discipline_notice"]["delivered"] == []


def test_legacy_unknown_notice_is_reused_for_external_channels(notice_db):
    old_id = None
    with notice_db.cursor() as cur:
        cur.execute("""INSERT INTO qd_strategy_notifications(user_id,symbol,signal_type,payload_json)
            VALUES(1,'TRX/USDC','discipline_exit_unconfirmed','{"request_id":"701"}') RETURNING id""")
        old_id = cur.fetchone()[0]
    notifications.notify_unconfirmed_exit(dict(user_id=1, credential_id=901, id=701, symbol="TRX/USDC"))
    with notice_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications")
        assert cur.fetchone()[0] == 1
    assert read_notice(notice_db, old_id)[1]["_discipline_notice"]["channels"] == ["webhook", "email"]


def add_exit(db, state="FILLED", filled="82", credential=901):
    raw = {"status": state, "orderId": 123, "symbol": "TRXUSDC",
           "_discipline_exit": {"instrument": "TRXUSDC", "submit_attempt": 0,
                                "submitted_qty": "82", "reason": "no_plan"},
           "_quick_trade": {"close_side": "short"}}
    with db.cursor() as cur:
        cur.execute("""INSERT INTO qd_quick_trades(user_id,credential_id,exchange_id,symbol,side,market_type,
            status,exchange_order_id,filled_amount,avg_fill_price,source,raw_result)
            VALUES(1,%s,'binance','TRX/USDC','buy','swap',%s,'123',%s,0.30,'trade_discipline',%s::jsonb) RETURNING id""",
                    (credential, "filled" if state == "FILLED" else "cancelled", filled, json.dumps(raw)))
        return cur.fetchone()[0]


@pytest.mark.parametrize("state,filled,kind", [("FILLED", "82", "exit_filled"),
                                             ("CANCELED", "40", "exit_partial"),
                                             ("EXPIRED", "0", "exit_failed")])
def test_restart_recovers_confirmed_result_once_without_resubmitting(notice_db, monkeypatch, state, filled, kind):
    from app.trade_discipline import exit as exit_module

    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "1")
    monkeypatch.setattr(notifications, "publish_pending_notices", lambda *a, **k: None)
    monkeypatch.setattr(exit_module, "submit_market_close", lambda *a, **k: pytest.fail("通知恢复重新下单"))
    request_id = add_exit(notice_db, state, filled)
    for _ in range(2):
        notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT signal_type,payload_json FROM qd_strategy_notifications")
        rows = cur.fetchall()
        cur.execute("SELECT raw_result->'_discipline_exit'->>'notice_marker' FROM qd_quick_trades WHERE id=%s", (request_id,))
        assert cur.fetchone()[0]
    assert len(rows) == 1 and rows[0][0] == f"discipline_{kind}"
    assert json.loads(rows[0][1])["recovered"] is True
    assert json.loads(rows[0][1])["request_id"] == request_id


def test_upgraded_fill_precision_does_not_starve_later_recovery_batches(notice_db, monkeypatch):
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "1")
    monkeypatch.setattr(notifications, "publish_pending_notices", lambda *a, **k: None)
    with notice_db.cursor() as cur:
        # 与已有 20260916_fill_accounting 迁移后的生产列类型一致。
        cur.execute("ALTER TABLE qd_quick_trades ALTER COLUMN filled_amount TYPE NUMERIC(38,18)")
    amount = "0.123456789123456789"
    ids = [add_exit(notice_db, filled=amount) for _ in range(12)]
    for _ in range(3):
        notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT payload_json FROM qd_strategy_notifications ORDER BY id")
        payloads = [json.loads(row[0]) for row in cur.fetchall()]
        cur.execute("SELECT raw_result->'_discipline_exit'->>'notice_marker' FROM qd_quick_trades ORDER BY id")
        markers = [row[0] for row in cur.fetchall()]
    assert len(payloads) == 12 and {p["request_id"] for p in payloads} == set(ids)
    assert all(p["filled"] == amount for p in payloads)
    assert markers == [f"0:FILLED:{amount}"] * 12


def test_worker_observes_actual_start_completion_and_never_restarts_it(notice_db, monkeypatch):
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "1")
    monkeypatch.setattr(notifications, "publish_pending_notices", lambda *a, **k: None)
    with notice_db.cursor() as cur:
        cur.execute("""UPDATE qd_strategies_trading SET status='starting',
            trading_config='{"position_management":{"discipline_adoption_key":"fixture"}}' WHERE id=42""")
        cur.execute("""INSERT INTO qd_strategy_commands(user_id,strategy_id,command_type,status,idempotency_key)
            VALUES(1,42,'start','pending','start-fixture') RETURNING id""")
        command_id = cur.fetchone()[0]
    notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications")
        assert cur.fetchone()[0] == 0
        cur.execute("UPDATE qd_strategy_commands SET status='succeeded' WHERE id=%s", (command_id,))
        cur.execute("UPDATE qd_strategies_trading SET status='running' WHERE id=42")
    for _ in range(2):
        notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT signal_type FROM qd_strategy_notifications")
        assert cur.fetchall() == [("discipline_start_succeeded",)]
        cur.execute("SELECT count(*) FROM qd_strategy_commands")
        assert cur.fetchone()[0] == 1


def test_created_manager_without_start_command_is_not_misreported_as_failed(notice_db, monkeypatch):
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "1")
    monkeypatch.setattr(notifications, "publish_pending_notices", lambda *a, **k: None)
    with notice_db.cursor() as cur:
        cur.execute("""UPDATE qd_strategies_trading SET status='stopped',
            trading_config='{"position_management":{"discipline_adoption_key":"fixture"}}' WHERE id=42""")
    notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM qd_strategy_notifications")
        assert cur.fetchone()[0] == 0
        cur.execute("""INSERT INTO qd_strategy_logs(strategy_id,level,message)
            VALUES(42,'error','策略已创建并登记仓位，但自动启动失败，请手动启动')""")
    for _ in range(2):
        notifications.process_discipline_notices(still_active=lambda: True)
    with notice_db.cursor() as cur:
        cur.execute("SELECT signal_type FROM qd_strategy_notifications")
        assert cur.fetchall() == [("discipline_start_failed",)]
        cur.execute("SELECT count(*) FROM qd_strategy_commands")
        assert cur.fetchone()[0] == 0


def test_queue_publication_is_claimed_and_uses_original_event(notice_db, monkeypatch):
    from contextlib import nullcontext
    from app.celery_app import celery_app

    notice_id = create_notice()
    calls = []
    monkeypatch.setattr(celery_app, "connection_for_write", lambda **kw: nullcontext(object()))
    monkeypatch.setattr(celery_app, "send_task", lambda name, **kw: calls.append((name, kw["args"])))
    publish_pending_notices(1, still_active=lambda: True)
    publish_pending_notices(1, still_active=lambda: True)
    assert calls == [("quantdinger.tasks.trade_discipline_notice", [1, notice_id])]
    assert read_notice(notice_db, notice_id)[1]["_discipline_notice"]["attempts"] == 0


def test_mixed_exposure_is_recorded_once_for_the_opening(notice_db):
    report = {"user_id": 1, "credential_id": 901}
    position = {"symbol": "TRX/USDC", "instrument": "TRXUSDC", "side": "short", "size": 82,
                "opening_fill_id": "fixture-open", "outcome": "notify_only", "reason": "mixed_strategy_exposure"}
    for _ in range(3):
        notifications.notify_observation(report, position)
    with notice_db.cursor() as cur:
        cur.execute("SELECT user_id,signal_type FROM qd_strategy_notifications")
        assert cur.fetchall() == [(1, "discipline_mixed_exposure")]


def test_production_probe_reports_actual_ack_without_private_details(notice_db, monkeypatch, capsys):
    import sys
    from pathlib import Path

    notice_id = create_notice()
    monkeypatch.setattr(SignalNotifier, "_deliver_channels", lambda self, **kw:
                        {kw["channels"][0]: {"ok": True}})
    assert deliver_notice(1, notice_id)["status"] == "delivered"
    script = (Path(__file__).resolve().parents[2] / "ops/deploy/deploy-remote.sh").read_text()
    probe = script.split('log "Checking discipline notification delivery (aggregate health only)"', 1)[1]
    probe = probe.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    monkeypatch.setattr(sys, "argv", ["-", "1"])
    exec(compile(probe, "production-notification-probe", "exec"), {})
    text = capsys.readouterr().out
    assert '"external_acknowledged": true' in text and '"pending": false' in text
    assert "example.invalid" not in text and "TRX" not in text and "fixture-secret" not in text


def test_missing_personal_target_never_falls_back_to_global_target(notice_db, monkeypatch):
    with notice_db.cursor() as cur:
        cur.execute("UPDATE qd_users SET notification_settings=%s WHERE id=1",
                    (json.dumps({"default_channels": ["webhook"]}),))
    monkeypatch.setenv("WEBHOOK_URL", "https://example.invalid/global-operations")
    monkeypatch.setattr(SignalNotifier, "_deliver_channels", lambda *a, **k: pytest.fail("不能发送到全局运维目标"))
    notice_id = create_notice()
    assert deliver_notice(1, notice_id)["status"] == "pending"
    result = read_notice(notice_db, notice_id)[1]["_discipline_notice"]["results"]["webhook"]
    assert result == {"ok": False, "skipped": False, "diagnostic": "target_not_configured"}
