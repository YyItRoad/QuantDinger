from app.services.live_trading.gate import GateUsdtFuturesClient
import pytest

from app.services.quick_trade.fee_reconciliation import (
    fee_update_from_fill,
    gate_swap_order_fees,
    quick_trade_fee_updates,
)
from app.services.quick_trade.orders import enrich_fill


def _client(rows):
    client = object.__new__(GateUsdtFuturesClient)
    client._signed_request = lambda method, path, params: rows
    return client


def test_gate_swap_order_fees_groups_exchange_fills():
    client = _client([
        {"contract": "BTC_USDT", "order_id": "order-1", "fee": "0.04"},
        {"contract": "BTC_USDT", "order_id": "order-1", "fee": "0.02"},
        {"contract": "BTC_USDT", "order_id": "order-2", "fee": "0.10"},
        {"contract": "ETH_USDT", "order_id": "order-1", "fee": "9"},
    ])

    result = gate_swap_order_fees(client, "BTC/USDT", ["order-1"])

    assert result == {
        "order-1": {
            "commission": 0.06,
            "commission_ccy": "USDT",
            "commission_quote": 0.06,
        }
    }


def test_fee_update_from_fill_converts_mixed_native_fees():
    class Client:
        def get_ticker(self, symbol):
            assert symbol == "BNB/USDT"
            return {"last": "600"}

    result = fee_update_from_fill(
        Client(),
        "BTC/USDT",
        {
            "avg_price": 80000,
            "fees_by_ccy": {"USDT": 0.1, "BNB": 0.001},
            "fee_status": "actual",
        },
    )

    assert result["commission"] == 0
    assert result["commission_ccy"] == "MIXED"
    assert result["commission_quote"] == pytest.approx(0.7)


def test_fee_update_from_fill_preserves_confirmed_zero():
    result = fee_update_from_fill(
        object(),
        "AAPL",
        {
            "avg_price": 250,
            "fees_by_ccy": {"USD": 0.0},
            "fee_status": "actual_zero",
        },
    )

    assert result["commission_quote"] == 0
    assert result["commission_ccy"] == "USD"
    assert result["fee_status"] == "actual_zero"


def test_generic_fee_updates_use_exchange_fill_adapter(monkeypatch):
    from app.services.quick_trade import orders

    monkeypatch.setattr(
        orders,
        "enrich_fill",
        lambda *args, **kwargs: {
            "avg_price": 100,
            "fees_by_ccy": {"USDT": 0.25},
            "fee_status": "actual",
        },
    )

    result = quick_trade_fee_updates(
        object(),
        "BTC/USDT",
        "spot",
        [{"exchange_order_id": "order-9", "avg_fill_price": 100}],
    )

    assert result["order-9"]["commission_quote"] == 0.25
    assert result["order-9"]["fee_status"] == "actual"


def test_enrich_fill_preserves_exchange_fee_breakdown():
    class Client:
        def wait_for_fill(self, **kwargs):
            return {
                "filled": 1,
                "avg_price": 100,
                "fees_by_ccy": {"USDT": 0.1, "BNB": 0.001},
                "fee_status": "actual",
                "status": "filled",
            }

    result = enrich_fill(
        Client(),
        order_id="order-1",
        symbol="BTC/USDT",
        market_type="spot",
        max_wait_sec=0,
    )

    assert result["fees_by_ccy"] == {"USDT": 0.1, "BNB": 0.001}
    assert result["fee_status"] == "actual"


def test_gate_batch_falls_back_to_order_lookup_when_trade_is_outside_recent_window(monkeypatch):
    from app.services.quick_trade import fee_reconciliation, orders

    client = _client([])
    monkeypatch.setattr(fee_reconciliation, "gate_swap_order_fees", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        orders,
        "enrich_fill",
        lambda *args, **kwargs: {
            "avg_price": 100,
            "fees_by_ccy": {"USDT": 0.3},
            "fee_status": "actual",
        },
    )

    result = quick_trade_fee_updates(
        client,
        "BTC/USDT",
        "swap",
        [{"exchange_order_id": "older-order", "avg_fill_price": 100}],
    )

    assert result["older-order"]["commission_quote"] == 0.3


def test_enrich_fill_marks_filled_alpaca_equity_as_confirmed_zero():
    broker_type = type("Broker", (), {})
    broker_type.__module__ = "app.services.alpaca_trading.fake"
    broker = broker_type()
    broker.get_order_status = lambda order_id: type(
        "OrderResult",
        (),
        {
            "filled": 2,
            "avg_price": 250,
            "status": "filled",
            "raw": {"symbol": "AAPL", "commission": 0.0, "commission_ccy": ""},
        },
    )()

    result = enrich_fill(
        broker,
        order_id="alpaca-order",
        symbol="AAPL",
        market_type="spot",
        max_wait_sec=0,
    )

    assert result["fees_by_ccy"] == {"USD": 0.0}
    assert result["fee_status"] == "actual_zero"
