from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.services.live_trading import position_management_liquidation as module
from app.services.live_trading.binance import BinanceFuturesClient


def position():
    return dict(id=10, strategy_id=40, user_id=3, credential_id=7,
                symbol="PROM/USDT", side="short", size="86.9",
                strategy_created_at=datetime.fromtimestamp(1000, timezone.utc))


def client():
    fill = dict(id=12, orderId=20, symbol="PROMUSDT", side="BUY", positionSide="BOTH",
                qty="86.9", price="6.1", time=2000000,
                realizedPnl="-50.402", commission="0.21", commissionAsset="USDT")
    return Mock(
        get_positions=Mock(return_value=[dict(symbol="PROMUSDT", positionSide="BOTH", positionAmt="0")]),
        get_liquidation_orders=Mock(return_value=[dict(orderId=20, symbol="PROMUSDT", side="BUY",
                                                     positionSide="BOTH", status="FILLED", time=2000000, executedQty="86.9")]),
        get_user_trades=Mock(return_value=[fill]),
    )


def test_full_liquidation_requires_real_complete_fills():
    venue = client()
    assert module._matching_fills(venue, position(), 1000000)[0]["price"] == "6.1"
    venue.get_user_trades.return_value[0]["qty"] = "80"
    assert module._matching_fills(venue, position(), 1000000) == []


@pytest.mark.parametrize("change", ["still_open", "missing_snapshot", "manual", "partial", "old", "wrong_side", "duplicate", "no_fee"])
def test_uncertain_evidence_does_not_close_local_position(change):
    venue = client()
    if change == "still_open":
        venue.get_positions.return_value[0]["positionAmt"] = "-86.9"
    elif change == "missing_snapshot":
        venue.get_positions.return_value = []
    elif change == "manual":
        venue.get_liquidation_orders.return_value = []
    elif change == "partial":
        venue.get_liquidation_orders.return_value[0]["executedQty"] = "40"
    elif change == "old":
        venue.get_liquidation_orders.return_value[0]["time"] = 999999
    elif change == "wrong_side":
        venue.get_user_trades.return_value[0]["side"] = "SELL"
    elif change == "duplicate":
        venue.get_user_trades.return_value *= 2
    else:
        del venue.get_user_trades.return_value[0]["commission"]
    assert module._matching_fills(venue, position(), 1000000) == []


def test_snapshot_failure_is_not_a_flat_position():
    venue = client()
    venue.get_positions.side_effect = RuntimeError("timeout")
    with pytest.raises(RuntimeError, match="timeout"):
        module._matching_fills(venue, position(), 1000000)
    venue.get_liquidation_orders.assert_not_called()


def test_multiple_fills_are_validated_against_full_order():
    venue = client()
    first = dict(venue.get_user_trades.return_value[0], qty="40", id=12)
    second = dict(first, qty="46.9", id=13)
    venue.get_user_trades.return_value = [second, first]
    assert [f["id"] for f in module._matching_fills(venue, position(), 1000000)] == [12, 13]
    assert venue.get_user_trades.call_args.kwargs["end_time_ms"] == 2000001


def test_non_finite_settlement_is_not_posted():
    venue = client()
    venue.get_user_trades.return_value[0]["realizedPnl"] = "NaN"
    assert module._matching_fills(venue, position(), 1000000) == []


@pytest.mark.parametrize("owners,already_posted,expected", [(True, False, True), (False, False, False), (True, True, False)])
def test_reconciliation_preserves_ownership_and_idempotency(monkeypatch, owners, already_posted, expected):
    venue = client()
    cursor = Mock()
    cursor.fetchone.return_value = {"exists": 1} if already_posted else None

    @contextmanager
    def db():
        yield Mock(cursor=Mock(return_value=cursor))

    @contextmanager
    def transaction():
        yield

    monkeypatch.setattr(module, "get_db_connection", db)
    monkeypatch.setattr(module, "get_db_transaction", transaction)
    monkeypatch.setattr(module, "load_strategy_configs", lambda _: {})
    monkeypatch.setattr(module, "resolve_exchange_config", lambda *a, **kw: dict(exchange_id="binance", credential_id=7))
    monkeypatch.setattr(module, "create_client", lambda *a, **kw: venue)
    monkeypatch.setattr(module, "lock_strategy_fills", Mock())
    monkeypatch.setattr(module, "_owners", lambda *a: owners)
    persist = Mock()
    monkeypatch.setattr(module, "persist_strategy_fill", persist)
    assert module._reconcile(position()) is expected
    if expected:
        posted = persist.call_args.kwargs
        assert posted["avg_price"] == 6.1
        assert posted["profit"] == pytest.approx(-50.402)
        assert posted["filled"] == 86.9
        assert posted["close_reason"] == "exchange_liquidation"
        assert posted["exchange_fill_id"] == "12"
    else:
        persist.assert_not_called()


def test_owner_check_rejects_another_strategy_on_same_leg():
    cursor = Mock()
    cursor.fetchall.return_value = [dict(id=10, size="86.9"), dict(id=11, size="1")]
    assert module._owners(cursor, position()) is False


def test_force_orders_request_only_reads_liquidations():
    venue = BinanceFuturesClient(api_key="test", secret_key="test")
    venue._signed_request = Mock(return_value=[])
    assert venue.get_liquidation_orders(symbol="PROM/USDT", start_time_ms=1000000) == []
    venue._signed_request.assert_called_once_with("GET", "/fapi/v1/forceOrders", params={
        "symbol": "PROMUSDT", "autoCloseType": "LIQUIDATION", "startTime": 1000000, "limit": 100,
    })


def test_periodic_reconciliation_includes_stopped_and_isolates_failures(monkeypatch):
    cursor = Mock()
    first, second = position(), dict(position(), strategy_id=41)
    cursor.fetchall.return_value = [first, second]

    @contextmanager
    def db():
        yield Mock(cursor=Mock(return_value=cursor))

    reconcile = Mock(side_effect=[RuntimeError("timeout"), True])
    monkeypatch.setattr(module, "get_db_connection", db)
    monkeypatch.setattr(module, "_reconcile", reconcile)
    module.sync_managed_binance_liquidations()
    assert "('running', 'stopped')" in cursor.execute.call_args.args[0]
    assert reconcile.call_count == 2


def test_recovery_failure_does_not_interrupt_existing_worker_sync(monkeypatch):
    from app.services.pending_order_worker import PendingOrderWorker
    from app.services.live_trading import funding_reconciliation, alpaca_activity_reconciliation

    worker = object.__new__(PendingOrderWorker)
    worker._position_sync_enabled = True
    worker._position_sync_interval_sec = 30
    worker._last_position_sync_ts = 0
    sync, funding, alpaca = Mock(), Mock(), Mock()
    monkeypatch.setattr(worker, "_sync_positions_best_effort", sync)
    monkeypatch.setattr(funding_reconciliation, "sync_running_strategy_funding", funding)
    monkeypatch.setattr(alpaca_activity_reconciliation, "sync_running_alpaca_activities", alpaca)
    monkeypatch.setattr(module, "sync_managed_binance_liquidations", Mock(side_effect=RuntimeError("DB unavailable")))
    worker._maybe_sync_positions()
    sync.assert_called_once()
    funding.assert_called_once()
    alpaca.assert_called_once()
