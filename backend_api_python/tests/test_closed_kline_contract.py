from types import SimpleNamespace

from app.data_sources.base import filter_closed_klines
from app.data_sources.factory import DataSourceFactory


def _row(timestamp, close):
    return {
        "time": timestamp,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
    }


def test_closed_kline_filter_deduplicates_and_rejects_the_open_interval():
    rows = [
        _row(120, 12),
        _row(60, 6),
        _row(0, 1),
        _row(60, 7),
    ]

    result = filter_closed_klines(rows, "1m", reference_time=179)

    assert [row["time"] for row in result] == [0, 60]
    assert result[1]["close"] == 7


def test_closed_kline_filter_accepts_millisecond_timestamps():
    rows = [_row(1_700_000_000_000, 6), _row(1_700_000_060_000, 12)]

    result = filter_closed_klines(rows, "1m", reference_time=1_700_000_119)

    assert [row["time"] for row in result] == [1_700_000_000_000]


def test_factory_applies_closed_only_at_the_strategy_boundary(monkeypatch):
    source = SimpleNamespace(
        get_kline=lambda *_args, **_kwargs: [_row(60, 6), _row(120, 12)]
    )
    monkeypatch.setattr(
        DataSourceFactory,
        "_resolve_source",
        classmethod(lambda cls, *_args, **_kwargs: source),
    )
    monkeypatch.setattr("app.data_sources.base.time.time", lambda: 179)

    all_rows, _ = DataSourceFactory.get_kline_with_diagnostics(
        market="Crypto",
        symbol="BTC/USDT",
        timeframe="1m",
        limit=10,
        closed_only=False,
    )
    closed_rows, _ = DataSourceFactory.get_kline_with_diagnostics(
        market="Crypto",
        symbol="BTC/USDT",
        timeframe="1m",
        limit=10,
        closed_only=True,
    )

    assert [row["time"] for row in all_rows] == [60, 120]
    assert [row["time"] for row in closed_rows] == [60]
