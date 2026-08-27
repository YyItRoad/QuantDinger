"""从现有创建策略流程接入交易所整笔仓位。"""

import inspect
from contextlib import contextmanager

import pytest
from flask import Flask, g

from app.routes import strategy_account_routes as routes
from app.services.live_trading import position_management


_REAL_MANAGEMENT_LEG_LOCK = position_management._management_leg_lock


@pytest.fixture(autouse=True)
def _without_database_advisory_lock(monkeypatch):
    @contextmanager
    def unlocked(**_kwargs):
        yield

    monkeypatch.setattr(position_management, "_management_leg_lock", unlocked)


def test_management_leg_lock_uses_same_advisory_key_for_lock_and_unlock(monkeypatch):
    statements = []

    class _Cursor:
        def execute(self, sql, params):
            statements.append((sql, params))

        def fetchone(self):
            return {"acquired": True}

        def close(self):
            statements.append(("close", ()))

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(position_management, "get_db_connection", lambda: _Db())

    with _REAL_MANAGEMENT_LEG_LOCK(
        credential_id=7,
        market_type="swap",
        symbol="KAITO/USDC:USDC",
        side="LONG",
    ):
        statements.append(("inside", ()))

    assert statements[0][0] == "SELECT pg_try_advisory_lock(%s, %s) AS acquired"
    assert statements[1] == ("inside", ())
    assert statements[2][0] == "SELECT pg_advisory_unlock(%s, %s)"
    assert statements[0][1] == statements[2][1]
    assert statements[0][1][0] == 7414


def test_management_leg_lock_rejects_when_another_request_owns_it(monkeypatch):
    statements = []

    class _Cursor:
        def execute(self, sql, params):
            statements.append((sql, params))

        def fetchone(self):
            return {"acquired": False}

        def close(self):
            statements.append(("close", ()))

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(position_management, "get_db_connection", lambda: _Db())

    with pytest.raises(position_management.PositionManagementError, match="正在被接管") as exc:
        with _REAL_MANAGEMENT_LEG_LOCK(
            credential_id=7,
            market_type="swap",
            symbol="KAITO/USDC",
            side="long",
        ):
            pytest.fail("未获取锁时不应进入临界区")

    assert exc.value.status_code == 409
    assert all("pg_advisory_unlock" not in sql for sql, _params in statements)


def test_exchange_snapshot_is_fetched_before_management_lock(monkeypatch):
    service = _StrategyService()
    events = []

    def snapshot(**_kwargs):
        events.append("snapshot")
        return _snapshot()

    @contextmanager
    def lock(**kwargs):
        events.append(("lock", kwargs))
        yield

    monkeypatch.setattr(position_management, "fetch_account_snapshot", snapshot)
    monkeypatch.setattr(position_management, "_management_leg_lock", lock)
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])
    monkeypatch.setattr(position_management, "upsert_position", lambda **_kwargs: None)

    position_management.create_managed_strategy(
        user_id=3,
        position_ref={
            "credential_id": 7,
            "symbol": "KAITO/USDC:USDC",
            "side": "long",
            "market_type": "swap",
            "inst_id": "KAITOUSDC",
        },
        strategy_payload={"sourceId": 9, "name": "管理策略"},
    )

    assert events[0] == "snapshot"
    assert events[1] == ("lock", {
        "credential_id": 7,
        "market_type": "swap",
        "symbol": "KAITO/USDC",
        "side": "long",
    })


class _StrategyService:
    def __init__(self):
        self.created_payload = None
        self.deleted = []

    def create_strategy(self, payload):
        self.created_payload = dict(payload)
        return 44

    def get_strategy(self, strategy_id, user_id=None):
        assert strategy_id == 44
        assert user_id == 3
        return {
            "id": 44,
            "strategy_name": payload_name(self.created_payload),
            "status": "stopped",
            "timeframe": "1h",
        }

    def delete_strategy(self, strategy_id, user_id=None):
        self.deleted.append((strategy_id, user_id))
        return True

def payload_name(payload):
    return str((payload or {}).get("name") or "")


def _snapshot():
    return {
        "swap_positions": [{
            "symbol": "KAITO/USDC",
            "side": "long",
            "size": "422.5",
            "entry_price": "0.355",
            "mark_price": "0.3397",
            "leverage": "5",
            "market_type": "swap",
            "inst_id": "KAITOUSDC",
        }],
        "spot_positions": [],
        "partial": False,
        "error": "",
    }


def test_create_managed_strategy_uses_fresh_full_exchange_position(monkeypatch):
    service = _StrategyService()
    recorded = []
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: _snapshot())
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])
    monkeypatch.setattr(position_management, "upsert_position", lambda **kwargs: recorded.append(kwargs))

    result = position_management.create_managed_strategy(
        user_id=3,
        position_ref={
            "credential_id": 7,
            "symbol": "KAITO/USDC:USDC",
            "side": "long",
            "market_type": "swap",
            "inst_id": "KAITOUSDC",
        },
        strategy_payload={
            "sourceId": 9,
            "name": "[持仓] KAITO/USDC ATR趋势管理",
            "initialCapital": 1000,
            "executionMode": "signal",
            "credentialId": 999,
            "leverageEnabled": False,
            "leverage": 1,
            "params": {"atr_period": 14},
        },
    )

    assert service.created_payload == {
        "sourceId": 9,
        "name": "[持仓] KAITO/USDC ATR趋势管理",
        "initialCapital": 1000,
        "executionMode": "live",
        "credentialId": 7,
        "leverageEnabled": True,
        "leverage": 5.0,
        "params": {"atr_period": 14},
        "positionManagement": {
            "enabled": True,
            "auto_stop_when_flat": True,
        },
        "user_id": 3,
    }
    assert recorded == [{
        "strategy_id": 44,
        "symbol": "KAITO/USDC",
        "side": "long",
        "size": 422.5,
        "entry_price": 0.355,
        "current_price": 0.3397,
        "highest_price": 0.3397,
        "lowest_price": 0.3397,
        "user_id": 3,
        "market_type": "swap",
        "credential_id": 7,
        "inst_id": "KAITOUSDC",
    }]
    assert result == {
        "strategy_id": 44,
        "strategy_name": "[持仓] KAITO/USDC ATR趋势管理",
        "status": "stopped",
        "timeframe": "1h",
        "symbol": "KAITO/USDC",
        "side": "long",
        "size": "422.5",
        "entry_price": "0.355",
        "mark_price": "0.3397",
        "leverage": "5",
    }


def test_create_managed_strategy_rejects_position_already_registered(monkeypatch):
    service = _StrategyService()
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: _snapshot())
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [{
        "strategy_id": 12,
        "symbol": "KAITO/USDC",
        "side": "long",
        "market_type": "swap",
    }])

    with pytest.raises(position_management.PositionManagementError) as exc:
        position_management.create_managed_strategy(
            user_id=3,
            position_ref={
                "credential_id": 7,
                "symbol": "KAITO/USDC",
                "side": "long",
                "market_type": "swap",
            },
            strategy_payload={"sourceId": 9, "name": "管理策略"},
        )

    assert exc.value.status_code == 409
    assert str(exc.value) == "该仓位已经由策略管理，请先同步持仓"
    assert service.created_payload is None


def test_create_managed_strategy_rejects_incomplete_snapshot(monkeypatch):
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: {
        "swap_positions": [],
        "spot_positions": [],
        "partial": True,
        "error": "币安合约仓位读取失败",
    })

    with pytest.raises(position_management.PositionManagementError, match="交易所仓位同步失败") as exc:
        position_management.create_managed_strategy(
            user_id=3,
            position_ref={"credential_id": 7, "symbol": "KAITO/USDC", "side": "long", "market_type": "swap"},
            strategy_payload={"sourceId": 9, "name": "管理策略"},
        )

    assert exc.value.status_code == 409


def test_partial_snapshot_accepts_usable_target_market_position(monkeypatch):
    snapshot = _snapshot()
    snapshot.update({
        "partial": True,
        "warnings": ["Binance 现货持仓：交易所鉴权失败"],
        "error": "",
    })

    fresh = position_management._fresh_position(
        snapshot,
        {
            "symbol": "KAITO/USDC",
            "side": "long",
            "market_type": "swap",
            "inst_id": "KAITOUSDC",
        },
    )

    assert fresh["inst_id"] == "KAITOUSDC"


def test_fresh_position_prefers_exact_inst_id_before_leg_fallback():
    snapshot = {
        "swap_positions": [
            {
                "symbol": "BTC/USD",
                "side": "long",
                "market_type": "swap",
                "inst_id": "BTC-USD-OLD",
                "size": 1,
            },
            {
                "symbol": "BTC/USD",
                "side": "long",
                "market_type": "swap",
                "inst_id": "BTC-USD-NEW",
                "size": 2,
            },
        ],
        "spot_positions": [],
        "partial": False,
        "error": "",
    }

    fresh = position_management._fresh_position(
        snapshot,
        {
            "symbol": "BTC/USD",
            "side": "long",
            "market_type": "swap",
            "inst_id": "BTC-USD-NEW",
        },
    )

    assert fresh["inst_id"] == "BTC-USD-NEW"
    assert fresh["size"] == 2


def test_create_spot_management_uses_takeover_price_when_cost_basis_missing(monkeypatch):
    service = _StrategyService()
    recorded = []
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: {
        "swap_positions": [],
        "spot_positions": [{
            "symbol": "ETH/USDT",
            "side": "long",
            "size": "0.25",
            "entry_price": "0",
            "market_type": "spot",
            "inst_id": "ETH-USDT",
        }],
        "partial": False,
        "error": "",
    })
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])
    monkeypatch.setattr(position_management, "_spot_last_price", lambda **_kwargs: 2500.0)
    monkeypatch.setattr(position_management, "upsert_position", lambda **kwargs: recorded.append(kwargs))

    result = position_management.create_managed_strategy(
        user_id=3,
        position_ref={
            "credential_id": 7,
            "symbol": "ETH/USDT",
            "side": "long",
            "market_type": "spot",
            "inst_id": "ETH-USDT",
        },
        strategy_payload={"sourceId": 9, "name": "ETH 现货管理"},
    )

    assert recorded[0]["entry_price"] == 2500.0
    assert recorded[0]["current_price"] == 2500.0
    assert recorded[0]["market_type"] == "spot"
    assert result["entry_price"] == "2500.0"
    assert result["mark_price"] == "2500.0"


def test_create_spot_management_falls_back_to_entry_when_ticker_is_unavailable(monkeypatch):
    service = _StrategyService()
    recorded = []
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: {
        "swap_positions": [],
        "spot_positions": [{
            "symbol": "ETH/USDT",
            "side": "long",
            "size": "0.25",
            "entry_price": "2100",
            "market_type": "spot",
            "inst_id": "ETH-USDT",
        }],
        "partial": False,
        "error": "",
    })
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])
    monkeypatch.setattr(position_management, "_spot_last_price", lambda **_kwargs: 0.0)
    monkeypatch.setattr(position_management, "upsert_position", lambda **kwargs: recorded.append(kwargs))

    position_management.create_managed_strategy(
        user_id=3,
        position_ref={
            "credential_id": 7,
            "symbol": "ETH/USDT",
            "side": "long",
            "market_type": "spot",
            "inst_id": "ETH-USDT",
        },
        strategy_payload={"sourceId": 9, "name": "ETH 现货管理"},
    )

    assert recorded[0]["entry_price"] == 2100.0
    assert recorded[0]["current_price"] == 2100.0


def test_create_managed_strategy_rejects_source_that_does_not_monitor_position(monkeypatch):
    service = _StrategyService()
    service.get_strategy = lambda strategy_id, user_id=None: {
        "id": strategy_id,
        "strategy_name": "BTC 管理策略",
        "status": "stopped",
        "timeframe": "1h",
        "trading_config": {
            "strategy_manifest": {
                "universe": {
                    "kind": "static",
                    "reference": "",
                    "instruments": [{"market": "Crypto", "symbol": "BTC/USDT", "market_type": "swap"}],
                }
            }
        },
    }
    recorded = []
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: _snapshot())
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])
    monkeypatch.setattr(position_management, "upsert_position", lambda **kwargs: recorded.append(kwargs))

    with pytest.raises(position_management.PositionManagementError, match="策略源码没有订阅当前仓位品种") as exc:
        position_management.create_managed_strategy(
            user_id=3,
            position_ref={"credential_id": 7, "symbol": "KAITO/USDC", "side": "long", "market_type": "swap"},
            strategy_payload={"sourceId": 9, "name": "管理策略"},
        )

    assert exc.value.status_code == 409
    assert service.deleted == [(44, 3)]
    assert recorded == []


def test_create_managed_strategy_reports_cleanup_failure(monkeypatch):
    service = _StrategyService()
    service.delete_strategy = lambda *_args, **_kwargs: False
    service.get_strategy = lambda strategy_id, user_id=None: {
        "id": strategy_id,
        "strategy_name": "BTC 管理策略",
        "status": "stopped",
        "timeframe": "1h",
        "trading_config": {
            "strategy_manifest": {
                "universe": {
                    "instruments": [{"market": "Crypto", "symbol": "BTC/USDT", "market_type": "swap"}],
                }
            }
        },
    }
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: _snapshot())
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])

    with pytest.raises(position_management.PositionManagementError, match="残留策略 ID：44") as exc:
        position_management.create_managed_strategy(
            user_id=3,
            position_ref={
                "credential_id": 7,
                "symbol": "KAITO/USDC",
                "side": "long",
                "market_type": "swap",
            },
            strategy_payload={"sourceId": 9, "name": "管理策略"},
        )

    assert exc.value.status_code == 500


def test_cleanup_exception_does_not_hide_original_creation_error(monkeypatch):
    service = _StrategyService()

    def fail_delete(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    service.delete_strategy = fail_delete
    service.get_strategy = lambda strategy_id, user_id=None: {
        "id": strategy_id,
        "strategy_name": "BTC 管理策略",
        "status": "stopped",
        "timeframe": "1h",
        "trading_config": {
            "strategy_manifest": {
                "universe": {
                    "instruments": [{"market": "Crypto", "symbol": "BTC/USDT", "market_type": "swap"}],
                }
            }
        },
    }
    monkeypatch.setattr(position_management, "get_strategy_service", lambda: service)
    monkeypatch.setattr(position_management, "fetch_account_snapshot", lambda **_kwargs: _snapshot())
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", lambda **_kwargs: [])

    with pytest.raises(position_management.PositionManagementError, match="策略源码没有订阅当前仓位品种") as exc:
        position_management.create_managed_strategy(
            user_id=3,
            position_ref={
                "credential_id": 7,
                "symbol": "KAITO/USDC",
                "side": "long",
                "market_type": "swap",
            },
            strategy_payload={"sourceId": 9, "name": "管理策略"},
        )

    assert exc.value.status_code == 500
    assert "残留策略 ID：44" in str(exc.value)


def test_create_managed_strategy_route_returns_created_instance(monkeypatch):
    app = Flask(__name__)
    expected = {"strategy_id": 44, "status": "stopped"}
    calls = []

    def fake_create(*, user_id, position_ref, strategy_payload):
        calls.append((user_id, position_ref, strategy_payload))
        return expected

    monkeypatch.setattr(position_management, "create_managed_strategy", fake_create)
    payload = {
        "position": {"credential_id": 7, "symbol": "KAITO/USDC", "side": "long", "market_type": "swap"},
        "strategy": {"sourceId": 9, "name": "管理策略"},
    }
    with app.test_request_context("/api/account/managed-strategies", method="POST", json=payload):
        g.user_id = 3
        response, status = inspect.unwrap(routes.create_managed_account_strategy)()

    assert status == 201
    assert response.get_json() == {"code": 1, "msg": "已创建持仓管理策略", "data": expected}
    assert calls == [(3, payload["position"], payload["strategy"])]
