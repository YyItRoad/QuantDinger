"""Reconcile Binance linear perpetual execution evidence for one instrument."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.services.exchange_execution import resolve_exchange_config
from app.services.live_trading.factory import create_client
from app.services.live_trading.records import normalize_strategy_symbol
from app.services.live_trading.symbols import to_binance_futures_symbol
from app.services.market.instrument_products import PRODUCT_CRYPTO
from app.services.market.product_catalog import get_catalog_product
from app.utils.db import get_db_connection

_HISTORY_LIMIT = 1000
_ORDER_LOOKUP_LIMIT = 20
_BINANCE_IDS = ("binance", "binanceusdm", "binancefutures")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class Fill:
    fill_id: int
    order_id: str
    time_ms: int
    book: str
    delta: Decimal
    source: str
    cumulative_quantity: Decimal = _ZERO


def _milliseconds(value):
    if isinstance(value, datetime):
        return round(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, str)) and str(value).isdigit() and len(str(value)) == 13:
        return int(value)
    return None


def _iso(time_ms):
    if time_ms is None:
        return None
    return datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_local_fills(user_id, credential_id, instrument):
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT exchange_fill_id, exchange_order_id, client_order_id, occurred_at, position_side,
                       side, quantity, cumulative_quantity
                FROM qd_execution_events
                WHERE user_id = %s AND credential_id = %s
                  AND exchange_id IN %s AND market_type = 'swap'
                  AND UPPER(REPLACE(symbol, '/', '')) = %s
                  AND exchange_fill_id <> '' AND quantity > 0 AND NOT is_cumulative
                ORDER BY occurred_at DESC, id DESC LIMIT %s
                """,
                (user_id, credential_id, _BINANCE_IDS, instrument, _HISTORY_LIMIT + 1),
            )
            return cur.fetchall()
        finally:
            cur.close()


def _read_local_orders(user_id, credential_id, instrument, order_ids):
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT exchange_order_id, client_order_id, exchange_response_json AS payload, 'pending_orders' AS source
                FROM pending_orders
                WHERE user_id = %s AND credential_id = %s AND exchange_id IN %s
                  AND market_type = 'swap' AND execution_mode = 'live'
                  AND (inst_id = %s OR (inst_id = '' AND UPPER(REPLACE(symbol, '/', '')) = %s))
                  AND exchange_order_id = ANY(%s)
                UNION ALL
                SELECT exchange_order_id, client_order_id, raw_result::text, 'qd_quick_trades'
                FROM qd_quick_trades
                WHERE user_id = %s AND credential_id = %s AND exchange_id IN %s
                  AND market_type = 'swap' AND UPPER(REPLACE(symbol, '/', '')) = %s
                  AND exchange_order_id = ANY(%s)
                """,
                (user_id, credential_id, _BINANCE_IDS, instrument, instrument, order_ids,
                 user_id, credential_id, _BINANCE_IDS, instrument, order_ids),
            )
            return cur.fetchall()
        finally:
            cur.close()


def _fill(fill_id, order_id, occurred_at, position_side, side, quantity, source, cumulative_quantity=0):
    time_ms = _milliseconds(occurred_at)
    book = str(position_side).lower()
    direction = str(side).lower()
    qty = Decimal(str(quantity))
    cumulative = Decimal(str(cumulative_quantity))
    if (not str(fill_id).isdigit() or not str(order_id).isdigit() or not time_ms
            or book not in {"both", "long", "short"} or direction not in {"buy", "sell"}
            or not qty.is_finite() or qty <= 0 or not cumulative.is_finite() or cumulative < 0):
        raise ValueError("invalid_fill")
    return Fill(int(fill_id), str(order_id), time_ms, book, qty if direction == "buy" else -qty, source, cumulative)


def _replay(fills, expected):
    by_id = {}
    for fill in fills:
        previous = by_id.setdefault(fill.fill_id, fill)
        if (previous.order_id, previous.time_ms, previous.book, previous.delta) != (
            fill.order_id, fill.time_ms, fill.book, fill.delta,
        ):
            raise ValueError("conflicting_fill")
    if not by_id:
        raise ValueError("no_fills")

    # 单向持仓使用净头寸。双向持仓分别回放多空，以免一侧成交冲掉另一侧。
    modes = {fill.book for fill in by_id.values()}
    if "both" in modes and (len(modes) > 1 or expected["long"] and expected["short"]):
        raise ValueError("position_mode_mismatch")
    books = {"both": _ZERO} if "both" in modes else {"long": _ZERO, "short": _ZERO}
    opened = {}
    events = []
    order_totals = {}
    order_counts = {}
    for fill in sorted(by_id.values(), key=lambda row: (row.time_ms, row.fill_id)):
        total = order_totals.get(fill.order_id, _ZERO) + abs(fill.delta)
        if fill.cumulative_quantity and total != fill.cumulative_quantity:
            raise ValueError("missing_order_fills")
        order_totals[fill.order_id] = total
        order_counts[fill.order_id] = order_counts.get(fill.order_id, 0) + 1
        before = books[fill.book]
        after = before + fill.delta
        if (fill.book == "long" and after < 0) or (fill.book == "short" and after > 0):
            raise ValueError("incomplete_hedge_history")
        # 反手成交先抵消原方向，剩余数量才计入新方向的开仓。
        closed = min(abs(before), abs(fill.delta)) if before * fill.delta < 0 else _ZERO
        added = abs(fill.delta) - closed
        if not before or before * after < 0:
            opened[fill.book] = fill.time_ms
        elif not after:
            opened.pop(fill.book, None)
        if closed and added:
            action = "reverse"
        elif closed:
            action = "reduce" if after else "close"
        else:
            action = "add" if before else "open"
        events.append({
            "order_id": fill.order_id,
            "order_fill_number": order_counts[fill.order_id],
            "fill_id": str(fill.fill_id),
            "filled_at": _iso(fill.time_ms),
            "book": fill.book,
            "side": "buy" if fill.delta > 0 else "sell",
            "action": action,
            "filled_quantity": format(abs(fill.delta), "f"),
            "closed_quantity": format(closed, "f"),
            "opened_quantity": format(added, "f"),
            "signed_before": format(before, "f"),
            "signed_after": format(after, "f"),
            "fill_source": fill.source,
        })
        books[fill.book] = after

    if "both" in books:
        net = books["both"]
        sizes = {"long": max(net, _ZERO), "short": max(-net, _ZERO)}
        current_opened = {side: opened.get("both") for side, size in sizes.items() if size}
    else:
        sizes = {"long": books["long"], "short": -books["short"]}
        current_opened = {side: opened.get(side) for side, size in sizes.items() if size}
    # 回放后的多空数量必须与快照完全一致，才能用这段历史证明当前仓位来源。
    if sizes != expected:
        raise ValueError("position_quantity_mismatch")
    return events, {side: _iso(time_ms) for side, time_ms in current_opened.items()}


def _order_details(payload, order_id, instrument):
    # 只接受当前账户查询或同账户本地记录中的同一原生合约、同一订单。
    # 客户端订单号保留大小写和完整值，不能按前缀或截断后的号码做归属匹配。
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("orderId") or "") != order_id:
        return None
    if str(payload.get("symbol") or "").upper() != instrument:
        return None
    return _milliseconds(payload.get("time")), str(payload.get("clientOrderId") or "")


def _linear_perpetual_product(inventory, instrument, exchange_client):
    # 用原生合约定位目录。目录可能还未同步到该报价币，允许用当前账户环境的合约资料补齐。
    symbol = normalize_strategy_symbol(instrument)
    catalog = get_catalog_product(market="Crypto", exchange_id="binance", market_type="swap",
                                  symbol=symbol, instrument_id=instrument)
    if catalog and catalog.get("product_type") not in {None, "", PRODUCT_CRYPTO}:
        return None
    settlement = str((catalog or {}).get("settle_currency") or "").upper()
    if settlement:
        if not symbol.endswith(f"/{settlement}"):
            return None
    else:
        meta = exchange_client().get_symbol_filters(symbol=instrument).get("_meta") or {}
        base, quote, settlement = (str(meta.get(field) or "").upper()
                                   for field in ("baseAsset", "quoteAsset", "marginAsset"))
        # exchangeInfo 可能返回多个合约，校验原生代码、产品类型和结算币后才使用这条记录。
        if (meta.get("symbol") != instrument or meta.get("contractType") != "PERPETUAL"
                or not base or not quote or quote != settlement or base + quote != instrument):
            return None
        symbol = f"{base}/{quote}"
    return {"market": "Crypto", "exchange_id": inventory["exchange_id"], "market_type": inventory["market_type"],
            "instrument_id": instrument, "symbol": symbol, "settlement_ccy": settlement}


def read_position_evidence(inventory, symbol, *, verify_current=False):
    """Reconcile observed fills to the current, completely readable position snapshot."""
    instrument = to_binance_futures_symbol(symbol)
    result = {"instrument": instrument, "status": "unverified", "coverage": "bounded_fill_replay", "issues": [],
              "opened_at": {}, "events": []}
    if inventory["market_type"] != "swap" or inventory["exchange_id"] not in _BINANCE_IDS:
        result["issues"].append("unsupported_account")
        return result
    if inventory["status"] != "ok":
        result["issues"].append("inventory_incomplete")
        return result
    if not instrument.isascii() or not instrument.isalnum():
        result["issues"].append("product_identity_unavailable")
        return result

    snapshot_ms = int(inventory["fetched_at"]) * 1000 + 999
    expected = {"long": _ZERO, "short": _ZERO}
    for position in inventory["positions"]:
        native = position.get("inst_id") or to_binance_futures_symbol(position["symbol"])
        if native == instrument:
            qty = Decimal(str(position["size"]))
            if not qty.is_finite() or qty <= 0 or position["side"] not in expected:
                result["issues"].append("invalid_position_snapshot")
                return result
            expected[position["side"]] += qty

    user_id, credential_id = inventory["user_id"], inventory["credential_id"]
    client = None

    def exchange_client():
        nonlocal client
        if client is None:
            config = resolve_exchange_config({"credential_id": credential_id}, user_id=user_id)
            if config.get("exchange_id") not in _BINANCE_IDS:
                raise ValueError("credential_unavailable")
            client = create_client(config, market_type="swap")
            client.timeout_sec = min(client.timeout_sec, 6.0)
        return client

    try:
        product = _linear_perpetual_product(inventory, instrument, exchange_client)
    except Exception:
        product = None
    if not product:
        result["issues"].append("product_identity_unavailable")
        return result
    result["product"] = product

    # 优先用本地成交回放并核对仓位数量，证据不足时再按合约补查交易所。
    fills = []
    try:
        rows = _read_local_fills(user_id, credential_id, instrument)
    except Exception:
        rows = []
        result["issues"].append("local_fills_unavailable")
    try:
        if len(rows) > _HISTORY_LIMIT:
            raise ValueError("local_history_limit")
        fills = [
            _fill(row["exchange_fill_id"], row["exchange_order_id"], row["occurred_at"],
                  row["position_side"], row["side"], row["quantity"], "local_execution_event",
                  row["cumulative_quantity"])
            for row in rows
        ]
        if any(fill.time_ms > snapshot_ms for fill in fills):
            raise ValueError("fills_newer_than_snapshot")
        if verify_current:
            raise ValueError("exchange_refresh_required")
        events, opened = _replay(fills, expected)
    except (ValueError, InvalidOperation):
        try:
            trades = exchange_client().get_user_trades(symbol=instrument, limit=_HISTORY_LIMIT)
        except Exception:
            result["issues"].append("exchange_fills_unavailable")
            return result
        try:
            if len(trades) >= _HISTORY_LIMIT:
                raise ValueError("exchange_history_limit")
            remote = [
                _fill(row.get("id"), row.get("orderId"), row.get("time"), row.get("positionSide"),
                      row.get("side"), row.get("qty"), "exchange_fill")
                for row in trades if str(row.get("symbol") or "").upper() == instrument
            ]
            if not remote:
                raise ValueError("no_exchange_fills")
            # 实时复核时，如果新旧成交没有交集，无法证明两段记录能接在一起。
            if verify_current and fills and not ({row.fill_id for row in fills} & {row.fill_id for row in remote}):
                raise ValueError("exchange_history_disjoint")
            combined = [*fills, *remote]
            if any(fill.time_ms > snapshot_ms for fill in combined):
                raise ValueError("fills_newer_than_snapshot")
            events, opened = _replay(combined, expected)
        except (ValueError, InvalidOperation) as exc:
            result["issues"].append(str(exc))
            return result

    if verify_current:
        result.update(status="partial" if result["issues"] else "reconciled", opened_at=opened, events=events)
        return result

    # 成交时间只能作为下单时间的上界。计划和 EMA 需要单独确认下单时间。
    first_fill = {}
    for event in events:
        first_fill.setdefault(event["order_id"], event["filled_at"])
    try:
        local_orders = _read_local_orders(user_id, credential_id, instrument, list(first_fill))
    except Exception:
        local_orders = []
        result["issues"].append("local_orders_unavailable")

    local_times = {}
    client_ids = {}
    for row in rows:
        client_id = str(row.get("client_order_id") or "")
        if client_id:
            client_ids.setdefault(str(row["exchange_order_id"]), set()).add(client_id)
    for order in local_orders:
        order_id = order["exchange_order_id"]
        details = _order_details(order["payload"], order_id, instrument)
        if not details:
            continue
        time_ms, client_id = details
        stored_id = str(order.get("client_order_id") or "")
        # 旧发单前记录可能保存格式化前的号码，原单保存实际发出的 broker 号码。
        # 复用现有格式化方法验证两者对应关系，避免把正常加前缀误报成来源冲突。
        if stored_id and client_id and stored_id != client_id:
            if exchange_client()._format_client_order_id(stored_id) == client_id:
                stored_id = client_id
        for value in (client_id, stored_id):
            if value:
                client_ids.setdefault(order_id, set()).add(str(value))
        if time_ms and _iso(time_ms) <= first_fill[order_id]:
            local_times.setdefault(order_id, {})[time_ms] = order["source"]
    order_times = {}
    for order_id, candidates in local_times.items():
        if len(candidates) == 1:
            order_times[order_id] = next(iter(candidates.items()))

    # 优先补查最近的缺失订单，并限制请求数量，避免单个品种占用整轮扫描。
    missing = [order_id for order_id in reversed(first_fill)
               if order_id not in order_times or not client_ids.get(order_id)]
    for order_id in missing[:_ORDER_LOOKUP_LIMIT]:
        try:
            payload = exchange_client().get_order(symbol=instrument, order_id=order_id)
        except Exception:
            continue
        details = _order_details(payload, order_id, instrument)
        if not details:
            continue
        time_ms, client_id = details
        if client_id:
            client_ids.setdefault(order_id, set()).add(client_id)
        if time_ms and _iso(time_ms) <= first_fill[order_id]:
            if order_id in order_times and order_times[order_id][0] != time_ms:
                result["issues"].append("order_submission_time_conflict")
                order_times.pop(order_id)
                continue
            order_times[order_id] = (time_ms, "exchange_order")
    for event in events:
        time_ms, source = order_times.get(event["order_id"], (None, None))
        candidates = client_ids.get(event["order_id"], set())
        if len(candidates) > 1:
            result["issues"].append("order_client_id_conflict")
        event.update(submitted_at=_iso(time_ms), order_time_source=source,
                     client_order_id=next(iter(candidates)) if len(candidates) == 1 else "")
    if len(order_times) < len(first_fill):
        result["issues"].append("order_submission_time_unavailable")

    result.update(status="partial" if result["issues"] else "reconciled", opened_at=opened, events=events)
    return result
