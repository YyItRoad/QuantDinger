from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.trade_discipline import adoption as adoption
from app.trade_discipline import observation as observation
from app.services import exchange_execution
from app.services.live_trading import position_management
from app.trade_discipline import position_evidence
from app.services.live_trading.binance import BinanceFuturesClient

_OPENED = "2026-11-21T02:05:00.000Z"
_NOW = datetime.fromisoformat("2026-11-21T03:05:00Z")
_POSITION = {"symbol": "BTC/USDT", "side": "long", "size": 7, "market_type": "swap",
             "entry_price": 99, "mark_price": 100, "leverage": 3, "inst_id": "BTCUSDT"}
_CANDIDATE = {"symbol": "BTC/USDT", "instrument": "BTCUSDT", "side": "long", "size": 7,
              "outcome": "proposed_adoption", "opened_at": _OPENED, "opening_fill_id": "100", "last_fill_id": "100"}
_SETTINGS = {"from": datetime.fromisoformat("2026-11-21T01:00:00Z"),
             "templates": {"long": {"source_id": 9, "source_version_id": 17, "initial_capital": 100}}}


def _trade(trade_id=100, *, side="BUY", time=_OPENED):
    return {"id": trade_id, "orderId": trade_id + 100, "symbol": "BTCUSDT", "positionSide": "BOTH",
            "side": side, "qty": "7", "time": int(datetime.fromisoformat(time).timestamp() * 1000)}


@pytest.fixture
def runtime(monkeypatch):
    config = {"exchange_id": "binance", "api_key": "fixture", "secret_key": "fixture"}
    monkeypatch.setattr(exchange_execution, "resolve_exchange_config", Mock(return_value=config))
    monkeypatch.setattr(adoption, "resolve_exchange_config", Mock(return_value=config))
    monkeypatch.setattr(position_evidence, "resolve_exchange_config", Mock(return_value=config))

    fixture = SimpleNamespace(orders=[], algo=[], fills=[_trade()], rows=[], registered=[], start=None, version=17)

    def exchange_read(method, path, *, params):
        assert method == "GET"
        if path == "/fapi/v2/positionRisk":
            return [{"symbol": "BTCUSDT", "positionAmt": "7", "entryPrice": "99", "markPrice": "100"}]
        assert params["symbol"] == "BTCUSDT"
        return {"/fapi/v1/userTrades": fixture.fills, "/fapi/v1/openOrders": fixture.orders,
                "/fapi/v1/openAlgoOrders": fixture.algo}[path]

    fixture.request = Mock(side_effect=exchange_read)
    monkeypatch.setattr(BinanceFuturesClient, "_signed_request", fixture.request)
    monkeypatch.setattr(position_evidence, "_read_local_fills", Mock(return_value=[{
        "exchange_fill_id": "100", "exchange_order_id": "200", "occurred_at": datetime.fromisoformat(_OPENED),
        "side": "buy", "position_side": "both", "quantity": "7", "cumulative_quantity": "7",
    }]))
    monkeypatch.setattr(position_evidence, "_read_local_orders", Mock(side_effect=AssertionError("No order backfill")))
    fixture.snapshot = Mock(return_value={
        "swap_positions": [dict(_POSITION)], "fetched_at": int(_NOW.timestamp()), "exchange_id": "binance",
    })
    monkeypatch.setattr(position_management, "fetch_target_position_snapshot", fixture.snapshot)
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", Mock(side_effect=lambda **_: fixture.registered))

    lock = Lock()

    @contextmanager
    def leg_lock(**kwargs):
        with lock:
            yield

    monkeypatch.setattr(position_management, "_management_leg_lock", leg_lock)
    cursor = Mock()

    def recover():
        user_id, key = cursor.execute.call_args.args[1]
        return [dict(strategy_id=row["id"], status=row["status"], start_command_status=fixture.start)
                for row in fixture.rows if user_id == row["user_id"]
                and row["position_management"]["discipline_adoption_key"] == key]

    cursor.fetchall.side_effect = recover

    @contextmanager
    def db():
        yield Mock(cursor=Mock(return_value=cursor))

    monkeypatch.setattr(position_management, "get_db_connection", db)
    fixture.cursor = cursor
    fixture.service = Mock()

    def create(payload):
        new_id = 44 + len(fixture.rows)
        fixture.rows.append({"id": new_id, "user_id": payload["user_id"], "status": "stopped",
                             "source_version_id": fixture.version, "position_management": payload["positionManagement"]})
        return new_id

    def update(strategy_id, status, *, user_id):
        for row in fixture.rows:
            if row["id"] == strategy_id and row["user_id"] == user_id:
                row["status"] = status
                return True
        return False

    fixture.service.create_strategy.side_effect = create
    fixture.service.get_strategy.side_effect = lambda strategy_id, **_: next(
        (row for row in fixture.rows if row["id"] == strategy_id), None,
    )
    fixture.service.update_strategy_status.side_effect = update

    def delete(strategy_id, *, user_id):
        fixture.rows[:] = [row for row in fixture.rows if (row["id"], row["user_id"]) != (strategy_id, user_id)]
        return True

    fixture.service.delete_strategy.side_effect = delete
    monkeypatch.setattr(position_management, "get_strategy_service", Mock(return_value=fixture.service))
    fixture.upsert = Mock(side_effect=lambda **row: fixture.registered.append(row))
    monkeypatch.setattr(position_management, "upsert_position", fixture.upsert)
    fixture.enqueue = Mock(side_effect=lambda **_: setattr(fixture, "start", "pending"))
    monkeypatch.setattr(position_management, "StrategyCommandRepository", Mock(return_value=Mock(enqueue=fixture.enqueue)))
    monkeypatch.setattr(position_management, "append_strategy_log", Mock())
    return fixture


def adopt(candidate=None, *, now=_NOW, active=lambda: True):
    return adoption.adopt_observed_position(
        user_id=3, credential_id=7, position=candidate or dict(_CANDIDATE), now=now,
        settings=_SETTINGS, still_active=active,
    )


def test_adoption_reuses_existing_creation_registration_and_start_on_repeat(runtime):
    first = adopt()
    second = adopt()

    assert first["status"] == "starting"
    assert first["pending_entry_orders_checked"] is True
    assert second == {"strategy_id": 44, "status": "running", "start_command_status": "pending",
                      "recovered": True, "adoption_key": first["adoption_key"]}
    runtime.service.create_strategy.assert_called_once()
    payload = runtime.service.create_strategy.call_args.args[0]
    assert payload["positionManagement"]["discipline_adoption_key"] == first["adoption_key"]
    assert payload["sourceId"] == 9 and payload["directionMode"] == "long_only"
    assert "entry_reason" not in payload["positionManagement"]
    runtime.upsert.assert_called_once()
    runtime.enqueue.assert_called_once_with(
        strategy_id=44, user_id=3, command_type="start", idempotency_key="position-management:start:44",
    )
    assert [call.args for call in runtime.request.call_args_list] == [
        ("GET", "/fapi/v1/userTrades"), ("GET", "/fapi/v1/openOrders"), ("GET", "/fapi/v1/openAlgoOrders"),
    ]


def test_concurrent_candidates_serialize_to_one_instance(runtime):
    barrier = Barrier(2)
    snapshot = runtime.snapshot.return_value

    def read(**_):
        barrier.wait(timeout=5)
        return snapshot

    runtime.snapshot.side_effect = read
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: adopt(), range(2)))

    assert {first["strategy_id"], second["strategy_id"]} == {44}
    assert sum(result.get("recovered", False) for result in (first, second)) == 1
    runtime.service.create_strategy.assert_called_once()
    runtime.enqueue.assert_called_once()


def test_start_failure_returns_and_recovers_same_stopped_instance(runtime):
    runtime.enqueue.side_effect = RuntimeError("simulated start command failure")
    first = adopt()
    runtime.snapshot.return_value = {"swap_positions": []}
    recovered = adopt()

    assert first["strategy_id"] == recovered["strategy_id"] == 44
    assert first["status"] == recovered["status"] == "stopped"
    assert first["warning"]
    assert recovered["recovered"] is True and recovered["start_command_status"] is None
    runtime.service.create_strategy.assert_called_once()
    runtime.upsert.assert_called_once()
    runtime.enqueue.assert_called_once()
    runtime.snapshot.assert_called_once()


def test_ambiguous_creation_result_is_resolved_to_original_instance(runtime):
    original_create = runtime.service.create_strategy.side_effect

    def fail_after_insert(payload):
        original_create(payload)
        raise TimeoutError("lost create response")

    runtime.service.create_strategy.side_effect = fail_after_insert
    first = adopt()
    repeated = adopt()

    assert first == repeated
    assert first["strategy_id"] == 44 and first["status"] == "stopped" and first["recovered"] is True
    runtime.service.create_strategy.assert_called_once()
    runtime.upsert.assert_not_called()
    runtime.enqueue.assert_not_called()


@pytest.mark.parametrize("order_class", ["orders", "algo"])
def test_remaining_open_orders_defer_adoption(runtime, order_class):
    setattr(runtime, order_class, [{"symbol": "BTCUSDT", "side": "BUY", "positionSide": "BOTH", "reduceOnly": False}])

    assert adopt() == {"status": "deferred", "reason": "entry_order_pending", "pending_entry_orders_checked": True}
    runtime.service.create_strategy.assert_not_called()


@pytest.mark.parametrize("order_class", ["orders", "algo"])
def test_unexpected_order_response_is_not_an_empty_snapshot(runtime, order_class):
    setattr(runtime, order_class, {"unexpected": []})

    assert adopt() == {"status": "deferred", "reason": "open_orders_unavailable"}
    runtime.service.create_strategy.assert_not_called()


def test_reduce_only_and_close_position_orders_do_not_block(runtime):
    runtime.orders = [{"symbol": "BTCUSDT", "side": "SELL", "positionSide": "BOTH", "reduceOnly": True}]
    runtime.algo = [{"symbol": "BTCUSDT", "side": "SELL", "positionSide": "BOTH", "closePosition": True}]

    assert adopt()["status"] == "starting"


def test_equal_size_close_and_reopen_is_a_different_position(runtime):
    runtime.fills += [_trade(101, side="SELL", time="2026-11-21T02:20:00Z"),
                      _trade(102, time="2026-11-21T02:20:01Z")]

    assert adopt() == {"status": "deferred", "reason": "position_evidence_changed"}
    runtime.service.create_strategy.assert_not_called()


def test_existing_manager_prevents_new_instance(runtime):
    runtime.registered = [dict(_POSITION, strategy_id=7)]

    assert adopt()["status"] == "needs_review"
    runtime.service.create_strategy.assert_not_called()
    runtime.request.assert_not_called()


def test_fresh_size_change_defers_before_querying_orders(runtime):
    runtime.snapshot.return_value["swap_positions"][0]["size"] = 6

    assert adopt() == {"status": "deferred", "reason": "position_changed"}
    runtime.service.create_strategy.assert_not_called()
    runtime.request.assert_not_called()


def test_less_than_one_hour_does_not_read_or_create(runtime):
    early = datetime.fromisoformat("2026-11-21T03:04:59Z")
    assert adopt(now=early) == {"status": "deferred", "reason": "adoption_not_due"}
    runtime.snapshot.assert_not_called()
    runtime.service.create_strategy.assert_not_called()


def test_changed_source_version_never_registers_or_starts(runtime):
    runtime.version = 18

    assert adopt()["status"] == "needs_review"
    runtime.service.delete_strategy.assert_called_once_with(44, user_id=3)
    runtime.upsert.assert_not_called()
    runtime.enqueue.assert_not_called()


def test_observer_with_default_adoption_switch_only_reports(monkeypatch, runtime):
    monkeypatch.setenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "true")
    monkeypatch.delenv("TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED", raising=False)
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "7")
    monkeypatch.setenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap")
    monkeypatch.setattr(observation, "observe_account", Mock(return_value={
        "user_id": 3, "credential_id": 7, "positions": [dict(_CANDIDATE)],
    }))
    monkeypatch.setattr(position_management, "list_position_inventory_accounts",
                        Mock(return_value=[{"id": 7, "exchange_id": "binance"}]))
    observer = observation.hourly_observer_from_env(_NOW)
    observer._run(_NOW)

    runtime.service.create_strategy.assert_not_called()
    runtime.request.assert_not_called()


def test_enabled_observer_hands_only_adoption_candidates_to_manager(monkeypatch, runtime):
    monkeypatch.setenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "true")
    monkeypatch.setenv("TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED", "true")
    monkeypatch.setenv("TRADE_DISCIPLINE_ADOPTION_FROM", "2026-11-21T09:00:00+08:00")
    monkeypatch.setenv("TRADE_DISCIPLINE_ADOPTION_TEMPLATES_JSON",
                       '{"long":{"source_id":9,"source_version_id":17,"initial_capital":100}}')
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "7")
    monkeypatch.setenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap")
    report = {"user_id": 3, "credential_id": 7, "positions": [
        dict(_CANDIDATE, outcome="proposed_exit"), dict(_CANDIDATE, approved_plan={"plan_id": "p", "version_id": "v"}),
    ]}
    monkeypatch.setattr(observation, "observe_account", Mock(return_value=report))
    monkeypatch.setattr("app.trade_discipline.manual.load_plan_parameters", Mock(return_value={}))

    monkeypatch.setattr(position_management, "list_position_inventory_accounts",
                        Mock(return_value=[{"id": 7, "exchange_id": "binance"}]))
    observer = observation.hourly_observer_from_env(_NOW)
    observer._run(_NOW)

    assert "adoption" not in report["positions"][0]
    assert report["positions"][1]["adoption"]["strategy_id"] == 44
    assert report["positions"][1]["pending_entry_orders_checked"] is True
    runtime.enqueue.assert_called_once()
