import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

import app
from app import startup
from app.commands import scheduler
from app.runtime import process
from app.trade_discipline import ema as trade_discipline_ema
from app.trade_discipline import observation
from app.services.live_trading import position_management
from app.trade_discipline import position_evidence
from app.services import strategy_command_repository
from app.trade_plans.repository import PlanRepository
from tests.test_trade_discipline_inventory import inventory_sources

pytestmark = pytest.mark.usefixtures("discipline_product_catalog")


def at(hour, minute=0):
    return datetime.fromisoformat(f"2026-11-21T{hour:02}:{minute:02}:00+08:00")


def stub_existing_reads(monkeypatch, inventory_sources):
    _, _, exchange = inventory_sources
    snapshot = position_management.fetch_target_position_snapshot

    def fixed_snapshot(**kwargs):
        return dict(snapshot(**kwargs), fetched_at=int(at(12).timestamp()))

    monkeypatch.setattr(position_management, "fetch_target_position_snapshot", fixed_snapshot)
    submitted = at(10, 4)
    filled = at(10, 5)
    order_time = int(submitted.timestamp() * 1000)

    def local_fills(_user, _credential, symbol):
        return [{
            "exchange_fill_id": "1", "exchange_order_id": "11", "occurred_at": filled,
            "position_side": "both", "side": "buy" if symbol == "BTCUSDT" else "sell",
            "quantity": "0.25" if symbol == "BTCUSDT" else "0.5", "cumulative_quantity": "0",
        }]

    def local_orders(_user, _credential, symbol, ids):
        assert ids == ["11"]
        return [{"exchange_order_id": "11", "source": "pending_orders",
                 "payload": {"symbol": symbol, "orderId": 11, "time": order_time,
                             "clientOrderId": "system_order_11"}}]

    monkeypatch.setattr(position_evidence, "_read_local_fills", local_fills)
    monkeypatch.setattr(position_evidence, "_read_local_orders", local_orders)
    cursor = Mock()
    cursor.fetchall.return_value = [{
        "plan_id": "btc-plan", "version_id": "v1", "version_no": 1,
        "version_created_at": at(9).isoformat(), "observing_since": at(9, 5).isoformat(),
        "snapshot": {
            "instrument": {
                "market": "Crypto", "exchange_id": "binance", "market_type": "swap",
                "symbol": "BTC/USDT", "instrument_id": "BTCUSDT", "settlement_ccy": "USDT",
            },
            "expires_at": at(16).isoformat(),
        },
    }]
    management_cursor = inventory_sources[1]
    management_cursor.fetchall.return_value = []

    @contextmanager
    def transaction(self):
        yield cursor

    monkeypatch.setattr(PlanRepository, "transaction", transaction)
    last_close = int(submitted.timestamp()) // 14400 * 14400
    market_read = Mock(return_value=[
        {"time": last_close - index * 14400, "close": 120 if index == 1 else 100}
        for index in range(100, 0, -1)
    ])
    monkeypatch.setattr(trade_discipline_ema.DataSourceFactory, "get_kline", market_read)
    return exchange, cursor, market_read


def test_hourly_round_and_restart_use_trade_time(monkeypatch, inventory_sources, caplog):
    exchange, plan_cursor, market = stub_existing_reads(monkeypatch, inventory_sources)
    monkeypatch.setenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "true")
    monkeypatch.delenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS", raising=False)
    monkeypatch.delenv("TRADE_DISCIPLINE_SYMBOLS", raising=False)
    caplog.set_level(logging.INFO, logger=observation.__name__)

    first = observation.HourlyDisciplineObserver(now=at(10, 30), user_id=3, credential_id=7)
    assert first.tick(at(10, 59)) is False
    assert first.tick(at(11)) is True
    first._thread.join(timeout=10)
    assert first.tick(at(11, 30)) is False
    first.stop()
    assert first.tick(at(12)) is False

    restarted = observation.HourlyDisciplineObserver(now=at(11, 30), user_id=3, credential_id=7)
    assert restarted.tick(at(11, 59)) is False
    assert restarted.tick(at(12)) is True
    restarted._thread.join(timeout=10)
    restarted.stop()

    prefix = "Trade discipline observation "
    reports = [json.loads(record.message.removeprefix(prefix)) for record in caplog.records
               if record.message.startswith(prefix)]
    assert len(reports) == 2
    btc_first, eth_first = reports[0]["positions"]
    btc_second, eth_second = reports[1]["positions"]
    assert (btc_first["outcome"], btc_second["outcome"]) == ("waiting_for_adoption", "proposed_adoption")
    assert btc_first["opened_at"] == btc_second["opened_at"] == "2026-11-21T02:05:00.000Z"
    assert btc_first["adopt_eligible_at"] == btc_second["adopt_eligible_at"] == "2026-11-21T03:05:00+00:00"
    assert btc_second["pending_entry_orders_checked"] is False
    assert eth_first["outcome"] == eth_second["outcome"] == "proposed_exit"
    assert eth_first["proposed_quantity"] == eth_second["proposed_quantity"] == 0.5
    assert eth_first["ema_checks"][0]["status"] == "blocked"
    assert all(call.args == ("GET", "/fapi/v2/positionRisk") for call in exchange.call_args_list)
    assert exchange.call_count == 2
    assert plan_cursor.execute.call_count == market.call_count == 4


def test_eligibility_at_exact_hour_and_symbol_scope(monkeypatch, inventory_sources):
    exchange, _, _ = stub_existing_reads(monkeypatch, inventory_sources)

    report = observation.observe_account(
        user_id=3, credential_id=7, observed_at=at(11, 5), symbols=("BTCUSDT",),
    )

    assert [(row["symbol"], row["outcome"]) for row in report["positions"]] == [("BTC/USDT", "proposed_adoption")]
    exchange.assert_called_once_with("GET", "/fapi/v2/positionRisk", params={})


def test_unavailable_plan_is_not_an_exit_candidate(monkeypatch, inventory_sources):
    _, cursor, _ = stub_existing_reads(monkeypatch, inventory_sources)
    cursor.execute.side_effect = RuntimeError("mock database timeout")

    report = observation.observe_account(user_id=3, credential_id=7, observed_at=at(12))

    assert [position["outcome"] for position in report["positions"]] == ["needs_review", "needs_review"]
    assert report["positions"][0]["plan_checks"][0]["reason"] == "plan_query_failed"


def test_preceding_closed_cycle_is_not_checked(monkeypatch):
    end = at(11, 5)
    opened = end - timedelta(hours=1)
    prior = opened - timedelta(hours=2)
    check = Mock(return_value=[{"status": "matched"}])
    monkeypatch.setattr(observation, "check_plans", check)
    monkeypatch.setattr(observation, "check_ema", Mock(return_value=[]))
    position = {"side": "long", "symbol": "BTC/USDT", "size": 1, "management_status": "unregistered"}
    events = [
        {"order_id": "1", "action": "open", "side": "buy", "book": "both", "filled_at": prior.isoformat()},
        {"order_id": "2", "action": "close", "side": "sell", "book": "both", "filled_at": opened.isoformat()},
        {"order_id": "3", "action": "open", "side": "buy", "book": "both", "filled_at": opened.isoformat()},
    ]
    evidence = {"status": "reconciled", "instrument": "BTCUSDT",
                "opened_at": {"long": opened.isoformat()}, "events": events}
    report = observation._observe_position({}, position, evidence, end, None)

    assert report["outcome"] == "proposed_adoption"
    assert check.call_args.args[0]["evidence"]["events"] == [events[-1]]


def test_observer_defaults_disabled(monkeypatch):
    monkeypatch.delenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", raising=False)
    assert observation.hourly_observer_from_env(at(11)) is None


def test_ema_unavailable_does_not_delay_confirmed_no_plan(monkeypatch, inventory_sources):
    _, _, market = stub_existing_reads(monkeypatch, inventory_sources)
    market.return_value = []

    report = observation.observe_account(user_id=3, credential_id=7, observed_at=at(11))

    eth = report["positions"][1]
    assert eth["outcome"] == "proposed_exit"
    assert eth["ema_checks"][0]["status"] == "unavailable"


def test_only_scheduler_leader_ticks_and_lease_loss_stops_observer(monkeypatch):
    @contextmanager
    def app_context():
        yield

    repository = Mock()
    repository.acquire_process_lease.side_effect = [False, True]
    repository.renew_process_lease.return_value = False
    shutdown = Mock()
    shutdown.event.is_set.return_value = False
    observer = Mock()
    build_observer = Mock(return_value=observer)
    monkeypatch.setenv("QD_PROCESS_ROLE", "scheduler")
    monkeypatch.setattr(app, "create_app", Mock(return_value=Mock(app_context=app_context)))
    monkeypatch.setattr(startup, "_start_scheduler_services", Mock())
    monkeypatch.setattr(process, "ShutdownSignal", Mock(return_value=shutdown))
    monkeypatch.setattr(strategy_command_repository, "StrategyCommandRepository", Mock(return_value=repository))
    monkeypatch.setattr(observation, "hourly_observer_from_env", build_observer)

    scheduler.main()

    assert repository.acquire_process_lease.call_count == 2
    assert repository.renew_process_lease.call_count == 1
    build_observer.assert_called_once()
    observer.tick.assert_called_once()
    observer.stop.assert_called_once()
    repository.mark_worker_stopped.assert_called_once()
