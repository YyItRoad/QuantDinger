import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.services import exchange_execution
from app.trade_discipline import exit as exits
from app.trade_discipline import observation as observation
from app.trade_discipline import manual as manual
from app.services.live_trading import execution, position_management
from app.trade_discipline import position_evidence
from app.services.live_trading.binance import BinanceFuturesClient
from tests.test_quick_trade_discipline import runtime as entry_runtime, submit
from tests.test_trade_discipline_adoption import runtime as adoption_runtime
from tests.test_trade_discipline_exit import runtime as exit_runtime, worker
from tests.test_trade_discipline_plan_lookup import candidate, lookup
from app.trade_plans.service import PlanService
from app.trade_plans.repository import PlanRepository
from app.services.live_trading.account_snapshot import fetch_target_position_snapshot
from tests.test_trade_plan_app import full_app

SIGNED_REQUEST = BinanceFuturesClient._signed_request
SYMBOL_FILTERS = BinanceFuturesClient.get_symbol_filters
MATCH_PLAN = PlanService.match_at_order_time
PLACE_ORDER = execution.place_order_from_signal
SOURCE_CLASSIFIER = manual.opening_order_origin


@pytest.mark.parametrize("exit_symbol", ["ETHUSDT", "BTCUSDC"])
def test_discipline_loop_on_one_simulated_account(
    monkeypatch, caplog, entry_runtime, adoption_runtime, exit_runtime, discipline_product_catalog, exit_symbol, full_app,
):
    def at(hm):
        return datetime.fromisoformat(f"2026-11-21T{hm}:00+08:00")

    def ms(hm):
        return int(at(hm).timestamp() * 1000)

    def trade(symbol, fill_id, order_id, hm, qty, side="BUY"):
        return dict(symbol=symbol, id=fill_id, orderId=order_id, positionSide="BOTH",
                    time=ms(hm), qty=qty, side=side)

    config = {"exchange_id": "binance", "api_key": "mock", "secret_key": "mock"}
    monkeypatch.setattr(exchange_execution, "resolve_exchange_config", Mock(return_value=config))
    for module in (position_evidence, exits):
        monkeypatch.setattr(module, "resolve_exchange_config", Mock(return_value=config))
    monkeypatch.setattr("app.services.live_trading.account_snapshot.resolve_exchange_config",
                        Mock(return_value=config))
    monkeypatch.setattr(BinanceFuturesClient, "_signed_request", SIGNED_REQUEST)
    exit_runtime.now = at("10:04")
    entry_runtime.config.update(config)
    monkeypatch.setattr("app.trade_discipline.entry_guard.datetime.now", lambda _: exit_runtime.now)
    for key, value in {
        "USER_ID": "7", "CREDENTIAL_ID": "9", "OBSERVATION_ENABLED": "true",
        "AUTO_ADOPT_ENABLED": "true", "ADOPTION_FROM": at("10:00").isoformat(),
        "EXIT_FROM": at("10:00").isoformat(), "AUTO_EXIT_ENABLED": "true",
        "EXTERNAL_OPENINGS_AS_MANUAL": "true",
        "ADOPTION_TEMPLATES_JSON": '{"long":{"source_id":9,"source_version_id":17,"initial_capital":100}}',
    }.items():
        monkeypatch.setenv(f"TRADE_DISCIPLINE_{key}", value)
    plan = candidate(expires=at("16:00"))
    plans = [plan]
    if exit_symbol == "BTCUSDC":
        usdc_plan = candidate(expires=at("16:00"), symbol="BTC/USDC", instrument_id="BTCUSDC", settlement_ccy="USDC")
        plans.append(dict(usdc_plan, plan_id="usdc-plan", version_id="usdc-version"))
        # 模拟真实目录没有 USDC。公开接口返回两份合约，使用实际币安客户端选中原生合约。
        def exchange_info(client, method, path, *, params):
            assert (method, path, params) == ("GET", "/fapi/v1/exchangeInfo", {"symbol": "BTCUSDC"})
            return {"symbols": [
                dict(symbol=f"BTC{quote}", contractType="PERPETUAL", baseAsset="BTC", quoteAsset=quote,
                     marginAsset=quote, quantityPrecision=2, filters=[
                         dict(filterType="MARKET_LOT_SIZE", stepSize="0.01", minQty="0.01"),
                     ])
                for quote in ("USDT", "USDC")
            ]}

        monkeypatch.setattr(BinanceFuturesClient, "_public_request", exchange_info)
        monkeypatch.setattr(BinanceFuturesClient, "get_symbol_filters", SYMBOL_FILTERS)
    entry_runtime.lookup.fetchall.return_value = plans
    entry_runtime.quotes.side_effect = lambda market, symbol, tf, limit, *, before_time, **_kw: [
        {"time": (before_time - 1) // 14400 * 14400 - i * 14400,
         "close": (80 if symbol == "BTC/USDC" else 120) if i == 1 else 100}
        for i in range(100, 0, -1)
    ]
    order_times = {201: "10:04", 202: "10:19", 301: "10:06", 401: "10:24", 402: "10:25",
                   101: "11:00", 102: "12:00"}
    btc = []
    exit_fills = []
    order_reads = []
    exit_rest = BinanceFuturesClient._request

    def venue(client, method, path, *, params, headers):
        symbol = params.get("symbol")
        if path == "/fapi/v2/positionRisk":
            positions = []
            for native, fills in (("BTCUSDT", btc), (exit_symbol, exit_fills)):
                qty = sum(Decimal(f["qty"]) * (1 if f["side"] == "BUY" else -1) for f in fills)
                if qty:
                    positions.append(dict(symbol=native, positionAmt=str(qty), entryPrice="100",
                                          markPrice="120", positionSide="BOTH"))
            return 200, positions, ""
        if path == "/fapi/v1/userTrades":
            return 200, {"BTCUSDT": btc, exit_symbol: exit_fills}[symbol], ""
        if path in {"/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"}:
            return 200, [], ""
        if path == "/fapi/v1/order" and "orderId" in params:
            order_id = int(params["orderId"])
            order_reads.append(order_id)
            return 200, dict(symbol=symbol, orderId=order_id, time=ms(order_times[order_id]),
                             clientOrderId=f"ios_im_fixture_{order_id}"), ""
        if path == "/fapi/v1/order" and method == "POST":
            try:
                return exit_rest(client, method, path, params=params, headers=headers)
            finally:
                if exit_runtime.posts:
                    client_id = exit_runtime.posts[-1][0]
                    order = exit_runtime.orders[client_id]
                    number = order["orderId"]
                    assert order["symbol"] == exit_symbol
                    if not any(fill["orderId"] == number for fill in exit_fills):
                        exit_fills.append(trade(symbol, 900 + number, number, order_times[number],
                                                order["executedQty"], "SELL"))
        return exit_rest(client, method, path, params=params, headers=headers)

    monkeypatch.setattr(BinanceFuturesClient, "_request", venue)
    monkeypatch.setattr(position_management, "fetch_target_position_snapshot",
                        fetch_target_position_snapshot)
    monkeypatch.setattr(position_evidence, "_read_local_fills", lambda uid, cid, symbol: [
        dict(exchange_fill_id=str(fill["id"]), exchange_order_id=str(fill["orderId"]),
             occurred_at=fill["time"], position_side="both", side=fill["side"].lower(),
             quantity=fill["qty"], cumulative_quantity=fill["qty"])
        for fill in (btc if symbol == "BTCUSDT" else [])])
    accepted_orders = {}

    def local_orders(uid, cid, symbol, ids):
        return [dict(exchange_order_id=oid, payload=accepted_orders[oid], source="qd_quick_trades")
                for oid in ids if oid in accepted_orders and accepted_orders[oid]["symbol"] == symbol]

    monkeypatch.setattr(position_evidence, "_read_local_orders", local_orders)
    # Replace the exit unit fixture's synthesized position/evidence/plan with production readers.
    monkeypatch.setattr(exits, "read_account_position_inventory", position_management.read_account_position_inventory)
    monkeypatch.setattr(exits, "read_position_evidence", position_evidence.read_position_evidence)
    # Restore the production method captured before the unit fixture patched it.
    monkeypatch.setattr(PlanService, "match_at_order_time", MATCH_PLAN)

    def mock_accept(**order):
        order_id, hm = (201, "10:05") if not btc else (202, "10:20")
        btc.append(trade("BTCUSDT", order_id + 100, order_id, hm, "0.2"))
        return type("Ack", (), {"exchange_order_id": str(order_id), "filled": 0.2, "avg_price": 100,
                                "raw": dict(symbol="BTCUSDT", orderId=order_id, time=ms(order_times[order_id]))})()

    entry_runtime.market_order.side_effect = mock_accept
    entry_runtime.record.side_effect = lambda **row: (
        accepted_orders.update({row["exchange_order_id"]: row["raw_result"]})
        if row["exchange_order_id"] else None
    ) or 1
    assert submit()[1]["code"] == 1
    exit_runtime.now = at("10:19")
    assert submit()[1]["code"] == 1
    status, rejected = submit(side="sell", order_type="limit")
    assert (status, rejected["data"]["reason"]) == (409, "ema25_direction_blocked")
    entry_runtime.lookup.fetchall.return_value = []
    status, rejected = submit(order_type="limit")
    assert (status, rejected["data"]["reason"]) == (409, "no_effective_plan")
    entry_runtime.lookup.fetchall.return_value = plans
    assert entry_runtime.market_order.call_count == 2 and not entry_runtime.limit_order.called
    assert entry_runtime.record.call_count == 4
    monkeypatch.setattr(execution, "place_order_from_signal", PLACE_ORDER)
    # 快捷交易开仓属于系统单。先保留其真实本地归属记录，再模拟平仓后由客户端
    # 人工重新开仓，避免把系统快捷交易伪装为人工来源来验收接管。
    _, db, _, _ = full_app
    with db.cursor() as cur:
        cur.execute("INSERT INTO qd_users(id,username,password_hash) VALUES (7,'source-e2e','fixture')")
        for order_id in (201, 202):
            cur.execute("""INSERT INTO qd_quick_trades
                (user_id,credential_id,exchange_id,market_type,symbol,exchange_order_id,client_order_id)
                VALUES (7,9,'binance','swap','BTC/USDT',%s,%s)""", (str(order_id), f"qt_fixture_{order_id}"))
    btc[:] = [trade("BTCUSDT", 501, 401, "10:25", "0.2"), trade("BTCUSDT", 502, 402, "10:26", "0.2")]
    exit_fills.append(trade(exit_symbol, 401, 301, "10:07", "0.5"))

    caplog.set_level(logging.INFO, logger=observation.__name__)

    def tick(observer, hm):
        exit_runtime.now = at(hm)
        assert observer.tick(exit_runtime.now)
        observer._thread.join(timeout=10)
        assert not observer._thread.is_alive()
        message = [record.message for record in caplog.records
                   if record.message.startswith("Trade discipline observation ")][-1]
        return {row["instrument"]: row for row in
                json.loads(message.removeprefix("Trade discipline observation "))["positions"]}

    monkeypatch.setattr(position_management, "list_position_inventory_accounts",
                        Mock(return_value=[{"id": 9, "exchange_id": "binance"}]))
    # 使用真实来源分类及隔离 PostgreSQL 的订单表，不注入 manual 结论。
    positions_passed = {}
    monkeypatch.setattr(manual, "opening_order_origin", SOURCE_CLASSIFIER)
    assert manual.opening_order_origin(dict(user_id=7, credential_id=9, exchange_id="binance",
        market_type="swap", environment="live"), "BTCUSDT", "201", "qt_fixture_201") == "system"

    def record_pass(cur, user_id, event_id, plan_id, version_id, round_at, observed_at):
        return positions_passed.setdefault(
            (user_id, event_id), dict(passed_round=round_at, plan_id=plan_id, version_id=version_id),
        )

    monkeypatch.setattr(PlanRepository, "discipline_opening_pass",
                        lambda self, cur, user_id, event_id: positions_passed.get((user_id, event_id)))
    monkeypatch.setattr(PlanRepository, "record_discipline_opening_pass", Mock(side_effect=record_pass))
    monkeypatch.setattr(manual, "load_plan_parameters", Mock(return_value={"fixed_stop_price": 90}))
    observer = observation.hourly_observer_from_env(at("10:30"))
    first = tick(observer, "11:00")
    assert first["BTCUSDT"]["outcome"] == "waiting_for_next_poll"
    assert first["BTCUSDT"]["size"] == 0.4 and first["BTCUSDT"]["management_status"] == "unregistered"
    assert [x["order_id"] for x in first["BTCUSDT"]["plan_checks"]] == ["401"]
    assert all(x["status"] == "matched" for x in first["BTCUSDT"]["plan_checks"])
    assert all(x["status"] == "allowed" and x["bar_closed_at"] == "2026-11-21T00:00:00Z"
               for x in first["BTCUSDT"]["ema_checks"])
    exiting = first[exit_symbol]
    assert exiting["outcome"] == "proposed_exit" and exiting["exit"]["request_id"] == 1
    if exit_symbol == "BTCUSDC":
        assert exiting["plan_checks"][0]["plan_id"] == "usdc-plan"
        assert exiting["ema_checks"][0]["symbol"] == "BTC/USDC"
        assert exiting["reason"] == "ema25_direction_blocked"
        assert exit_runtime.rows[1]["symbol"] == "BTC/USDC"
    else:
        assert exiting["reason"] == "no_plan"
    assert {301, 401, 402} <= set(order_reads) and 201 not in order_reads and 202 not in order_reads
    assert not adoption_runtime.rows

    exit_runtime.timeout, exit_runtime.close_fill = True, "0.2"
    worker()
    assert len(exit_runtime.posts) == 1 and exit_runtime.rows[1]["status"] == exits._SENT
    original_id = exit_runtime.posts[0][0]
    exit_runtime.now = at("11:01")
    exit_runtime.orders[original_id]["status"] = "EXPIRED"
    worker()
    assert exit_runtime.reads == [original_id] and exit_runtime.rows[1]["filled_amount"] == 0.2
    assert len(exit_runtime.posts) == 1
    observer.stop()

    restarted = observation.hourly_observer_from_env(at("11:30"))
    second = tick(restarted, "12:00")
    assert second["BTCUSDT"]["approved_plan"]["version_id"] == "version-2"
    assert second["BTCUSDT"]["adoption"]["strategy_id"] == 44
    assert second["BTCUSDT"]["adoption"]["status"] == "starting"
    assert second["BTCUSDT"]["pending_entry_orders_checked"] is True
    assert adoption_runtime.registered[0]["size"] == 0.4
    adoption_runtime.enqueue.assert_called_once()
    assert second[exit_symbol]["proposed_quantity"] == 0.3
    exit_runtime.timeout, exit_runtime.close_status, exit_runtime.close_fill = False, "FILLED", "0.3"
    worker()
    assert exit_runtime.rows[2]["status"] == "filled" and exit_runtime.rows[2]["filled_amount"] == 0.3
    third = tick(restarted, "13:00")
    assert set(third) == {"BTCUSDT"} and third["BTCUSDT"]["outcome"] == "already_managed"
    assert len(adoption_runtime.rows) == 1 and len(exit_runtime.posts) == 2
    assert [Decimal(quantity) for _, quantity in exit_runtime.posts] == [Decimal("0.5"), Decimal("0.3")]
    restarted.stop()
