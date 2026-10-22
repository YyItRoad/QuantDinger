"""Recovery helpers for submitted live-order reconciliation."""

from __future__ import annotations

from typing import Any, Dict

from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


def restore_live_sent_order_after_sync_error(row: Dict[str, Any], exc: Exception) -> None:
    """Release a reconciliation claim so a later poll can retry it."""
    order_id = int(row.get("id") or 0)
    if order_id <= 0:
        return
    error = str(exc).strip() or type(exc).__name__
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE pending_orders
                SET status = 'sent',
                    dispatch_note = 'live_fill_sync:retry_error',
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s AND status = 'syncing'
                """,
                (error[:1000], order_id),
            )
            db.commit()
            cur.close()
    except Exception as restore_exc:
        logger.error(
            "Live fill sync recovery failed: pending_id=%s exchange=%s err=%s",
            order_id,
            row.get("exchange_id"),
            restore_exc,
            exc_info=True,
        )
