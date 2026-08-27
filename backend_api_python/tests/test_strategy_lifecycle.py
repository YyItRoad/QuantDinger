"""Tests for unified strategy auto-stop helpers."""

import pytest

from app.services import strategy_lifecycle
from app.services.live_trading import records
from app.services.strategy_lifecycle import is_fatal_exchange_error


class _ManagedStrategyService:
    def __init__(self, *, managed=True):
        self.strategy = {
            "id": 44,
            "user_id": 3,
            "status": "running",
            "trading_config": {
                "position_management": {
                    "enabled": managed,
                    "auto_stop_when_flat": managed,
                }
            },
        }

    def get_strategy(self, strategy_id):
        return dict(self.strategy) if int(strategy_id) == 44 else None

    def update_strategy_status(self, strategy_id, status, user_id=None):
        assert int(strategy_id) == 44
        assert int(user_id) == 3
        self.strategy["status"] = status
        return True


def test_managed_strategy_stops_after_last_position_is_flat(monkeypatch):
    service = _ManagedStrategyService()
    logs = []
    monkeypatch.setattr(strategy_lifecycle, "get_strategy_service", lambda: service)
    monkeypatch.setattr(strategy_lifecycle, "_strategy_has_open_positions", lambda _sid: False)
    monkeypatch.setattr(strategy_lifecycle, "append_strategy_log", lambda *args: logs.append(args))

    assert strategy_lifecycle.maybe_stop_position_management_strategy(44) is True
    assert service.strategy["status"] == "stopped"
    assert logs == [(44, "info", "持仓已全部平仓，管理策略已自动停止")]


def test_confirmed_full_close_notifies_position_management_lifecycle(monkeypatch):
    deleted = []
    lifecycle_checks = []
    monkeypatch.setattr(records, "_fetch_position_fuzzy", lambda *_args: ({
        "size": 2,
        "entry_price": 0.30,
        "highest_price": 0.36,
        "lowest_price": 0.28,
        "symbol": "KAITO/USDC",
    }, "KAITO/USDC"))
    monkeypatch.setattr(records, "_delete_position", lambda *args: deleted.append(args))
    monkeypatch.setattr(
        strategy_lifecycle,
        "maybe_stop_position_management_strategy",
        lambda strategy_id: lifecycle_checks.append(strategy_id) or True,
    )

    profit, position, matched_entry = records.apply_fill_to_local_position(
        strategy_id=44,
        symbol="KAITO/USDC",
        signal_type="close_long",
        filled=2,
        avg_price=0.35,
    )

    assert profit == pytest.approx(0.1)
    assert position is None
    assert matched_entry == 0.30
    assert deleted == [(44, "KAITO/USDC", "long")]
    assert lifecycle_checks == [44]


def test_lifecycle_failure_does_not_break_confirmed_close(monkeypatch):
    deleted = []
    monkeypatch.setattr(records, "_fetch_position_fuzzy", lambda *_args: ({
        "size": 2,
        "entry_price": 0.30,
        "symbol": "KAITO/USDC",
    }, "KAITO/USDC"))
    monkeypatch.setattr(records, "_delete_position", lambda *args: deleted.append(args))

    def fail_lifecycle(_strategy_id):
        raise RuntimeError("lifecycle database unavailable")

    monkeypatch.setattr(
        strategy_lifecycle,
        "maybe_stop_position_management_strategy",
        fail_lifecycle,
    )

    profit, position, matched_entry = records.apply_fill_to_local_position(
        strategy_id=44,
        symbol="KAITO/USDC",
        signal_type="close_long",
        filled=2,
        avg_price=0.35,
    )

    assert profit == pytest.approx(0.1)
    assert position is None
    assert matched_entry == 0.30
    assert deleted == [(44, "KAITO/USDC", "long")]


def test_normal_strategy_is_not_stopped_when_flat(monkeypatch):
    service = _ManagedStrategyService(managed=False)
    monkeypatch.setattr(strategy_lifecycle, "get_strategy_service", lambda: service)
    monkeypatch.setattr(strategy_lifecycle, "_strategy_has_open_positions", lambda _sid: False)

    assert strategy_lifecycle.maybe_stop_position_management_strategy(44) is False
    assert service.strategy["status"] == "running"


def test_managed_strategy_keeps_running_while_any_position_remains(monkeypatch):
    service = _ManagedStrategyService()
    monkeypatch.setattr(strategy_lifecycle, "get_strategy_service", lambda: service)
    monkeypatch.setattr(strategy_lifecycle, "_strategy_has_open_positions", lambda _sid: True)

    assert strategy_lifecycle.maybe_stop_position_management_strategy(44) is False
    assert service.strategy["status"] == "running"


def test_partial_close_does_not_notify_position_management_lifecycle(monkeypatch):
    lifecycle_checks = []
    updated = []
    monkeypatch.setattr(records, "_fetch_position_fuzzy", lambda *_args: ({
        "size": 2,
        "entry_price": 0.30,
        "highest_price": 0.36,
        "lowest_price": 0.28,
        "symbol": "KAITO/USDC",
    }, "KAITO/USDC"))
    monkeypatch.setattr(records, "upsert_position", lambda **kwargs: updated.append(kwargs))
    monkeypatch.setattr(records, "_fetch_position", lambda *_args: {"size": 1})
    monkeypatch.setattr(
        strategy_lifecycle,
        "maybe_stop_position_management_strategy",
        lambda strategy_id: lifecycle_checks.append(strategy_id) or True,
    )

    profit, position, matched_entry = records.apply_fill_to_local_position(
        strategy_id=44,
        symbol="KAITO/USDC",
        signal_type="reduce_long",
        filled=1,
        avg_price=0.35,
    )

    assert profit == pytest.approx(0.05)
    assert position == {"size": 1}
    assert matched_entry == 0.30
    assert updated[0]["size"] == 1
    assert lifecycle_checks == []


def test_binance_auth_fatal():
    assert is_fatal_exchange_error('Binance HTTP 401: {"code":-2015,"msg":"Invalid API-key"}')


def test_ibkr_connection_fatal():
    assert is_fatal_exchange_error("Connect call failed ('127.0.0.1', 7497)")


def test_bitget_ip_fatal():
    assert is_fatal_exchange_error("Bitget error 40018: Invalid IP")


def test_okx_missing_credentials_fatal():
    assert is_fatal_exchange_error("Missing OKX api_key/secret_key/passphrase")


def test_maybe_auto_stop_fatal():
    from unittest.mock import patch

    from app.services.strategy_lifecycle import maybe_auto_stop_on_exchange_error

    with patch("app.services.strategy_lifecycle.auto_stop_live_strategy") as stop:
        assert maybe_auto_stop_on_exchange_error(1, "Missing OKX api_key/secret_key/passphrase", source="test")
        stop.assert_called_once()


def test_maybe_auto_stop_repeated():
    from unittest.mock import patch

    from app.services.strategy_lifecycle import maybe_auto_stop_on_exchange_error

    with patch("app.services.strategy_lifecycle.auto_stop_live_strategy") as stop:
        assert not maybe_auto_stop_on_exchange_error(1, "timeout", consecutive_failures=2, consecutive_threshold=5)
        stop.assert_not_called()
        assert maybe_auto_stop_on_exchange_error(1, "timeout", consecutive_failures=5, consecutive_threshold=5)
        stop.assert_called_once()
