import json
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from app.trade_discipline import ema_cli as trade_discipline_ema
from app.trade_discipline import inventory_cli as trade_discipline_inventory
from app.trade_discipline import ema as discipline
from app.trade_discipline import position_evidence
from app.services.market_schedule import equity_hourly_bar_windows

_CLOSE = int(datetime.fromisoformat("2026-11-18T12:00:00+00:00").timestamp())
_INTERVAL = 4 * 3600


def bars(last_close=120, *, count=100):
    return [
        {"time": _CLOSE - i * _INTERVAL, "close": last_close if i == 1 else 1 if i <= 0 else 100}
        for i in range(count, -2, -1)
    ]


def crypto(direction="long", submitted_at="2026-11-18T12:00:00Z"):
    return discipline.check_ema25_at_order_time(
        market="Crypto", symbol="BTC/USDT", direction=direction, submitted_at=submitted_at,
        exchange_id="binance", market_type="swap",
    )


@pytest.mark.parametrize(("last_close", "direction", "relation", "status", "allowed"), [
    (120, "long", "above", "allowed", "long"),
    (120, "short", "above", "blocked", "long"),
    (80, "long", "below", "blocked", "short"),
    (80, "short", "below", "allowed", "short"),
    (100, "long", "equal", "blocked", None),
    (100, "short", "equal", "blocked", None),
])
def test_4h_ema25_uses_last_closed_bar(monkeypatch, last_close, direction, relation, status, allowed):
    provider = Mock(return_value=bars(last_close))
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", provider)

    result = crypto(direction)

    provider.assert_called_once_with("Crypto", "BTC/USDT", "4H", 101, before_time=_CLOSE + 1,
                                     exchange_id="binance", market_type="swap")
    assert (result["status"], result["relation"], result["allowed_direction"]) == (status, relation, allowed)
    assert result["bar_opened_at"] == "2026-11-18T08:00:00Z"
    assert result["bar_closed_at"] == "2026-11-18T12:00:00Z"
    assert result["close"] == last_close and result["bar_count"] == 100
    assert result["ema25"] == pytest.approx(100 + (last_close - 100) / 13)


def test_order_just_before_close_excludes_current_bar(monkeypatch):
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", Mock(return_value=bars()))

    result = crypto(submitted_at="2026-11-18T11:59:59.999Z")

    assert result["relation"] == "equal"
    assert result["bar_closed_at"] == "2026-11-18T08:00:00Z"
    assert result["close"] == 100


@pytest.mark.parametrize(("rows", "reason"), [
    ([], "market_data_unavailable"),
    ([row for row in bars() if row["time"] != _CLOSE - _INTERVAL], "latest_closed_bar_missing"),
    ([row for row in bars() if row["time"] != _CLOSE - 5 * _INTERVAL], "closed_bar_gap"),
    (bars(count=24), "insufficient_closed_bars"),
    ([row for row in bars() if row["time"] % (24 * 3600) == 0], "latest_closed_bar_missing"),
])
def test_unavailable_data_has_no_direction(monkeypatch, rows, reason):
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", Mock(return_value=rows))

    result = crypto()

    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert "allowed_direction" not in result


@pytest.mark.parametrize(("market", "symbol", "at", "open_at", "close_at", "label"), [
    ("CNStock", "600000", "2026-11-18T11:45:00+08:00", "02:30", "03:30", 0),
    ("CNStock", "600000", "2026-11-18T13:40:00+08:00", "02:30", "03:30", 1),
    ("HKStock", "00700", "2026-11-18T12:15:00+08:00", "03:30", "04:00", 1),
    ("USStock", "AAPL", "2026-11-27T13:02:00-05:00", "17:30", "18:00", 0),
    ("USStock", "AAPL", "2026-11-18T12:42:00-05:00", "16:30", "17:30", 0),
])
def test_1h_stock_windows_handle_recess_and_short_session(
    monkeypatch, market, symbol, at, open_at, close_at, label,
):
    submitted = datetime.fromisoformat(at)
    schedule = equity_hourly_bar_windows(market, submitted - timedelta(days=28), submitted)
    completed = [window for window in schedule if window[1] <= submitted.timestamp()]
    selected = completed[-1]
    rows = [{"time": window[label], "close": 120 if window == selected else 1
             if window[1] > submitted.timestamp() else 100} for window in schedule]
    provider = Mock(return_value=rows)
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", provider)

    result = discipline.check_ema25_at_order_time(market=market, symbol=symbol, direction="long", submitted_at=at)

    provider.assert_called_once_with(market, symbol, "1H", 101, before_time=int(submitted.timestamp()) + 1,
                                     exchange_id=None, market_type=None)
    assert result["status"] == "allowed"
    assert result["bar_opened_at"] == submitted.date().isoformat() + f"T{open_at}:00Z"
    assert result["bar_closed_at"] == submitted.date().isoformat() + f"T{close_at}:00Z"
    assert result["close"] == 120


def test_stock_ambiguous_labels_do_not_fall_back_to_utc_grid(monkeypatch):
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", Mock(return_value=[
        {"time": int(datetime.fromisoformat("2026-11-18T10:30:00+08:00").timestamp()), "close": 100},
    ]))

    result = discipline.check_ema25_at_order_time(
        market="CNStock", symbol="600000", direction="long", submitted_at="2026-11-18T11:45:00+08:00",
    )

    assert (result["status"], result["reason"]) == ("unavailable", "stock_bar_time_unverified")


def test_missing_order_time_does_not_read_market_data(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", provider)

    assert crypto(submitted_at=None)["reason"] == "order_submission_time_unavailable"
    provider.assert_not_called()


def test_inventory_uses_actual_order_time_and_checks_partial_fills_once(monkeypatch, capsys):
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "7")
    monkeypatch.setenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap")
    monkeypatch.setattr("sys.argv", ["inventory", "--evidence-symbol", "BTC/USDT", "--check-ema"])
    monkeypatch.setattr(trade_discipline_inventory, "read_account_position_inventory", Mock(return_value={
        "status": "ok", "exchange_id": "binance", "market_type": "swap",
    }))
    order = {"order_id": "101", "side": "sell", "action": "open",
             "submitted_at": "2026-11-18T12:00:00Z", "filled_at": "2026-11-18T16:01:00Z"}
    monkeypatch.setattr(position_evidence, "read_position_evidence", Mock(return_value={
        "status": "reconciled", "instrument": "BTCUSDT", "events": [
            order, dict(order, action="add"), dict(order, order_id="102", action="close"),
        ],
    }))
    provider = Mock(return_value=bars())
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", provider)

    assert trade_discipline_inventory.main() == 0
    report = json.loads(capsys.readouterr().out)["ema_checks"]
    assert [(item["order_id"], item["status"], item["bar_closed_at"]) for item in report] == [
        ("101", "blocked", "2026-11-18T12:00:00Z"),
    ]
    provider.assert_called_once()


def test_stock_cli_runs_without_inventory_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ema", "--market", "CNStock", "--symbol", "600000",
                                    "--direction", "long", "--submitted-at", "2026-11-18T11:45:00+08:00"])
    monkeypatch.setattr(discipline.DataSourceFactory, "get_kline", Mock(return_value=[]))

    assert trade_discipline_ema.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["timeframe"] == "1H" and result["reason"] == "market_data_unavailable"
