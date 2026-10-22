"""Immutable execution identity checks for queued strategy orders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


_EXECUTION_MODES = frozenset({"signal", "live"})


class ExecutionContractError(ValueError):
    """Raised when a queued order's immutable identity is inconsistent."""

    def __init__(self, code: str):
        self.code = str(code or "invalid_execution_contract")
        super().__init__(self.code)


@dataclass(frozen=True)
class QueuedExecutionContract:
    strategy_id: int
    strategy_run_id: int
    order_intent_id: int
    credential_id: int
    mode: str
    signal_type: str
    symbol: str
    market_type: str

    @property
    def is_risk_reducing(self) -> bool:
        return self.signal_type.startswith(("reduce_long", "reduce_short", "close_long", "close_short"))


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalized_market_type(value: Any) -> str:
    market_type = str(value or "").strip().lower()
    if market_type in {"future", "futures", "perp", "perpetual"}:
        return "swap"
    return market_type


def _matching_integer(
    order_row: Mapping[str, Any],
    payload: Mapping[str, Any],
    field: str,
) -> int:
    row_value = _integer(order_row.get(field))
    payload_value = _integer(payload.get(field))
    if row_value > 0 and payload_value > 0 and row_value != payload_value:
        raise ExecutionContractError(f"{field}_mismatch")
    return row_value or payload_value


def _matching_text(
    order_row: Mapping[str, Any],
    payload: Mapping[str, Any],
    field: str,
    *,
    normalizer,
) -> str:
    row_value = normalizer(order_row.get(field))
    payload_value = normalizer(payload.get(field))
    if row_value and payload_value and row_value != payload_value:
        raise ExecutionContractError(f"{field}_mismatch")
    return row_value or payload_value


def resolve_queued_execution_contract(
    order_row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> QueuedExecutionContract:
    """Resolve authoritative queue identity and reject conflicting payload copies."""
    mode = str(order_row.get("execution_mode") or "signal").strip().lower()
    payload_mode = str(payload.get("execution_mode") or "").strip().lower()
    if mode not in _EXECUTION_MODES:
        raise ExecutionContractError(f"unsupported_execution_mode:{mode or 'missing'}")
    if payload_mode and payload_mode not in _EXECUTION_MODES:
        raise ExecutionContractError(f"unsupported_payload_execution_mode:{payload_mode}")
    if payload_mode and payload_mode != mode:
        raise ExecutionContractError(
            f"execution_mode_mismatch:order={mode}:payload={payload_mode}"
        )

    strategy_id = _matching_integer(order_row, payload, "strategy_id")
    strategy_run_id = _matching_integer(order_row, payload, "strategy_run_id")
    order_intent_id = _matching_integer(order_row, payload, "order_intent_id")
    credential_id = _matching_integer(order_row, payload, "credential_id")
    signal_type = _matching_text(
        order_row,
        payload,
        "signal_type",
        normalizer=lambda value: str(value or "").strip().lower(),
    )
    symbol = _matching_text(
        order_row,
        payload,
        "symbol",
        normalizer=lambda value: str(value or "").strip().upper(),
    )
    market_type = _matching_text(
        order_row,
        payload,
        "market_type",
        normalizer=_normalized_market_type,
    )
    if mode == "live" and strategy_id <= 0:
        raise ExecutionContractError("live_strategy_id_required")

    return QueuedExecutionContract(
        strategy_id=strategy_id,
        strategy_run_id=strategy_run_id,
        order_intent_id=order_intent_id,
        credential_id=credential_id,
        mode=mode,
        signal_type=signal_type,
        symbol=symbol,
        market_type=market_type,
    )


def validate_current_strategy_mode(
    contract: QueuedExecutionContract,
    current_mode: Any,
) -> None:
    """Reject stale entry intents while preserving risk-reducing exits."""
    normalized = str(current_mode or "signal").strip().lower()
    if normalized not in _EXECUTION_MODES:
        raise ExecutionContractError(
            f"unsupported_strategy_execution_mode:{normalized or 'missing'}"
        )
    if normalized != contract.mode and not contract.is_risk_reducing:
        raise ExecutionContractError(
            f"execution_mode_mismatch:order={contract.mode}:strategy={normalized}"
        )


def validate_persisted_strategy_mode(
    contract: QueuedExecutionContract,
    loader: Callable[[int], Mapping[str, Any]],
) -> None:
    """Validate a queued order against the strategy state at dispatch time."""
    if contract.strategy_id <= 0:
        return
    strategy = loader(contract.strategy_id)
    if not isinstance(strategy, Mapping):
        raise ExecutionContractError("strategy_unavailable")
    validate_current_strategy_mode(contract, strategy.get("execution_mode"))


__all__ = [
    "ExecutionContractError",
    "QueuedExecutionContract",
    "resolve_queued_execution_contract",
    "validate_current_strategy_mode",
    "validate_persisted_strategy_mode",
]
