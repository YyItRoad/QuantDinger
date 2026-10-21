"""持仓管理交易历史同步。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.services.live_trading import position_management_history as history


def _source_row():
    return {
        "trade_id": 12,
        "trade_user_id": 1,
        "strategy_id": 24,
        "trade_symbol": "1000CAT/USDT",
        "symbol_canonical": "1000CAT/USDT",
        "trade_type": "close_long",
        "trade_price": 0.002575,
        "trade_amount": 51046,
        "trade_value": 131.44345,
        "commission": 0,
        "commission_ccy": "",
        "commission_quote": 0,
        "trade_profit": 31.444336,
        "close_reason": "atr_long_position_stop",
        "matched_entry_price": 0.001959,
        "grid_matched_profit": 31.444336,
        "trade_market_type": "swap",
        "trade_credential_id": 2,
        "trade_inst_id": "1000CATUSDT",
        "fill_source": "worker",
        "trade_pending_order_id": 13,
        "grid_order_id": 0,
        "trade_strategy_run_id": 45,
        "trade_order_intent_id": 13,
        "execution_event_id": 0,
        "exchange_fill_id": "",
        "fee_status": "pending",
        "fee_source": "",
        "trade_created_at": datetime(2026, 9, 5, 20, 0, 27),
        "strategy_name": "[持仓] 1000CAT/USDT",
        "strategy_type": "StrategyV2",
        "market_category": "Crypto",
        "execution_mode": "live",
        "strategy_status": "stopped",
        "strategy_timeframe": "1h",
        "strategy_leverage": 5,
        "strategy_created_at": datetime(2026, 9, 4, 12, 28, 56),
        "strategy_started_at": datetime(2026, 9, 4, 14, 38, 23),
        "strategy_stopped_at": datetime(2026, 9, 5, 20, 0, 29),
        "strategy_stop_reason": "strategy stopped",
        "order_exchange_id": "binance",
        "order_credential_id": 2,
        "order_type": "market",
        "order_status": "filled",
        "requested_amount": 51046,
        "exchange_order_id": "2240430598",
        "client_order_id": "qd_24_13_mkt",
        "signal_ts": 1788634800,
        "order_created_at": datetime(2026, 9, 5, 20, 0, 4),
        "order_sent_at": datetime(2026, 9, 5, 20, 0, 26),
        "order_executed_at": datetime(2026, 9, 5, 20, 0, 26),
        "exchange_account_name": "trade-spring",
        "credential_exchange_id": "binance",
    }


def test_history_row_uses_existing_pnl_semantics_and_strategy_creation_time():
    item = history._history_row(
        _source_row(),
        funding_total=-0.01639376,
        funding_currency="USDT",
    )

    assert item["entry_time"] == datetime(2026, 9, 4, 12, 28, 56)
    assert item["exit_time"] == datetime(2026, 9, 5, 20, 0, 26)
    assert item["entry_price"] == pytest.approx(0.001959)
    assert item["exit_price"] == pytest.approx(0.002575)
    assert item["profit_rate"] == pytest.approx(157.22307299642675)
    assert item["exchange_account_name"] == "trade-spring"
    assert item["settlement_ccy"] == "USDT"
    assert item["funding_fee_total"] == pytest.approx(-0.01639376)


def test_insert_history_rows_is_idempotent_by_source_trade_id():
    class Cursor:
        rowcount = 0

        def execute(self, sql, params):
            assert "ON CONFLICT (source_trade_id) DO NOTHING" in sql
            assert len(params) == len(history.HISTORY_COLUMNS)

    row = {column: None for column in history.HISTORY_COLUMNS}
    assert history._insert_history_rows(Cursor(), [row]) is None


def test_public_history_row_serializes_utc_time_and_decimals():
    item = history._public_history_row({
        "exit_time": datetime(2026, 9, 5, 20, 0, 26),
        "profit": Decimal("31.444336"),
    })

    assert item == {
        "exit_time": "2026-09-05T20:00:26Z",
        "profit": pytest.approx(31.444336),
    }


def test_list_history_is_scoped_and_paginated(monkeypatch):
    class Cursor:
        calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return {"total": 21}

        def fetchall(self):
            return [{
                "id": 9,
                "credential_id": 2,
                "exit_time": datetime(2026, 9, 5, 20, 0, 26),
                "profit": Decimal("31.44"),
            }]

        def close(self):
            return None

    cursor = Cursor()

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return cursor

    monkeypatch.setattr(history, "get_db_connection", lambda: Database())

    result = history.list_position_management_trade_history(
        user_id=1,
        credential_id=2,
        page=2,
        page_size=20,
    )

    assert result["total"] == 21
    assert result["page"] == 2
    assert result["page_size"] == 20
    assert result["items"][0]["exit_time"] == "2026-09-05T20:00:26Z"
    assert cursor.calls[0][1] == (1, 2)
    assert cursor.calls[1][1] == (1, 2, 20, 20)


def test_sync_rejects_running_strategy(monkeypatch):
    class Cursor:
        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return {
                "id": 24,
                "user_id": 1,
                "status": "running",
                "trading_config": {"position_management": {"enabled": True}},
            }

        def close(self):
            return None

    class Database:
        rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(history, "get_db_connection", lambda: Database())

    with pytest.raises(history.PositionManagementHistorySyncError, match="尚未停止"):
        history.sync_position_management_trade_history(24, user_id=1)


def test_periodic_sync_only_scans_stopped_strategies_with_missing_history(monkeypatch):
    class Cursor:
        def execute(self, sql, params):
            assert "LOWER(COALESCE(s.status, '')) = 'stopped'" in sql
            assert "h.id IS NULL" in sql
            assert "latest_run.stopped_at" not in sql
            assert params == ()

        def fetchall(self):
            return [{"id": 24}, {"id": 25}]

        def close(self):
            return None

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    synced = []

    def sync(strategy_id):
        synced.append(strategy_id)
        if strategy_id == 25:
            raise history.PositionManagementHistorySyncError("策略没有可同步的交易记录")
        return {"inserted_count": 1, "already_synced_count": 0}

    monkeypatch.setattr(history, "get_db_connection", lambda: Database())
    monkeypatch.setattr(history, "sync_position_management_trade_history", sync)

    result = history.sync_stopped_position_management_history()

    assert synced == [24, 25]
    assert result["scan_scope"] == "stopped_with_unsynced_trades"
    assert result["archived_strategy_ids"] == [24]
    assert result["inserted_count"] == 1
    assert result["skipped"] == [{"strategy_id": 25, "reason": "策略没有可同步的交易记录"}]
    assert result["deleted_count"] == 0


@pytest.mark.parametrize('start,end', [
    ('2026-10-01', None), ('bad', None), ('', None),
    ('2026-10-02T00:00:00Z', '2026-10-01T00:00:00Z'),
    ('2026-10-01T00:00:00Z', '2026-10-01T00:00:00+00:00'),
])
def test_archive_filter_rejects_invalid_time_bounds_before_database(monkeypatch, start, end):
    def unexpected_connection():
        pytest.fail('Invalid filters must not query the database')
    monkeypatch.setattr(history, 'get_db_connection', unexpected_connection)
    with pytest.raises(ValueError):
        history.list_position_management_trade_history(user_id=7, credential_id=2,
                                                       start_time=start, end_time=end)


def test_archive_query_filters_are_bound_and_count_matches_list(monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import Mock
    cursor = Mock()
    cursor.fetchone.return_value = {'total': 0}
    cursor.fetchall.return_value = []
    db = Mock()
    db.cursor.return_value = cursor
    @contextmanager
    def connect():
        yield db
    monkeypatch.setattr(history, 'get_db_connection', connect)
    symbol = "BTC/USDT' OR 1=1 --"
    result = history.list_position_management_trade_history(user_id=7, credential_id=2,
        symbol=symbol, market_type='swap', position_side='long', source_strategy_id=99,
        start_time='2026-10-01T08:00:00+08:00', end_time='2026-10-02T00:00:00Z', page=2, page_size=5)
    assert result == {'items': [], 'total': 0, 'page': 2, 'page_size': 5}
    expected = (7, 2, symbol, 'swap', 'long', 99, datetime(2026, 10, 1), datetime(2026, 10, 2))
    count, listing = cursor.execute.call_args_list
    assert count.args[1] == expected
    assert listing.args[1] == (*expected, 5, 5)
    for call in (count, listing):
        sql = call.args[0]
        assert symbol not in sql
        assert 'user_id = %s AND credential_id = %s' in sql
        assert 'exit_time >= %s AND exit_time < %s' in sql
        assert 'JOIN' not in sql
    assert 'ORDER BY exit_time DESC NULLS LAST, id DESC' in listing.args[0]
    db.commit.assert_not_called()
    cursor.close.assert_called_once()


@pytest.mark.parametrize('row', [None, {'id': 11, 'entry_time': datetime(2026, 10, 1), 'profit': Decimal('1.5')}])
def test_detail_is_account_and_user_scoped_without_source_dependency(monkeypatch, row):
    from contextlib import contextmanager
    from unittest.mock import Mock
    cursor = Mock()
    cursor.fetchone.return_value = row
    db = Mock()
    db.cursor.return_value = cursor
    @contextmanager
    def connect():
        yield db
    monkeypatch.setattr(history, 'get_db_connection', connect)
    result = history.get_position_management_trade_history(user_id=7, credential_id=2, history_id=11)
    sql, params = cursor.execute.call_args.args
    assert 'WHERE user_id = %s AND credential_id = %s AND id = %s' in sql
    assert 'JOIN' not in sql and params == (7, 2, 11)
    assert result == (None if row is None else {'id': 11, 'entry_time': '2026-10-01T00:00:00Z', 'profit': 1.5})
    db.commit.assert_not_called()
    cursor.close.assert_called_once()


def test_real_archive_selects_cannot_cross_users_or_accounts(monkeypatch):
    """Execute the archive SELECTs against an isolated SQL fixture."""
    import sqlite3
    from contextlib import contextmanager
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE qd_position_management_trade_history '
               '(id INTEGER, user_id INTEGER, credential_id INTEGER, exit_time TEXT)')
    db.executemany('INSERT INTO qd_position_management_trade_history VALUES (?, ?, ?, ?)', [
        (11, 7, 2, '2026-10-01T00:00:00Z'), (12, 8, 2, '2026-10-01T00:00:00Z'),
        (13, 7, 3, '2026-10-01T00:00:00Z'), (14, 7, 2, '2026-10-01T00:00:00Z'),
    ])

    class Cursor:
        def __init__(self):
            self.cursor = db.cursor()
        def execute(self, sql, params):
            self.cursor.execute(sql.replace('%s', '?'), params)
        def fetchone(self):
            row = self.cursor.fetchone()
            return dict(row) if row else None
        def fetchall(self):
            return [dict(row) for row in self.cursor.fetchall()]
        def close(self):
            self.cursor.close()

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connect():
        yield Connection()
    monkeypatch.setattr(history, 'get_db_connection', connect)
    try:
        result = history.list_position_management_trade_history(user_id=7, credential_id=2)
        assert result['total'] == 2 and [row['id'] for row in result['items']] == [14, 11]
        assert history.get_position_management_trade_history(user_id=7, credential_id=2, history_id=11)['id'] == 11
        for foreign_id in (12, 13, 999):
            assert history.get_position_management_trade_history(user_id=7, credential_id=2, history_id=foreign_id) is None
        assert history.list_position_management_trade_history(user_id=7, credential_id=999)['total'] == 0
    finally:
        db.close()
