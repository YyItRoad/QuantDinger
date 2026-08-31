# QuantDinger 图表指标：EMA 金叉爆发单笔交易复盘器。
# 仅使用真实 K 线模拟一笔机会并绘图，不代表交易所订单或真实成交。

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
# @param trade_offset int 0 显示倒数第几笔模拟交易，0为最近一笔 range=0:20:1
# @param manual_entry_time str auto 手动入场K线ISO时间，auto使用形态信号
# @param manual_entry_price float 0.0 手动入场价，0使用入场K线开盘价
# @param manual_structure_bars int 4 手动模式结构低点回看K线数 range=1:50:1

my_indicator_name = "EMA金叉爆发单笔复盘"
my_indicator_description = "选择最近一笔或指定入场K线，持续绘制入场、初始止损、2R追踪启动和ATR移动止盈，并标记模拟退出原因。"

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
trade_offset = max(0, int(params.get("trade_offset", 0)))
manual_entry_time = str(params.get("manual_entry_time", "auto") or "auto").strip()
manual_entry_price = max(0.0, float(params.get("manual_entry_price", 0.0)))
manual_structure_bars = max(1, int(params.get("manual_structure_bars", 4)))

open_price = df["open"]
high = df["high"]
low = df["low"]
close = df["close"]


def to_plot_list(series):
    return [None if pd.isna(value) else float(value) for value in series]


def empty_line():
    return [None] * len(df)


ema_lead = close.ewm(span=lead_ema_period, adjust=False).mean()
ema_cross_fast = close.ewm(span=cross_fast_period, adjust=False).mean()
ema_cross_slow = close.ewm(span=cross_slow_period, adjust=False).mean()

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
    body_rise.shift(1)
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
    [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
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


def dataframe_times():
    source = df["time"] if "time" in df.columns else df.index
    try:
        return pd.to_datetime(source, utc=True, errors="coerce")
    except Exception:
        return None


def resolve_manual_entry_index():
    if manual_entry_time.lower() in ("", "0", "auto", "none", "off"):
        return None
    try:
        target = pd.to_datetime(manual_entry_time, utc=True, errors="coerce")
    except Exception:
        return None
    if pd.isna(target):
        return None
    timestamps = dataframe_times()
    if timestamps is None:
        return None
    for index, value in enumerate(timestamps):
        if not pd.isna(value) and value >= target:
            return index
    return None


def latest_cross_index(signal_index):
    first = max(0, signal_index - nearby_bars)
    for index in range(signal_index, first - 1, -1):
        if bool(qualified_cross.iloc[index]):
            return index
    return signal_index


def new_trade(entry_index, signal_index=None, forced=False):
    if entry_index < 0 or entry_index >= len(df) or pd.isna(atr.iloc[entry_index]):
        return None
    entry = manual_entry_price if forced and manual_entry_price > 0 else float(open_price.iloc[entry_index])
    if entry <= 0:
        return None
    if forced:
        structure_start = max(0, entry_index - manual_structure_bars)
        setup_low = float(low.iloc[structure_start : entry_index + 1].min())
        burst_low = float(low.iloc[entry_index])
    else:
        cross_index = latest_cross_index(signal_index)
        setup_low = float(low.iloc[cross_index : signal_index + 1].min())
        burst_low = float(low.iloc[signal_index])
    atr_value = float(atr.iloc[entry_index])
    initial_stop = min(
        setup_low - structure_buffer_atr * atr_value,
        entry - initial_atr_mult * atr_value,
    )
    risk = entry - initial_stop
    if risk <= 0:
        return None
    return {
        "entry_index": entry_index,
        "signal_index": signal_index,
        "manual": forced,
        "entry_price": entry,
        "initial_stop": initial_stop,
        "risk": risk,
        "activation_price": entry + trail_activation_r * risk,
        "burst_low": burst_low,
        "highest_high": float(high.iloc[entry_index]),
        "trail": None,
        "bars_held": 0,
        "exit_index": None,
        "exit_price": None,
        "exit_reason": "",
        "entry_line": {},
        "initial_stop_line": {},
        "activation_line": {},
        "active_stop_line": {},
        "trail_line": {},
    }


def finish_trade(trade, index, price, reason):
    trade["exit_index"] = index
    trade["exit_price"] = float(price)
    trade["exit_reason"] = reason


manual_index = resolve_manual_entry_index()
manual_mode = manual_index is not None
trades = []
active = None

for index in range(len(df)):
    if active is None:
        if manual_mode:
            if not trades and index == manual_index:
                active = new_trade(index, forced=True)
        elif index > 0 and bool(pattern.iloc[index - 1]):
            active = new_trade(index, signal_index=index - 1, forced=False)
        if active is None:
            continue

    active_stop = active["trail"] if active["trail"] is not None else active["initial_stop"]
    active["entry_line"][index] = active["entry_price"]
    active["initial_stop_line"][index] = active["initial_stop"]
    active["activation_line"][index] = active["activation_price"]
    active["active_stop_line"][index] = active_stop
    if active["trail"] is not None:
        active["trail_line"][index] = active["trail"]

    if float(low.iloc[index]) <= active_stop:
        reason = "ATR移动止盈触发" if active["trail"] is not None else "初始止损触发"
        finish_trade(active, index, active_stop, reason)
    elif active["bars_held"] <= failure_bars and float(close.iloc[index]) < active["burst_low"]:
        finish_trade(active, index, float(close.iloc[index]), "形态失败退出信号")
    else:
        trend_break = False
        if index >= trend_exit_bars:
            prior_low = float(low.iloc[index - trend_exit_bars : index].min())
            trend_break = float(close.iloc[index]) < prior_low
        dead_cross = bool(
            index > 0
            and float(ema_cross_fast.iloc[index]) < float(ema_cross_slow.iloc[index])
            and float(ema_cross_fast.iloc[index - 1]) >= float(ema_cross_slow.iloc[index - 1])
        )
        if trend_break or dead_cross:
            finish_trade(active, index, float(close.iloc[index]), "趋势退出信号")
        elif (
            active["bars_held"] >= stagnation_bars
            and max(active["highest_high"], float(high.iloc[index]))
            < active["entry_price"] + stagnation_min_r * active["risk"]
            and float(ema_lead.iloc[index]) < float(ema_cross_fast.iloc[index])
        ):
            finish_trade(active, index, float(close.iloc[index]), "动能停滞退出信号")

    if active["exit_index"] is not None:
        trades.append(active)
        active = None
        if manual_mode:
            break
        continue

    active["highest_high"] = max(active["highest_high"], float(high.iloc[index]))
    if active["highest_high"] >= active["activation_price"]:
        trail_candidate = max(
            active["initial_stop"],
            active["highest_high"] - trail_atr_mult * float(atr.iloc[index]),
        )
        active["trail"] = (
            trail_candidate if active["trail"] is None else max(active["trail"], trail_candidate)
        )
    active["bars_held"] += 1

if active is not None:
    trades.append(active)

selected = trades[-1 - trade_offset] if len(trades) > trade_offset else None
entry_line = empty_line()
initial_stop_line = empty_line()
activation_line = empty_line()
active_stop_line = empty_line()
atr_trail_line = empty_line()
entry_marks = empty_line()
stop_exit_marks = empty_line()
trail_exit_marks = empty_line()
rule_exit_marks = empty_line()
exit_text = [None] * len(df)

if selected is not None:
    for index, value in selected["entry_line"].items():
        entry_line[index] = value
    for index, value in selected["initial_stop_line"].items():
        initial_stop_line[index] = value
    for index, value in selected["activation_line"].items():
        activation_line[index] = value
    for index, value in selected["active_stop_line"].items():
        active_stop_line[index] = value
    for index, value in selected["trail_line"].items():
        atr_trail_line[index] = value
    entry_index = selected["entry_index"]
    entry_marks[entry_index] = float(low.iloc[entry_index] * 0.995)
    exit_index = selected["exit_index"]
    if exit_index is not None:
        reason = selected["exit_reason"]
        exit_text[exit_index] = reason
        mark = float(high.iloc[exit_index] * 1.005)
        if reason == "初始止损触发":
            stop_exit_marks[exit_index] = mark
        elif reason == "ATR移动止盈触发":
            trail_exit_marks[exit_index] = mark
        else:
            rule_exit_marks[exit_index] = mark

status = "没有找到可模拟的入场机会"
entry_summary = None
stop_summary = None
activation_summary = None
exit_summary = None
return_summary = None
if selected is not None:
    status = "持仓模拟中" if selected["exit_index"] is None else "模拟已退出"
    entry_summary = round(float(selected["entry_price"]), 10)
    stop_summary = round(float(selected["initial_stop"]), 10)
    activation_summary = round(float(selected["activation_price"]), 10)
    if selected["exit_price"] is not None:
        exit_summary = round(float(selected["exit_price"]), 10)
        return_summary = round(
            (float(selected["exit_price"]) - float(selected["entry_price"]))
            / float(selected["entry_price"])
            * 100.0,
            4,
        )

output = {
    "name": my_indicator_name,
    "plots": [
        {"name": "EMA7领先线", "data": to_plot_list(ema_lead), "color": "#f59e0b", "type": "line", "overlay": True},
        {"name": "EMA25金叉快线", "data": to_plot_list(ema_cross_fast), "color": "#22c55e", "type": "line", "overlay": True},
        {"name": "EMA90金叉慢线", "data": to_plot_list(ema_cross_slow), "color": "#3b82f6", "type": "line", "overlay": True},
        {"name": "模拟入场价", "data": entry_line, "color": "#06b6d4", "type": "line", "lineWidth": 2, "overlay": True},
        {"name": "初始止损价", "data": initial_stop_line, "color": "#ef4444", "type": "line", "overlay": True},
        {"name": "2R追踪启动价（不是固定止盈）", "data": activation_line, "color": "#8b5cf6", "type": "line", "lineStyle": "dashed", "overlay": True},
        {"name": "当前有效止损", "data": active_stop_line, "color": "#f43f5e", "type": "line", "lineWidth": 2, "overlay": True},
        {"name": "ATR移动止盈线", "data": atr_trail_line, "color": "#f97316", "type": "line", "lineWidth": 2, "overlay": True},
    ],
    "signals": [
        {"type": "buy", "text": "合格金叉", "color": "#a855f7", "data": cross_marks, "renderMode": "events"},
        {"type": "buy", "text": "爆发K线", "color": "#ef4444", "data": pattern_marks, "renderMode": "events"},
        {"type": "buy", "text": "模拟入场", "color": "#06b6d4", "data": entry_marks, "renderMode": "events"},
        {"type": "sell", "text": "初始止损触发", "color": "#dc2626", "data": stop_exit_marks, "renderMode": "events"},
        {"type": "sell", "text": "ATR移动止盈触发", "color": "#ea580c", "data": trail_exit_marks, "renderMode": "events"},
        {"type": "sell", "text": "规则退出信号", "textData": exit_text, "color": "#7c3aed", "data": rule_exit_marks, "renderMode": "events"},
    ],
    "layers": [],
    "calculatedVars": {
        "说明": "该结果是K线模拟，不是交易所真实订单状态或成交价",
        "模式": "手动指定入场" if manual_mode else "自动形态入场",
        "状态": status,
        "可选模拟交易数": len(trades),
        "当前选择": trade_offset,
        "入场价": entry_summary,
        "初始止损价": stop_summary,
        "2R追踪启动价": activation_summary,
        "退出原因": selected["exit_reason"] if selected is not None else "",
        "退出参考价": exit_summary,
        "模拟收益率百分比": return_summary,
    },
}
