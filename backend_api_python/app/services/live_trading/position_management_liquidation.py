"""Reconcile complete Binance liquidations into existing managed-position ledgers."""

from datetime import datetime, timezone
from decimal import Decimal

from app.services.exchange_execution import load_strategy_configs, resolve_exchange_config
from app.services.live_trading.factory import create_client
from app.services.live_trading.fill_accounting import lock_strategy_fills
from app.services.live_trading.leg_context import credential_id_from_exchange_config
from app.services.pending_orders.fill_records import persist_strategy_fill
from app.utils.db import get_db_connection, get_db_transaction
from app.utils.logger import get_logger
from app.utils.trade_close_reason import EXCHANGE_LIQUIDATION

logger = get_logger(__name__)


def _symbol(value):
    return str(value or "").upper().replace("/", "").replace("-", "").split(":")[0]


def _matching_fills(client, position, start_ms):
    """Require one full liquidation order and its complete authoritative fills."""
    symbol, side = position["symbol"], position["side"]
    if side not in {"long", "short"}:
        return []
    native = _symbol(symbol)
    positions = client.get_positions(symbol=symbol)
    if not isinstance(positions, list) or not positions:
        return []
    for row in positions:
        if _symbol(row.get("symbol")) != native:
            return []
        amount = Decimal(str(row.get("positionAmt", "0")))
        if amount and (row.get("positionSide") == side.upper() or
                       (row.get("positionSide") == "BOTH" and (amount > 0) == (side == "long"))):
            return []
    close_side = "SELL" if side == "long" else "BUY"
    candidates = [order for order in client.get_liquidation_orders(symbol=symbol, start_time_ms=start_ms)
                  if _symbol(order.get("symbol")) == native
                  and order.get("side") == close_side
                  and order.get("positionSide") in (side.upper(), "BOTH")
                  and order.get("status") == "FILLED"
                  and int(order.get("time") or 0) >= start_ms
                  and Decimal(str(order.get("executedQty") or 0)) == Decimal(str(position["size"]))]
    if len(candidates) != 1:
        return []
    order = candidates[0]
    fills = client.get_user_trades(symbol=symbol, order_id=str(order["orderId"]), limit=1000,
                                   end_time_ms=int(order.get("updateTime") or order["time"]) + 1)
    if not isinstance(fills, list) or not fills or len(fills) >= 1000:
        return []
    ids = set()
    for fill in fills:
        if (_symbol(fill.get("symbol")) != native or str(fill.get("orderId")) != str(order["orderId"])
                or fill.get("side") != close_side or fill.get("positionSide") not in (side.upper(), "BOTH")
                or int(fill.get("time") or 0) < start_ms or fill.get("id") is None
                or str(fill["id"]) in ids or Decimal(str(fill.get("qty") or 0)) <= 0
                or Decimal(str(fill.get("price") or 0)) <= 0
                or "realizedPnl" not in fill or "commission" not in fill or not fill.get("commissionAsset")):
            return []
        ids.add(str(fill["id"]))
        if not all(Decimal(str(fill[key])).is_finite() for key in ("qty", "price", "realizedPnl", "commission")):
            return []
    if sum(Decimal(str(f["qty"])) for f in fills) != Decimal(str(position["size"])):
        return []
    return sorted(fills, key=lambda f: (int(f["time"]), int(f["id"])))


def _owners(cur, position):
    # Include other strategies, not just management instances, when checking ownership.
    cur.execute("""
        SELECT p.id, p.strategy_id, p.size FROM qd_strategy_positions p
        JOIN qd_strategies_trading s ON s.id = p.strategy_id
        WHERE s.user_id = %s AND s.execution_mode = 'live'
          AND (p.credential_id = %s OR (COALESCE(p.credential_id, 0) = 0
               AND COALESCE(NULLIF(s.exchange_config::jsonb->>'credential_id', ''),
                            s.exchange_config::jsonb->>'credentials_id') = %s))
          AND p.market_type = 'swap' AND p.side = %s
          AND regexp_replace(upper(p.symbol), '[-/_]', '', 'g') = %s AND p.size > 0
        ORDER BY p.id FOR UPDATE OF p
    """, (position["user_id"], position["credential_id"], str(position["credential_id"]), position["side"], _symbol(position["symbol"])))
    rows = cur.fetchall() or []
    return len(rows) == 1 and int(rows[0]["id"]) == int(position["id"]) and Decimal(str(rows[0]["size"])) == Decimal(str(position["size"]))


def _reconcile(position):
    sid = int(position["strategy_id"])
    config = load_strategy_configs(sid)
    exchange_config = resolve_exchange_config(config.get("exchange_config") or {}, user_id=int(position["user_id"]))
    if str(exchange_config.get("exchange_id") or "").lower() != "binance":
        return False
    if int(credential_id_from_exchange_config(exchange_config) or 0) != int(position["credential_id"]):
        return False
    client = create_client(exchange_config, market_type="swap")
    created = position["strategy_created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    fills = _matching_fills(client, position, int(created.timestamp() * 1000))
    if not fills:
        return False
    with get_db_transaction():
        lock_strategy_fills(sid)
        with get_db_connection() as db:
            cur = db.cursor()
            if not _owners(cur, position):
                cur.close()
                return False
            # Account-scoped IDs prevent REST retries from reposting an existing fill.
            cur.execute("""SELECT 1 FROM qd_strategy_trades WHERE credential_id = %s
                AND market_type = 'swap' AND exchange_order_id = %s AND exchange_fill_id IN %s LIMIT 1""",
                (position["credential_id"], str(fills[0]["orderId"]), tuple(str(f["id"]) for f in fills)))
            if cur.fetchone():
                cur.close()
                return False
            remaining = float(position["size"])
            for index, fill in enumerate(fills):
                # Consume the exact float remainder on the last fill to avoid a dust row.
                position_filled = remaining if index == len(fills) - 1 else float(fill["qty"])
                persist_strategy_fill(
                    strategy_id=sid, symbol=position["symbol"], signal_type="close_" + position["side"],
                    filled=float(fill["qty"]), avg_price=float(fill["price"]), market_type="swap",
                    exchange_config=exchange_config, exchange_id="binance", fill_source="liquidation",
                    close_reason=EXCHANGE_LIQUIDATION, profit=float(fill["realizedPnl"]),
                    commission=float(fill["commission"]), commission_ccy=fill["commissionAsset"],
                    fees_by_ccy={fill["commissionAsset"]: float(fill["commission"])},
                    fee_status="actual" if float(fill["commission"]) else "actual_zero", fee_source="rest",
                    commission_quote=float(fill["commission"]) if fill["commissionAsset"] == "USDT" else None,
                    exchange_order_id=str(fill["orderId"]), exchange_fill_id=str(fill["id"]), raw_fill=fill,
                    position_filled=position_filled,
                )
                remaining -= position_filled
                cur.execute("""UPDATE qd_strategy_trades SET created_at = %s
                    WHERE strategy_id = %s AND fill_source = 'liquidation'
                      AND exchange_order_id = %s AND exchange_fill_id = %s""",
                    (datetime.fromtimestamp(int(fill["time"]) / 1000, timezone.utc).replace(tzinfo=None),
                     sid, str(fill["orderId"]), str(fill["id"])))
            cur.close()
    # Confirmed-fill accounting already checks auto-stop; the existing history task archives it.
    return True


def sync_managed_binance_liquidations():
    """Also recover stopped instances whose registered position remains non-flat."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("""
            SELECT p.*, s.user_id, s.created_at AS strategy_created_at
            FROM qd_strategy_positions p JOIN qd_strategies_trading s ON s.id = p.strategy_id
            JOIN qd_exchange_credentials c ON c.id = p.credential_id AND c.user_id = s.user_id
            WHERE s.execution_mode = 'live' AND s.status IN ('running', 'stopped')
              AND s.trading_config::jsonb -> 'position_management' ->> 'enabled' = 'true'
              AND c.exchange_id = 'binance' AND p.market_type = 'swap' AND p.size > 0
            ORDER BY p.id
        """)
        positions = [dict(row) for row in (cur.fetchall() or [])]
        cur.close()
    for position in positions:
        try:
            _reconcile(position)
        except Exception as exc:
            logger.warning("Managed liquidation reconciliation failed strategy=%s: %s", position["strategy_id"], exc)
