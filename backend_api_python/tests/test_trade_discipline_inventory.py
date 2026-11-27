import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app.trade_discipline import inventory_cli as trade_discipline_inventory
from app.services import exchange_execution
from app.services.live_trading import position_management
from app.services.live_trading.binance import BinanceFuturesClient
from app.utils.credential_crypto import encrypt_credential_blob


@pytest.fixture
def inventory_sources(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-inventory-key")
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "7")
    monkeypatch.delenv("TRADE_DISCIPLINE_MARKET_TYPE", raising=False)
    monkeypatch.setattr("sys.argv", ["trade_discipline_inventory"])
    credential = {
        "exchange_id": "binance",
        "encrypted_config": encrypt_credential_blob(json.dumps({
            "api_key": "mock-api-key", "secret_key": "mock-secret-key",
        })),
    }
    credential_cursor = Mock()
    credential_cursor.fetchone.side_effect = (
        lambda: credential if credential_cursor.execute.call_args.args[1] == (7, 3) else None
    )
    management_cursor = Mock()
    management_cursor.fetchall.return_value = [
        {
            "strategy_id": 12, "strategy_name": "管理策略", "strategy_status": "stopped",
            "execution_mode": "live", "symbol": "BTCUSDT", "market_type": "futures",
            "side": "LONG", "size": "0.25", "credential_id": 7, "inst_id": "BTCUSDT",
        },
        {
            "strategy_id": 16, "symbol": "ETHUSDT", "market_type": "spot",
            "side": "short", "size": "0.5", "credential_id": 7, "inst_id": "ETHUSDT",
        },
        {
            "strategy_id": 17, "symbol": "BTCUSDT", "market_type": "swap",
            "side": "short", "size": "0.25", "credential_id": 7, "inst_id": "BTCUSDT",
        },
        {
            "strategy_id": 18, "symbol": "BTCUSDT", "market_type": "swap",
            "side": "long", "size": "0.25", "credential_id": 7, "inst_id": "BTCUSDT_OTHER",
        },
    ]

    @contextmanager
    def read_connection(cursor):
        yield Mock(cursor=Mock(return_value=cursor))

    monkeypatch.setattr(exchange_execution, "get_db_connection", lambda: read_connection(credential_cursor))
    monkeypatch.setattr(position_management, "get_db_connection", lambda: read_connection(management_cursor))
    request = Mock(return_value=[
        {"symbol": "BTCUSDT", "positionAmt": "0.25", "entryPrice": "60000", "markPrice": "61000"},
        {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3000", "markPrice": "2900"},
    ])
    monkeypatch.setattr(BinanceFuturesClient, "_signed_request", request)
    return credential_cursor, management_cursor, request


def test_inventory_command_reuses_position_and_registration_reads(inventory_sources, capsys):
    credential_cursor, management_cursor, request = inventory_sources

    assert trade_discipline_inventory.main() == 0
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "ok"
    assert report["source"] == "position_snapshot"
    assert report["fetched_at"] > 0
    assert report["exchange_id"] == "binance"
    assert credential_cursor.execute.call_args.args[1] == (7, 3)
    assert management_cursor.execute.call_args.args[1] == (3, 7)
    for cursor in (credential_cursor, management_cursor):
        assert cursor.execute.call_count == 1
        assert cursor.execute.call_args.args[0].lstrip().startswith("SELECT ")
    request.assert_called_once_with("GET", "/fapi/v2/positionRisk", params={})
    btc, eth = report["positions"]
    assert (btc["symbol"], btc["side"], btc["size"], btc["management_status"]) == (
        "BTC/USDT", "long", 0.25, "registered",
    )
    assert [item["strategy_id"] for item in btc["registrations"]] == [12]
    assert btc["registrations"][0]["strategy_status"] == "stopped"
    assert (eth["symbol"], eth["side"], eth["size"], eth["management_status"]) == (
        "ETH/USDT", "short", 0.5, "unregistered",
    )


def test_inventory_wrong_owner_never_reads_exchange(inventory_sources, monkeypatch, capsys):
    _credential_cursor, management_cursor, request = inventory_sources
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "4")

    assert trade_discipline_inventory.main() == 1
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "unavailable"
    assert report["positions"] == []
    request.assert_not_called()
    management_cursor.execute.assert_not_called()


@pytest.mark.parametrize(
    ("times_out", "expected_status", "expected_exit"),
    [(False, "ok", 0), (True, "unavailable", 1)],
)
def test_inventory_distinguishes_empty_positions_from_timeout(
    inventory_sources, capsys, times_out, expected_status, expected_exit,
):
    _credential_cursor, management_cursor, request = inventory_sources
    request.return_value = []
    if times_out:
        request.side_effect = TimeoutError("mock exchange timeout")

    assert trade_discipline_inventory.main() == expected_exit
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == expected_status
    assert report["positions"] == []
    management_cursor.execute.assert_not_called()


def test_inventory_preserves_positions_if_registration_read_fails(inventory_sources, capsys):
    _credential_cursor, management_cursor, _request = inventory_sources
    management_cursor.execute.side_effect = RuntimeError("mock database timeout")

    assert trade_discipline_inventory.main() == 1
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "partial"
    assert report["issues"] == ["management_registrations_unavailable"]
    assert [row["management_status"] for row in report["positions"]] == ["unknown", "unknown"]


def test_inventory_partial_snapshot_is_visible(monkeypatch):
    monkeypatch.setattr(position_management, "fetch_managed_positions_snapshot", Mock(return_value={
        "exchange_id": "binance",
        "fetched_at": 123,
        "swap_positions": [{"symbol": "BTC/USDT", "side": "long", "size": 0.25}],
        "spot_positions": [],
        "partial": True,
    }))
    monkeypatch.setattr(position_management, "list_managed_positions_for_account", Mock(return_value=[]))

    report = position_management.read_account_position_inventory(user_id=3, credential_id=7, market_type="all")

    assert report["status"] == "partial"
    assert report["issues"] == ["position_snapshot_partial"]
    assert report["positions"][0]["symbol"] == "BTC/USDT"


@pytest.mark.parametrize(("key", "value"), [
    ("TRADE_DISCIPLINE_USER_ID", ""),
    ("TRADE_DISCIPLINE_CREDENTIAL_ID", "0"),
    ("TRADE_DISCIPLINE_MARKET_TYPE", "unknown"),
])
def test_inventory_rejects_invalid_scope(inventory_sources, monkeypatch, key, value):
    credential_cursor, management_cursor, request = inventory_sources
    monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit, match="2"):
        trade_discipline_inventory.main()

    credential_cursor.execute.assert_not_called()
    management_cursor.execute.assert_not_called()
    request.assert_not_called()


def test_read_only_all_accounts_reports_surplus_and_isolates_failure(monkeypatch, capsys):
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setattr("sys.argv", ["trade_discipline_inventory", "--all-accounts"])
    monkeypatch.setattr(position_management, "list_position_inventory_accounts", Mock(return_value=[
        {"id": 7, "exchange_id": "binance"},
        {"id": 8, "exchange_id": "okx"},
        {"id": 9, "exchange_id": "binance"},
    ]))

    def inventory(*, user_id, credential_id, market_type):
        assert user_id == 3 and market_type == "swap"
        if credential_id == 8:
            raise RuntimeError("simulated read error")
        registrations = [{"execution_mode": "live", "size": "0.4"}] if credential_id == 7 else []
        return {
            "credential_id": credential_id, "exchange_id": "binance", "status": "ok",
            "positions": [{"symbol": "BTC/USDT", "size": 0.6, "side": "long",
                           "management_status": "registered" if registrations else "unregistered",
                           "registrations": registrations}],
        }

    read = Mock(side_effect=inventory)
    monkeypatch.setattr(position_management, "read_account_position_inventory", read)
    assert trade_discipline_inventory.main() == 1
    first, failed, last = json.loads(capsys.readouterr().out)["accounts"]
    assert first["positions"][0]["monitor_candidate"] == "unallocated_surplus"
    assert failed["status"] == "unavailable"
    assert failed["discipline_coverage"] == "inventory_only"
    assert last["positions"][0]["monitor_candidate"] == "opening_origin_unverified"
    assert read.call_count == 3
