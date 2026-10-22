import pytest

from app.services.live_trading.base import LiveOrderResult
from app.services.live_trading.close_reconciliation import (
    close_target_remaining,
    reconcile_reduce_only_market_close,
)
from app.services.live_trading.contracts import FillSnapshot, OrderIntent
from app.services.live_trading.executors import OrderExecutionResult


class RetryAdapter:
    exchange_id = "test"

    def __init__(self, *, filled=0.0, status="filled"):
        self.filled = filled
        self.status = status
        self.intents = []

    def place_market_order(self, intent):
        self.intents.append(intent)
        return LiveOrderResult(
            exchange_id="test",
            exchange_order_id=f"retry-{len(self.intents)}",
            filled=self.filled,
            avg_price=100.0,
            raw={},
        )

    def wait_for_fill(self, intent, *, order_id="", max_wait_sec=0):
        return FillSnapshot(
            filled_qty=self.filled,
            avg_price=100.0,
            status=self.status,
            raw={},
        )


def close_intent(quantity=4.76):
    return OrderIntent(
        symbol="SOL/USDT",
        side="buy",
        quantity=quantity,
        market_type="swap",
        pos_side="short",
        reduce_only=True,
        client_order_id="initial",
    )


def initial_result(*, filled, status="filled"):
    return OrderExecutionResult(
        success=True,
        exchange_id="test",
        exchange_order_id="initial-order",
        filled_qty=filled,
        avg_price=100.0,
        status=status,
    )


def test_partial_market_close_retries_confirmed_residual_to_zero():
    adapter = RetryAdapter(filled=1.9)
    positions = iter([1.9, 0.0])

    result = reconcile_reduce_only_market_close(
        adapter=adapter,
        intent=close_intent(),
        initial_result=initial_result(filled=2.86),
        pre_position=4.76,
        query_position=lambda: next(positions),
        normalize_quantity=lambda qty: qty,
        client_order_id=lambda attempt: f"retry-{attempt}",
    )

    assert result.status == "closed"
    assert result.filled_quantity == pytest.approx(4.76)
    assert result.retry_count == 1
    assert adapter.intents[0].quantity == 1.9
    assert adapter.intents[0].client_order_id == "retry-1"


def test_open_exchange_order_is_never_duplicated():
    adapter = RetryAdapter(filled=1.9)
    result = reconcile_reduce_only_market_close(
        adapter=adapter,
        intent=close_intent(),
        initial_result=initial_result(filled=2.86, status="open"),
        pre_position=4.76,
        query_position=lambda: 1.9,
        normalize_quantity=lambda qty: qty,
        client_order_id=lambda attempt: f"retry-{attempt}",
    )

    assert result.status == "awaiting_exchange"
    assert result.remaining_quantity == 1.9
    assert adapter.intents == []


def test_non_executable_residual_is_reported_as_dust():
    adapter = RetryAdapter(filled=0.0)
    result = reconcile_reduce_only_market_close(
        adapter=adapter,
        intent=close_intent(0.0004),
        initial_result=initial_result(filled=0.0003),
        pre_position=0.0004,
        query_position=lambda: 0.0001,
        normalize_quantity=lambda qty: 0.0,
        client_order_id=lambda attempt: f"retry-{attempt}",
    )

    assert result.status == "dust"
    assert result.remaining_quantity == 0.0001
    assert adapter.intents == []


def test_strategy_target_does_not_sell_other_account_inventory():
    expected, remaining = close_target_remaining(
        target_quantity=3.0,
        filled_quantity=3.0,
        pre_position=10.0,
        post_position=8.0,
    )

    assert expected == 7.0
    assert remaining == 0.0


def test_full_account_close_uses_verified_position_when_fill_status_is_optimistic():
    expected, remaining = close_target_remaining(
        target_quantity=4.76,
        filled_quantity=4.76,
        pre_position=4.76,
        post_position=1.9,
    )

    assert expected == 0.0
    assert remaining == 1.9


def test_material_partial_fill_is_reported_by_close_reconciliation():
    adapter = RetryAdapter(filled=0.0)
    result = reconcile_reduce_only_market_close(
        adapter=adapter,
        intent=close_intent(),
        initial_result=initial_result(filled=2.86),
        pre_position=4.76,
        query_position=lambda: 1.9,
        normalize_quantity=lambda qty: qty,
        client_order_id=lambda attempt: f"retry-{attempt}",
        max_retries=0,
    )

    assert result.status == "partial"
    assert result.remaining_quantity == 1.9
