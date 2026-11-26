"""Queue a confirmed discipline exit and reconcile its original Quick Trade request."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

from app.services.exchange_execution import resolve_exchange_config
from app.services.live_trading.binance import BinanceFuturesClient
from app.services.live_trading.execution_lock import TradeExecutionBusy, trade_execution_key, trade_execution_lock
from app.services.live_trading.factory import create_client
from app.trade_discipline.position_evidence import read_position_evidence
from app.services.live_trading.position_management import _management_leg_lock, read_account_position_inventory
from app.services.live_trading.records import normalize_strategy_symbol
from app.services.quick_trade.history import record_quick_trade
from app.services.quick_trade.orders import quick_order_status, submit_market_close
from app.trade_discipline.observation import _next_hour, _observe_position
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)
_SOURCE = "trade_discipline"
_QUEUED = "discipline_queued"
_SENT = "discipline_sent"
_SCOPE = "source = 'trade_discipline' AND (raw_result->'_discipline_exit'->>'key') IS NOT NULL"
_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def exit_start_from_env():
    if any(os.getenv(name, "").strip().lower() not in {"1", "true", "yes", "on"}
           for name in ("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "TRADE_DISCIPLINE_AUTO_EXIT_ENABLED")):
        return None
    try:
        start = datetime.fromisoformat(os.getenv("TRADE_DISCIPLINE_EXIT_FROM", ""))
        if start.utcoffset() is not None:
            return start
    except ValueError:
        pass
    logger.error("Trade discipline auto exit requires a timezone-aware TRADE_DISCIPLINE_EXIT_FROM")
    return None


def _write(sql, params):
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        db.commit()
        cur.close()
        return dict(row) if row else None


def queue_observed_exit(*, user_id, credential_id, position, observed_at, exit_from):
    # 只有手动归属、未管理、成交可核对且明确违规的仓位才允许登记退出请求。
    reason = position.get("reason")
    manual = position.get("manual_opening") is True
    if (not manual or position.get("outcome") != "proposed_exit" or reason not in {"no_plan", "ema25_direction_blocked"}
            or position.get("management_status") != "unregistered"
            or position.get("evidence_status") != "reconciled"
            or not position.get("opening_fill_id") or not position.get("last_fill_id")):
        return {"status": "deferred", "reason": "attribution_unconfirmed"}
    if datetime.fromisoformat(position["opened_at"]) < exit_from:
        return {"status": "deferred", "reason": "opened_before_exit_enabled"}

    plan_checks = position.get("plan_checks") or []
    expected_plan = "matched" if reason == "ema25_direction_blocked" else "no_plan"
    if not plan_checks or any(plan["status"] != expected_plan or not plan.get("submitted_at") for plan in plan_checks):
        return {"status": "deferred", "reason": "plan_evidence_unavailable"}
    side, instrument = position["side"], position["instrument"]
    if (side not in {"long", "short"} or instrument == "USDT" or not instrument.endswith("USDT")
            or not instrument.isascii() or not instrument.isalnum()):
        return {"status": "deferred", "reason": "unsupported_position"}
    cap = Decimal(str(position["proposed_quantity"]))
    if not cap.is_finite() or cap <= 0:
        return {"status": "deferred", "reason": "invalid_quantity"}
    key = hashlib.sha256(
        f'{user_id}|{credential_id}|{instrument}|{side}|{position["opening_fill_id"]}|{position["last_fill_id"]}'.encode()
    ).hexdigest()
    # 本轮请求在下一整点过期，同一开仓成交与尾笔成交用同一键去重。
    meta = {name: position[name] for name in ("opened_at", "opening_fill_id", "last_fill_id", "instrument")}
    meta.update(key=key, cap=str(cap), manual_opening=manual, reason=reason, expires_at=int(_next_hour(observed_at).timestamp()),
                entry_order_times={str(plan["order_id"]): plan["submitted_at"] for plan in plan_checks})
    payload = {"_discipline_exit": meta, "_quick_trade": {"is_close": True, "close_side": side}}
    row = _write(
        f"""INSERT INTO qd_quick_trades
            (user_id, credential_id, exchange_id, market_type, symbol, side, source, status, raw_result)
            VALUES (%s, %s, 'binance', 'swap', %s, %s, '{_SOURCE}', '{_QUEUED}', %s::jsonb)
            ON CONFLICT (user_id, credential_id, (raw_result->'_discipline_exit'->>'key')) WHERE {_SCOPE}
            DO UPDATE SET raw_result = EXCLUDED.raw_result
            WHERE qd_quick_trades.status = '{_QUEUED}'
              AND (qd_quick_trades.raw_result->'_discipline_exit'->>'expires_at')::bigint
                   < (EXCLUDED.raw_result->'_discipline_exit'->>'expires_at')::bigint
            RETURNING id, status""",
        (user_id, credential_id, normalize_strategy_symbol(instrument), "sell" if side == "long" else "buy",
         json.dumps(payload)),
    )
    return {"status": "queued", "request_id": row["id"]} if row else {"status": "existing", "key": key}


def _read_rows(user_id, credential_id, *, row_id=None):
    where = "id = %s" if row_id is not None else f"""
        (status = '{_SENT}' OR (status = '{_QUEUED}'
            AND (raw_result->'_discipline_exit'->>'expires_at')::bigint > EXTRACT(EPOCH FROM NOW())))
        AND COALESCE((raw_result->'_discipline_exit'->>'checked_at')::bigint, 0) <= EXTRACT(EPOCH FROM NOW()) - 30"""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"""SELECT * FROM qd_quick_trades WHERE user_id = %s AND credential_id = %s AND {_SCOPE}
                AND {where}
                ORDER BY COALESCE((raw_result->'_discipline_exit'->>'checked_at')::bigint, 0), id
                LIMIT 4""",
            (user_id, credential_id, row_id) if row_id is not None else (user_id, credential_id),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.close()
        return rows


def _has_unresolved_exit(row):
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"""SELECT id FROM qd_quick_trades WHERE {_SCOPE}
                AND user_id = %s AND credential_id = %s AND symbol = %s AND side = %s AND status = '{_SENT}' LIMIT 1""",
            (row["user_id"], row["credential_id"], row["symbol"], row["side"]),
        )
        found = bool(cur.fetchone())
        cur.close()
        return found


def _update_request(row, payload, *, status, client_order_id=""):
    expires_at = payload["_discipline_exit"]["expires_at"]
    return _write(
        f"""UPDATE qd_quick_trades SET status = %s, raw_result = %s::jsonb, client_order_id = %s
            WHERE id = %s AND user_id = %s AND credential_id = %s AND status = %s AND {_SCOPE}
                AND (raw_result->'_discipline_exit'->>'expires_at')::bigint = %s
            RETURNING *""",
        (status, json.dumps(payload), client_order_id or row["client_order_id"],
         row["id"], row["user_id"], row["credential_id"], row["status"], expires_at),
    )


def _fresh_cap(row, meta, side, now, lookback_hours):
    inventory = read_account_position_inventory(user_id=row["user_id"], credential_id=row["credential_id"])
    if inventory["status"] != "ok" or inventory["exchange_id"] != "binance":
        return None
    matches = [position for position in inventory["positions"]
               if position["side"] == side and position.get("inst_id") == meta["instrument"]]
    if len(matches) != 1 or matches[0]["management_status"] != "unregistered":
        return None
    manual = meta.get("manual_opening") is True
    evidence = read_position_evidence(inventory, meta["instrument"], verify_current=True)
    if evidence["status"] != "reconciled":
        return None
    # 只把已保存的下单时间回填给相同的交易所订单号，防止套用到新订单。
    order_times = meta["entry_order_times"]
    evidence = dict(evidence, events=[dict(event, submitted_at=order_times.get(str(event["order_id"])))
                                      for event in evidence["events"]])
    current = _observe_position(dict(inventory, exit_recheck=True), matches[0], evidence, now, lookback_hours, include_ema=False, manual_only=manual)
    if (current["outcome"] != "proposed_exit" or (manual and current.get("reason") != meta.get("reason"))
            or any(current[name] != meta[name] for name in ("opening_fill_id", "opened_at"))):
        return None
    book = [event for event in evidence["events"] if event["book"] in {"both", side}]
    observed_index = next((index for index, event in enumerate(book)
                           if event["fill_id"] == meta["last_fill_id"]), None)
    # 观察之后只允许发生减仓。遇到加仓、反手或证据断裂时撤回本轮执行资格。
    if observed_index is None or any(event["action"] != "reduce" for event in book[observed_index + 1:]):
        return None
    return min(Decimal(meta["cap"]), Decimal(str(current["proposed_quantity"])))


def _save_order(row, raw):
    payload = row["raw_result"]
    meta = payload["_discipline_exit"]
    native_status = raw.get("status")
    close_side = payload["_quick_trade"]["close_side"].upper()
    reduce_only = raw.get("positionSide") == close_side or (
        raw.get("positionSide") == "BOTH" and raw.get("reduceOnly") is True
    )
    if (not reduce_only or raw.get("clientOrderId") != row["client_order_id"]
            or raw.get("symbol") != meta["instrument"] or raw.get("side") != row["side"].upper()
            or not raw.get("orderId")
            or native_status not in _TERMINAL | {"NEW", "PARTIALLY_FILLED"}):
        return
    filled = Decimal(str(raw["executedQty"]))
    price = Decimal(str(raw["avgPrice"]))
    requested = Decimal(meta["submitted_qty"])
    if (Decimal(str(raw["origQty"])) != requested or not filled.is_finite() or not price.is_finite()
            or not Decimal(str(row["filled_amount"] or 0)) <= filled <= requested or price < 0
            or (native_status == "FILLED" and filled != requested)
            or (native_status == "PARTIALLY_FILLED" and not 0 < filled < requested)):
        return
    # Keep ownership with this reconciler until authoritative terminal evidence.
    terminal = native_status in _TERMINAL
    # Store the fields used for evidence without copying the full exchange response.
    payload = {name: raw[name] for name in ("orderId", "symbol", "side", "positionSide", "reduceOnly",
                                           "origQty", "executedQty", "avgPrice", "status", "time", "updateTime")
               if name in raw} | {"_discipline_exit": meta, "_quick_trade": payload["_quick_trade"]}
    payload["_quick_trade"].update(exchange_status=native_status, executable_base_qty=float(requested),
                                   fee_status="pending")
    if not terminal:
        # The execution stream binds only after the original request is terminal.
        _write(
            f"""UPDATE qd_quick_trades SET raw_result = %s::jsonb,
                    filled_amount = %s, avg_fill_price = %s, exchange_order_id = %s
                WHERE id = %s AND user_id = %s AND credential_id = %s AND status = '{_SENT}' AND {_SCOPE}
                RETURNING id""",
            (json.dumps(payload), filled, price, str(raw["orderId"]), row["id"], row["user_id"], row["credential_id"]),
        )
        return
    status = quick_order_status(requested_qty=float(requested), filled_qty=float(filled), exchange_status=native_status)
    trade_id = record_quick_trade(
        user_id=row["user_id"], credential_id=row["credential_id"], exchange_id="binance",
        symbol=row["symbol"], side=row["side"], order_type="market", amount=float(requested * price),
        price=float(price), leverage=1, market_type="swap", tp_price=0, sl_price=0, status=status,
        exchange_order_id=str(raw["orderId"]), filled=float(filled), avg_price=float(price), error_msg="",
        source=_SOURCE, raw_result=payload, client_order_id=row["client_order_id"], record_id=row["id"],
        connection_factory=get_db_connection, logger=logger,
    )
    if trade_id:
        from app.trade_discipline.manual import audit

        audit("exit_reconciled", user_id=row["user_id"], credential_id=row["credential_id"],
              instrument=meta["instrument"], side=payload["_quick_trade"]["close_side"],
              request_id=trade_id, client_order_id=row["client_order_id"], status=status, filled=filled)


def _process_exit(row, *, exit_from, lookback_hours, still_active):
    user_id, credential_id, symbol = row["user_id"], row["credential_id"], row["symbol"]
    key = trade_execution_key(user_id=user_id, credential_id=credential_id, market_type="swap", symbol=symbol)
    with trade_execution_lock(key, timeout_sec=0):
        current = _read_rows(user_id, credential_id, row_id=row["id"])
        if not current or not still_active():
            return
        row = current[0]
        if row["status"] not in {_SENT, _QUEUED}:
            return
        from app.trade_discipline.manual import audit

        context = dict(user_id=user_id, credential_id=credential_id, symbol=symbol, request_id=row["id"])
        payload = row["raw_result"]
        meta = payload["_discipline_exit"]
        side = payload["_quick_trade"]["close_side"]
        now = datetime.now(timezone.utc)
        if now.timestamp() < meta.get("checked_at", 0) + 30:
            return
        if row["status"] == _QUEUED and now.timestamp() >= meta["expires_at"]:
            return
        meta["checked_at"] = int(now.timestamp())
        row = _update_request(row, payload, status=row["status"])
        if not row:
            return
        if (row["status"] == _QUEUED
                and (not meta.get("manual_opening") or datetime.fromisoformat(meta["opened_at"]) < exit_from or _has_unresolved_exit(row))):
            return
        cfg = resolve_exchange_config({"credential_id": credential_id}, user_id=user_id)
        client = create_client(cfg, market_type="swap")
        if not isinstance(client, BinanceFuturesClient):
            return
        client.timeout_sec = min(client.timeout_sec, 6.0)
        # 只在原交易接口和原凭证仍一致时回查已提交请求。
        account = hashlib.sha256(f"{client.base_url}|{client.api_key}".encode()).hexdigest()
        # 已提交状态进入回查分支。即使上次请求超时，也继续按原号查结果。
        if row["status"] == _SENT:
            if meta["account"] == account:
                audit("exit_lookup", **context, client_order_id=row["client_order_id"])
                raw = client.get_order(symbol=meta["instrument"], client_order_id=row["client_order_id"])
                _save_order(row, raw)
            return
        with _management_leg_lock(credential_id=credential_id, market_type="swap", symbol=symbol, side=side):
            # 发送退出请求前复核当前仓位和归属，数量上限以本次核对结果为准。
            cap = _fresh_cap(row, meta, side, now, lookback_hours)
            if cap is None or cap <= 0:
                audit("exit_deferred", **context, reason="live_recheck_failed")
                return
            if not client.get_symbol_filters(symbol=symbol):
                return
            quantity, _ = client._normalize_quantity(symbol=symbol, quantity=float(cap), for_market=True)
            if not 0 < quantity <= cap or client.get_dual_side_position() is None:
                return
            def authorized():
                return still_active() and datetime.now(timezone.utc).timestamp() < meta["expires_at"]

            if not authorized():
                return
            client.strict_market_close_guard = authorized
            now = datetime.now(timezone.utc)
            # 请求发出前先保存客户端订单号，超时或重启后按原订单号回查。
            client_order_id = client._format_client_order_id(f"qde{meta['key']}")
            meta.update(submitted_qty=str(quantity), checked_at=int(now.timestamp()), account=account)
            tracked = _update_request(row, payload, status=_SENT, client_order_id=client_order_id)
            if tracked and authorized():
                audit("exit_submit", **context, client_order_id=client_order_id, quantity=quantity,
                      reason=meta.get("reason"))
                result = submit_market_close(client=client, position_side=side, symbol=symbol,
                                             amount=float(quantity), market_type="swap", exchange_config=cfg,
                                             client_order_id=client_order_id)
                _save_order(tracked, result.raw)


def process_discipline_exits(*, still_active):
    exit_from = exit_start_from_env()
    if exit_from is None or os.getenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap").strip().lower() != "swap":
        return
    try:
        user_id = int(os.getenv("TRADE_DISCIPLINE_USER_ID", ""))
        lookback = os.getenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS")
        lookback_hours = int(lookback) if lookback else None
        if user_id <= 0 or (lookback_hours is not None and lookback_hours <= 0):
            return
    except ValueError:
        return
    from app.services.live_trading.position_management import list_position_inventory_accounts

    for account in list_position_inventory_accounts(user_id=user_id):
        if not still_active():
            break
        try:
            _process_account_exits(user_id, int(account["id"]), exit_from, lookback_hours, still_active)
        except Exception:
            logger.warning("Discipline exit account read failed user=%s credential=%s", user_id, account["id"])


def _process_account_exits(user_id, credential_id, exit_from, lookback_hours, still_active):
    for row in _read_rows(user_id, credential_id):
        if not still_active():
            break
        try:
            _process_exit(row, exit_from=exit_from, lookback_hours=lookback_hours, still_active=still_active)
        except TradeExecutionBusy:
            continue
        except Exception:
            logger.warning("Discipline exit awaiting verification request_id=%s", row["id"])
