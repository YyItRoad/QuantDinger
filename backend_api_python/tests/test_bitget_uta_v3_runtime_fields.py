import pytest

from app.services.live_trading.account_snapshot import _parse_swap_position_items
from app.services.live_trading.base import LiveTradingError
from app.services.live_trading.bitget import BitgetMixClient
from app.services.live_trading.bitget_spot import BitgetSpotClient


def test_bitget_uta_account_assets_are_flattened_for_runtime_consumers():
    client = BitgetMixClient.__new__(BitgetMixClient)
    client._signed_request = lambda *_args, **_kwargs: {
        "code": "00000",
        "data": {
            "assets": [
                {"coin": "USDT", "available": "900", "equity": "1000"},
            ],
        },
    }

    result = client.get_accounts()

    assert result["data"] == [
        {"coin": "USDT", "available": "900", "equity": "1000"},
    ]


def test_bitget_uta_position_uses_avg_price_and_total_quantity():
    result = _parse_swap_position_items(
        [{
            "symbol": "BTCUSDT",
            "posSide": "long",
            "total": "0.025",
            "avgPrice": "64123.5",
        }]
    )

    assert result == [{
        "symbol": "BTC/USDT",
        "side": "long",
        "size": 0.025,
        "entry_price": 64123.5,
        "market_type": "swap",
        "inst_id": "BTCUSDT",
    }]


def test_gate_position_uses_snake_case_entry_price():
    result = _parse_swap_position_items(
        [{
            "contract": "DOGE_USDT",
            "size": "630",
            "entry_price": "0.07941",
        }]
    )

    assert result == [{
        "symbol": "DOGE/USDT",
        "side": "long",
        "size": 630.0,
        "entry_price": 0.07941,
        "market_type": "swap",
        "inst_id": "DOGE_USDT",
    }]


@pytest.mark.parametrize("client_type, method_name", [
    (BitgetMixClient, "get_accounts"),
    (BitgetSpotClient, "get_assets"),
])
def test_bitget_empty_uta_assets_explain_hybrid_account(client_type, method_name):
    client = client_type.__new__(client_type)
    responses = iter([
        {"code": "00000", "data": {"assets": []}},
        {"code": "00000", "data": {"accountMode": "hybrid"}},
    ])
    client._signed_request = lambda *_args, **_kwargs: next(responses)

    with pytest.raises(LiveTradingError, match="BITGET_UTA_HYBRID_BALANCE_UNAVAILABLE"):
        getattr(client, method_name)()


def test_bitget_empty_unified_account_remains_a_valid_zero_balance():
    client = BitgetMixClient.__new__(BitgetMixClient)
    responses = iter([
        {"code": "00000", "data": {"assets": []}},
        {"code": "00000", "data": {"accountMode": "unified"}},
    ])
    client._signed_request = lambda *_args, **_kwargs: next(responses)

    assert client.get_accounts()["data"] == []
