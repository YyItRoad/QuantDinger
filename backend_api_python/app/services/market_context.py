"""Canonical market-data identity shared by research, backtest, and live flows."""

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.config.data_sources import CryptoPublicConfig


CRYPTO_MARKET_TYPES = frozenset({"spot", "swap"})
SUPPORTED_CRYPTO_EXCHANGE_IDS = frozenset({"binance", "bitget", "bybit", "okx", "gate", "htx"})


def normalize_exchange_id(value: Any) -> str:
    exchange_id = str(value or "").strip().lower()
    aliases = {
        "okex": "okx",
        "gateio": "gate",
        "huobi": "htx",
        "binanceusdm": "binance",
    }
    return aliases.get(exchange_id, exchange_id)


def default_crypto_exchange_id() -> str:
    exchange_id = normalize_exchange_id(CryptoPublicConfig.DEFAULT_EXCHANGE or "binance") or "binance"
    return exchange_id if exchange_id in SUPPORTED_CRYPTO_EXCHANGE_IDS else "binance"


def normalize_market_type(value: Any, *, market: str = "") -> str:
    market_type = str(value or "").strip().lower()
    if market_type in {"future", "futures", "perp", "perpetual"}:
        market_type = "swap"
    if str(market or "").strip() != "Crypto":
        return "spot"
    return market_type if market_type in CRYPTO_MARKET_TYPES else "spot"


def canonical_crypto_symbol(value: Any) -> str:
    return parse_crypto_identifier(value)[0]


def parse_crypto_identifier(value: Any) -> tuple[str, str, str]:
    """Parse a crypto symbol without assigning an implicit venue."""
    identifier = str(value or "").strip()
    if identifier.lower().startswith("crypto:"):
        identifier = identifier.split(":", 1)[1]

    exchange_id = ""
    market_type = ""
    if "@" in identifier:
        if identifier.count("@") != 1:
            raise ValueError("marketData.invalidCryptoIdentifier")
        identifier, qualifier = identifier.rsplit("@", 1)
        qualifier = qualifier.strip()
        if not identifier.strip() or not qualifier:
            raise ValueError("marketData.invalidCryptoIdentifier")
        if ":" in qualifier:
            exchange_value, market_type_value = qualifier.rsplit(":", 1)
            exchange_id = normalize_exchange_id(exchange_value)
            if not exchange_id:
                raise ValueError("marketData.invalidCryptoExchange")
            if market_type_value.strip().lower() not in {
                "spot", "swap", "future", "futures", "perp", "perpetual"
            }:
                raise ValueError("marketData.invalidCryptoProduct")
            market_type = normalize_market_type(market_type_value, market="Crypto")
        elif qualifier.strip().lower() in {
            "spot", "swap", "future", "futures", "perp", "perpetual"
        }:
            market_type = normalize_market_type(qualifier, market="Crypto")
        else:
            exchange_id = normalize_exchange_id(qualifier)

    symbol = identifier.strip().upper().replace("-", "/").replace("_", "/")
    if ":" in symbol:
        pair, settle_currency = symbol.rsplit(":", 1)
        if "/" not in pair:
            raise ValueError("marketData.invalidCryptoSettlement")
        quote_currency = pair.rsplit("/", 1)[1]
        if settle_currency != quote_currency:
            raise ValueError("marketData.cryptoSettlementMismatch")
        symbol = pair
    if "/" not in symbol:
        for quote_currency in ("USDT", "USDC", "USD", "BTC", "ETH"):
            if symbol.endswith(quote_currency) and len(symbol) > len(quote_currency):
                symbol = f"{symbol[:-len(quote_currency)]}/{quote_currency}"
                break
    if not symbol:
        raise ValueError("marketData.cryptoSymbolRequired")
    return symbol, market_type, exchange_id


@dataclass(frozen=True)
class MarketContext:
    market: str
    symbol: str
    exchange_id: str = ""
    market_type: str = "spot"
    instrument_id: str = ""
    settle_currency: str = ""
    timeframe: str = ""

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        apply_crypto_defaults: bool = True,
    ) -> "MarketContext":
        data = payload or {}
        market = str(
            data.get("market")
            or data.get("market_category")
            or data.get("marketCategory")
            or ""
        ).strip()
        suffix_market_type = ""
        suffix_exchange_id = ""
        if market == "Crypto":
            symbol, suffix_market_type, suffix_exchange_id = parse_crypto_identifier(
                data.get("symbol")
            )
        else:
            symbol = str(data.get("symbol") or "").strip().upper()
        explicit_exchange_id = normalize_exchange_id(
            data.get("exchange_id")
            or data.get("exchangeId")
            or data.get("exchange")
        )
        if (
            suffix_exchange_id
            and explicit_exchange_id
            and suffix_exchange_id != explicit_exchange_id
        ):
            raise ValueError("marketData.cryptoExchangeConflict")
        exchange_id = explicit_exchange_id or suffix_exchange_id
        explicit_market_type = data.get("market_type") or data.get("marketType")
        if (
            market == "Crypto"
            and explicit_market_type
            and str(explicit_market_type).strip().lower()
            not in {"spot", "swap", "future", "futures", "perp", "perpetual"}
        ):
            raise ValueError("marketData.invalidCryptoProduct")
        normalized_market_type = normalize_market_type(
            explicit_market_type,
            market=market,
        )
        if (
            suffix_market_type
            and explicit_market_type
            and suffix_market_type != normalized_market_type
        ):
            raise ValueError("marketData.cryptoProductConflict")
        market_type = suffix_market_type or normalized_market_type
        if market == "Crypto" and exchange_id and exchange_id not in SUPPORTED_CRYPTO_EXCHANGE_IDS:
            raise ValueError("marketData.unsupportedCryptoExchange")
        if market == "Crypto" and apply_crypto_defaults and not exchange_id:
            exchange_id = default_crypto_exchange_id()
        if market != "Crypto":
            exchange_id = ""
        return cls(
            market=market,
            symbol=symbol,
            exchange_id=exchange_id,
            market_type=market_type,
            instrument_id=str(data.get("instrument_id") or data.get("instrumentId") or "").strip(),
            settle_currency=str(data.get("settle_currency") or data.get("settleCurrency") or "").strip().upper(),
            timeframe=str(data.get("timeframe") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.market,
            self.symbol,
            self.exchange_id,
            self.market_type,
            self.instrument_id,
        )
