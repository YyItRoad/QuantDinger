"""Conservative spot cost estimates from exchange executions."""

from math import isfinite, isclose
from typing import Any


def average_cost_from_fills(rows: list, *, holding: float, base: str) -> float:
    """Return a moving-average basis only when fills reconcile to the wallet."""
    quantity = cost = 0.0
    seen = set()
    try:
        for row in sorted(rows, key=lambda r: float(r.get("create_time_ms") or float(r.get("create_time") or 0) * 1000)):
            fill_id = str(row.get("id") or "")
            if not fill_id or fill_id in seen:
                continue
            seen.add(fill_id)
            amount, price = float(row["amount"]), float(row["price"])
            fee = float(row.get("fee") or 0)
            if not all(isfinite(n) for n in (amount, price, fee)) or amount <= 0 or price <= 0:
                return 0.0
            base_fee = fee if str(row.get("fee_currency") or "").upper() == base.upper() else 0.0
            if row.get("side") == "buy":
                quantity += amount - base_fee
                cost += amount * price
            elif row.get("side") == "sell":
                disposed = amount + base_fee
                if disposed > quantity + 1e-10 or quantity <= 0:
                    return 0.0
                cost *= max(0.0, quantity - disposed) / quantity
                quantity = max(0.0, quantity - disposed)
            else:
                return 0.0
        if quantity > 0 and isclose(quantity, holding, rel_tol=1e-6, abs_tol=1e-10):
            return cost / quantity
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


def exchange_spot_cost(client: Any, *, symbol: str, holding: float) -> float:
    from app.services.live_trading.gate import GateSpotClient
    from app.services.live_trading.symbols import to_gate_currency_pair, _split_base_quote

    if not isinstance(client, GateSpotClient):
        return 0.0
    pair = to_gate_currency_pair(symbol)
    rows = client._order_trade_rows('/api/v4/spot/my_trades', {'currency_pair': pair})
    rows = [row for row in rows if str(row.get('currency_pair') or pair) == pair]
    base, _ = _split_base_quote(symbol)
    return average_cost_from_fills(rows, holding=holding, base=base)
