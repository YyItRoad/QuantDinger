"""Quick Trade history response helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.services.quick_trade.orders import normalize_order_status


def normalize_entry_reason(value: Any, *, required: bool = False) -> str:
    """Use one plain-text entry reason for market context and the trader's idea."""
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("quickTrade.entryReasonInvalid")
    reason = value.strip()
    if required and not reason:
        raise ValueError("quickTrade.entryReasonRequired")
    return reason


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_quick_trade_metadata(raw_result: Any) -> dict[str, Any]:
    """Extract stable history fields from stored execution metadata."""
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except (TypeError, ValueError):
            raw_result = {}
    candidate = raw_result.get("_quick_trade") if isinstance(raw_result, dict) else {}
    metadata = candidate if isinstance(candidate, dict) else {}
    return {
        "entry_reason": metadata.get("entry_reason") if isinstance(metadata.get("entry_reason"), str) else "",
        "margin_mode": metadata.get("margin_mode") or "",
        "requested_base_qty": _number(metadata.get("requested_base_qty")),
        "notional_usdt": _number(metadata.get("notional_usdt")),
        "amount_semantics": metadata.get("amount_semantics") or "",
        "client_order_id": metadata.get("client_order_id") or "",
        "is_close": bool(metadata.get("is_close")),
        "close_side": str(metadata.get("close_side") or ""),
        "close_scope": str(metadata.get("close_scope") or ""),
        "entry_price": _number(metadata.get("entry_price")),
        "fee_status": str(metadata.get("fee_status") or "pending"),
        "fees_by_ccy": metadata.get("fees_by_ccy") if isinstance(metadata.get("fees_by_ccy"), dict) else {},
    }


def history_order_status(row: dict[str, Any]) -> str:
    """Repair display of legacy Gate terminal orders from persisted evidence."""
    current = str(row.get("status") or "")
    if current not in {"submitted", "partial", "partially_filled", "open", "pending"}:
        return current
    raw = row.get("raw_result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return current
    if not isinstance(raw, dict):
        return current
    metadata = raw.get("_quick_trade") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    status = normalize_order_status(metadata.get("exchange_status") or raw.get("status"), raw)
    return status if status in {"filled", "cancelled", "failed"} else current


def record_quick_trade(
    user_id: int,
    credential_id: int,
    exchange_id: str,
    symbol: str,
    side: str,
    order_type: str,
    amount: float,
    price: float,
    leverage: int,
    market_type: str,
    tp_price: float,
    sl_price: float,
    status: str,
    exchange_order_id: str,
    filled: float,
    avg_price: float,
    error_msg: str,
    source: str,
    raw_result: Dict[str, Any],
    commission: float = 0.0,
    commission_ccy: str = "",
    commission_quote: Optional[float] = None,
    client_order_id: str = "",
    entry_reason: str = "",
    record_id: Optional[int] = None,
    *,
    connection_factory,
    logger,
):
    """Insert a quick trade record into the database."""
    from app.services.live_trading.partner_attribution import redact_partner_attribution

    raw_result = redact_partner_attribution(raw_result or {})
    if entry_reason:
        raw_result = dict(raw_result)
        raw_result["_quick_trade"] = {**(raw_result.get("_quick_trade") or {}), "entry_reason": entry_reason}
    try:
        with connection_factory() as db:
            cur = db.cursor()
            values = (
                user_id, credential_id, exchange_id, symbol, side, order_type,
                amount, price, leverage, market_type, tp_price, sl_price,
                status, exchange_order_id, filled, avg_price,
                float(commission or 0.0), str(commission_ccy or "").strip().upper(),
                float(commission_quote) if commission_quote is not None else None,
                str(client_order_id or "")[:100], error_msg, source, json.dumps(raw_result or {}),
            )
            if record_id:
                cur.execute(
                    """UPDATE qd_quick_trades SET
                    credential_id=%s, exchange_id=%s, symbol=%s, side=%s, order_type=%s,
                    amount=%s, price=%s, leverage=%s, market_type=%s, tp_price=%s, sl_price=%s,
                    status=%s, exchange_order_id=%s, filled_amount=%s, avg_fill_price=%s,
                    commission=%s, commission_ccy=%s, commission_quote=%s,
                    client_order_id=%s, error_msg=%s, source=%s, raw_result=%s
                    WHERE id=%s AND user_id=%s RETURNING id""",
                    (*values[1:], record_id, user_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO qd_quick_trades
                        (user_id, credential_id, exchange_id, symbol, side, order_type,
                         amount, price, leverage, market_type, tp_price, sl_price,
                         status, exchange_order_id, filled_amount, avg_fill_price,
                         commission, commission_ccy, commission_quote,
                         client_order_id, error_msg, source, raw_result, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
                    """,
                    values,
                )
            row = cur.fetchone()
            db.commit()
            cur.close()
            trade_id = int((row or {}).get("id") or 0)
            if trade_id > 0 and exchange_order_id:
                from app.services.execution_streams.repository import ExecutionEventRepository

                ExecutionEventRepository().register_binding(
                    credential_id=int(credential_id or 0),
                    exchange_id=str(exchange_id or ""),
                    market_type=str(market_type or "swap"),
                    owner_type="quick_trade",
                    owner_id=trade_id,
                    user_id=int(user_id or 1),
                    symbol=str(symbol or ""),
                    signal_type=str(side or ""),
                    client_order_id=str(client_order_id or ""),
                    exchange_order_id=str(exchange_order_id or ""),
                    observed_filled=float(filled or 0.0),
                )
            return trade_id
    except Exception as e:
        logger.error(f"Failed to record quick trade: {e}")
        return None
