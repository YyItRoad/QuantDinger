"""Spot holding cost and tradability enrichment for Quick Trade."""

from __future__ import annotations

from math import isclose
from typing import Any, Callable

from app.services.live_trading.symbols import _split_base_quote
from app.services.quick_trade.order_safety import spot_dust_metadata
from app.services.quick_trade.spot_cost import exchange_spot_cost
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


def quick_trade_spot_avg_entry_price(
    user_id: int,
    credential_id: int,
    symbol: str,
    market_type: str,
    expected_size: float = 0.0,
) -> float:
    sym = str(symbol or "").strip()
    mt = (market_type or "spot").strip().lower()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT side, filled_amount, avg_fill_price, price, commission, commission_ccy
            FROM qd_quick_trades
            WHERE user_id = %s AND credential_id = %s AND symbol = %s AND market_type = %s
              AND COALESCE(filled_amount, 0) > 0
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id), int(credential_id), sym, mt),
        )
        rows = cur.fetchall() or []
        cur.close()

    qty = 0.0
    cost = 0.0
    base, _ = _split_base_quote(sym)
    for row in rows:
        if not isinstance(row, dict):
            continue
        side = str(row.get("side") or "").strip().lower()
        try:
            filled = float(row.get("filled_amount") or 0.0)
            price = float(row.get("avg_fill_price") or row.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if filled <= 0 or price <= 0:
            continue
        base_fee = (
            float(row.get("commission") or 0)
            if str(row.get("commission_ccy") or "").upper() == base.upper()
            else 0.0
        )
        if side == "buy":
            cost += filled * price
            qty += filled - base_fee
        elif side == "sell" and qty > 0:
            sell_qty = min(filled + base_fee, qty)
            cost -= sell_qty * (cost / qty)
            qty -= sell_qty
    if qty <= 1e-12 or cost <= 0:
        return 0.0
    if expected_size > 0 and not isclose(qty, expected_size, rel_tol=1e-6, abs_tol=1e-10):
        return 0.0
    return cost / qty


def enrich_spot_positions(
    positions: list,
    *,
    client: Any,
    symbol: str,
    user_id: int,
    credential_id: int,
    market_type: str,
    exchange_id: str = "",
    average_cost_loader: Callable[..., float] = quick_trade_spot_avg_entry_price,
) -> list:
    if not positions:
        return positions
    from app.services.instrument_rules import get_instrument_rules_provider
    from app.services.live_trading.spot_sizing import fetch_spot_last_price
    expected_size = sum(
        float(position.get("size") or 0)
        for position in positions
        if isinstance(position, dict)
    )
    database_average = average_cost_loader(
        user_id,
        credential_id,
        symbol,
        market_type,
        expected_size=expected_size,
    )
    last_price = fetch_spot_last_price(client, symbol=symbol)
    rules = None
    if exchange_id:
        try:
            rules = get_instrument_rules_provider().get_rules(
                symbol,
                exchange_id=exchange_id,
                market_type=market_type,
                client=client,
            )
        except Exception as exc:
            logger.info("Spot dust rules unavailable for %s: %s", symbol, exc)

    enriched = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        row = dict(position)
        entry = float(row.get("entry_price") or 0.0)
        cost_source = "exchange" if entry > 0 else ""
        if entry <= 0 and database_average > 0:
            entry = database_average
            cost_source = "quick_trade_estimate"
        if entry <= 0:
            try:
                entry = exchange_spot_cost(client, symbol=symbol, holding=float(row.get("size") or 0))
                if entry > 0:
                    cost_source = "exchange_fills_estimate"
            except Exception as exc:
                logger.info("Spot cost unavailable for %s: %s", symbol, exc)
        mark = float(row.get("mark_price") or 0.0) or last_price
        size = float(row.get("size") or 0.0)
        available = max(0.0, float(row.get("available", size) or 0.0))
        side = str(row.get("side") or "long").strip().lower()
        row.update(entry_price=entry, cost_source=cost_source or "unavailable")
        if mark > 0:
            row["mark_price"] = mark
        unrealized = float(row.get("unrealized_pnl") or 0.0)
        if abs(unrealized) < 1e-12 and entry > 0 and mark > 0 and size > 0:
            row["unrealized_pnl"] = (entry - mark if side == "short" else mark - entry) * size
        row.update(
            spot_dust_metadata(
                rules=rules,
                size=size,
                available=available,
                mark_price=mark,
            )
        )
        enriched.append(row)
    return enriched
