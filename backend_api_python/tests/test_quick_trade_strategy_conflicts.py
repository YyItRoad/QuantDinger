from app.services.live_trading.gate import GateUsdtFuturesClient
from app.services.quick_trade.strategy_conflicts import recent_gate_strategy_activity


def _client(rows):
    client = object.__new__(GateUsdtFuturesClient)
    client._signed_request = lambda method, path, params: rows
    return client


def test_recent_gate_strategy_activity_identifies_strategy_owner():
    client = _client([
        {
            "contract": "BTC_USDT",
            "order_id": "close-1",
            "create_time": 950.0,
            "text": "t-qd_888_170982_mkt",
        },
        {
            "contract": "BTC_USDT",
            "order_id": "manual-1",
            "create_time": 990.0,
            "text": "t-qt309266237eb0f1",
        },
    ])

    conflict = recent_gate_strategy_activity(client, "BTC/USDT", now=1000.0)

    assert conflict["active"] is True
    assert conflict["strategy_id"] == "888"
    assert conflict["exchange_order_id"] == "close-1"


def test_recent_gate_strategy_activity_ignores_stale_or_manual_fills():
    client = _client([
        {"contract": "BTC_USDT", "create_time": -1000.0, "text": "t-qd_888_170900_mkt"},
        {"contract": "BTC_USDT", "create_time": 995.0, "text": "t-qt309266237eb0f1"},
    ])

    assert recent_gate_strategy_activity(client, "BTC/USDT", now=1000.0) is None


def test_recent_gate_strategy_activity_clears_after_stable_manual_fill():
    client = _client([
        {
            "contract": "BTC_USDT",
            "order_id": "strategy-close",
            "create_time": 700.0,
            "text": "t-qd_888_170984_mkt",
        },
        {
            "contract": "BTC_USDT",
            "order_id": "manual-open",
            "create_time": 800.0,
            "text": "t-qt310213ade0a4e1",
        },
    ])

    assert recent_gate_strategy_activity(client, "BTC/USDT", now=1000.0) is None


def test_recent_gate_strategy_activity_keeps_grace_after_new_manual_fill():
    client = _client([
        {
            "contract": "BTC_USDT",
            "order_id": "strategy-close",
            "create_time": 950.0,
            "text": "t-qd_888_170984_mkt",
        },
        {
            "contract": "BTC_USDT",
            "order_id": "manual-open",
            "create_time": 990.0,
            "text": "t-qt310213ade0a4e1",
        },
    ])

    conflict = recent_gate_strategy_activity(client, "BTC/USDT", now=1000.0)
    assert conflict["strategy_id"] == "888"
