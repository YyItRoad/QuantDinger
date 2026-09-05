"""币安账户快照解析与目标市场刷新测试。"""

from app.services.live_trading import account_snapshot
from app.services.live_trading.account_snapshot import _parse_binance_futures_positions


def test_parse_binance_futures_position_keeps_mark_price():
    rows = _parse_binance_futures_positions(
        [
            {
                "symbol": "KAITOUSDC",
                "positionAmt": "422.5",
                "entryPrice": "0.355",
                "markPrice": "0.3612",
                "leverage": "10",
            }
        ]
    )

    assert len(rows) == 1
    assert rows[0]["entry_price"] == 0.355
    assert rows[0]["mark_price"] == 0.3612
    assert rows[0]["leverage"] == 10


def test_target_swap_snapshot_only_fetches_swap_positions(monkeypatch):
    client = object()
    calls = []
    expected = [{"symbol": "BTC/USDT", "side": "long", "size": 1}]

    monkeypatch.setattr(
        account_snapshot,
        "resolve_exchange_config",
        lambda *_args, **_kwargs: {"exchange_id": "binance"},
    )

    def create_client(_config, *, market_type):
        calls.append(("client", market_type))
        return client

    def fetch_swap(actual_client, exchange_id, errors):
        calls.append(("swap", actual_client, exchange_id, errors))
        return expected

    monkeypatch.setattr(account_snapshot, "create_client", create_client)
    monkeypatch.setattr(account_snapshot, "_fetch_swap_positions_snapshot", fetch_swap)
    monkeypatch.setattr(
        account_snapshot,
        "_fetch_spot_wallet",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应查询现货")),
    )

    snapshot = account_snapshot.fetch_target_position_snapshot(
        user_id=3,
        credential_id=7,
        market_type="swap",
    )

    assert calls == [
        ("client", "swap"),
        ("swap", client, "binance", []),
    ]
    assert snapshot["swap_positions"] == expected
    assert snapshot["spot_positions"] == []
    assert snapshot["open_orders"] == []
    assert snapshot["partial"] is False
    assert snapshot["error"] == ""


def test_target_spot_snapshot_only_fetches_spot_wallet(monkeypatch):
    client = object()
    expected = [{"symbol": "ETH/USDT", "side": "long", "size": 0.25}]

    monkeypatch.setattr(
        account_snapshot,
        "resolve_exchange_config",
        lambda *_args, **_kwargs: {"exchange_id": "binance"},
    )
    monkeypatch.setattr(account_snapshot, "create_client", lambda _config, *, market_type: client)
    monkeypatch.setattr(
        account_snapshot,
        "_fetch_swap_positions_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应查询合约")),
    )
    monkeypatch.setattr(
        account_snapshot,
        "_fetch_spot_wallet",
        lambda actual_client, _errors, *, label: expected
        if actual_client is client and label == "BINANCE 现货持仓"
        else [],
    )

    snapshot = account_snapshot.fetch_target_position_snapshot(
        user_id=3,
        credential_id=7,
        market_type="spot",
    )

    assert snapshot["swap_positions"] == []
    assert snapshot["spot_positions"] == expected
    assert snapshot["open_orders"] == []


def test_target_snapshot_preserves_exchange_failure(monkeypatch):
    monkeypatch.setattr(
        account_snapshot,
        "resolve_exchange_config",
        lambda *_args, **_kwargs: {"exchange_id": "binance"},
    )
    monkeypatch.setattr(
        account_snapshot,
        "create_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("request timeout")),
    )

    snapshot = account_snapshot.fetch_target_position_snapshot(
        user_id=3,
        credential_id=7,
        market_type="swap",
    )

    assert snapshot["swap_positions"] == []
    assert "request timeout" in snapshot["error"]
    assert snapshot["warnings"] == [snapshot["error"]]


def test_managed_positions_snapshot_fetches_both_markets_without_orders(monkeypatch):
    calls = []

    def fetch_target(**kwargs):
        calls.append(kwargs)
        if kwargs["market_type"] == "swap":
            return {
                "swap_positions": [{"symbol": "BTC/USDT", "size": 1}],
                "spot_positions": [],
                "warnings": [],
                "error": "",
                "exchange_id": "binance",
            }
        return {
            "swap_positions": [],
            "spot_positions": [{"symbol": "ETH/USDT", "size": 2}],
            "warnings": [],
            "error": "",
            "exchange_id": "binance",
        }

    monkeypatch.setattr(account_snapshot, "fetch_target_position_snapshot", fetch_target)

    snapshot = account_snapshot.fetch_managed_positions_snapshot(
        user_id=3,
        credential_id=7,
    )

    assert calls == [
        {
            "user_id": 3,
            "credential_id": 7,
            "market_type": "swap",
            "request_timeout_sec": 6.0,
        },
        {
            "user_id": 3,
            "credential_id": 7,
            "market_type": "spot",
            "request_timeout_sec": 6.0,
        },
    ]
    assert snapshot["swap_positions"] == [{"symbol": "BTC/USDT", "size": 1}]
    assert snapshot["spot_positions"] == [{"symbol": "ETH/USDT", "size": 2}]
    assert snapshot["open_orders"] == []
    assert snapshot["warnings"] == []
    assert snapshot["error"] == ""


def test_target_snapshot_caps_request_timeout(monkeypatch):
    class Client:
        timeout_sec = 15.0

    client = Client()
    observed = []
    monkeypatch.setattr(
        account_snapshot,
        "resolve_exchange_config",
        lambda *_args, **_kwargs: {"exchange_id": "binance"},
    )
    monkeypatch.setattr(account_snapshot, "create_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        account_snapshot,
        "_fetch_swap_positions_snapshot",
        lambda actual_client, *_args: observed.append(actual_client.timeout_sec) or [],
    )

    account_snapshot.fetch_target_position_snapshot(
        user_id=3,
        credential_id=7,
        market_type="swap",
        request_timeout_sec=6.0,
    )

    assert observed == [6.0]
