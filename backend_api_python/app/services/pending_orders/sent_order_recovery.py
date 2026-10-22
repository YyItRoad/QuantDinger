"""Pure helpers for durable submitted-order reconciliation."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def normalize_live_order_status(status: str) -> str:
    normalized = str(status or "").strip().lower().replace("_", "")
    if normalized in ("filled", "complete", "completed"):
        return "filled"
    if normalized in ("cancelled", "canceled", "apicancelled", "inactive", "expired", "rejected"):
        return "cancelled"
    if normalized in ("partiallyfilled", "partial", "partialfill"):
        return "partial"
    if normalized in ("submitted", "presubmitted", "pendingsubmit", "pendingcancel", "open", "new"):
        return "open"
    return "unknown"


def is_final_fill(requested: float, filled: float, avg_price: float, status: Any = "") -> bool:
    requested_qty = max(0.0, float(requested or 0.0))
    filled_qty = max(0.0, float(filled or 0.0))
    if requested_qty <= 0 or filled_qty <= 0 or float(avg_price or 0.0) <= 0:
        return False
    if normalize_live_order_status(status) == "filled":
        return True
    return filled_qty >= requested_qty * 0.999999


def is_final_reconciled_fill(
    requested: float,
    filled: float,
    avg_price: float,
    status: Any,
    phases: Dict[str, Any],
) -> bool:
    final = is_final_fill(requested, filled, avg_price, status)
    close_state = phases.get("close_reconciliation") if isinstance(phases, dict) else None
    if isinstance(close_state, dict):
        return str(close_state.get("status") or "") in {"closed", "dust"}
    return final


def reconciled_close_snapshot(
    *,
    row: Dict[str, Any],
    queue_status: str,
    exchange_status: str,
    aggregate_filled: float,
    aggregate_avg: float,
    requested_qty: float,
    executable_qty: float,
    cumulative_filled: float,
    cumulative_avg: float,
    cumulative_fees: Dict[str, float],
) -> Tuple[str, Dict[str, Any]]:
    try:
        previous = json.loads(str(row.get("exchange_response_json") or "{}")) or {}
    except Exception:
        previous = {}
    phases = previous.get("phases") or {}
    phases = phases if isinstance(phases, dict) else {}
    close_state = phases.get("close_reconciliation") or {}
    if isinstance(close_state, dict) and close_state:
        close_status = str(close_state.get("status") or "").strip().lower()
        target = max(0.0, float(close_state.get("target_quantity") or requested_qty))
        remaining = max(0.0, float(close_state.get("remaining_quantity") or 0.0))
        tolerance = max(1e-12, target * 1e-9)
        if close_status not in {"closed", "dust"} and (
            aggregate_filled + tolerance < target or remaining > tolerance
        ):
            queue_status = "sent"
    snapshot: Dict[str, Any] = {
        "status": exchange_status,
        "filled": aggregate_filled,
        "avg_price": aggregate_avg,
        "requested_qty": requested_qty,
        "executable_qty": executable_qty,
        "live_fill_sync": {
            "tracked_filled": cumulative_filled,
            "tracked_avg_price": cumulative_avg,
            "fees_by_ccy": cumulative_fees,
        },
    }
    if phases:
        snapshot["phases"] = phases
    return queue_status, snapshot


def tracked_fill_baseline(
    row: Dict[str, Any],
    *,
    exchange_order_id: str,
    previous_filled: float,
    previous_avg: float,
) -> Tuple[float, float]:
    """Return the freshest cumulative baseline for the tracked exchange leg.

    ``live_fill_sync`` is a progress snapshot, not an authoritative source: a
    sync can write zero before the executor records the fill in the row.  Keep
    it as a candidate instead of allowing it to hide newer row or executor
    state after a worker restart.

    When the executor summary names this exchange order, the pending-order row
    may aggregate multiple legs.  In that case only the executor summary and
    sync marker compete; otherwise the row aggregate is a valid candidate.
    """
    candidates: List[Tuple[float, float]] = []
    tracked_leg_baseline: Tuple[float, float] | None = None
    try:
        previous_response = json.loads(str(row.get("exchange_response_json") or "{}")) or {}
        sync_state = previous_response.get("live_fill_sync") or {}
        if isinstance(sync_state, dict) and "tracked_filled" in sync_state:
            candidates.append(
                (
                    max(0.0, float(sync_state.get("tracked_filled") or 0.0)),
                    max(0.0, float(sync_state.get("tracked_avg_price") or 0.0)),
                )
            )
        executor_raw = ((previous_response.get("phases") or {}).get("executor") or {})
        market_summary = executor_raw.get("market_summary") or {}
        if (
            isinstance(market_summary, dict)
            and str(market_summary.get("exchange_order_id") or "") == str(exchange_order_id or "")
        ):
            tracked_leg_baseline = (
                max(0.0, float(market_summary.get("filled_qty") or 0.0)),
                max(0.0, float(market_summary.get("avg_price") or 0.0)),
            )
            candidates.append(tracked_leg_baseline)
        close_state = ((previous_response.get("phases") or {}).get("close_reconciliation") or {})
        retry_orders = close_state.get("retry_orders") or []
        for retry_order in retry_orders if isinstance(retry_orders, list) else []:
            if not isinstance(retry_order, dict):
                continue
            if str(retry_order.get("exchange_order_id") or "") != str(exchange_order_id or ""):
                continue
            tracked_leg_baseline = (
                max(0.0, float(retry_order.get("filled_quantity") or 0.0)),
                max(0.0, float(retry_order.get("avg_price") or 0.0)),
            )
            candidates.append(tracked_leg_baseline)
            break
    except Exception:
        pass
    if tracked_leg_baseline is None:
        candidates.append(
            (
                max(0.0, float(previous_filled or 0.0)),
                max(0.0, float(previous_avg or 0.0)),
            )
        )
    return max(candidates, key=lambda item: item[0])
