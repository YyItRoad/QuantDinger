"""Thin Agent adapters for the existing whole-position management workflow."""
from __future__ import annotations

import json
import math

from app.services.live_trading import position_management as management
from app.services.live_trading.records import normalize_strategy_symbol
from app.utils.agent_auth import (
    SCOPE_R, SCOPE_W, agent_required, current_user_id, instrument_allowed, market_allowed,
)
from app.utils.logger import get_logger
from . import agent_v1_bp
from ._helpers import envelope, error, get_json_or_400
from .trading_data import _credential

logger = get_logger(__name__)

# Only transport names change. Strategy validation remains in the shared service.
_STRATEGY_FIELDS = {
    "source_id": "sourceId", "name": "name", "initial_capital": "initialCapital",
    "timeframe": "timeframe", "params": "params", "entry_reason": "entryReason",
    "direction_mode": "directionMode", "account_risk": "accountRisk",
    "notification_channels": "notificationChannels", "notification_targets": "notificationTargets",
}
_POSITION_FIELDS = {"symbol", "side", "market_type", "inst_id"}
_FIELDS = set(_STRATEGY_FIELDS) | _POSITION_FIELDS | {"confirm_adoption"}


def _adoption_payload(body: dict, credential_id: int) -> tuple[dict, dict]:
    unknown = set(body) - _FIELDS
    if unknown:
        raise ValueError(f"Unsupported adoption fields: {', '.join(sorted(unknown))}")
    if body.get("confirm_adoption") is not True:
        raise ValueError("confirm_adoption=true is required to adopt and start whole-position management")
    for field in ("symbol", "name"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if body.get("market_type") not in {"spot", "swap"}:
        raise ValueError("market_type must be spot or swap")
    if body.get("side") not in {"long", "short"}:
        raise ValueError("side must be long or short")
    source_id = body.get("source_id")
    if type(source_id) is not int or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    capital = body.get("initial_capital")
    if type(capital) not in (int, float) or not math.isfinite(capital) or capital <= 0:
        raise ValueError("initial_capital must be a positive finite number")
    for field in ("inst_id", "timeframe", "direction_mode", "entry_reason"):
        if field in body and not isinstance(body[field], str):
            raise ValueError(f"{field} must be a string")
    if len(body.get("entry_reason", "")) > 500:
        raise ValueError("entry_reason must not exceed 500 characters")
    for field in ("params", "account_risk", "notification_targets"):
        if field in body and not isinstance(body[field], dict):
            raise ValueError(f"{field} must be an object")
    if "notification_channels" in body and (
        not isinstance(body["notification_channels"], list)
        or any(not isinstance(item, str) for item in body["notification_channels"])
    ):
        raise ValueError("notification_channels must be an array of strings")
    # Flask accepts NaN/Infinity in JSON; never persist those as strategy settings.
    json.dumps(body, allow_nan=False)
    position = {key: body[key] for key in _POSITION_FIELDS if key in body}
    position.update(credential_id=credential_id, symbol=normalize_strategy_symbol(body["symbol"]))
    strategy = {_STRATEGY_FIELDS[key]: body[key] for key in _STRATEGY_FIELDS if key in body}
    return position, strategy


def _authorize_position(position: dict) -> None:
    """Check the actual fresh leg, including instrument-id based resolution."""
    symbol = normalize_strategy_symbol(position.get("symbol"))
    if not market_allowed("Crypto") or not instrument_allowed(symbol):
        raise management.PositionManagementError("Position is outside token allowlists", status_code=403)


@agent_v1_bp.route("/trading/accounts/<int:credential_id>/managed-positions", methods=["GET"])
@agent_required(SCOPE_R)
def list_managed_positions(credential_id: int):
    try:
        if not _credential(credential_id):
            return error(404, "Trading account not found", http=404)
        rows = management.list_managed_positions_for_account(
            user_id=current_user_id(), credential_id=credential_id,
        )
        return envelope({"items": [row for row in rows if market_allowed("Crypto")
                                   and instrument_allowed(row["symbol"])]})
    except Exception:
        logger.exception("Agent managed position listing failed")
        return error(500, "Failed to read managed positions", http=500)


@agent_v1_bp.route("/trading/accounts/<int:credential_id>/managed-strategies", methods=["POST"])
@agent_required(SCOPE_W)
def adopt_position_management(credential_id: int):
    body, err = get_json_or_400()
    if err:
        return err
    try:
        position, strategy = _adoption_payload(body, credential_id)
    except (ValueError, TypeError, OverflowError) as exc:
        return error(400, str(exc), http=400)
    try:
        if not _credential(credential_id):
            return error(404, "Trading account not found", http=404)
        _authorize_position(position)
        result = management.create_managed_strategy(
            user_id=current_user_id(), position_ref=position, strategy_payload=strategy,
            authorize_position=_authorize_position,
        )
        return envelope(result, message="position-management-created", status=201)
    except management.PositionManagementError as exc:
        return error(exc.status_code, str(exc), http=exc.status_code)
    except ValueError as exc:
        return error(400, str(exc), http=400)
    except Exception:
        logger.exception("Agent position adoption failed")
        return error(500, "Failed to adopt position management", http=500)
