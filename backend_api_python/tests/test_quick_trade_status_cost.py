import pytest

from app.services.live_trading.bitget import BitgetMixClient
from app.services.pending_orders.order_quantities import exchange_executable_base_quantity
from app.services.quick_trade.orders import normalize_order_status, quick_order_status
from app.services.quick_trade.history import history_order_status
from app.services.quick_trade.spot_cost import average_cost_from_fills


def test_gate_rounding_does_not_make_a_completed_order_partial():
    native = {"status": "finished", "finish_as": "filled", "size": 61, "left": 0}
    status = normalize_order_status(native["status"], native)
    assert quick_order_status(requested_qty=0.00610818, filled_qty=0.0061, exchange_status=status) == "filled"
    row = {"status": "partially_filled", "raw_result": {**native, "_quick_trade": {"exchange_status": "finished"}}}
    assert history_order_status(row) == "filled"
    assert row["status"] == "partially_filled"


@pytest.mark.parametrize("native,expected", [
    ({"finish_as": "ioc", "size": 61, "left": 20}, "cancelled"),
    ({"finish_as": "cancelled", "size": 61, "left": 20}, "cancelled"),
    ({"size": -61, "left": 0}, "filled"),
    ({}, "finished"),
    ({"size": "bad", "left": 0}, "finished"),
])
def test_gate_terminal_reason_is_not_assumed_filled(native, expected):
    assert normalize_order_status("finished", native) == expected


def test_open_partial_and_missing_evidence_stay_partial():
    assert quick_order_status(requested_qty=1, filled_qty=.5, exchange_status="open") == "partially_filled"
    assert history_order_status({"status": "partially_filled", "raw_result": {}}) == "partially_filled"


def test_bitget_step_rounded_market_fill_is_complete():
    client = BitgetMixClient.__new__(BitgetMixClient)
    client.get_contract = lambda **_kwargs: {
        "sizePlace": "1",
        "minTradeNum": "0.1",
    }
    executable = exchange_executable_base_quantity(
        client,
        exchange_id="bitget",
        symbol="SOL/USDT",
        market_type="swap",
        requested=4.7671259,
        exchange_config={"product_type": "USDT-FUTURES"},
    )

    assert executable == 4.7
    assert quick_order_status(
        requested_qty=executable,
        filled_qty=4.7,
        exchange_status="partially_filled",
    ) == "filled"


def fill(fid, side, amount, price, fee=0, fee_currency="BTC"):
    return dict(id=fid, create_time=fid, side=side, amount=amount, price=price, fee=fee, fee_currency=fee_currency)


def test_spot_average_accounts_for_base_fees_and_partial_sales():
    rows = [fill(1, 'buy', 1, 100, .01), fill(2, 'buy', 1, 200), fill(3, 'sell', .5, 300)]
    assert average_cost_from_fills(rows[::-1], holding=1.49, base='BTC') == pytest.approx(300 / 1.99)


def test_spot_cost_does_not_guess_missing_or_transferred_inventory():
    assert average_cost_from_fills([fill(1, 'buy', 1, 100)], holding=2, base='BTC') == 0
    assert average_cost_from_fills([fill(1, 'sell', 1, 100), fill(2, 'buy', 2, 100)], holding=1, base='BTC') == 0
    assert average_cost_from_fills([fill(1, 'buy', 1, 100)], holding=.5, base='BTC') == 0


def test_spot_cost_deduplicates_fills():
    row = fill(1, 'buy', 1, 100)
    assert average_cost_from_fills([row, row], holding=1, base='BTC') == 100
