"""Quick Trade history response helpers."""

from __future__ import annotations

import json
from typing import Any

from app.services.quick_trade.orders import normalize_order_status


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
