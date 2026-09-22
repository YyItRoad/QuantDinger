"""分析任务输入校验；复用项目统一的行情身份规则。"""

from app.services.market.watchlist import validate_watchlist_pair
from app.services.market_context import (
    CRYPTO_MARKET_TYPES,
    MarketContext,
    SUPPORTED_CRYPTO_EXCHANGE_IDS,
)

from .constants import SUPPORTED_TIMEFRAMES
from .errors import RequestValidationError


IDENTITY_FIELDS = (
    'market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'timeframe',
)


def validate_task_input(body):
    if not isinstance(body, dict):
        raise RequestValidationError('请求内容必须为对象')
    raw = {}
    for key in IDENTITY_FIELDS:
        value = body.get(key, '')
        if not isinstance(value, str) or len(value) > 120:
            raise RequestValidationError(f'{key} 格式不正确')
        raw[key] = value.strip()

    context = MarketContext.from_mapping(raw, apply_crypto_defaults=False)
    validation_error = validate_watchlist_pair(context.market, context.symbol)
    if validation_error:
        raise RequestValidationError(validation_error)
    if context.timeframe not in SUPPORTED_TIMEFRAMES:
        raise RequestValidationError('请选择品种和分析周期')
    if context.market == 'Crypto':
        if context.exchange_id not in SUPPORTED_CRYPTO_EXCHANGE_IDS or context.market_type not in CRYPTO_MARKET_TYPES:
            raise RequestValidationError('请选择有效交易所和品种类型')

    value = context.as_dict()
    return {key: value[key] for key in IDENTITY_FIELDS}
