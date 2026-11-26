"""Read-only EMA25 direction check at an evidenced order time."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.data_sources import DataSourceFactory
from app.data_sources.base import filter_closed_klines, timeframe_seconds
from app.services.market.technical_indicators import ema_series_sma_seed
from app.services.market_schedule import CALENDAR_BY_MARKET, equity_hourly_bar_windows
from app.utils.timeutil import to_utc_iso

_PERIOD = 25
_HISTORY = 100


def _closes_by_time(rows, cutoff):
    closes = {}
    for row in rows:
        try:
            stamp = int(row["time"])
            stamp = stamp // 1000 if stamp > 10_000_000_000 else stamp
            if stamp > cutoff:
                continue
            close = float(row["close"])
            close = close if close > 0 and math.isfinite(close) else None
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if stamp in closes and closes[stamp] != close:
            closes[stamp] = None
        else:
            closes[stamp] = close
    return closes


def _stock_labels(windows, closes, cutoff):
    openings = {opened for opened, _ in windows if opened <= cutoff}
    endings = {closed for _, closed in windows if closed <= cutoff}
    open_only = bool(closes.keys() & (openings - endings))
    close_only = bool(closes.keys() & (endings - openings))
    if open_only == close_only:
        return None
    return 0 if open_only else 1


def check_ema25_at_order_time(
    *, market: str, symbol: str, direction: str, submitted_at: str | None,
    exchange_id: str = "", market_type: str = "",
) -> dict:
    """Compare the most recent completed bar's close with its EMA25."""
    market = DataSourceFactory.normalize_market(market)
    # 加密货币使用 4 小时线，股票使用 1 小时线并遵守交易所会话和休市窗口。
    timeframe = "4H" if market == "Crypto" else "1H"
    result = {"status": "unavailable", "market": market, "symbol": symbol, "timeframe": timeframe,
              "period": _PERIOD, "direction": direction, "submitted_at": submitted_at}
    if market != "Crypto" and market not in CALENDAR_BY_MARKET:
        return dict(result, reason="unsupported_market")
    if direction not in {"long", "short"} or not symbol:
        return dict(result, reason="order_identity_unavailable")
    try:
        order_time = datetime.fromisoformat(submitted_at or "")
        if order_time.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        return dict(result, reason="order_submission_time_unavailable")
    cutoff = int(order_time.timestamp())
    if market == "Crypto":
        if not exchange_id.strip() or market_type not in {"swap", "spot"}:
            return dict(result, reason="order_identity_unavailable")
        if exchange_id.lower() in {"binanceusdm", "binancefutures"}:
            exchange_id = "binance"

    try:
        rows = DataSourceFactory.get_kline(
            market, symbol, timeframe, _HISTORY + 1, before_time=cutoff + 1,
            exchange_id=exchange_id or None, market_type=market_type or None,
        )
    except Exception:
        return dict(result, reason="market_data_unavailable")
    if not rows:
        return dict(result, reason="market_data_unavailable")

    # 用下单前已收盘且连续的 K 线计算 EMA，避免读到当时尚未收盘的数据。
    if market == "Crypto":
        duration = timeframe_seconds(timeframe)
        latest_close = cutoff // duration * duration
        windows = [(end - duration, end) for end in range(
            latest_close - (_HISTORY - 1) * duration, latest_close + 1, duration,
        )]
        rows = filter_closed_klines(rows, timeframe, reference_time=cutoff)
        closes = _closes_by_time(rows, cutoff)
        label = 0
    else:
        order_time = order_time.astimezone(timezone.utc)
        try:
            schedule = equity_hourly_bar_windows(market, order_time - timedelta(days=60), order_time)
        except Exception:
            return dict(result, reason="stock_session_unavailable")
        closes = _closes_by_time(rows, cutoff)
        label = _stock_labels(schedule, closes, cutoff)
        if label is None:
            return dict(result, reason="stock_bar_time_unverified")
        windows = [window for window in schedule if window[1] <= cutoff][-_HISTORY:]
    # 最近一根理论上已收盘的 K 线缺失时返回不可判定，避免用上一根旧数据替代。
    if not windows or closes.get(windows[-1][label]) is None:
        return dict(result, reason="latest_closed_bar_missing")
    prices = [closes.get(window[label]) for window in windows]
    start = next(index for index, value in enumerate(prices) if value is not None)
    prices = prices[start:]
    if any(value is None for value in prices):
        return dict(result, reason="closed_bar_gap")
    if len(prices) < _PERIOD:
        return dict(result, reason="insufficient_closed_bars")

    close = prices[-1]
    ema = ema_series_sma_seed(prices, _PERIOD)[-1]
    if not math.isfinite(ema):
        return dict(result, reason="invalid_ema")
    # 收盘价与 EMA25 相等时没有允许的开仓方向。
    if math.isclose(close, ema, rel_tol=1e-12, abs_tol=0):
        relation, allowed = "equal", None
    elif close > ema:
        relation, allowed = "above", "long"
    else:
        relation, allowed = "below", "short"
    opened, closed = windows[-1]
    return dict(result, status="allowed" if direction == allowed else "blocked",
                relation=relation, allowed_direction=allowed, close=close, ema25=ema, bar_count=len(prices),
                bar_opened_at=to_utc_iso(opened), bar_closed_at=to_utc_iso(closed))
