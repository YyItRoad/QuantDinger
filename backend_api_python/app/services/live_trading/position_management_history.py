"""将已停止的持仓管理策略成交同步到独立历史表。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, Dict, Iterable
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.utils.pnl import calc_pnl_percent


class PositionManagementHistorySyncError(ValueError):
    """持仓管理交易历史无法安全同步。"""


logger = get_logger(__name__)


HISTORY_COLUMNS = (
    "source_trade_id",
    "source_strategy_id",
    "user_id",
    "strategy_name",
    "strategy_type",
    "market_category",
    "execution_mode",
    "strategy_status",
    "timeframe",
    "leverage",
    "entry_time",
    "strategy_run_id",
    "strategy_started_at",
    "strategy_stopped_at",
    "strategy_stop_reason",
    "exchange_id",
    "credential_id",
    "exchange_account_name",
    "market_type",
    "inst_id",
    "settlement_ccy",
    "pending_order_id",
    "order_intent_id",
    "order_type",
    "order_status",
    "requested_amount",
    "filled_amount",
    "exchange_order_id",
    "client_order_id",
    "signal_at",
    "order_created_at",
    "order_sent_at",
    "exit_time",
    "symbol",
    "symbol_canonical",
    "trade_type",
    "position_side",
    "entry_price",
    "exit_price",
    "trade_value",
    "profit_rate",
    "profit",
    "profit_ccy",
    "close_reason",
    "trade_recorded_at",
    "fill_source",
    "grid_order_id",
    "grid_matched_profit",
    "execution_event_id",
    "exchange_fill_id",
    "commission",
    "commission_ccy",
    "commission_quote",
    "fee_status",
    "fee_source",
    "funding_fee_total",
    "funding_fee_ccy",
)


def _history_json_value(value: Any) -> Any:
    """Convert database values to a stable JSON representation.

    The history table uses timezone-naive ``TIMESTAMP`` columns whose values
    are written in UTC.  Appending ``Z`` prevents browsers from interpreting
    those timestamps as local time.
    """
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return f"{normalized.isoformat()}Z"
    if isinstance(value, Decimal):
        return float(value)
    return value


def _public_history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _history_json_value(value) for key, value in dict(row).items()}


def list_position_management_trade_history(
    *,
    user_id: int,
    credential_id: int,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """Return one exchange account's archived trades using server pagination."""
    uid = int(user_id or 0)
    cid = int(credential_id or 0)
    current_page = max(1, int(page or 1))
    per_page = min(100, max(1, int(page_size or 20)))
    if uid <= 0:
        raise PositionManagementHistorySyncError("user_id 必须大于 0")
    if cid <= 0:
        raise PositionManagementHistorySyncError("credential_id 必须大于 0")

    offset = (current_page - 1) * per_page
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM qd_position_management_trade_history
                WHERE user_id = %s AND credential_id = %s
                """,
                (uid, cid),
            )
            total = int((cur.fetchone() or {}).get("total") or 0)
            cur.execute(
                """
                SELECT *
                FROM qd_position_management_trade_history
                WHERE user_id = %s AND credential_id = %s
                ORDER BY exit_time DESC NULLS LAST, id DESC
                LIMIT %s OFFSET %s
                """,
                (uid, cid, per_page, offset),
            )
            items = [_public_history_row(row) for row in (cur.fetchall() or [])]
            return {
                "items": items,
                "total": total,
                "page": current_page,
                "page_size": per_page,
            }
        finally:
            cur.close()


def _object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _signal_time(value: Any) -> datetime | None:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp >= 1_000_000_000_000:
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _position_side(trade_type: Any) -> str:
    value = str(trade_type or "").strip().lower()
    if "long" in value:
        return "long"
    if "short" in value:
        return "short"
    return ""


def _settlement_currency(symbol: Any, funding_currency: Any) -> str:
    funding = str(funding_currency or "").strip().upper()
    if funding and funding != "MIXED":
        return funding
    raw = str(symbol or "").strip().upper()
    if "/" not in raw:
        return funding
    return raw.split("/", 1)[1].split(":", 1)[0].strip()


def _history_row(
    row: Dict[str, Any],
    *,
    funding_total: float,
    funding_currency: str,
) -> Dict[str, Any]:
    entry_price = float(row.get("matched_entry_price") or 0.0)
    filled_amount = float(row.get("trade_amount") or 0.0)
    profit = float(row.get("trade_profit") or 0.0)
    leverage = float(row.get("strategy_leverage") or 1.0)
    market_type = str(row.get("trade_market_type") or "").strip().lower()
    settlement_ccy = _settlement_currency(row.get("trade_symbol"), funding_currency)
    credential_id = int(row.get("trade_credential_id") or row.get("order_credential_id") or 0)
    exchange_id = str(row.get("order_exchange_id") or row.get("credential_exchange_id") or "")

    return {
        "source_trade_id": int(row.get("trade_id") or 0),
        "source_strategy_id": int(row.get("strategy_id") or 0),
        "user_id": int(row.get("trade_user_id") or 0),
        "strategy_name": str(row.get("strategy_name") or ""),
        "strategy_type": str(row.get("strategy_type") or ""),
        "market_category": str(row.get("market_category") or ""),
        "execution_mode": str(row.get("execution_mode") or ""),
        "strategy_status": str(row.get("strategy_status") or ""),
        "timeframe": str(row.get("strategy_timeframe") or ""),
        "leverage": leverage,
        "entry_time": row.get("strategy_created_at"),
        "strategy_run_id": int(row.get("trade_strategy_run_id") or 0),
        "strategy_started_at": row.get("strategy_started_at"),
        "strategy_stopped_at": row.get("strategy_stopped_at"),
        "strategy_stop_reason": str(row.get("strategy_stop_reason") or ""),
        "exchange_id": exchange_id,
        "credential_id": credential_id,
        "exchange_account_name": str(row.get("exchange_account_name") or ""),
        "market_type": market_type,
        "inst_id": str(row.get("trade_inst_id") or ""),
        "settlement_ccy": settlement_ccy,
        "pending_order_id": int(row.get("trade_pending_order_id") or 0),
        "order_intent_id": int(row.get("trade_order_intent_id") or 0),
        "order_type": str(row.get("order_type") or ""),
        "order_status": str(row.get("order_status") or ""),
        "requested_amount": float(row.get("requested_amount") or 0.0),
        "filled_amount": filled_amount,
        "exchange_order_id": str(row.get("exchange_order_id") or ""),
        "client_order_id": str(row.get("client_order_id") or ""),
        "signal_at": _signal_time(row.get("signal_ts")),
        "order_created_at": row.get("order_created_at"),
        "order_sent_at": row.get("order_sent_at"),
        "exit_time": row.get("order_executed_at") or row.get("trade_created_at"),
        "symbol": str(row.get("trade_symbol") or ""),
        "symbol_canonical": str(row.get("symbol_canonical") or row.get("trade_symbol") or ""),
        "trade_type": str(row.get("trade_type") or ""),
        "position_side": _position_side(row.get("trade_type")),
        "entry_price": entry_price,
        "exit_price": float(row.get("trade_price") or 0.0),
        "trade_value": float(row.get("trade_value") or 0.0),
        "profit_rate": calc_pnl_percent(
            entry_price,
            filled_amount,
            profit,
            leverage=leverage,
            market_type=market_type,
        ),
        "profit": profit,
        "profit_ccy": settlement_ccy,
        "close_reason": str(row.get("close_reason") or ""),
        "trade_recorded_at": row.get("trade_created_at"),
        "fill_source": str(row.get("fill_source") or ""),
        "grid_order_id": int(row.get("grid_order_id") or 0),
        "grid_matched_profit": row.get("grid_matched_profit"),
        "execution_event_id": int(row.get("execution_event_id") or 0),
        "exchange_fill_id": str(row.get("exchange_fill_id") or ""),
        "commission": float(row.get("commission") or 0.0),
        "commission_ccy": str(row.get("commission_ccy") or ""),
        "commission_quote": row.get("commission_quote"),
        "fee_status": str(row.get("fee_status") or ""),
        "fee_source": str(row.get("fee_source") or ""),
        "funding_fee_total": float(funding_total or 0.0),
        "funding_fee_ccy": str(funding_currency or ""),
    }


def _insert_history_rows(cur: Any, rows: Iterable[Dict[str, Any]]) -> None:
    columns = ", ".join(HISTORY_COLUMNS)
    placeholders = ", ".join(["%s"] * len(HISTORY_COLUMNS))
    sql = (
        f"INSERT INTO qd_position_management_trade_history ({columns}) "
        f"VALUES ({placeholders}) "
        "ON CONFLICT (source_trade_id) DO NOTHING"
    )
    for row in rows:
        cur.execute(sql, tuple(row[column] for column in HISTORY_COLUMNS))


def sync_position_management_trade_history(
    strategy_id: int,
    *,
    user_id: int | None = None,
) -> Dict[str, Any]:
    """同步一个已停止且已清仓的持仓管理策略，不删除原策略。"""
    sid = int(strategy_id or 0)
    if sid <= 0:
        raise PositionManagementHistorySyncError("strategy_id 必须大于 0")

    with get_db_connection() as db:
        cur = db.cursor()
        try:
            where_user = " AND s.user_id = %s" if user_id is not None else ""
            params = (sid, int(user_id)) if user_id is not None else (sid,)
            cur.execute(
                f"""
                SELECT s.id, s.user_id, s.status, s.trading_config
                FROM qd_strategies_trading s
                WHERE s.id = %s{where_user}
                """,
                params,
            )
            strategy = cur.fetchone()
            if not strategy:
                raise PositionManagementHistorySyncError("策略不存在")
            strategy = dict(strategy)
            position_management = _object(_object(strategy.get("trading_config")).get("position_management"))
            if position_management.get("enabled") is not True:
                raise PositionManagementHistorySyncError("不是持仓管理策略")
            if str(strategy.get("status") or "").strip().lower() != "stopped":
                raise PositionManagementHistorySyncError("策略尚未停止")

            cur.execute(
                """
                SELECT COUNT(*) AS position_count
                FROM qd_strategy_positions
                WHERE strategy_id = %s AND COALESCE(size, 0) > 0
                """,
                (sid,),
            )
            position_count = int((cur.fetchone() or {}).get("position_count") or 0)
            if position_count > 0:
                raise PositionManagementHistorySyncError("策略仍有持仓")

            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS funding_total,
                       CASE WHEN COUNT(DISTINCT asset) = 1 THEN MIN(asset)
                            WHEN COUNT(DISTINCT asset) > 1 THEN 'MIXED'
                            ELSE '' END AS funding_currency
                FROM qd_strategy_funding_fees
                WHERE strategy_id = %s
                """,
                (sid,),
            )
            funding = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT t.id AS trade_id,
                       t.user_id AS trade_user_id,
                       t.strategy_id,
                       t.symbol AS trade_symbol,
                       t.symbol_canonical,
                       t.type AS trade_type,
                       t.price AS trade_price,
                       t.amount AS trade_amount,
                       t.value AS trade_value,
                       t.commission,
                       t.commission_ccy,
                       t.commission_quote,
                       t.profit AS trade_profit,
                       t.close_reason,
                       t.matched_entry_price,
                       t.grid_matched_profit,
                       t.market_type AS trade_market_type,
                       t.credential_id AS trade_credential_id,
                       t.inst_id AS trade_inst_id,
                       t.fill_source,
                       t.pending_order_id AS trade_pending_order_id,
                       t.grid_order_id,
                       t.strategy_run_id AS trade_strategy_run_id,
                       t.order_intent_id AS trade_order_intent_id,
                       t.execution_event_id,
                       t.exchange_fill_id,
                       t.fee_status,
                       t.fee_source,
                       t.created_at AS trade_created_at,
                       s.strategy_name,
                       s.strategy_type,
                       s.market_category,
                       s.execution_mode,
                       s.status AS strategy_status,
                       s.timeframe AS strategy_timeframe,
                       s.leverage AS strategy_leverage,
                       s.created_at AS strategy_created_at,
                       r.started_at AS strategy_started_at,
                       r.stopped_at AS strategy_stopped_at,
                       r.stop_reason AS strategy_stop_reason,
                       po.exchange_id AS order_exchange_id,
                       po.credential_id AS order_credential_id,
                       po.order_type,
                       po.status AS order_status,
                       po.amount AS requested_amount,
                       po.exchange_order_id,
                       po.client_order_id,
                       po.signal_ts,
                       po.created_at AS order_created_at,
                       po.sent_at AS order_sent_at,
                       po.executed_at AS order_executed_at,
                       ec.name AS exchange_account_name,
                       ec.exchange_id AS credential_exchange_id
                FROM qd_strategy_trades t
                JOIN qd_strategies_trading s ON s.id = t.strategy_id
                LEFT JOIN pending_orders po ON po.id = t.pending_order_id
                LEFT JOIN strategy_runs r ON r.id = t.strategy_run_id
                LEFT JOIN qd_exchange_credentials ec
                  ON ec.id = CASE WHEN COALESCE(t.credential_id, 0) > 0
                                  THEN t.credential_id ELSE po.credential_id END
                WHERE t.strategy_id = %s
                ORDER BY t.id ASC
                """,
                (sid,),
            )
            source_rows = [dict(row) for row in (cur.fetchall() or [])]
            if not source_rows:
                raise PositionManagementHistorySyncError("策略没有可同步的交易记录")

            last_trade_id = int(source_rows[-1].get("trade_id") or 0)
            history_rows = [
                _history_row(
                    row,
                    funding_total=(
                        float(funding.get("funding_total") or 0.0)
                        if int(row.get("trade_id") or 0) == last_trade_id
                        else 0.0
                    ),
                    funding_currency=(
                        str(funding.get("funding_currency") or "")
                        if int(row.get("trade_id") or 0) == last_trade_id
                        else ""
                    ),
                )
                for row in source_rows
            ]
            cur.execute(
                """
                SELECT COUNT(*) AS synced_count
                FROM qd_strategy_trades t
                JOIN qd_position_management_trade_history h
                  ON h.source_trade_id = t.id
                WHERE t.strategy_id = %s
                """,
                (sid,),
            )
            already_synced_count = int((cur.fetchone() or {}).get("synced_count") or 0)
            _insert_history_rows(cur, history_rows)

            cur.execute(
                """
                SELECT COUNT(*) AS missing_count
                FROM qd_strategy_trades t
                LEFT JOIN qd_position_management_trade_history h
                  ON h.source_trade_id = t.id
                WHERE t.strategy_id = %s AND h.id IS NULL
                """,
                (sid,),
            )
            missing_count = int((cur.fetchone() or {}).get("missing_count") or 0)
            if missing_count:
                raise PositionManagementHistorySyncError(
                    f"仍有 {missing_count} 条交易未写入历史表"
                )
            inserted_count = len(source_rows) - already_synced_count
            db.commit()
            return {
                "strategy_id": sid,
                "source_count": len(source_rows),
                "inserted_count": inserted_count,
                "already_synced_count": already_synced_count,
                "complete": True,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()


def sync_stopped_position_management_history() -> Dict[str, Any]:
    """同步已停止且存在未归档成交的持仓管理策略，不删除任何数据。"""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT s.id
                FROM qd_strategies_trading s
                WHERE LOWER(COALESCE(s.status, '')) = 'stopped'
                  AND COALESCE(s.trading_config -> 'position_management' ->> 'enabled', 'false') = 'true'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qd_strategy_positions p
                      WHERE p.strategy_id = s.id AND COALESCE(p.size, 0) > 0
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM qd_strategy_trades t
                      LEFT JOIN qd_position_management_trade_history h
                        ON h.source_trade_id = t.id
                      WHERE t.strategy_id = s.id AND h.id IS NULL
                  )
                ORDER BY s.id ASC
                """,
                (),
            )
            strategy_ids = [int(row["id"]) for row in (cur.fetchall() or [])]
        finally:
            cur.close()

    archived = []
    skipped = []
    inserted_count = 0
    already_synced_count = 0
    for strategy_id in strategy_ids:
        try:
            result = sync_position_management_trade_history(strategy_id)
            archived.append(strategy_id)
            inserted_count += int(result.get("inserted_count") or 0)
            already_synced_count += int(result.get("already_synced_count") or 0)
        except PositionManagementHistorySyncError as exc:
            skipped.append({"strategy_id": strategy_id, "reason": str(exc)})
            logger.warning("持仓管理策略 %s 未归档：%s", strategy_id, exc)
        except Exception as exc:
            skipped.append({"strategy_id": strategy_id, "reason": str(exc)})
            logger.exception("持仓管理策略 %s 归档失败", strategy_id)

    return {
        "scan_scope": "stopped_with_unsynced_trades",
        "scanned_count": len(strategy_ids),
        "archived_strategy_ids": archived,
        "archived_count": len(archived),
        "inserted_count": inserted_count,
        "already_synced_count": already_synced_count,
        "skipped": skipped,
        "deleted_count": 0,
    }
