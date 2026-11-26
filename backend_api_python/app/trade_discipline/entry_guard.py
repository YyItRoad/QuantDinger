"""Preflight for Quick Trade orders capable of increasing exposure."""

from datetime import datetime, timezone
import os

from app.services.live_trading.records import normalize_strategy_symbol
from app.services.market.instrument_products import PRODUCT_CRYPTO
from app.services.market.product_catalog import get_catalog_product
from app.services.market_schedule import CALENDAR_BY_MARKET
from app.trade_discipline.ema import check_ema25_at_order_time
from app.trade_plans.service import PlanService
from app.utils.logger import get_logger

logger = get_logger(__name__)

BLOCKED = "quickTrade.workflow.disciplineBlocked"


def entry_discipline_rejection(*, user_id, credential_id, symbol, side, market_type, exchange_config,
                               resolved_product=None):
    """Return a rejection reason, or None when the entry may continue."""
    # 快捷平仓走独立入口。买入现货以及可能开仓或反手的合约请求走此判定。
    # 现货卖出减少库存，直接放行。
    if market_type == "spot" and side == "sell":
        return None
    if os.getenv("TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        scope = tuple(int(os.getenv(name, "")) for name in
                      ("TRADE_DISCIPLINE_USER_ID", "TRADE_DISCIPLINE_CREDENTIAL_ID"))
        if min(scope) <= 0:
            raise ValueError()
    except ValueError:
        return "discipline_scope_invalid"
    # 快捷开仓拦截只对配置中的用户和交易凭证生效。
    if scope != (user_id, credential_id):
        return None

    try:
        return _check_entry(user_id, symbol, side, market_type, exchange_config, resolved_product)
    except Exception:
        logger.warning("Quick Trade discipline check unavailable user=%s credential=%s", user_id, credential_id)
        return "discipline_check_unavailable"


def _check_entry(user_id, symbol, side, market_type, config, product):
    lookback = os.getenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS")
    lookback_hours = int(lookback) if lookback else None
    if lookback_hours is not None and lookback_hours <= 0:
        return "discipline_lookback_invalid"

    exchange_id = config["exchange_id"].strip().lower()
    if product is None:
        catalog_scope = dict(market="Crypto", exchange_id=exchange_id, market_type=market_type,
                             instrument_id=config.get("instrument_id") or "")
        product = get_catalog_product(symbol=symbol, **catalog_scope)
        canonical = normalize_strategy_symbol(symbol)
        if product is None and canonical != symbol:
            product = get_catalog_product(symbol=canonical, **catalog_scope)
    if not product or not product.get("product_type"):
        return "order_identity_unavailable"

    # 用产品目录提供的原生合约和结算币匹配计划。股票映射产品另用标的行情看 EMA。
    instrument = dict(market=product["market"], symbol=product["symbol"], exchange_id=exchange_id,
                      market_type=market_type, instrument_id=product["instrument_id"],
                      settlement_ccy=product["settle_currency"] or (
                          product["symbol"].split("/")[-1] if market_type == "spot" else ""
                      ))
    direction = "long" if side == "buy" else "short"
    ema_market, ema_symbol = instrument["market"], instrument["symbol"]
    ema_exchange, ema_product = exchange_id, market_type
    if product["product_type"] != PRODUCT_CRYPTO:
        ema_market, ema_symbol = product.get("underlying_market"), product.get("underlying_symbol")
        if ema_market not in CALENDAR_BY_MARKET or not ema_symbol:
            return "stock_identity_unavailable"
        if product["api_family"] != market_type and config.get("api_family") != product["api_family"]:
            return "order_product_route_unavailable"
        ema_exchange, ema_product = "", ""

    submitted_at = datetime.now(timezone.utc).isoformat()
    plan = PlanService().match_at_order_time(
        user_id, instrument=instrument, direction=direction, submitted_at=submitted_at,
        lookback_hours=lookback_hours,
    )
    # 快捷开仓只有确认存在匹配计划才进入 EMA 判定，区分无计划与查询失败。
    if plan["status"] != "matched":
        return "no_effective_plan" if plan["status"] == "no_plan" else "plan_check_unavailable"
    ema = check_ema25_at_order_time(
        market=ema_market, symbol=ema_symbol, direction=direction, submitted_at=submitted_at,
        exchange_id=ema_exchange, market_type=ema_product,
    )
    if ema["status"] == "allowed":
        return None
    return "ema25_direction_blocked" if ema["status"] == "blocked" else "ema25_check_unavailable"
