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
