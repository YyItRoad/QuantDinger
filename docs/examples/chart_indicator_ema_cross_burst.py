# QuantDinger 图表指标示例：EMA 金叉后近期大阳线形态。
# 仅生成图表标记，不回测、不下单。

# @param lead_ema_period int 7 EMA领先线周期 range=2:30:1
# @param cross_fast_period int 25 金叉快线EMA周期 range=5:100:1
# @param cross_slow_period int 90 金叉慢线EMA周期 range=20:300:1
# @param comparison_bars int 10 爆发K线对比的前序周期数 range=2:50:1
# @param nearby_bars int 3 金叉后允许出现爆发K线的周期数 range=0:10:1
# @param burst_multiplier float 1.0 爆发K线相对近期最大实体涨幅的倍数 range=1.0:5.0:0.1
# @param min_body_rise_pct float 0.0 阳线实体最小涨幅百分比 range=0.0:20.0:0.1
# @param atr_period int 14 风控线使用的ATR周期 range=2:100:1
# @param initial_atr_mult float 2.5 初始止损ATR倍数 range=0.5:10.0:0.25
# @param structure_buffer_atr float 0.2 结构低点下方的ATR缓冲倍数 range=0.0:3.0:0.1
# @param failure_bars int 3 收盘跌破爆发K线低点的形态失败期限 range=1:20:1
# @param trail_activation_r float 2.0 启动ATR追踪止盈所需盈利R倍数 range=0.5:10.0:0.25
# @param trail_atr_mult float 3.5 ATR追踪止盈倍数 range=0.5:10.0:0.25
# @param trend_exit_bars int 10 趋势退出使用的前序低点周期数 range=2:100:1
# @param stagnation_bars int 8 检查动能是否停滞的等待周期数 range=2:100:1
# @param stagnation_min_r float 0.5 动能停滞检查要求的最小有利波动R倍数 range=0.0:5.0:0.25

my_indicator_name = "EMA金叉爆发风控图"
my_indicator_description = "标记EMA金叉爆发形态，并模拟虚拟入场、初始止损、ATR追踪止盈与退出。"

df = df.copy()

lead_ema_period = max(1, int(params.get("lead_ema_period", 7)))
cross_fast_period = max(1, int(params.get("cross_fast_period", 25)))
cross_slow_period = max(1, int(params.get("cross_slow_period", 90)))
comparison_bars = max(1, int(params.get("comparison_bars", 10)))
nearby_bars = max(0, int(params.get("nearby_bars", 3)))
burst_multiplier = max(1.0, float(params.get("burst_multiplier", 1.0)))
min_body_rise_pct = max(0.0, float(params.get("min_body_rise_pct", 0.0)))
atr_period = max(2, int(params.get("atr_period", 14)))
initial_atr_mult = max(0.5, float(params.get("initial_atr_mult", 2.5)))
structure_buffer_atr = max(0.0, float(params.get("structure_buffer_atr", 0.2)))
failure_bars = max(1, int(params.get("failure_bars", 3)))
trail_activation_r = max(0.5, float(params.get("trail_activation_r", 2.0)))
trail_atr_mult = max(0.5, float(params.get("trail_atr_mult", 3.5)))
trend_exit_bars = max(2, int(params.get("trend_exit_bars", 10)))
stagnation_bars = max(2, int(params.get("stagnation_bars", 8)))
stagnation_min_r = max(0.0, float(params.get("stagnation_min_r", 0.5)))

open_price = df["open"]
high = df["high"]
low = df["low"]
close = df["close"]


def to_plot_list(series):
    return [None if pd.isna(value) else float(value) for value in series]


ema_lead = close.ewm(span=lead_ema_period, adjust=False).mean()
ema_cross_fast = close.ewm(span=cross_fast_period, adjust=False).mean()
ema_cross_slow = close.ewm(span=cross_slow_period, adjust=False).mean()

# 慢线至少积累一个完整周期后，才允许确认金叉。
bar_number = pd.Series(range(len(df)), index=df.index)
ema_ready = bar_number >= cross_slow_period - 1
period_order_valid = lead_ema_period < cross_fast_period < cross_slow_period

golden_cross = (
    (ema_cross_fast > ema_cross_slow)
    & (ema_cross_fast.shift(1) <= ema_cross_slow.shift(1))
    & ema_ready
)
qualified_cross = (
    golden_cross
    & (ema_lead > ema_cross_fast)
    & (ema_lead > ema_cross_slow)
    & period_order_valid
)

# 当前 K 线只向后回看：窗口内出现过合格金叉，即仍处在“附近”。
cross_is_nearby = (
    qualified_cross
    .rolling(window=nearby_bars + 1, min_periods=1)
    .max()
    .fillna(0)
    .astype(bool)
)

safe_open = open_price.where(open_price != 0)
body_rise = close / safe_open - 1.0
previous_max_body_rise = (
    body_rise
    .shift(1)
    .rolling(window=comparison_bars, min_periods=comparison_bars)
    .max()
)
burst_candle = (
    (body_rise > 0)
    & (body_rise >= min_body_rise_pct / 100.0)
    & (body_rise > previous_max_body_rise * burst_multiplier)
)
pattern = (cross_is_nearby & burst_candle).fillna(False).astype(bool)

previous_close = close.shift(1)
true_range = pd.concat(
    [
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ],
    axis=1,
).max(axis=1)
atr = true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()

pattern_marks = [
    float(low.iloc[index] * 0.995) if bool(pattern.iloc[index]) else None
    for index in range(len(df))
]
cross_marks = [
    float(low.iloc[index] * 0.99) if bool(qualified_cross.iloc[index]) else None
    for index in range(len(df))
]

# 信号只能在当前 K 线收盘后确认，虚拟入场从下一根 K 线开盘开始。
virtual_entry = [None] * len(df)
virtual_entry_marks = [None] * len(df)
initial_stop_line = [None] * len(df)
atr_trail_line = [None] * len(df)
initial_stop_marks = [None] * len(df)
atr_trail_marks = [None] * len(df)
pattern_failure_marks = [None] * len(df)
stagnation_marks = [None] * len(df)
trend_exit_marks = [None] * len(df)
active_entry_price = None
active_initial_stop = None
active_risk = None
active_highest_high = None
active_atr_trail = None
active_burst_low = None
active_entry_index = None
for index in range(1, len(df)):
    if active_entry_price is None and bool(pattern.iloc[index - 1]):
        active_entry_price = float(open_price.iloc[index])
        virtual_entry_marks[index] = float(low.iloc[index] * 0.995)
        signal_index = index - 1
        cross_index = signal_index
        while cross_index > 0 and not bool(qualified_cross.iloc[cross_index]):
            cross_index -= 1
        setup_low = float(low.iloc[cross_index : signal_index + 1].min())
        atr_value = float(atr.iloc[index])
        structure_stop = setup_low - structure_buffer_atr * atr_value
        volatility_stop = active_entry_price - initial_atr_mult * atr_value
        active_initial_stop = min(structure_stop, volatility_stop)
        active_risk = active_entry_price - active_initial_stop
        active_highest_high = float(high.iloc[index])
        active_atr_trail = None
        active_burst_low = float(low.iloc[signal_index])
        active_entry_index = index
    if active_entry_price is not None:
        virtual_entry[index] = active_entry_price
        initial_stop_line[index] = active_initial_stop
        atr_trail_line[index] = active_atr_trail
        active_stop = (
            active_atr_trail if active_atr_trail is not None else active_initial_stop
        )
        if float(low.iloc[index]) <= active_stop:
            if active_atr_trail is None:
                initial_stop_marks[index] = float(high.iloc[index] * 1.005)
            else:
                atr_trail_marks[index] = float(high.iloc[index] * 1.005)
            active_entry_price = None
            active_initial_stop = None
            active_risk = None
            active_highest_high = None
            active_atr_trail = None
            active_burst_low = None
            active_entry_index = None
        elif (
            index - active_entry_index <= failure_bars
            and float(close.iloc[index]) < active_burst_low
        ):
            pattern_failure_marks[index] = float(high.iloc[index] * 1.005)
            active_entry_price = None
            active_initial_stop = None
            active_risk = None
            active_highest_high = None
            active_atr_trail = None
            active_burst_low = None
            active_entry_index = None
        elif (
            index - active_entry_index >= stagnation_bars
            and max(active_highest_high, float(high.iloc[index]))
            < active_entry_price + stagnation_min_r * active_risk
            and float(ema_lead.iloc[index]) < float(ema_cross_fast.iloc[index])
        ):
            stagnation_marks[index] = float(high.iloc[index] * 1.005)
            active_entry_price = None
            active_initial_stop = None
            active_risk = None
            active_highest_high = None
            active_atr_trail = None
            active_burst_low = None
            active_entry_index = None
        elif (
            index >= trend_exit_bars
            and (
                float(close.iloc[index])
                < float(low.iloc[index - trend_exit_bars : index].min())
                or (
                    float(ema_cross_fast.iloc[index]) < float(ema_cross_slow.iloc[index])
                    and float(ema_cross_fast.iloc[index - 1])
                    >= float(ema_cross_slow.iloc[index - 1])
                )
            )
        ):
            trend_exit_marks[index] = float(high.iloc[index] * 1.005)
            active_entry_price = None
            active_initial_stop = None
            active_risk = None
            active_highest_high = None
            active_atr_trail = None
            active_burst_low = None
            active_entry_index = None
        else:
            active_highest_high = max(active_highest_high, float(high.iloc[index]))
            activation_price = active_entry_price + trail_activation_r * active_risk
            if active_highest_high >= activation_price:
                trail_candidate = max(
                    active_initial_stop,
                    active_highest_high - trail_atr_mult * float(atr.iloc[index]),
                )
                active_atr_trail = (
                    trail_candidate
                    if active_atr_trail is None
                    else max(active_atr_trail, trail_candidate)
                )

output = {
    "name": my_indicator_name,
    "plots": [
        {
            "name": "EMA7领先线",
            "data": to_plot_list(ema_lead),
            "color": "#f59e0b",
            "type": "line",
            "overlay": True,
        },
        {
            "name": "EMA25金叉快线",
            "data": to_plot_list(ema_cross_fast),
            "color": "#22c55e",
            "type": "line",
            "overlay": True,
        },
        {
            "name": "EMA90金叉慢线",
            "data": to_plot_list(ema_cross_slow),
            "color": "#3b82f6",
            "type": "line",
            "overlay": True,
        },
        {
            "name": "虚拟入场",
            "data": virtual_entry,
            "color": "#06b6d4",
            "type": "line",
            "overlay": True,
        },
        {
            "name": "初始止损",
            "data": initial_stop_line,
            "color": "#ef4444",
            "type": "line",
            "overlay": True,
        },
        {
            "name": "ATR追踪止盈",
            "data": atr_trail_line,
            "color": "#f97316",
            "type": "line",
            "overlay": True,
        },
    ],
    "signals": [
        {
            "type": "buy",
            "text": "爆发K线",
            "color": "#ef4444",
            "data": pattern_marks,
        },
        {
            "type": "buy",
            "text": "合格金叉",
            "color": "#a855f7",
            "data": cross_marks,
        },
        {
            "type": "buy",
            "text": "虚拟入场",
            "color": "#06b6d4",
            "data": virtual_entry_marks,
        },
        {
            "type": "sell",
            "text": "初始止损",
            "color": "#dc2626",
            "data": initial_stop_marks,
        },
        {
            "type": "sell",
            "text": "ATR追踪止盈",
            "color": "#ea580c",
            "data": atr_trail_marks,
        },
        {
            "type": "sell",
            "text": "形态失败",
            "color": "#f97316",
            "data": pattern_failure_marks,
        },
        {
            "type": "sell",
            "text": "动能停滞",
            "color": "#64748b",
            "data": stagnation_marks,
        },
        {
            "type": "sell",
            "text": "趋势退出",
            "color": "#7c3aed",
            "data": trend_exit_marks,
        },
    ],
    "layers": [],
}
