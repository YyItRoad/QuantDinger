import pytest

from app.services.live_trading.base import LiveTradingError
from app.services.live_trading.bitget import BitgetMixClient
from app.services.live_trading.bitget_classic import BitgetClassicMixClient
from app.services.live_trading.bitget_compat import (
    BitgetCompatibleMixClient,
    BitgetCompatibleSpotClient,
)
from app.services.live_trading.bitget_spot import BitgetSpotClient
from app.services.live_trading.bitget_spot_classic import BitgetClassicSpotClient
from app.services.live_trading.factory import create_client


def _client_kwargs(api_key: str) -> dict:
    return {
        "api_key": api_key,
        "secret_key": "secret",
        "passphrase": "passphrase",
    }


def test_bitget_hybrid_futures_account_routes_to_classic_v2():
    client = BitgetCompatibleMixClient(**_client_kwargs("hybrid-futures"))
    client._uta_client._signed_request = lambda *_args, **_kwargs: {
        "code": "00000",
        "data": {"accountMode": "hybrid"},
    }
    client._classic_client.get_accounts = lambda **_kwargs: {
        "code": "00000",
        "data": [{"marginCoin": "USDT", "available": "125"}],
    }

    result = client.get_accounts(product_type="USDT-FUTURES")

    assert result["data"][0]["available"] == "125"
    assert client.bitget_account_mode == "classic"


def test_bitget_unified_spot_account_keeps_uta_v3():
    client = BitgetCompatibleSpotClient(**_client_kwargs("unified-spot"))

    def signed_request(_method, path, **_kwargs):
        if path == "/api/v3/account/settings":
            return {"code": "00000", "data": {"accountMode": "unified"}}
        assert path == "/api/v3/account/assets"
        return {
            "code": "00000",
            "data": {"assets": [{"coin": "USDT", "available": "75"}]},
        }

    client._uta_client._signed_request = signed_request

    result = client.get_assets()

    assert result["data"][0]["available"] == "75"
    assert client.bitget_account_mode == "uta"


def test_bitget_auto_falls_back_to_classic_when_v3_is_unavailable():
    client = BitgetCompatibleMixClient(**_client_kwargs("classic-fallback"))
    client._uta_client._signed_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        LiveTradingError("Bitget error: account is not unified")
    )
    calls = []

    def get_accounts(**_kwargs):
        calls.append(True)
        return {"code": "00000", "data": []}

    client._classic_client.get_accounts = get_accounts

    assert client.get_accounts()["data"] == []
    assert len(calls) == 2
    assert client.bitget_account_mode == "classic"


def test_bitget_transitioning_account_blocks_trading_until_switch_finishes():
    client = BitgetCompatibleMixClient(**_client_kwargs("transitioning-futures"))
    client._uta_client._signed_request = lambda *_args, **_kwargs: {
        "code": "00000",
        "data": {"accountMode": "switching"},
    }

    with pytest.raises(LiveTradingError, match="BITGET_ACCOUNT_MODE_TRANSITIONING"):
        client.get_accounts()


def test_bitget_factory_returns_compatible_clients_without_breaking_type_checks():
    base = {
        "exchange_id": "bitget",
        "api_key": "factory-key",
        "secret_key": "secret",
        "passphrase": "passphrase",
        "bitget_account_mode": "classic",
    }

    spot = create_client(base, market_type="spot")
    swap = create_client(base, market_type="swap")

    assert isinstance(spot, BitgetSpotClient)
    assert isinstance(swap, BitgetMixClient)
    assert spot._configured_account_mode == "classic"
    assert swap._configured_account_mode == "classic"


def test_bitget_classic_futures_keeps_v2_hedge_order_fields():
    client = BitgetClassicMixClient.__new__(BitgetClassicMixClient)
    client.get_account_pos_mode = lambda **_kwargs: "hedge_mode"

    fields = client._mix_order_position_fields(
        symbol="BTC/USDT",
        product_type="USDT-FUTURES",
        margin_coin="USDT",
        side="sell",
        reduce_only=True,
        hold_side="long",
    )

    assert fields == {"side": "buy", "tradeSide": "close"}


def test_bitget_classic_spot_market_buy_uses_v2_endpoint_and_size_field():
    client = BitgetClassicSpotClient(**_client_kwargs("classic-spot-order"))
    client.get_symbol_meta = lambda **_kwargs: {
        "quantityPrecision": "6",
        "quotePrecision": "2",
        "minTradeUSDT": "1",
    }
    calls = []
    client._signed_request = (
        lambda method, path, json_body=None, params=None: calls.append((path, json_body))
        or {"data": {"orderId": "classic-order"}}
    )

    result = client.place_market_order(symbol="DOGE/USDT", side="buy", size=12.3456)

    assert result.exchange_order_id == "classic-order"
    assert calls == [
        (
            "/api/v2/spot/trade/place-order",
            {
                "symbol": "DOGEUSDT",
                "side": "buy",
                "orderType": "market",
                "force": "gtc",
                "size": "12.34",
            },
        )
    ]


def test_bitget_compatible_client_delegates_orders_to_selected_classic_client():
    client = BitgetCompatibleMixClient(
        **_client_kwargs("classic-order-routing"),
        account_mode="classic",
    )
    expected = object()
    client._classic_client.place_market_order = lambda **_kwargs: expected

    assert client.place_market_order(symbol="BTC/USDT", side="buy", size=0.01) is expected
    assert client.bitget_account_mode == "classic"
