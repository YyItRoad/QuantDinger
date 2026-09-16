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
