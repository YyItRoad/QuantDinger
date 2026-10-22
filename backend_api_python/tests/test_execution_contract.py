import pytest

from app.services.pending_orders.execution_contract import (
    ExecutionContractError,
    resolve_queued_execution_contract,
    validate_current_strategy_mode,
)


def _row(**overrides):
    row = {
        "strategy_id": 7,
        "strategy_run_id": 11,
        "order_intent_id": 13,
        "credential_id": 17,
        "execution_mode": "signal",
        "signal_type": "open_long",
        "symbol": "BTC/USDT",
        "market_type": "swap",
    }
    row.update(overrides)
    return row


def test_queue_row_is_the_authoritative_execution_identity():
    contract = resolve_queued_execution_contract(
        _row(),
        {
            "strategy_id": 7,
            "strategy_run_id": 11,
            "order_intent_id": 13,
            "credential_id": 17,
            "execution_mode": "signal",
            "signal_type": "OPEN_LONG",
            "symbol": "btc/usdt",
            "market_type": "perpetual",
        },
    )

    assert contract.strategy_id == 7
    assert contract.mode == "signal"
    assert contract.signal_type == "open_long"
    assert contract.symbol == "BTC/USDT"
    assert contract.market_type == "swap"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"execution_mode": "live"}, "execution_mode_mismatch"),
        ({"strategy_id": 8}, "strategy_id_mismatch"),
        ({"strategy_run_id": 12}, "strategy_run_id_mismatch"),
        ({"order_intent_id": 14}, "order_intent_id_mismatch"),
        ({"credential_id": 18}, "credential_id_mismatch"),
        ({"signal_type": "open_short"}, "signal_type_mismatch"),
        ({"symbol": "ETH/USDT"}, "symbol_mismatch"),
        ({"market_type": "spot"}, "market_type_mismatch"),
    ],
)
def test_conflicting_payload_identity_is_rejected(payload, error):
    with pytest.raises(ExecutionContractError, match=error):
        resolve_queued_execution_contract(_row(), payload)


def test_mode_change_rejects_entries_but_preserves_protective_exits():
    entry = resolve_queued_execution_contract(_row(), {})
    with pytest.raises(ExecutionContractError, match="execution_mode_mismatch"):
        validate_current_strategy_mode(entry, "live")

    exit_order = resolve_queued_execution_contract(
        _row(execution_mode="live", signal_type="close_long_trailing"),
        {},
    )
    validate_current_strategy_mode(exit_order, "signal")
