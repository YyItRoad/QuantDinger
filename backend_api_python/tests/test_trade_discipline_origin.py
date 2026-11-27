"""在隔离 PostgreSQL 中验证真实归属查询，避免用固定 manual 返回值绕过入口。"""

from datetime import datetime, timezone

import pytest

from app.services.execution_streams.repository import ExecutionEventRepository
from app.services.live_trading.binance import BinanceFuturesClient
from app.trade_discipline import manual, observation, position_evidence
from tests.test_trade_plan_app import full_app


@pytest.fixture
def source_db(full_app, monkeypatch):
    _, db, _, _ = full_app
    monkeypatch.setenv("TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL", "true")
    with db.cursor() as cur:
        cur.execute("""INSERT INTO qd_strategies_trading
            (id,user_id,strategy_name,market_type,exchange_config)
            VALUES (42,1,'source fixture','swap','{"credential_id":901,"exchange_id":"binance"}')""")
    return db


def inventory():
    return dict(user_id=1, credential_id=901, exchange_id="binance", environment="live", market_type="swap")


@pytest.mark.parametrize("source", ["pending_orders", "qd_quick_trades", "grid", "binding"])
@pytest.mark.parametrize("formatted", [False, True])
def test_system_records_exclude_orders_even_without_exchange_ack(source_db, source, formatted, monkeypatch):
    client_id = "ios_im_same_prefix_but_system" if not formatted else "qd_42_123_very_long_local_order_id"
    with source_db.cursor() as cur:
        if source == "pending_orders":
            cur.execute("""INSERT INTO pending_orders
                (user_id,credential_id,exchange_id,market_type,symbol,signal_type,idempotency_key,
                 execution_mode,status,client_order_id)
                VALUES (1,901,'binance','swap','BTC/USDT','open_long','fixture','live','processing',%s)""", (client_id,))
        elif source == "qd_quick_trades":
            cur.execute("""INSERT INTO qd_quick_trades
                (user_id,credential_id,exchange_id,market_type,symbol,status,client_order_id)
                VALUES (1,901,'binance','swap','BTC/USDT','pending',%s)""", (client_id,))
        elif source == "grid":
            cur.execute("""INSERT INTO qd_grid_resting_orders
                (strategy_id,symbol,purpose,side,price,status,client_order_id)
                VALUES (42,'BTC/USDT','long_entry','buy',100,'pending',%s)""", (client_id,))
        else:
            # 不存在的策略编号模拟策略已删除，历史绑定仍须排除系统单。
            cur.execute("""INSERT INTO qd_live_order_bindings
                (user_id,credential_id,exchange_id,market_type,symbol,owner_type,owner_id,strategy_id,client_order_id)
                VALUES (1,901,'binance','swap','BTC/USDT','pending_order',10,999,%s)""", (client_id,))
    monkeypatch.setattr(ExecutionEventRepository, "register_binding", lambda *a, **k: pytest.fail("来源查询必须只读"))
    wire_id = f"x-{BinanceFuturesClient._BROKER_ID}{client_id}"[:36] if formatted else client_id
    result = manual.opening_order_origin(inventory(), "BTCUSDT", "123", wire_id)
    assert result == ("system" if source == "qd_quick_trades" else "strategy")


def test_binding_identifier_retains_earlier_submission_phase(source_db):
    with source_db.cursor() as cur:
        cur.execute("""INSERT INTO qd_live_order_bindings
            (user_id,credential_id,exchange_id,market_type,symbol,owner_type,owner_id,strategy_id,client_order_id)
            VALUES (1,901,'binance','swap','BTC/USDT','pending_order',11,42,'later_market') RETURNING id""")
        binding_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO qd_live_order_binding_identifiers
            (binding_id,identifier_type,identifier_value) VALUES (%s,'client_order_id','earlier_limit')""", (binding_id,))
    assert manual.opening_order_origin(inventory(), "BTCUSDT", "123", "earlier_limit") == "strategy"


@pytest.mark.parametrize("scope_change, symbol", [({"credential_id": 902}, "BTCUSDT"),
                                               ({"user_id": 2}, "BTCUSDT"), ({}, "BTCUSDC")])
def test_complete_scope_prevents_cross_account_or_settlement_matches(source_db, scope_change, symbol):
    with source_db.cursor() as cur:
        cur.execute("""INSERT INTO qd_quick_trades
            (user_id,credential_id,exchange_id,market_type,symbol,exchange_order_id,client_order_id)
            VALUES (1,901,'binance','swap','BTC/USDT','123','ios_im_shared')""")
    assert manual.opening_order_origin(dict(inventory(), **scope_change), symbol, "123", "ios_im_shared") == "manual"


def test_unidentified_submission_defers_external_order(source_db):
    with source_db.cursor() as cur:
        cur.execute("""INSERT INTO pending_orders
            (user_id,credential_id,exchange_id,market_type,symbol,signal_type,idempotency_key,execution_mode,status)
            VALUES (1,901,'binance','swap','BTCUSDT','open_long','unidentified','live','processing')""")
    assert manual.opening_order_origin(inventory(), "BTCUSDT", "123", "ios_im_external") == "unknown"


def test_deleted_strategy_history_is_not_reclassified_as_manual(source_db):
    client_id = f"x-{BinanceFuturesClient._BROKER_ID}original_system_order"
    with source_db.cursor() as cur:
        cur.execute("""INSERT INTO qd_live_order_bindings
            (user_id,credential_id,exchange_id,market_type,symbol,owner_type,owner_id,strategy_id,client_order_id)
            VALUES (1,901,'binance','swap','BTC/USDT','pending_order',99,42,%s)""", (client_id,))
        cur.execute("DELETE FROM qd_strategies_trading WHERE id=42")
        cur.execute("SELECT count(*) FROM qd_live_order_bindings WHERE strategy_id=42")
        assert cur.fetchone()[0] == 0
    assert manual.opening_order_origin(inventory(), "BTCUSDT", "123", client_id) == "unknown"


def test_real_ios_identity_reaches_no_plan_without_origin_override(source_db, monkeypatch):
    from unittest.mock import Mock

    now = datetime(2026, 11, 27, 3, tzinfo=timezone.utc)
    filled = int(now.timestamp() * 1000) - 60_000
    snapshot = dict(inventory(), status="ok", fetched_at=int(now.timestamp()), issues=[], positions=[
        dict(symbol="TRX/USDC", inst_id="TRXUSDC", side="long", size=1,
             management_status="unregistered", registrations=[]),
    ])
    client = Mock(timeout_sec=6)
    client.get_symbol_filters.return_value = {"_meta": dict(symbol="TRXUSDC", contractType="PERPETUAL",
        baseAsset="TRX", quoteAsset="USDC", marginAsset="USDC")}
    client.get_user_trades.return_value = [dict(symbol="TRXUSDC", id=10, orderId=123, positionSide="BOTH",
                                               side="BUY", qty="1", time=filled)]
    client.get_order.return_value = dict(symbol="TRXUSDC", orderId=123, time=filled - 1000,
                                         clientOrderId="ios_im_fixture_complete_id")
    monkeypatch.setattr(observation, "read_account_position_inventory", lambda **kw: snapshot)
    monkeypatch.setattr(position_evidence, "resolve_exchange_config", lambda *a, **kw: {"exchange_id": "binance"})
    monkeypatch.setattr(position_evidence, "create_client", lambda *a, **kw: client)
    market = Mock(side_effect=AssertionError("无计划必须在取行情之前退出"))
    monkeypatch.setattr(observation, "check_ema", market)

    result = observation.observe_account(user_id=1, credential_id=901, observed_at=now, manual_only=True)
    position = result["positions"][0]
    assert position["opening_client_order_id"] == "ios_im_fixture_complete_id"
    assert (position["opening_origin"], position["outcome"], position["reason"]) == ("manual", "proposed_exit", "no_plan")
    assert position["plan_checks"][0]["submitted_at"] == datetime.fromtimestamp((filled - 1000)/1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    market.assert_not_called()
