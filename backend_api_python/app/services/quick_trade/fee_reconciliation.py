"""Late fee reconciliation for Quick Trade fills."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable

from app.services.live_trading.gate import GateUsdtFuturesClient
from app.services.quick_trade.credentials import build_exchange_config, create_exchange_client
from app.services.quick_trade.history import parse_quick_trade_metadata
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.utils.request_guard import cache_key, guarded_cached
from app.services.live_trading.symbols import to_gate_currency_pair
logger = get_logger(__name__)


from app.services.pending_orders.fee_reconciliation import (
    fee_breakdown_to_quote,
    fee_storage_values,
)


def gate_swap_order_fees(
    client: Any,
    symbol: str,
    order_ids: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    """Return authoritative Gate swap fees grouped by exchange order ID."""
    if not isinstance(client, GateUsdtFuturesClient):
        return {}

    wanted = {str(order_id or "").strip() for order_id in order_ids}
    wanted.discard("")
    if not wanted:
        return {}

    contract = to_gate_currency_pair(symbol)
    rows = client._signed_request(
        "GET",
        "/api/v4/futures/usdt/my_trades",
        params={"contract": contract, "limit": 100},
    )
    if not isinstance(rows, list):
        return {}

    totals: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("contract") or "") != contract:
            continue
        order_id = str(row.get("order_id") or row.get("order") or "").strip()
        if order_id not in wanted:
            continue
        try:
            fee = float(row.get("fee") or 0.0)
        except (TypeError, ValueError):
            continue
        totals[order_id] = totals.get(order_id, 0.0) + fee

    return {
        order_id: {
            "commission": abs(total),
            "commission_ccy": "USDT",
            "commission_quote": abs(total),
        }
        for order_id, total in totals.items()
    }


def fee_update_from_fill(
    client: Any,
    symbol: str,
    fill: Dict[str, Any],
    *,
    fallback_price: float = 0.0,
) -> Dict[str, Any]:
    """Normalize one fill snapshot into Quick Trade fee storage fields."""
    raw_fees = fill.get("fees_by_ccy") if isinstance(fill, dict) else None
    fees: Dict[str, float] = {}
    if isinstance(raw_fees, dict):
        for currency, amount in raw_fees.items():
            try:
                value = float(amount or 0.0)
            except (TypeError, ValueError):
                continue
            fees[str(currency or "").strip().upper() or "UNKNOWN"] = value
    if not fees:
        currency = str(fill.get("fee_ccy") or "").strip().upper()
        if currency and "fee" in fill:
            fees[currency] = float(fill.get("fee") or 0.0)

    status = str(fill.get("fee_status") or "pending").strip().lower()
    if status not in {"actual", "actual_zero"}:
        if fees:
            status = "actual" if any(abs(value) > 1e-18 for value in fees.values()) else "actual_zero"
        else:
            return {}

    commission, commission_ccy = fee_storage_values(fees)
    fill_price = float(fill.get("avg_price") or fallback_price or 0.0)
    commission_quote = fee_breakdown_to_quote(
        client,
        symbol=symbol,
        fees=fees,
        fill_price=fill_price,
    )
    if status == "actual_zero" and commission_quote is None:
        commission_quote = 0.0
    return {
        "commission": abs(float(commission or 0.0)),
        "commission_ccy": commission_ccy,
        "commission_quote": None if commission_quote is None else abs(float(commission_quote)),
        "fees_by_ccy": fees,
        "fee_status": status,
    }


def quick_trade_fee_updates(
    client: Any,
    symbol: str,
    market_type: str,
    rows: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Fetch late exchange fees for multiple Quick Trade rows."""
    candidates = [dict(row or {}) for row in rows]
    updates: Dict[str, Dict[str, Any]] = {}

    if isinstance(client, GateUsdtFuturesClient) and str(market_type or "").lower() == "swap":
        updates.update(gate_swap_order_fees(
            client,
            symbol,
            [row.get("exchange_order_id") for row in candidates],
        ))
        for update in updates.values():
            update["fees_by_ccy"] = {"USDT": update["commission"]}
            update["fee_status"] = "actual" if update["commission"] else "actual_zero"
        candidates = [
            row for row in candidates
            if str(row.get("exchange_order_id") or "").strip() not in updates
        ]

    from app.services.quick_trade.orders import enrich_fill

    for row in candidates[:12]:
        order_id = str(row.get("exchange_order_id") or "").strip()
        if not order_id:
            continue
        fill = enrich_fill(
            client,
            order_id=order_id,
            symbol=symbol,
            market_type=market_type,
            max_wait_sec=0.0,
        )
        update = fee_update_from_fill(
            client,
            symbol,
            fill,
            fallback_price=float(row.get("avg_fill_price") or row.get("price") or 0.0),
        )
        if update:
            updates[order_id] = update
    return updates

def reconcile_quick_trade_fees(
    rows: list[Dict[str, Any]],
    *,
    user_id: int,
    credential_id: int,
    symbol: str,
    market_type: str,
) -> None:
    """Persist authoritative late fee data for visible Quick Trade rows."""
    if credential_id > 0 and symbol and market_type in ("spot", "swap"):
        missing_fee_rows = [
            row for row in rows
            if str(row.get("exchange_order_id") or "").strip()
            and float(row.get("filled_amount") or 0) > 0
            and float(row.get("commission_quote") or 0) <= 0
            and parse_quick_trade_metadata(row.get("raw_result")).get("fee_status")
                not in ("actual", "actual_zero")
        ]
        if missing_fee_rows:
            try:
                config = build_exchange_config(
                    credential_id,
                    user_id,
                    {"market_type": market_type},
                )
                client = create_exchange_client(config, market_type=market_type)
                missing_order_ids = ",".join(
                    str(row.get("exchange_order_id") or "") for row in missing_fee_rows[:12]
                )
                fee_updates = guarded_cached(
                    cache_key(
                        "quick_trade_fee_reconciliation",
                        user_id,
                        credential_id,
                        market_type,
                        symbol,
                        missing_order_ids,
                    ),
                    lambda: quick_trade_fee_updates(
                        client,
                        symbol,
                        market_type,
                        missing_fee_rows,
                    ),
                    ttl_sec=20,
                    stale_ttl_sec=60,
                    timeout_sec=8,
                    namespace="quick_trade_fee_reconciliation",
                    max_concurrent=4,
                )
                if fee_updates:
                    with get_db_connection() as db:
                        cur = db.cursor()
                        for row in missing_fee_rows:
                            update = fee_updates.get(str(row.get("exchange_order_id") or ""))
                            if not update or update.get("commission_quote") is None:
                                continue
                            stored_raw = row.get("raw_result") or {}
                            if isinstance(stored_raw, str):
                                try:
                                    stored_raw = json.loads(stored_raw) or {}
                                except Exception:
                                    stored_raw = {}
                            if not isinstance(stored_raw, dict):
                                stored_raw = {"raw": stored_raw}
                            metadata = stored_raw.get("_quick_trade")
                            if not isinstance(metadata, dict):
                                metadata = {}
                            metadata["fee_status"] = update["fee_status"]
                            metadata["fees_by_ccy"] = update["fees_by_ccy"]
                            stored_raw["_quick_trade"] = metadata
                            cur.execute(
                                """
                                UPDATE qd_quick_trades
                                SET commission = %s,
                                    commission_ccy = %s,
                                    commission_quote = %s,
                                    raw_result = %s
                                WHERE id = %s AND user_id = %s AND credential_id = %s
                                """,
                                (
                                    update["commission"],
                                    update["commission_ccy"],
                                    update["commission_quote"],
                                    json.dumps(stored_raw, ensure_ascii=False),
                                    row.get("id"),
                                    user_id,
                                    credential_id,
                                ),
                            )
                            row.update(update, raw_result=stored_raw)
                        db.commit()
                        cur.close()
            except Exception as exc:
                logger.info("Quick Trade fee reconciliation deferred: %s", exc)
