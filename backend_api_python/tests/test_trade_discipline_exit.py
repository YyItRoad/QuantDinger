from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.trade_discipline import exit as exits
from app.trade_discipline import observation as observation
from app.services.live_trading.base import LiveTradingError
from app.services.live_trading import position_management
from app.services.live_trading.binance import BinanceFuturesClient
from app.services.pending_order_loops import PendingOrderLoops
from app.trade_plans.service import PlanService

OPENED = "2026-11-21T02:05:00.000Z"
SUBMITTED = "2026-11-21T02:04:00.000Z"
START = datetime.fromisoformat("2026-11-21T02:00:00Z")
ROUND = datetime.fromisoformat("2026-11-21T03:00:00Z")
CANDIDATE = dict(manual_opening=True, outcome="proposed_exit", reason="no_plan", management_status="unregistered",
                 evidence_status="reconciled", instrument="BTCUSDT", symbol="BTC/USDT", side="long",
                 opened_at=OPENED, opening_fill_id="10", last_fill_id="10", proposed_quantity="0.70",
                 plan_checks=[dict(status="no_plan", submitted_at=SUBMITTED, order_id="1")])


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(now=ROUND, orders={}, posts=[], reads=[], rows={},
                            qty="0.70", fills=[dict(book="both", side="buy", action="open", fill_id="10",
                                                   filled_at=OPENED, order_id="1")],
                            plan="no_plan", managed="unregistered", close_status="PARTIALLY_FILLED",
                            close_fill="0.20", timeout=False, invisible=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return state.now.astimezone(tz)

    monkeypatch.setattr(exits, "datetime", Clock)
    config = {"exchange_id": "binance", "api_key": "mock", "secret_key": "mock"}
    monkeypatch.setattr(exits, "resolve_exchange_config", Mock(return_value=config))
    monkeypatch.setattr(BinanceFuturesClient, "_ensure_server_time", Mock())
    monkeypatch.setattr(BinanceFuturesClient, "get_symbol_filters", Mock(return_value={
        "MARKET_LOT_SIZE": {"stepSize": "0.01", "minQty": "0.01"}, "_meta": {"quantityPrecision": 2},
    }))
    monkeypatch.setattr("app.services.live_trading.binance.submit_binance_order",
                        Mock(side_effect=AssertionError("discipline close must use one REST attempt")))

    def rest(self, method, path, *, params, headers):
        if path == "/fapi/v1/positionSide/dual":
            return 200, {"dualSidePosition": False}, ""
        assert path == "/fapi/v1/order"
        if method == "GET":
            key = params["origClientOrderId"]
            state.reads.append(key)
            if state.invisible:
                return 400, {"code": -2013}, '{"code":-2013}'
            return 200, state.orders[key], ""
        key = params["newClientOrderId"]
        state.posts.append((key, params["quantity"]))
        assert key not in state.orders
        assert params["reduceOnly"] == "true" and params["side"] == "SELL"
        order = dict(orderId=100 + len(state.posts), clientOrderId=key, symbol=params["symbol"],
                     side="SELL", positionSide="BOTH", reduceOnly=True, origQty=params["quantity"],
                     executedQty=state.close_fill, avgPrice="100", status=state.close_status)
        state.orders[key] = order
        if state.timeout:
            raise TimeoutError("exchange accepted before the local timeout")
        return 200, order, ""

    monkeypatch.setattr(BinanceFuturesClient, "_request", rest)

    def inventory(**scope):
        return dict(scope, status="ok", exchange_id="binance", market_type="swap",
                    positions=[dict(inst_id="BTCUSDT", side="long", symbol="BTC/USDT",
                                    size=state.qty, management_status=state.managed)] if state.qty else [])

    monkeypatch.setattr(exits, "read_account_position_inventory", inventory)
    monkeypatch.setattr(exits, "read_position_evidence", lambda *args, **kwargs: {
        "instrument": "BTCUSDT", "status": "reconciled", "opened_at": {"long": OPENED},
        "events": deepcopy(state.fills),
    })
    state.plan_lookup = Mock(side_effect=lambda *args, **kwargs: {"status": state.plan})
    monkeypatch.setattr(PlanService, "match_at_order_time", state.plan_lookup)

    @contextmanager
    def lock(*args, **kwargs):
        yield

    monkeypatch.setattr(exits, "trade_execution_lock", lock)
    monkeypatch.setattr(exits, "_management_leg_lock", lock)

    def write(sql, params):
        if "INSERT INTO" in sql:
            uid, cred, symbol, side, payload = params
            raw = json.loads(payload)
            key = raw["_discipline_exit"]["key"]
            for row in state.rows.values():
                if row["raw_result"]["_discipline_exit"]["key"] == key:
                    return None
            row_id = len(state.rows) + 1
            state.rows[row_id] = dict(id=row_id, user_id=uid, credential_id=cred, symbol=symbol, side=side,
                                      raw_result=raw, client_order_id="", status=exits._QUEUED, filled_amount=0)
            return deepcopy(state.rows[row_id])
        if "avg_fill_price = %s" in sql:
            payload, filled, price, order_id, row_id, _, _ = params
            state.rows[row_id].update(raw_result=json.loads(payload), filled_amount=filled,
                                      avg_fill_price=price, exchange_order_id=order_id)
        else:
            status, payload, client_id, row_id, _, _, expected, expires = params
            if (state.rows[row_id]["status"] != expected
                    or state.rows[row_id]["raw_result"]["_discipline_exit"]["expires_at"] != expires):
                return None
            state.rows[row_id].update(status=status, raw_result=json.loads(payload), client_order_id=client_id)
        return deepcopy(state.rows[row_id])

    def record(**data):
        row_id = data["record_id"]
        state.rows[row_id].update(status=data["status"], filled_amount=data["filled"],
                                  exchange_order_id=data["exchange_order_id"], raw_result=data["raw_result"])
        return row_id

    monkeypatch.setattr(position_management, "list_position_inventory_accounts", Mock(return_value=[{"id": 7}]))
    monkeypatch.setattr("app.trade_discipline.manual.opening_order_origin", Mock(return_value="manual"))
    monkeypatch.setattr(exits, "_write", write)
    monkeypatch.setattr(exits, "_read_rows", lambda *args, row_id=None: [deepcopy(state.rows[row_id])]
                        if row_id is not None else deepcopy(list(state.rows.values())))
    monkeypatch.setattr(exits, "_has_unresolved_exit", lambda row: any(
        other["status"] == exits._SENT and other["side"] == row["side"] for other in state.rows.values()))
    monkeypatch.setattr(exits, "record_quick_trade", record)
    for name, value in {"OBSERVATION_ENABLED": "true", "AUTO_EXIT_ENABLED": "true", "USER_ID": "3",
                        "CREDENTIAL_ID": "7", "MARKET_TYPE": "swap", "EXIT_FROM": START.isoformat()}.items():
        monkeypatch.setenv(f"TRADE_DISCIPLINE_{name}", value)
    return state


def queue(position=CANDIDATE, now=ROUND):
    return exits.queue_observed_exit(user_id=3, credential_id=7, position=position, observed_at=now, exit_from=START)


def worker():
    # A new loop object models restart with only the saved rows and the simulated venue retained.
    loop = PendingOrderLoops()
    loop.lease_guard = lambda: True
    loop._stop_event = Event()
    loop._sync_discipline_exits()


def test_partial_does_not_retry_and_new_round_only_reduces_proven_residual(runtime):
    assert queue()["request_id"] == 1
    assert queue()["status"] == "existing"
    worker()
    client_id, quantity = runtime.posts[0]
    assert Decimal(quantity) == Decimal("0.70")
    assert runtime.rows[1]["status"] == exits._SENT
    assert runtime.rows[1]["filled_amount"] == Decimal("0.20")
    runtime.now += timedelta(seconds=31)
    runtime.qty = "0.50"
    runtime.fills.append(dict(book="both", side="sell", action="reduce", fill_id="11",
                              filled_at=ROUND.isoformat(), order_id="101"))
    residual = dict(CANDIDATE, last_fill_id="11", proposed_quantity="0.50")
    runtime.now = ROUND + timedelta(hours=1)
    assert queue(residual, now=runtime.now)["request_id"] == 2
    worker()
    assert runtime.reads == [client_id]
    assert len(runtime.posts) == 1
    runtime.orders[client_id]["status"] = "EXPIRED"
    runtime.now += timedelta(seconds=31)
    runtime.close_status, runtime.close_fill = "FILLED", "0.50"
    worker()
    assert runtime.rows[1]["status"] == "cancelled"
    assert runtime.rows[1]["filled_amount"] == 0.20
    assert runtime.rows[2]["status"] == "filled"
    assert [Decimal(quantity) for _, quantity in runtime.posts] == [Decimal("0.70"), Decimal("0.50")]
    assert runtime.posts[1][0] != client_id


def test_timeout_after_acceptance_and_restart_only_query_original_id(runtime):
    runtime.timeout, runtime.close_status, runtime.close_fill = True, "FILLED", "0.70"
    queue()
    worker()
    (client_id, _), = runtime.posts
    assert runtime.rows[1]["client_order_id"] == client_id
    assert runtime.rows[1]["status"] == exits._SENT
    runtime.now += timedelta(seconds=31)
    runtime.invisible = True
    assert queue()["status"] == "existing"
    worker()
    assert runtime.rows[1]["status"] == exits._SENT
    runtime.now += timedelta(seconds=31)
    runtime.invisible = False
    worker()
    worker()
    assert runtime.reads == [client_id, client_id]
    assert len(runtime.posts) == 1
    assert runtime.rows[1]["status"] == "filled"
    assert runtime.rows[1]["exchange_order_id"] == "101"
    assert runtime.rows[1]["filled_amount"] == 0.70


def test_sent_exit_does_not_query_another_exchange_endpoint(runtime):
    runtime.timeout, runtime.close_status, runtime.close_fill = True, "FILLED", "0.70"
    queue()
    worker()
    (client_id, _), = runtime.posts

    config = exits.resolve_exchange_config.return_value
    config["environment"] = "demo"
    runtime.now += timedelta(seconds=31)
    worker()
    assert not runtime.reads
    assert runtime.rows[1]["status"] == exits._SENT

    config["environment"] = "live"
    runtime.now += timedelta(seconds=31)
    worker()
    assert runtime.reads == [client_id]
    assert len(runtime.posts) == 1
    assert runtime.rows[1]["status"] == "filled"


@pytest.mark.parametrize("change, value", [("plan", "matched"), ("plan", "unavailable"),
                                           ("managed", "registered"), ("qty", None)])
def test_worker_rechecks_plan_and_live_ownership(runtime, change, value):
    queue()
    setattr(runtime, change, value)
    worker()
    assert runtime.posts == []


def test_new_fill_requires_a_new_observation(runtime):
    queue()
    runtime.fills.append(dict(book="both", side="buy", action="add", fill_id="15",
                              filled_at=ROUND.isoformat(), order_id="5"))
    worker()
    assert runtime.posts == []


def test_size_is_capped_by_observed_attributable_and_exchange_lot(runtime):
    queue(dict(CANDIDATE, proposed_quantity="0.205"))
    runtime.close_status, runtime.close_fill = "FILLED", "0.20"
    worker()
    assert [Decimal(quantity) for _, quantity in runtime.posts] == [Decimal("0.20")]
    assert runtime.plan_lookup.call_args.kwargs["submitted_at"] == SUBMITTED


def test_scheduler_round_queues_exit_without_waiting_one_hour(runtime, monkeypatch):
    monkeypatch.setattr(observation, "observe_account", lambda **kw: {
        "user_id": 3, "credential_id": 7, "positions": [deepcopy(CANDIDATE)],
    })
    scheduler = observation.hourly_observer_from_env(ROUND - timedelta(minutes=1))
    assert scheduler.tick(ROUND)
    scheduler._thread.join(timeout=2)
    assert list(runtime.rows) == [1]
    assert runtime.posts == []
    worker()
    assert len(runtime.posts) == 1  # Opening fill was only 55 minutes earlier.


def test_disabled_worker_and_unconfirmed_exit_never_submit(runtime, monkeypatch):
    assert queue(dict(CANDIDATE, plan_checks=[]))["status"] == "deferred"
    assert queue(dict(CANDIDATE, management_status="registered"))["status"] == "deferred"
    assert queue(dict(CANDIDATE, opened_at="2026-11-20T02:05:00.000Z"))["status"] == "deferred"
    queue()
    monkeypatch.setenv("TRADE_DISCIPLINE_AUTO_EXIT_ENABLED", "false")
    worker()
    assert runtime.posts == []


@pytest.mark.parametrize("code", [-4061, -1021])
def test_strict_binance_close_does_not_retry_mode_or_timestamp_errors(runtime, monkeypatch, code):
    rest = Mock(return_value=(400, {"code": code}, json.dumps({"code": code})))
    monkeypatch.setattr(BinanceFuturesClient, "_request", rest)
    monkeypatch.setattr(BinanceFuturesClient, "get_dual_side_position", Mock(return_value=True))
    client = BinanceFuturesClient(api_key="mock", secret_key="mock")
    client.strict_market_close_guard = lambda: True
    with pytest.raises(LiveTradingError):
        client.place_market_order(symbol="BTCUSDT", side="SELL", quantity=0.7, reduce_only=True, position_side="long")
    rest.assert_called_once()
    assert rest.call_args.kwargs["params"]["positionSide"] == "LONG"


def test_strict_close_checks_lease_at_transport_boundary(runtime, monkeypatch):
    rest = Mock()
    monkeypatch.setattr(BinanceFuturesClient, "_request", rest)
    monkeypatch.setattr(BinanceFuturesClient, "get_dual_side_position", Mock(return_value=False))
    client = BinanceFuturesClient(api_key="mock", secret_key="mock")
    client.strict_market_close_guard = lambda: False
    with pytest.raises(LiveTradingError, match="no longer authorized"):
        client.place_market_order(symbol="BTCUSDT", side="SELL", quantity=0.7, reduce_only=True, position_side="long")
    rest.assert_not_called()


def test_same_quantity_reopen_cannot_use_old_attribution(runtime):
    queue()
    runtime.fills = [dict(runtime.fills[0], fill_id="11", order_id="2")]
    worker()
    assert runtime.posts == []


def test_unconfirmed_original_blocks_new_request_even_when_position_changed(runtime):
    runtime.timeout, runtime.invisible = True, True
    queue()
    worker()
    runtime.now += timedelta(hours=1)
    queue(dict(CANDIDATE, last_fill_id="11", proposed_quantity="0.5"), now=runtime.now)
    worker()
    assert len(runtime.posts) == 1
    assert runtime.rows[1]["status"] == exits._SENT
    assert runtime.rows[2]["status"] == exits._QUEUED


def test_reduction_between_observation_and_dispatch_caps_to_live_residual(runtime):
    queue()
    runtime.qty = "0.45"
    runtime.fills.append(dict(book="both", side="sell", action="reduce", fill_id="11",
                              filled_at=ROUND.isoformat(), order_id="2"))
    runtime.close_status, runtime.close_fill = "FILLED", "0.45"
    worker()
    assert [Decimal(quantity) for _, quantity in runtime.posts] == [Decimal("0.45")]


def test_confirmed_ema_violation_reaches_worker_market_exit(runtime, monkeypatch):
    from app.trade_discipline import observation as observe

    runtime.plan = "matched"
    monkeypatch.setattr(observe, "check_ema", Mock(return_value=[{"status": "blocked"}]))
    candidate = dict(CANDIDATE, reason="ema25_direction_blocked",
                     plan_checks=[dict(status="matched", submitted_at=SUBMITTED, order_id="1")])
    assert queue(candidate)["status"] == "queued"
    runtime.close_status, runtime.close_fill = "FILLED", "0.70"
    worker()
    assert len(runtime.posts) == 1
    assert runtime.rows[1]["status"] == "filled"
