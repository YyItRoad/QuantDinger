"""Exchange-owned position history and conditional exits for Quick Trade."""

import json
from datetime import datetime, timezone
from math import isfinite

from app.services.live_trading.gate import GateUsdtFuturesClient
from app.services.live_trading.symbols import to_gate_currency_pair


def utc_time(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if value else None


def gate_position_history(client, symbol, limit=50):
    if not isinstance(client, GateUsdtFuturesClient):
        return {"supported": False, "positions": []}
    contract = to_gate_currency_pair(symbol)
    rows = client._signed_request("GET", "/api/v4/futures/usdt/position_close", params={"contract": contract, "limit": limit})
    result = []
    for row in rows or []:
        if row.get("contract") != contract:
            continue
        long = row.get("side") == "long"
        result.append({
            "id": str(row.get("time_us") or row.get("time")) + str(row.get("side")),
            "symbol": symbol, "side": row.get("side"),
            "opened_at": utc_time(row.get("first_open_time")), "closed_at": utc_time(row.get("time")),
            "entry_price": float(row.get("long_price" if long else "short_price") or 0),
            "exit_price": float(row.get("short_price" if long else "long_price") or 0),
            "quantity": client.contracts_signed_to_base_qty(contract=contract, contracts_signed=float(row.get("accum_size") or 0)),
            "pnl": float(row["pnl"]) if row.get("pnl") is not None else None,
            "gross_pnl": float(row["pnl_pnl"]) if row.get("pnl_pnl") is not None else None,
            "fee": -float(row["pnl_fee"]) if row.get("pnl_fee") is not None else None,
            "funding": float(row["pnl_fund"]) if row.get("pnl_fund") is not None else None,
            "source": "exchange",
        })
    return {"supported": True, "positions": result}


def gate_native_protection_fills(client, symbol, limit=100):
    """Return filled Gate price orders created by exchange-native protection."""
    if not isinstance(client, GateUsdtFuturesClient):
        return {"supported": False, "fills": []}

    contract = to_gate_currency_pair(symbol)
    rows = client._signed_request(
        "GET",
        "/api/v4/futures/usdt/my_trades",
        params={"contract": contract, "limit": max(1, min(int(limit or 100), 1000))},
    )
    grouped = {}
    for row in rows or []:
        if str(row.get("contract") or "") != contract:
            continue
        text = str(row.get("text") or "").strip()
        if not text.startswith("ao-"):
            continue
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        try:
            contracts = float(row.get("size") or 0)
            price = float(row.get("price") or 0)
            fee = abs(float(row.get("fee") or 0))
            created_at = float(row.get("create_time") or row.get("time") or 0)
        except (TypeError, ValueError):
            continue
        if contracts == 0 or price <= 0:
            continue
        item = grouped.setdefault(order_id, {
            "exchange_order_id": order_id,
            "client_order_id": text,
            "contracts": 0.0,
            "contract_weight": 0.0,
            "weighted_price": 0.0,
            "commission": 0.0,
            "created_at": 0.0,
            "exchange_fill_ids": [],
        })
        item["contracts"] += contracts
        item["contract_weight"] += abs(contracts)
        item["weighted_price"] += abs(contracts) * price
        item["commission"] += fee
        item["created_at"] = max(item["created_at"], created_at)
        fill_id = str(row.get("id") or "").strip()
        if fill_id:
            item["exchange_fill_ids"].append(fill_id)

    fills = []
    for item in grouped.values():
        contracts = float(item.pop("contracts") or 0)
        weight = float(item.pop("contract_weight") or 0)
        weighted_price = float(item.pop("weighted_price") or 0)
        created_at = float(item.pop("created_at") or 0)
        if contracts == 0 or weight <= 0:
            continue
        quantity = client.contracts_signed_to_base_qty(
            contract=contract,
            contracts_signed=contracts,
        )
        if quantity <= 0:
            continue
        side = "sell" if contracts < 0 else "buy"
        fills.append({
            **item,
            "symbol": symbol,
            "side": side,
            "close_side": "long" if side == "sell" else "short",
            "filled_amount": quantity,
            "avg_fill_price": weighted_price / weight,
            "commission_ccy": "USDT",
            "created_at": utc_time(created_at),
        })
    fills.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return {"supported": True, "fills": fills}


def gate_protection_orders(client, symbol):
    if not isinstance(client, GateUsdtFuturesClient):
        return {"supported": False, "orders": []}
    contract = to_gate_currency_pair(symbol)
    rows = client._signed_request("GET", "/api/v4/futures/usdt/price_orders", params={"status": "open", "contract": contract, "limit": 100})
    orders = []
    for row in rows or []:
        initial = row.get("initial") or {}
        trigger = row.get("trigger") or {}
        reducing = initial.get("is_reduce_only", initial.get("reduce_only", False))
        if initial.get("contract") != contract or not reducing:
            continue
        side = "long" if float(initial.get("size") or initial.get("amount") or 0) < 0 else "short"
        if initial.get("auto_size") in ("close_long", "close_short"):
            side = initial["auto_size"].removeprefix("close_")
        orders.append({"id": str(row.get("id_string") or row["id"]), "side": side, "price": float(trigger.get("price") or 0),
                       "kind": "tp" if (int(trigger.get("rule") or 0) == 1) == (side == "long") else "sl"})
    return {"supported": True, "orders": orders}


def validate_exit_price(side, kind, price, reference):
    if not all(isfinite(v) and v > 0 for v in (price, reference)):
        raise ValueError("quickTrade.workflow.invalidExitPrice")
    rises = (side == "long") == (kind == "tp")
    if (rises and price <= reference) or (not rises and price >= reference):
        raise ValueError("quickTrade.workflow.invalidExitPrice")


def _raw_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _metadata(row):
    raw = _raw_dict(row.get("raw_result"))
    value = raw.get("_quick_trade")
    return value if isinstance(value, dict) else {}


def _component_ids(raw):
    result = set()
    payload = _raw_dict(raw)
    for key in ("exchange_close", "gate_auto_order"):
        section = payload.get(key)
        if not isinstance(section, dict):
            continue
        for value in section.get("exchange_fill_ids") or []:
            if str(value or "").strip():
                result.add(str(value).strip())
        for component in section.get("components") or []:
            if not isinstance(component, dict):
                continue
            for field in ("exchange_fill_id", "exchange_order_id"):
                value = str(component.get(field) or "").strip()
                if value:
                    result.add(value)
    return result


def tracked_quick_trade_legs(rows, market_type="swap"):
    """Build unresolved account legs and recorded exchange references."""
    market = str(market_type or "swap").strip().lower()
    legs = {
        "long": {"side": "long", "quantity": 0.0, "entry_price": 0.0, "opened_at": None, "leverage": 1},
        "short": {"side": "short", "quantity": 0.0, "entry_price": 0.0, "opened_at": None, "leverage": 1},
    }
    recorded_ids = set()
    ordered = sorted(rows or [], key=lambda row: str(row.get("created_at") or ""))
    for row in ordered:
        if str(row.get("status") or "").lower() != "filled":
            continue
        order_id = str(row.get("exchange_order_id") or "").strip()
        if order_id:
            recorded_ids.add(order_id)
        recorded_ids.update(_component_ids(row.get("raw_result")))
        qty = abs(_number(row.get("filled_amount") or row.get("amount")))
        if qty <= 0:
            continue
        side = str(row.get("side") or "").strip().lower()
        meta = _metadata(row)
        is_close = bool(meta.get("is_close"))
        if market == "spot":
            position_side = "long"
            is_close = is_close or side == "sell"
        else:
            position_side = str(meta.get("close_side") or "").lower() if is_close else ("long" if side == "buy" else "short")
        if position_side not in legs:
            continue
        leg = legs[position_side]
        if is_close:
            leg["quantity"] = max(0.0, leg["quantity"] - qty)
            if leg["quantity"] <= 1e-12:
                leg.update(quantity=0.0, entry_price=0.0, opened_at=None, leverage=1)
            continue
        previous = leg["quantity"]
        price = _number(row.get("avg_fill_price") or row.get("price"))
        leg["quantity"] = previous + qty
        if price > 0:
            leg["entry_price"] = (
                (leg["entry_price"] * previous + price * qty) / leg["quantity"]
                if previous > 0 else price
            )
        if previous <= 1e-12:
            leg["opened_at"] = row.get("created_at")
        leg["leverage"] = int(_number(row.get("leverage")) or 1)
    return {
        "legs": [dict(leg) for leg in legs.values() if leg["quantity"] > 1e-12],
        "recorded_ids": recorded_ids,
    }


def quick_trade_position_history(rows, symbol):
    """Build derivative close history when a venue has no native position-history API."""
    states = {
        "long": {"quantity": 0.0, "entry_price": 0.0, "opened_at": None, "entry_fee": 0.0},
        "short": {"quantity": 0.0, "entry_price": 0.0, "opened_at": None, "entry_fee": 0.0},
    }
    result = []
    ordered = sorted(rows or [], key=lambda row: str(row.get("created_at") or ""))
    for row in ordered:
        if str(row.get("status") or "").lower() != "filled":
            continue
        qty = abs(_number(row.get("filled_amount") or row.get("amount")))
        price = _number(row.get("avg_fill_price") or row.get("price"))
        if qty <= 0 or price <= 0:
            continue
        meta = _metadata(row)
        side = str(row.get("side") or "").lower()
        if not meta.get("is_close"):
            position_side = "long" if side == "buy" else "short"
            state = states[position_side]
            previous = state["quantity"]
            state["quantity"] = previous + qty
            state["entry_price"] = (
                (state["entry_price"] * previous + price * qty) / state["quantity"]
                if previous > 0 else price
            )
            if previous <= 1e-12:
                state["opened_at"] = row.get("created_at")
            state["entry_fee"] += abs(_number(row.get("commission_quote") or row.get("commission")))
            continue
        position_side = str(meta.get("close_side") or ("long" if side == "sell" else "short")).lower()
        if position_side not in states:
            continue
        state = states[position_side]
        closed_qty = min(qty, state["quantity"])
        if closed_qty <= 1e-12:
            continue
        fee_ratio = min(1.0, closed_qty / state["quantity"])
        entry_fee = state["entry_fee"] * fee_ratio
        close_fee = abs(_number(row.get("commission_quote") or row.get("commission")))
        gross = (price - state["entry_price"]) * closed_qty
        if position_side == "short":
            gross = -gross
        total_fee = entry_fee + close_fee
        result.append({
            "id": f"quick-trade-{row.get('id')}-{position_side}",
            "symbol": symbol,
            "side": position_side,
            "opened_at": state["opened_at"],
            "closed_at": row.get("created_at"),
            "entry_price": state["entry_price"],
            "exit_price": price,
            "quantity": closed_qty,
            "pnl": gross - total_fee,
            "gross_pnl": gross,
            "fee": total_fee,
            "funding": 0.0,
            "source": "quick_trade_reconciled",
        })
        state["quantity"] = max(0.0, state["quantity"] - closed_qty)
        state["entry_fee"] = max(0.0, state["entry_fee"] - entry_fee)
        if state["quantity"] <= 1e-12:
            state.update(quantity=0.0, entry_price=0.0, opened_at=None, entry_fee=0.0)
    result.sort(key=lambda row: str(row.get("closed_at") or ""), reverse=True)
    return {"supported": True, "positions": result[:50]}
