"""Post-trade verification and bounded retries for reduce-only market exits."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.live_trading.contracts import OrderIntent
from app.services.live_trading.executors import MarketOrderExecutor, OrderExecutionResult
from app.services.pending_orders.sent_order_recovery import normalize_live_order_status


_EPSILON = 1e-12


@dataclass(frozen=True)
class CloseReconciliationResult:
    status: str
    target_quantity: float
    filled_quantity: float
    pre_position: float
    expected_position: float
    post_position: Optional[float]
    remaining_quantity: float
    retry_count: int = 0
    retry_results: List[OrderExecutionResult] = field(default_factory=list)
    error: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "closed"

    def metadata(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "target_quantity": self.target_quantity,
            "filled_quantity": self.filled_quantity,
            "pre_position": self.pre_position,
            "expected_position": self.expected_position,
            "post_position": self.post_position,
            "remaining_quantity": self.remaining_quantity,
            "retry_count": self.retry_count,
            "error": self.error,
            "retry_orders": [
                {
                    "exchange_order_id": str(item.exchange_order_id or ""),
                    "filled_quantity": float(item.filled_qty or 0.0),
                    "avg_price": float(item.avg_price or 0.0),
                    "status": str(item.status or ""),
                    "error": str(item.error or ""),
                }
                for item in self.retry_results
            ],
        }


def close_target_remaining(
    *,
    target_quantity: float,
    filled_quantity: float,
    pre_position: float,
    post_position: Optional[float],
) -> tuple[float, float]:
    """Return expected post-close position and the still-unexecuted target."""
    target = max(0.0, float(target_quantity or 0.0))
    before = max(0.0, float(pre_position or 0.0))
    expected = max(0.0, before - min(before, target))
    fill_gap = max(0.0, target - max(0.0, float(filled_quantity or 0.0)))
    if post_position is None:
        return expected, fill_gap
    after = max(0.0, float(post_position or 0.0))
    position_gap = max(0.0, after - expected)
    if position_gap <= _EPSILON:
        return expected, 0.0
    if fill_gap <= _EPSILON:
        # A full-account close must trust the post-trade position over a child
        # order's optimistic FILLED status. For allocation-only closes, never
        # use unrelated account inventory as permission to sell more.
        full_account_close = target + max(_EPSILON, before * 1e-9) >= before
        return expected, position_gap if full_account_close else 0.0
    return expected, min(fill_gap, position_gap)


def reconcile_reduce_only_market_close(
    *,
    adapter: Any,
    intent: OrderIntent,
    initial_result: OrderExecutionResult,
    pre_position: float,
    query_position: Callable[[], float],
    normalize_quantity: Callable[[float], float],
    client_order_id: Callable[[int], str],
    max_retries: int = 2,
    max_wait_sec: float = 12.0,
) -> CloseReconciliationResult:
    """Verify a reduce-only market exit and retry only a confirmed residual.

    The target is the strategy-owned quantity in ``intent``. Exchange account
    inventory is used as a safety boundary, never as permission to sell another
    strategy's or a user's manually held position.
    """
    target = max(0.0, float(intent.quantity or 0.0))
    before = max(0.0, float(pre_position or 0.0))
    total_filled = max(0.0, float(initial_result.filled_qty or 0.0))
    latest = initial_result
    retries: List[OrderExecutionResult] = []
    after: Optional[float] = None
    verification_error = ""

    for attempt in range(max(0, int(max_retries or 0)) + 1):
        try:
            observed = float(query_position())
            if observed < 0:
                raise ValueError("negative exchange position")
            after = observed
            verification_error = ""
        except Exception as exc:
            after = None
            verification_error = str(exc)

        expected, remaining = close_target_remaining(
            target_quantity=target,
            filled_quantity=total_filled,
            pre_position=before,
            post_position=after,
        )
        tolerance = max(_EPSILON, target * 1e-9)
        if remaining <= tolerance:
            return CloseReconciliationResult(
                status="closed",
                target_quantity=target,
                filled_quantity=total_filled,
                pre_position=before,
                expected_position=expected,
                post_position=after,
                remaining_quantity=0.0,
                retry_count=len(retries),
                retry_results=retries,
                error=verification_error,
            )

        exchange_status = normalize_live_order_status(str(latest.status or ""))
        if exchange_status not in {"filled", "cancelled"}:
            return CloseReconciliationResult(
                status="awaiting_exchange",
                target_quantity=target,
                filled_quantity=total_filled,
                pre_position=before,
                expected_position=expected,
                post_position=after,
                remaining_quantity=remaining,
                retry_count=len(retries),
                retry_results=retries,
                error=verification_error,
            )
        if attempt >= max_retries:
            return CloseReconciliationResult(
                status="partial",
                target_quantity=target,
                filled_quantity=total_filled,
                pre_position=before,
                expected_position=expected,
                post_position=after,
                remaining_quantity=remaining,
                retry_count=len(retries),
                retry_results=retries,
                error=verification_error,
            )

        executable = max(0.0, float(normalize_quantity(remaining) or 0.0))
        if executable <= tolerance:
            return CloseReconciliationResult(
                status="dust",
                target_quantity=target,
                filled_quantity=total_filled,
                pre_position=before,
                expected_position=expected,
                post_position=after,
                remaining_quantity=remaining,
                retry_count=len(retries),
                retry_results=retries,
                error=verification_error,
            )

        retry_intent = replace(
            intent,
            quantity=min(executable, remaining),
            price=0.0,
            quote_amount=0.0,
            client_order_id=str(client_order_id(attempt + 1) or ""),
            fallback_client_order_id="",
        )
        latest = MarketOrderExecutor(adapter, max_wait_sec=max_wait_sec).execute(retry_intent)
        retries.append(latest)
        if not latest.success:
            status = "awaiting_exchange" if latest.status == "unknown" else "failed"
            return CloseReconciliationResult(
                status=status,
                target_quantity=target,
                filled_quantity=total_filled,
                pre_position=before,
                expected_position=expected,
                post_position=after,
                remaining_quantity=remaining,
                retry_count=len(retries),
                retry_results=retries,
                error=str(latest.error or verification_error),
            )
        total_filled += max(0.0, float(latest.filled_qty or 0.0))

    expected, remaining = close_target_remaining(
        target_quantity=target,
        filled_quantity=total_filled,
        pre_position=before,
        post_position=after,
    )
    return CloseReconciliationResult(
        status="partial",
        target_quantity=target,
        filled_quantity=total_filled,
        pre_position=before,
        expected_position=expected,
        post_position=after,
        remaining_quantity=remaining,
        retry_count=len(retries),
        retry_results=retries,
        error=verification_error,
    )


def reconcile_strategy_market_close(
    *,
    adapter: Any,
    intent: OrderIntent,
    initial_result: OrderExecutionResult,
    pre_position: float,
    client: Any,
    exchange_id: str,
    exchange_config: Dict[str, Any],
    symbol: str,
    market_type: str,
    side: str,
    pos_side: str,
    strategy_id: int,
    signal_type: str,
    client_order_id: Callable[[int], str],
    apply_result: Callable[[OrderExecutionResult], None],
    append_log: Callable[[int, str, str], None],
) -> Tuple[CloseReconciliationResult, str, str]:
    """Run close verification, apply retry fills, and emit one risk log."""
    from app.services.live_trading.position_query import query_exchange_position_size
    from app.services.live_trading.spot_sizing import clamp_spot_close_quantity
    from app.services.pending_orders.order_quantities import exchange_executable_base_quantity

    def query_position() -> float:
        return float(
            query_exchange_position_size(
                client=client,
                symbol=symbol,
                pos_side=pos_side,
                market_type=market_type,
                exchange_config=exchange_config,
                strict=True,
            )
            or 0.0
        )

    def normalize_quantity(quantity: float) -> float:
        candidate = max(0.0, float(quantity or 0.0))
        if market_type == "spot" and side == "sell":
            candidate, _ = clamp_spot_close_quantity(
                client,
                symbol=symbol,
                requested_qty=candidate,
            )
        return exchange_executable_base_quantity(
            client,
            exchange_id=exchange_id,
            symbol=symbol,
            market_type=market_type,
            requested=candidate,
            exchange_config=exchange_config,
        )

    result = reconcile_reduce_only_market_close(
        adapter=adapter,
        intent=intent,
        initial_result=initial_result,
        pre_position=pre_position,
        query_position=query_position,
        normalize_quantity=normalize_quantity,
        client_order_id=client_order_id,
        max_retries=2,
    )
    for retry_result in result.retry_results:
        apply_result(retry_result)
    last_order_id = ""
    last_client_order_id = ""
    if result.retry_results:
        last_order_id = str(result.retry_results[-1].exchange_order_id or "")
        last_client_order_id = client_order_id(result.retry_count)
    if result.status in {"partial", "failed", "awaiting_exchange"}:
        level = "error" if result.status in {"partial", "failed"} else "warning"
        append_log(
            strategy_id,
            level,
            f"Close reconciliation {result.status}: {symbol} {signal_type} "
            f"remaining={result.remaining_quantity:.12g} after {result.retry_count} retry attempt(s).",
        )
    elif result.status == "dust":
        append_log(
            strategy_id,
            "warning",
            f"Close completed to exchange minimum: {symbol} {signal_type}; "
            f"non-executable residual={result.remaining_quantity:.12g}.",
        )
    return result, last_order_id, last_client_order_id


def maybe_reconcile_strategy_market_close(
    *,
    enabled: bool,
    context: Dict[str, Any],
    client_order_id: Callable[[int], str],
    apply_result: Callable[[OrderExecutionResult], None],
    append_log: Callable[[int, str, str], None],
) -> Optional[Tuple[CloseReconciliationResult, str, str]]:
    if not enabled:
        return None
    values = {
        "adapter": context["adapter"],
        "intent": context["intent"],
        "initial_result": context["execution_result"],
        "pre_position": context["pre_position_qty"],
        "client": context["client"],
        "exchange_id": context["exchange_id"],
        "exchange_config": context["exchange_config"],
        "symbol": str(context["symbol"]),
        "market_type": str(context["market_type"]),
        "side": context["side"],
        "pos_side": str(context["pos_side"]),
        "strategy_id": context["strategy_id"],
        "signal_type": str(context["signal_type"]),
    }
    return reconcile_strategy_market_close(
        **values,
        client_order_id=client_order_id,
        apply_result=apply_result,
        append_log=append_log,
    )
