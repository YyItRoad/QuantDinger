"""Read-only access to the existing position-management archive."""
from flask import request

from app.services.live_trading import position_management_history as history
from app.utils.agent_auth import SCOPE_R, agent_required, current_user_id
from app.utils.logger import get_logger
from . import agent_v1_bp
from ._helpers import envelope, error

logger = get_logger(__name__)
_PUBLIC_FIELDS = frozenset(("id", "archived_at", *history.HISTORY_COLUMNS)) - {"user_id"}


def _public(row):
    return {key: value for key, value in row.items() if key in _PUBLIC_FIELDS}


def _positive_int(name, default=None, maximum=None):
    raw = request.args.get(name, default)
    if raw is None:
        raise ValueError(f"{name} is required")
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is out of range")
    return value


@agent_v1_bp.route("/trading/position-management/trade-history", methods=["GET"])
@agent_required(SCOPE_R)
def list_position_management_trade_history():
    try:
        filters = {}
        for name, allowed in (("market_type", {"spot", "swap", "futures"}),
                              ("position_side", {"long", "short"})):
            if name in request.args:
                value = request.args[name]
                if value not in allowed:
                    raise ValueError(f"Invalid {name}")
                filters[name] = value
        if "symbol" in request.args:
            symbol = request.args["symbol"].strip()
            if not symbol or len(symbol) > 80:
                raise ValueError("symbol must contain 1 to 80 characters")
            filters["symbol"] = symbol
        if "source_strategy_id" in request.args:
            filters["source_strategy_id"] = _positive_int("source_strategy_id")
        for name in ("start_time", "end_time"):
            if name in request.args:
                filters[name] = request.args[name]
        result = history.list_position_management_trade_history(
            user_id=current_user_id(), credential_id=_positive_int("credential_id"),
            page=_positive_int("page", 1), page_size=_positive_int("page_size", 20, 100),
            **filters,
        )
        return envelope({**result, "items": [_public(row) for row in result["items"]]})
    except ValueError as exc:
        return error(400, str(exc), http=400)
    except Exception:
        logger.exception("Agent archive list failed")
        return error(500, "Failed to read trade history", http=500)


@agent_v1_bp.route("/trading/position-management/trade-history/<int:history_id>", methods=["GET"])
@agent_required(SCOPE_R)
def get_position_management_trade_history(history_id):
    try:
        row = history.get_position_management_trade_history(
            user_id=current_user_id(), credential_id=_positive_int("credential_id"),
            history_id=history_id,
        )
        if row is None:
            return error(404, "Trade history not found", http=404)
        return envelope(_public(row))
    except ValueError as exc:
        return error(400, str(exc), http=400)
    except Exception:
        logger.exception("Agent archive detail failed")
        return error(500, "Failed to read trade history", http=500)
