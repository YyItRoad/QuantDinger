"""Safe, extensible configuration snapshots for managed trade archives."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

from app.utils.db import get_db_connection


def history_ext_from_run(snapshot: Any, run_id: int | None) -> dict:
    """Copy supported public settings from an exact immutable run snapshot."""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return {}
    if not isinstance(snapshot, dict) or int(run_id or 0) <= 0:
        return {}
    params = snapshot.get("params")
    if not isinstance(params, dict):
        return {}
    value = params.get("fixed_stop_price")
    if isinstance(value, bool):
        return {}
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not math.isfinite(price) or price <= 0:
        return {}
    # Never archive the full runtime config: it may contain credentials.
    return {
        "schema_version": 1,
        "params": {"fixed_stop_price": price},
        "snapshot_source": "strategy_run",
    }


def backfill_history_ext(*, user_id: int, apply: bool = False,
                         after_id: int = 0, limit: int = 1000) -> dict:
    """Explicit bounded repair; missing or ambiguous sources remain untouched."""
    if user_id <= 0 or after_id < 0 or not 1 <= limit <= 10000:
        raise ValueError("Invalid user_id, after_id or limit")
    result = {"scanned": 0, "eligible": 0, "updated": 0, "skipped": 0,
              "last_id": after_id, "apply": apply}
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """SELECT h.id, h.ext, h.strategy_run_id, r.parameter_snapshot_json
                   FROM qd_position_management_trade_history h
                   LEFT JOIN strategy_runs r
                     ON r.id = h.strategy_run_id
                    AND r.strategy_id = h.source_strategy_id
                    AND r.user_id = h.user_id
                    AND r.credential_id = h.credential_id
                    AND r.market_type = h.market_type
                   WHERE h.user_id = %s AND h.id > %s
                   ORDER BY h.id LIMIT %s""",
                (user_id, after_id, limit),
            )
            rows = cur.fetchall() or []
            for row in rows:
                result["scanned"] += 1
                result["last_id"] = int(row["id"])
                ext = row.get("ext")
                if not isinstance(ext, dict) or not isinstance(ext.get("params", {}), dict):
                    result["skipped"] += 1
                    continue
                if "fixed_stop_price" in ext.get("params", {}):
                    result["skipped"] += 1
                    continue
                addition = history_ext_from_run(row.get("parameter_snapshot_json"), row["strategy_run_id"])
                if not addition:
                    result["skipped"] += 1
                    continue
                result["eligible"] += 1
                if not apply:
                    continue
                merged = deepcopy(ext)
                merged.setdefault("schema_version", addition["schema_version"])
                merged.setdefault("snapshot_source", addition["snapshot_source"])
                merged.setdefault("params", {}).update(addition["params"])
                cur.execute(
                    """UPDATE qd_position_management_trade_history SET ext = %s::jsonb
                       WHERE id = %s AND user_id = %s AND ext = %s::jsonb""",
                    (json.dumps(merged), row["id"], user_id, json.dumps(ext)),
                )
                result["updated"] += int(cur.rowcount or 0)
            if apply:
                db.commit()
            else:
                db.rollback()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()
