import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, g

from app.routes import quick_trade
from app.routes.agent_v1 import quick_trade as agent_trade
from app.services.live_trading import account_configuration, execution, spot_sizing
from app.services.pending_orders import live_order_phases
from app.services.quick_trade import credentials
from app.trade_discipline import entry_guard as discipline
from app.trade_discipline.ema import DataSourceFactory
from tests.test_trade_discipline_ema import bars
from tests.test_trade_discipline_plan_lookup import candidate, lookup


@pytest.fixture
def runtime(monkeypatch, lookup):
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(discipline, "datetime", Mock(now=Mock(return_value=now)))
    for name, value in {"ENTRY_GUARD_ENABLED": "true", "USER_ID": "7", "CREDENTIAL_ID": "9"}.items():
        monkeypatch.setenv(f"TRADE_DISCIPLINE_{name}", value)
    monkeypatch.delenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS", raising=False)
    config = {"exchange_id": "binance"}
    product = dict(market="Crypto", symbol="BTC/USDT", product_type="crypto",
                   instrument_id="BTCUSDT", settle_currency="USDT")
    monkeypatch.setattr(discipline, "get_catalog_product", Mock(return_value=product))
    monkeypatch.setattr(quick_trade, "_build_product_aware_config", lambda *a, **k: (config, None))
    monkeypatch.setattr(credentials, "build_exchange_config", Mock(return_value=config))
    lookup.fetchall.return_value = [candidate(expires=now + timedelta(hours=1), order_time=now)]
    order = SimpleNamespace(exchange_order_id="sim-42", filled=0, avg_price=0, raw={})
    client = Mock(place_limit_order=Mock(return_value=order))
    factory = Mock(return_value=client)
    market_order = Mock(return_value=order)
    configure = Mock()
    record = Mock(return_value=1)
    quotes = Mock(return_value=bars())
    monkeypatch.setattr(DataSourceFactory, "get_kline", quotes)
    monkeypatch.setattr(quick_trade, "create_exchange_client", factory)
    monkeypatch.setattr(credentials, "create_exchange_client", factory)
    monkeypatch.setattr(execution, "place_order_from_signal", market_order)
    monkeypatch.setattr(account_configuration, "configure_derivatives_account", configure)
    monkeypatch.setattr(quick_trade, "configure_quick_trade_derivatives", configure)
    monkeypatch.setattr(quick_trade, "_recent_strategy_conflict", Mock(return_value=None))
    monkeypatch.setattr(quick_trade, "_convert_usdt_to_base_qty", Mock(return_value=0.2))
    monkeypatch.setattr(quick_trade, "normalized_submission_quantity", lambda *a, **k: (0.2, {}))
    monkeypatch.setattr(quick_trade, "fetch_balance_raw", Mock(return_value={"available": 10000}))
    monkeypatch.setattr(quick_trade, "_record_quick_trade", record)
    monkeypatch.setattr(quick_trade, "limit_order_kwargs", Mock(return_value={"quantity": 0.2, "price": 100}))
    monkeypatch.setattr(quick_trade, "enrich_fill", Mock(return_value={"status": "NEW"}))
    monkeypatch.setattr("app.services.quick_trade.orders.enrich_fill", Mock(return_value={"status": "NEW"}))
    monkeypatch.setattr(spot_sizing, "normalize_spot_base_quantity", lambda *a, **k: k["quantity"])
    return SimpleNamespace(now=now, lookup=lookup, quotes=quotes, product=product, config=config,
                           market_order=market_order, limit_order=client.place_limit_order,
                           factory=factory, configure=configure, record=record)


def submit(**changes):
    app = Flask(__name__)
    with app.test_request_context():
        g.user_id = 7
        result = inspect.unwrap(quick_trade.place_order)(dict(
            {"symbol": "BTC/USDT", "credential_id": 9, "side": "buy", "market_type": "swap",
             "amount": 20, "entry_reason": "simulated setup", "price": 100}, **changes,
        ))
        if isinstance(result, tuple):
            response, status = result
            return status, response.get_json()
        return result.status_code, result.get_json()


@pytest.mark.parametrize("order_type", ["market", "limit"])
@pytest.mark.parametrize(("side", "last_close"), [("buy", 120), ("sell", 80), ("buy", 100), ("sell", 100)])
def test_valid_directional_plan_continues_to_normal_quick_order(runtime, order_type, side, last_close):
    runtime.quotes.return_value = bars(last_close)
    status, body = submit(order_type=order_type, side=side)
    assert status == 200 and body["code"] == 1
    (runtime.market_order if order_type == "market" else runtime.limit_order).assert_called_once()
    runtime.configure.assert_called_once()
    assert runtime.lookup.execute.call_args.args[1] == (
        runtime.now, runtime.now, 7, runtime.now, "long" if side == "buy" else "short",
    )
    assert runtime.quotes.call_args.args[:3] == ("Crypto", "BTC/USDT", "4H")


@pytest.mark.parametrize(("failure", "expected"), [
    ("missing", "no_effective_plan"), ("expired", "no_effective_plan"),
    ("other_product", "no_effective_plan"), ("plan_error", "plan_check_unavailable"),
    ("opposite", "ema25_direction_blocked"),
    ("no_bars", "ema25_check_unavailable"),
])
def test_guard_blocks_entries_before_exchange_side_effects(runtime, failure, expected):
    if failure == "missing":
        runtime.lookup.fetchall.return_value = []
    elif failure == "expired":
        runtime.lookup.fetchall.return_value = [candidate(expires=runtime.now, order_time=runtime.now)]
    elif failure == "other_product":
        runtime.lookup.fetchall.return_value = [candidate(market_type="spot")]
    elif failure == "plan_error":
        runtime.lookup.execute.side_effect = TimeoutError()
    else:
        runtime.quotes.return_value = {"opposite": bars(80), "no_bars": []}[failure]

    status, body = submit(order_type="limit")
    assert (status, body["data"]["reason"]) == (409, expected)
    for operation in (runtime.factory, runtime.configure, runtime.record, runtime.market_order, runtime.limit_order):
        operation.assert_not_called()


def test_stock_product_uses_underlying_one_hour_and_matches_traded_product(runtime, monkeypatch):
    from app.services.market_schedule import equity_hourly_bar_windows

    runtime.product.update(symbol="AAPL/USDT", product_type="stock_perpetual", instrument_id="AAPLUSDT",
                           api_family="swap", underlying_market="USStock", underlying_symbol="AAPL")
    runtime.now = datetime(2026, 11, 18, 17, 42, tzinfo=timezone.utc)
    monkeypatch.setattr(discipline.datetime, "now", Mock(return_value=runtime.now))
    runtime.lookup.fetchall.return_value = [candidate(order_time=runtime.now,
        expires=runtime.now + timedelta(hours=1), symbol="AAPL/USDT", instrument_id="AAPLUSDT")]
    windows = equity_hourly_bar_windows("USStock", runtime.now - timedelta(days=30), runtime.now)
    latest = max(end for _, end in windows if end <= runtime.now.timestamp())
    runtime.quotes.return_value = [{"time": start, "close": 120 if end == latest else 100} for start, end in windows]
    assert submit(symbol="AAPL/USDT")[1]["code"] == 1
    assert runtime.quotes.call_args.args[:3] == ("USStock", "AAPL", "1H")


@pytest.mark.parametrize("size", [0, 0.1])
def test_derivative_close_and_partial_reduce_bypass_discipline(runtime, monkeypatch, size):
    monkeypatch.setattr(quick_trade, "_fetch_exchange_positions_raw", lambda *a, **k: [
        {"symbol": "BTC/USDT", "size": 0.5, "side": "buy", "entry_price": 100}])
    with Flask(__name__).test_request_context():
        g.user_id = 7
        result = inspect.unwrap(quick_trade.close_position)({
            "credential_id": 9, "symbol": "BTC/USDT", "market_type": "swap", "size": size,
        })
        assert result.json["code"] == 1
    assert runtime.market_order.call_args.kwargs["signal_type"] == "close_long"
    assert runtime.market_order.call_args.kwargs["amount"] == (size or 0.5)
    runtime.lookup.execute.assert_not_called()
    runtime.quotes.assert_not_called()


def test_spot_sell_bypasses_discipline(runtime):
    assert submit(side="sell", market_type="spot", entry_reason="")[1]["code"] == 1
    assert runtime.market_order.call_args.kwargs["signal_type"] == "close_long"
    runtime.lookup.execute.assert_not_called()


def test_disabled_guard_and_other_credential_bypass_discipline(runtime, monkeypatch):
    assert submit(credential_id=10)[1]["code"] == 1
    monkeypatch.setenv("TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED", "false")
    assert submit()[1]["code"] == 1
    runtime.lookup.execute.assert_not_called()


def test_agent_live_entry_uses_same_guard(runtime):
    body = dict(symbol="BTC/USDT", credential_id=9, side="buy", market_type="swap", qty=0.2,
                entry_reason="simulated setup")
    runtime.lookup.fetchall.return_value = []
    with pytest.raises(agent_trade.EntryDisciplineBlocked, match="no_effective_plan"):
        agent_trade._place_live_order(body=body, user_id=7)
    runtime.market_order.assert_not_called()
    runtime.lookup.fetchall.return_value = [candidate(expires=runtime.now + timedelta(hours=1), order_time=runtime.now)]
    agent_trade._place_live_order(body=body, user_id=7)
    runtime.market_order.assert_called_once()


@pytest.mark.parametrize("paper", [True, False])
def test_agent_http_contract_for_paper_and_blocked_entry(runtime, monkeypatch, paper):
    from contextlib import nullcontext

    monkeypatch.setattr(agent_trade, "market_allowed", Mock(return_value=True))
    monkeypatch.setattr(agent_trade, "instrument_allowed", Mock(return_value=True))
    monkeypatch.setattr(agent_trade, "with_idempotency", lambda *_: nullcontext(None))
    monkeypatch.setattr(agent_trade, "paper_only", lambda: paper)
    monkeypatch.setattr(agent_trade, "_live_trading_kill_switch", lambda: True)
    monkeypatch.setattr(agent_trade, "_last_price", Mock(return_value=100))
    monkeypatch.setattr(agent_trade, "_reserve_live_notional", Mock(return_value=(True, {})))
    finish = Mock()
    monkeypatch.setattr(agent_trade, "_finish_live_notional", finish)
    monkeypatch.setattr(agent_trade, "current_user_id", lambda: 7)
    monkeypatch.setattr(agent_trade, "current_token", lambda: {"id": 1})
    monkeypatch.setattr(agent_trade, "_record_paper_order", Mock(return_value={"paper": True}))
    monkeypatch.setattr(agent_trade, "record_completed_job", Mock())
    runtime.lookup.fetchall.return_value = []
    with Flask(__name__).test_request_context(json={
        "market": "Crypto", "symbol": "BTC/USDT", "credential_id": 9,
        "side": "buy", "market_type": "swap", "qty": 0.2, "entry_reason": "simulated setup",
    }):
        response = inspect.unwrap(agent_trade.place_order)()
    if paper:
        assert response[0].get_json()["data"]["paper"] is True
        runtime.lookup.execute.assert_not_called()
        finish.assert_not_called()
    else:
        result, status = response
        assert status == 409
        assert result.get_json()["details"] == {"reason": "no_effective_plan"}
        finish.assert_called_once_with("failed")
    runtime.market_order.assert_not_called()
