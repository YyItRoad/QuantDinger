import pytest

from app.services.live_trading import execution_lock


class FakeCursor:
    def __init__(self, acquisitions):
        self.acquisitions = iter(acquisitions)
        self.queries = []
        self.closed = False

    def execute(self, query, params):
        self.queries.append((query, params))

    def fetchone(self):
        return {"acquired": next(self.acquisitions)}

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_trade_execution_key_normalizes_market_and_symbol():
    assert execution_lock.trade_execution_key(
        user_id=7,
        credential_id=9,
        market_type="perpetual",
        symbol="solusdt",
    ) == "trade-execution:7:9:swap:SOL/USDT"


def test_trade_execution_lock_releases_acquired_session_lock(monkeypatch):
    cursor = FakeCursor([True])
    monkeypatch.setattr(
        execution_lock,
        "get_db_connection",
        lambda: FakeConnection(cursor),
    )

    with execution_lock.trade_execution_lock("account-symbol", timeout_sec=0):
        pass

    assert "pg_try_advisory_lock" in cursor.queries[0][0]
    assert "pg_advisory_unlock" in cursor.queries[-1][0]
    assert cursor.closed is True


def test_trade_execution_lock_reports_contention_without_unlock(monkeypatch):
    cursor = FakeCursor([False])
    monkeypatch.setattr(
        execution_lock,
        "get_db_connection",
        lambda: FakeConnection(cursor),
    )

    with pytest.raises(execution_lock.TradeExecutionBusy):
        with execution_lock.trade_execution_lock("account-symbol", timeout_sec=0):
            pass

    assert len(cursor.queries) == 1
    assert "pg_try_advisory_lock" in cursor.queries[0][0]
    assert cursor.closed is True
