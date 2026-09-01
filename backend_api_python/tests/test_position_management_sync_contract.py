"""持仓管理实例现有交易所仓位同步行为。"""

from app.services.live_trading import position_management
from app.services.live_trading import strategy_position_sync as position_sync


def test_exchange_snapshot_checks_management_lifecycle_after_reconcile(monkeypatch):
    lifecycle_checks = []
    monkeypatch.setattr(position_sync, "_delete_position", lambda *_args: None)
    monkeypatch.setattr(position_sync, "upsert_position", lambda **_kwargs: None)
    monkeypatch.setattr(
        position_management,
        "maybe_stop_position_management_strategy",
        lambda strategy_id: lifecycle_checks.append(strategy_id) or False,
    )

    written = position_sync.apply_exchange_snapshot_to_strategy_ledger(
        strategy_id=42,
        strategy_config={"trading_config": {"symbol": "ETH/USDT"}},
        exch_size={"ETH/USDT": {"long": 0.5, "short": 0.0}},
        exch_entry_price={"ETH/USDT": {"long": 3200.0, "short": 0.0}},
        market_type="swap",
        exchange_config={"credential_id": 7},
    )

    assert written == 1
    assert lifecycle_checks == [42]


def test_exchange_flat_snapshot_checks_management_lifecycle_once(monkeypatch):
    deletes = []
    lifecycle_checks = []
    monkeypatch.setattr(position_sync, "_delete_position", lambda *args: deletes.append(args))
    monkeypatch.setattr(position_sync, "upsert_position", lambda **_kwargs: None)
    monkeypatch.setattr(
        position_management,
        "maybe_stop_position_management_strategy",
        lambda strategy_id: lifecycle_checks.append(strategy_id) or True,
    )

    written = position_sync.apply_exchange_snapshot_to_strategy_ledger(
        strategy_id=42,
        strategy_config={"trading_config": {"symbol": "ETH/USDT"}},
        exch_size={"ETH/USDT": {"long": 0.0, "short": 0.0}},
        exch_entry_price={},
        market_type="swap",
    )

    assert written == 0
    assert deletes == [(42, "ETH/USDT", "long"), (42, "ETH/USDT", "short")]
    assert lifecycle_checks == [42]
