"""持仓管理页只读查询现有策略仓位的接口契约。"""

import inspect
from decimal import Decimal

from flask import Flask, g

from app.routes import strategy_position_management_routes as routes
from app.services.live_trading import position_management


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = tuple(params)

    def fetchall(self):
        return self.rows

    def close(self):
        return None


class _Database:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def test_list_managed_positions_uses_existing_strategy_ledger(monkeypatch):
    cursor = _Cursor([{
        "strategy_id": 12,
        "strategy_name": "ATR 趋势管理",
        "strategy_status": "running",
        "execution_mode": "live",
        "symbol": "KAITOUSDC",
        "symbol_canonical": "",
        "side": "LONG",
        "size": Decimal("422.50000000"),
        "market_type": "futures",
        "credential_id": 7,
        "inst_id": "KAITOUSDC",
    }])
    monkeypatch.setattr(position_management, "get_db_connection", lambda: _Database(cursor))

    rows = position_management.list_managed_positions_for_account(user_id=3, credential_id=7)

    assert cursor.params == (3, 7)
    assert "p.size > 0" in cursor.sql
    assert rows == [{
        "strategy_id": 12,
        "strategy_name": "ATR 趋势管理",
        "strategy_status": "running",
        "execution_mode": "live",
        "symbol": "KAITO/USDC",
        "side": "long",
        "size": "422.50000000",
        "market_type": "swap",
        "credential_id": 7,
        "inst_id": "KAITOUSDC",
    }]


def test_managed_positions_returns_existing_strategy_rows(monkeypatch):
    app = Flask(__name__)
    expected = [{
        "strategy_id": 12,
        "strategy_name": "ATR 趋势管理",
        "strategy_status": "running",
        "execution_mode": "live",
        "symbol": "KAITO/USDC",
        "side": "long",
        "size": "422.5",
        "market_type": "swap",
        "credential_id": 7,
        "inst_id": "KAITOUSDC",
    }]
    calls = []

    def fake_list(*, user_id, credential_id):
        calls.append((user_id, credential_id))
        return expected

    monkeypatch.setattr(position_management, "list_managed_positions_for_account", fake_list)
    with app.test_request_context("/api/account/managed-positions?credential_id=7"):
        g.user_id = 3
        response = inspect.unwrap(routes.get_managed_account_positions)()

    assert calls == [(3, 7)]
    assert response.get_json() == {
        "code": 1,
        "msg": "success",
        "data": {"items": expected},
    }


def test_managed_positions_requires_credential_id():
    app = Flask(__name__)
    with app.test_request_context("/api/account/managed-positions"):
        g.user_id = 3
        response, status = inspect.unwrap(routes.get_managed_account_positions)()

    assert status == 400
    assert response.get_json() == {
        "code": 0,
        "msg": "Missing credential_id",
        "data": {"items": []},
    }


def test_position_snapshot_uses_lightweight_management_service(monkeypatch):
    app = Flask(__name__)
    expected = {
        "swap_positions": [{"symbol": "BTC/USDT", "size": 1}],
        "spot_positions": [],
        "open_orders": [],
        "warnings": [],
        "error": "",
    }
    calls = []

    def fake_snapshot(*, user_id, credential_id):
        calls.append((user_id, credential_id))
        return expected

    monkeypatch.setattr(
        "app.services.live_trading.account_snapshot.fetch_managed_positions_snapshot",
        fake_snapshot,
    )
    with app.test_request_context("/api/account/position-snapshot?credential_id=7"):
        g.user_id = 3
        response = inspect.unwrap(routes.get_managed_position_snapshot)()

    assert calls == [(3, 7)]
    assert response.get_json() == {
        "code": 1,
        "msg": "success",
        "data": expected,
    }
