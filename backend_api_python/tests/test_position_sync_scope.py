from __future__ import annotations

from app.services import pending_order_worker as worker_module
from app.services import pending_order_position_sync as sync_module


class _Cursor:
    def __init__(self):
        self._rows = []

    def execute(self, sql, _params=None):
        if "FROM qd_strategy_positions" in sql:
            self._rows = [
                {
                    "id": 1,
                    "strategy_id": 76,
                    "symbol": "SPY",
                    "side": "long",
                    "size": 0,
                    "entry_price": 0,
                }
            ]
        elif "FROM qd_strategies_trading" in sql:
            self._rows = []
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _Database:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def cursor():
        return _Cursor()


def test_position_sync_skips_stopped_live_strategy_rows(monkeypatch):
    monkeypatch.setattr(sync_module, "get_db_connection", lambda: _Database())
    monkeypatch.setattr(
        sync_module,
        "load_strategy_configs",
        lambda _strategy_id: {
            "status": "stopped",
            "execution_mode": "live",
            "user_id": 3,
            "exchange_config": {"exchange_id": "alpaca", "credential_id": 1},
        },
    )
    monkeypatch.setattr(sync_module, "should_skip_position_sync", lambda _strategy_id: False)
    create_client_calls = []
    monkeypatch.setattr(
        sync_module,
        "create_client",
        lambda *_args, **_kwargs: create_client_calls.append(True),
    )
    worker = object.__new__(worker_module.PendingOrderWorker)

    worker._sync_positions_best_effort()

    assert create_client_calls == []


def test_position_sync_preserves_gate_swap_position(monkeypatch):
    class GateCursor:
        def __init__(self):
            self._rows = []

        def execute(self, sql, _params=None):
            if "FROM qd_strategy_positions" in sql:
                self._rows = [
                    {
                        "id": 2,
                        "strategy_id": 90,
                        "symbol": "BTC/USDT",
                        "side": "long",
                        "size": 0.005,
                        "entry_price": 84000,
                    }
                ]
            elif "FROM qd_strategies_trading" in sql:
                self._rows = []
            else:
                raise AssertionError(f"Unexpected SQL: {sql}")

        def fetchall(self):
            return list(self._rows)

        def close(self):
            return None

    class GateDatabase:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def cursor():
            return GateCursor()

    from app.services.live_trading.gate import GateUsdtFuturesClient

    class FakeGate(GateUsdtFuturesClient):
        def __init__(self):
            pass

        def get_positions(self):
            return [
                {
                    "contract": "BTC_USDT",
                    "mode": "dual_long",
                    "size": "5",
                    "entry_price": "84589.1",
                }
            ]

        def get_contract(self, *, contract):
            assert contract == "BTC_USDT"
            return {"quanto_multiplier": "0.001"}

    monkeypatch.setattr(sync_module, "get_db_connection", lambda: GateDatabase())
    monkeypatch.setattr(sync_module, "should_skip_position_sync", lambda _strategy_id: False)
    monkeypatch.setattr(
        sync_module,
        "load_strategy_configs",
        lambda _strategy_id: {
            "status": "running",
            "execution_mode": "live",
            "user_id": 7,
            "market_type": "swap",
            "symbol": "BTC/USDT",
            "exchange_config": {"exchange_id": "gate", "credential_id": 11},
        },
    )
    monkeypatch.setattr(sync_module, "resolve_exchange_config", lambda cfg, user_id: dict(cfg))
    monkeypatch.setattr(sync_module, "safe_exchange_config_for_log", lambda cfg: dict(cfg))
    monkeypatch.setattr(sync_module, "strategy_allowed_symbols", lambda _cfg: {"BTC/USDT"})
    monkeypatch.setattr(sync_module, "position_sync_cache_key", lambda *_args: "gate-cache")
    monkeypatch.setattr(sync_module, "get_position_sync_snapshot", lambda _key: None)
    monkeypatch.setattr(sync_module, "is_exchange_sync_backoff", lambda _key: False)
    monkeypatch.setattr(sync_module, "create_client", lambda *_args, **_kwargs: FakeGate())
    monkeypatch.setattr(sync_module, "credential_id_from_exchange_config", lambda _cfg: 11)

    snapshots = []
    account_legs = []
    monkeypatch.setattr(
        sync_module,
        "set_position_sync_snapshot",
        lambda key, sizes, prices, inst_ids: snapshots.append(
            (key, sizes, prices, inst_ids)
        ),
    )
    monkeypatch.setattr(
        sync_module,
        "sync_account_positions",
        lambda **kwargs: account_legs.extend(kwargs["legs"]),
    )
    monkeypatch.setattr(
        sync_module,
        "_purge_flat_strategy_positions_from_exchange",
        lambda **_kwargs: 0,
    )

    worker = object.__new__(worker_module.PendingOrderWorker)
    worker._sync_positions_best_effort(target_strategy_id=90)

    assert snapshots[0][0] == "gate-cache"
    assert snapshots[0][1]["BTC/USDT"]["long"] == 0.005
    assert snapshots[0][2]["BTC/USDT"]["long"] == 84589.1
    assert snapshots[0][3]["BTC/USDT"]["long"] == "BTC_USDT"
    assert len(account_legs) == 1
    assert account_legs[0].symbol == "BTC/USDT"
    assert account_legs[0].side == "long"
    assert account_legs[0].size == 0.005
