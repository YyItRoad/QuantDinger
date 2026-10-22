"""Reconcile exchange-side closes that bypass Quick Trade routes."""

from __future__ import annotations

import json
from typing import Any

from app.services.quick_trade.credentials import build_exchange_config, create_exchange_client
from app.services.quick_trade.symbols import is_supported_crypto_exchange
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.utils.request_guard import cache_key, guarded_cached


logger = get_logger(__name__)


def _sync_gate_native_protection_fills(
    *, user_id: int, credential_id: int, symbol: str, market_type: str
) -> int:
    """Import Gate-native TP/SL fills that bypass the Quick Trade order route."""
    if market_type != "swap":
        return 0
    try:
        config = build_exchange_config(
            credential_id,
            user_id,
            {"market_type": market_type},
        )
        exchange_id = str(config.get("exchange_id") or "").strip().lower()
        if exchange_id not in ("gate", "gateio"):
            return 0
        client = create_exchange_client(config, market_type=market_type)
        from app.services.quick_trade.position_activity import gate_native_protection_fills

        payload = guarded_cached(
            cache_key(
                "quick_trade_gate_native_protection_fills",
                user_id,
                credential_id,
                market_type,
                symbol,
            ),
            lambda: gate_native_protection_fills(client, symbol, limit=100),
            ttl_sec=2,
            stale_ttl_sec=15,
            timeout_sec=4,
            namespace="quick_trade_gate_native_protection_fills",
            max_concurrent=4,
        )
        fills = payload.get("fills") if isinstance(payload, dict) else []
        if not fills:
            return 0

        inserted = 0
        with get_db_connection() as db:
            cur = db.cursor()
            for fill in fills:
                order_id = str(fill.get("exchange_order_id") or "").strip()
                if not order_id:
                    continue
                close_side = str(fill.get("close_side") or "").strip().lower()
                entry_side = "buy" if close_side == "long" else "sell"
                cur.execute(
                    """
                    SELECT leverage, avg_fill_price
                    FROM qd_quick_trades
                    WHERE user_id = %s AND credential_id = %s
                      AND UPPER(symbol) = UPPER(%s) AND market_type = %s
                      AND side = %s AND status = 'filled'
                      AND created_at <= %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (
                        user_id,
                        credential_id,
                        symbol,
                        market_type,
                        entry_side,
                        fill.get("created_at"),
                    ),
                )
                entry = cur.fetchone() or {}
                quantity = float(fill.get("filled_amount") or 0)
                avg_price = float(fill.get("avg_fill_price") or 0)
                commission = float(fill.get("commission") or 0)
                metadata = {
                    "requested_base_qty": quantity,
                    "exchange_status": "filled",
                    "is_close": True,
                    "close_side": close_side,
                    "close_scope": "exchange_protection",
                    "entry_price": float(entry.get("avg_fill_price") or 0),
                    "fees_by_ccy": {"USDT": commission} if commission > 0 else {},
                    "fee_status": "actual" if commission > 0 else "actual_zero",
                    "native_protection_fill": True,
                }
                raw_result = {
                    "gate_auto_order": {
                        "text": str(fill.get("client_order_id") or ""),
                        "exchange_fill_ids": list(fill.get("exchange_fill_ids") or []),
                    },
                    "_quick_trade": metadata,
                }
                cur.execute(
                    """
                    INSERT INTO qd_quick_trades
                        (user_id, credential_id, exchange_id, symbol, side, order_type,
                         amount, price, leverage, market_type, tp_price, sl_price,
                         status, exchange_order_id, filled_amount, avg_fill_price,
                         commission, commission_ccy, commission_quote,
                         client_order_id, error_msg, source, raw_result, created_at)
                    SELECT %s, %s, %s, %s, %s, 'market',
                           %s, 0, %s, %s, 0, 0,
                           'filled', %s, %s, %s,
                           %s, 'USDT', %s,
                           %s, '', 'quick_trade', %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM qd_quick_trades
                        WHERE user_id = %s AND credential_id = %s
                          AND market_type = %s AND exchange_order_id = %s
                    )
                    RETURNING id
                    """,
                    (
                        user_id,
                        credential_id,
                        exchange_id,
                        symbol,
                        str(fill.get("side") or ""),
                        quantity * avg_price,
                        int(entry.get("leverage") or 1),
                        market_type,
                        order_id,
                        quantity,
                        avg_price,
                        commission,
                        commission,
                        str(fill.get("client_order_id") or "")[:100],
                        json.dumps(raw_result, ensure_ascii=False),
                        fill.get("created_at"),
                        user_id,
                        credential_id,
                        market_type,
                        order_id,
                    ),
                )
                if cur.fetchone():
                    inserted += 1
            db.commit()
            cur.close()
        return inserted
    except Exception as exc:
        logger.info("Gate native protection fill sync deferred: %s", exc)
        return 0


def _json_safe_close_snapshot(snapshot: Any) -> Any:
    if not isinstance(snapshot, dict):
        return snapshot
    result = dict(snapshot)
    closed_at = result.get("closed_at")
    if hasattr(closed_at, "isoformat"):
        result["closed_at"] = closed_at.isoformat()
    components = []
    for value in result.get("components") or []:
        row = dict(value) if isinstance(value, dict) else {}
        timestamp = row.get("closed_at")
        if hasattr(timestamp, "isoformat"):
            row["closed_at"] = timestamp.isoformat()
        components.append(row)
    result["components"] = components
    return result


def sync_exchange_close_fills(
    *, user_id: int, credential_id: int, symbol: str, market_type: str
) -> int:
    """Import exchange-side closes that bypassed the Quick Trade order route."""
    market = str(market_type or "swap").strip().lower()
    try:
        config = build_exchange_config(credential_id, user_id, {"market_type": market})
        exchange_id = str(config.get("exchange_id") or "").strip().lower()
        if exchange_id in ("gate", "gateio") and market == "swap":
            return _sync_gate_native_protection_fills(
                user_id=user_id,
                credential_id=credential_id,
                symbol=symbol,
                market_type=market,
            )
        if exchange_id != "alpaca" and not is_supported_crypto_exchange(exchange_id):
            return 0

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT id, side, amount, leverage, status, exchange_order_id,
                       filled_amount, avg_fill_price, commission, commission_quote,
                       raw_result, created_at
                FROM qd_quick_trades
                WHERE user_id = %s AND credential_id = %s
                  AND UPPER(symbol) = UPPER(%s) AND market_type = %s
                ORDER BY created_at ASC, id ASC
                """,
                (user_id, credential_id, symbol, market),
            )
            rows = cur.fetchall() or []
            cur.close()
        if not rows:
            return 0

        from app.services.live_trading.exchange_close_history import resolve_exchange_external_close
        from app.services.quick_trade.position_activity import tracked_quick_trade_legs

        tracked = tracked_quick_trade_legs(rows, market)
        legs = tracked.get("legs") or []
        recorded_ids = set(tracked.get("recorded_ids") or set())
        if not legs:
            return 0
        client = create_exchange_client(config, market_type=market)
        inserted = 0
        for leg in legs:
            quantity = float(leg.get("quantity") or 0)
            opened_at = leg.get("opened_at")
            if quantity <= 0 or not opened_at:
                continue

            def _load_close():
                return _json_safe_close_snapshot(
                    resolve_exchange_external_close(
                        client,
                        symbol=symbol,
                        side=str(leg.get("side") or "long"),
                        target_qty=quantity,
                        opened_after=opened_at,
                        market_type=market,
                        is_recorded=lambda value: str(value or "") in recorded_ids,
                    )
                )

            snapshot = guarded_cached(
                cache_key(
                    "quick_trade_exchange_close_fills",
                    user_id,
                    credential_id,
                    market,
                    symbol,
                    leg.get("side"),
                    quantity,
                ),
                _load_close,
                ttl_sec=2,
                stale_ttl_sec=15,
                timeout_sec=5,
                namespace="quick_trade_exchange_close_fills",
                max_concurrent=4,
                cache_if=lambda value: bool(value),
            )
            if not isinstance(snapshot, dict):
                continue
            components = [row for row in snapshot.get("components") or [] if isinstance(row, dict)]
            fill_ids = [str(row.get("exchange_fill_id") or "") for row in components if row.get("exchange_fill_id")]
            order_id = str(snapshot.get("exchange_order_id") or "").strip()
            if not order_id and fill_ids:
                order_id = f"fill-{exchange_id}-{fill_ids[-1]}"[:120]
            if not order_id:
                continue
            avg_price = float(snapshot.get("price") or 0)
            commission = abs(float(snapshot.get("fee") or 0))
            fee_ccy = str(snapshot.get("fee_ccy") or ("USD" if exchange_id == "alpaca" else "USDT")).upper()
            commission_quote = commission if fee_ccy in {"USD", "USDT", "USDC"} else None
            close_side = str(leg.get("side") or "").lower()
            side = "sell" if close_side == "long" else "buy"
            metadata = {
                "requested_base_qty": quantity,
                "exchange_status": "filled",
                "is_close": True,
                "close_side": close_side,
                "close_scope": "exchange_protection",
                "entry_price": float(leg.get("entry_price") or 0),
                "fees_by_ccy": {fee_ccy: commission} if commission > 0 else {},
                "fee_status": "actual" if commission > 0 else "actual_zero",
                "native_protection_fill": True,
                "close_reason": str(snapshot.get("close_reason") or "exchange_native_close"),
            }
            raw_result = {
                "exchange_close": {
                    "exchange_fill_ids": fill_ids,
                    "components": components,
                    "source": str(snapshot.get("source") or "fills_history"),
                },
                "_quick_trade": metadata,
            }
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    """
                    INSERT INTO qd_quick_trades
                        (user_id, credential_id, exchange_id, symbol, side, order_type,
                         amount, price, leverage, market_type, tp_price, sl_price,
                         status, exchange_order_id, filled_amount, avg_fill_price,
                         commission, commission_ccy, commission_quote,
                         client_order_id, error_msg, source, raw_result, created_at)
                    SELECT %s, %s, %s, %s, %s, 'market',
                           %s, 0, %s, %s, 0, 0,
                           'filled', %s, %s, %s,
                           %s, %s, %s,
                           %s, '', 'quick_trade', %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM qd_quick_trades
                        WHERE user_id = %s AND credential_id = %s
                          AND market_type = %s AND exchange_order_id = %s
                    )
                    RETURNING id
                    """,
                    (
                        user_id,
                        credential_id,
                        exchange_id,
                        symbol,
                        side,
                        quantity * avg_price,
                        int(leg.get("leverage") or 1),
                        market,
                        order_id,
                        quantity,
                        avg_price,
                        commission,
                        fee_ccy,
                        commission_quote,
                        str(snapshot.get("client_order_id") or "")[:100],
                        json.dumps(raw_result, ensure_ascii=False),
                        snapshot.get("closed_at"),
                        user_id,
                        credential_id,
                        market,
                        order_id,
                    ),
                )
                if cur.fetchone():
                    inserted += 1
                    recorded_ids.add(order_id)
                    recorded_ids.update(fill_ids)
                db.commit()
                cur.close()
        return inserted
    except Exception as exc:
        logger.info("Exchange native protection fill sync deferred: %s", exc)
        return 0
