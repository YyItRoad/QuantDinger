import pytest

from app.services.market_context import MarketContext, parse_crypto_identifier
from app.services.strategy_v2.instruments import parse_instrument


@pytest.mark.parametrize("asset", ["BTC", "ETH", "XAUT"])
def test_explicit_perpetual_identity_is_asset_agnostic(asset):
    value = f"Crypto:{asset}/USDT:USDT@binance:swap"

    symbol, market_type, exchange_id = parse_crypto_identifier(value)
    context = MarketContext.from_mapping(
        {"market": "Crypto", "symbol": value},
        apply_crypto_defaults=False,
    )
    instrument = parse_instrument(value)

    assert (symbol, market_type, exchange_id) == (f"{asset}/USDT", "swap", "binance")
    assert context.identity_key()[:4] == ("Crypto", f"{asset}/USDT", "binance", "swap")
    assert instrument.key == f"Crypto:{asset}/USDT@binance:swap"


def test_unqualified_swap_keeps_venue_open_for_public_research_failover():
    context = MarketContext.from_mapping(
        {"market": "Crypto", "symbol": "Crypto:XAUT/USDT@swap"},
        apply_crypto_defaults=False,
    )

    assert context.symbol == "XAUT/USDT"
    assert context.market_type == "swap"
    assert context.exchange_id == ""


@pytest.mark.parametrize(
    "payload",
    [
        {
            "market": "Crypto",
            "symbol": "Crypto:BTC/USDT@binance:swap",
            "exchange_id": "okx",
        },
        {
            "market": "Crypto",
            "symbol": "Crypto:BTC/USDT@swap",
            "market_type": "spot",
        },
        {"market": "Crypto", "symbol": "Crypto:BTC/USDT:BTC@swap"},
        {"market": "Crypto", "symbol": "Crypto:BTC/USDT@quarterly"},
    ],
)
def test_conflicting_or_unsupported_crypto_identity_is_rejected(payload):
    with pytest.raises(ValueError):
        MarketContext.from_mapping(payload, apply_crypto_defaults=False)
