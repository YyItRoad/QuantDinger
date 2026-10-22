import pytest

from app.services.quick_trade.orders import quick_trade_protection_status


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "requested": False,
                "market_type": "swap",
                "filled_qty": 1,
                "avg_price": 100,
                "responses": [],
            },
            ("not_requested", ""),
        ),
        (
            {
                "requested": True,
                "market_type": "spot",
                "filled_qty": 1,
                "avg_price": 100,
                "responses": [],
            },
            ("unsupported_market", ""),
        ),
        (
            {
                "requested": True,
                "market_type": "swap",
                "filled_qty": 0,
                "avg_price": 0,
                "responses": [],
            },
            ("pending_fill", ""),
        ),
        (
            {
                "requested": True,
                "market_type": "swap",
                "filled_qty": 1,
                "avg_price": 100,
                "responses": [{"kind": "sl"}],
            },
            ("attached", ""),
        ),
        (
            {
                "requested": True,
                "market_type": "swap",
                "filled_qty": 1,
                "avg_price": 100,
                "responses": [],
            },
            ("failed", "native_protection_empty_response"),
        ),
        (
            {
                "requested": True,
                "market_type": "swap",
                "filled_qty": 1,
                "avg_price": 100,
                "responses": [],
                "error": "exchange rejected trigger",
            },
            ("failed", "exchange rejected trigger"),
        ),
    ],
)
def test_quick_trade_protection_status(kwargs, expected):
    assert quick_trade_protection_status(**kwargs) == expected
