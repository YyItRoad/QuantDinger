"""通用 1H ATR 多单持仓管理策略。

实际品种、杠杆和仓位由“接管持仓”创建的策略实例注入。策略只管理已经
登记给实例的整笔多头仓位，不开仓、不加仓、不反手。
"""

MANIFEST_INSTRUMENT = "Crypto:BTC/USDT@swap"
PERSIST_RUNTIME_STATE = True

# @param atr_period int 14 ATR周期 range=2:100:1
# @param initial_atr_mult float 2.5 初始止损ATR倍数 range=0.5:10.0:0.25
# @param structure_buffer_atr float 0.2 接管K线低点下方ATR缓冲 range=0.0:3.0:0.1
# @param trail_activation_r float 2.0 移动止损启动盈利R倍数 range=0.5:10.0:0.25
# @param trail_atr_mult float 3.5 移动止损距离ATR倍数 range=0.5:10.0:0.25


def initialize(context):
    g.state = None
    context.set_universe([MANIFEST_INSTRUMENT])
    context.subscribe(
        frequency="1h",
        fields=["open", "high", "low", "close", "volume"],
    )
    context.set_warmup(120)
    context.allow_leverage(max_leverage=125)
    context.set_metadata(
        direction_mode="long_only",
        position_management_generic=True,
    )


def _number(context, name, default, minimum):
    return max(minimum, float(context.params.get(name, default)))


def _integer(context, name, default, minimum):
    return max(minimum, int(context.params.get(name, default)))


def _instrument(context):
    return str(context.params.get("managed_instrument") or "").strip()


def _atr(bars, period):
    high = bars["high"]
    low = bars["low"]
    previous_close = bars["close"].shift(1)
    true_range = (high - low).to_frame("range")
    true_range["high_gap"] = (high - previous_close).abs()
    true_range["low_gap"] = (low - previous_close).abs()
    return float(true_range.max(axis=1).ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def _price(value):
    return str(round(float(value), 8))


def _percent(value):
    return str(round(float(value), 4)) + "%"


def _initialize_position(context, instrument, position, bars, atr_value):
    entry_price = float(position.avg_cost or bars["close"].iloc[-1])
    initial_stop = min(
        entry_price - _number(context, "initial_atr_mult", 2.5, 0.5) * atr_value,
        float(bars["low"].iloc[-1])
        - _number(context, "structure_buffer_atr", 0.2, 0.0) * atr_value,
    )
    g.state = {
        "entry_price": entry_price,
        "initial_stop": initial_stop,
        "risk": max(1e-12, entry_price - initial_stop),
        "highest_high": float(bars["high"].iloc[-1]),
        "trail_stop": 0.0,
        "exit_ref": "",
    }
    log(
        "多单管理初始化"
        + "｜品种=" + instrument
        + "｜数量=" + _price(abs(float(position.amount or 0.0)))
        + "｜开仓均价=" + _price(entry_price)
        + "｜ATR=" + _price(atr_value)
        + "｜初始止损=" + _price(initial_stop)
        + "｜每R价格距离=" + _price(g.state["risk"])
        + "｜杠杆=" + _price(_number(context, "leverage", 1.0, 1.0)) + "x"
    )


def _active_stop():
    trail_stop = float(g.state["trail_stop"] or 0.0)
    return trail_stop if trail_stop > 0 else float(g.state["initial_stop"])


def _log_cycle(context, instrument, position, bars, atr_value, stop_before, stop_after, decision):
    entry = float(g.state["entry_price"])
    close = float(bars["close"].iloc[-1])
    leverage = _number(context, "leverage", 1.0, 1.0)
    activation = entry + _number(context, "trail_activation_r", 2.0, 0.5) * float(g.state["risk"])
    current_pnl = (close - entry) / entry * leverage * 100.0
    stop_pnl = (stop_after - entry) / entry * leverage * 100.0
    log(
        "1H多单持仓检查"
        + "｜K线时间=" + str(context.current_dt)
        + "｜品种=" + instrument
        + "｜数量=" + _price(abs(float(position.amount or 0.0)))
        + "｜开=" + _price(bars["open"].iloc[-1])
        + "｜高=" + _price(bars["high"].iloc[-1])
        + "｜低=" + _price(bars["low"].iloc[-1])
        + "｜收=" + _price(close)
        + "｜开仓均价=" + _price(entry)
        + "｜ATR=" + _price(atr_value)
        + "｜接管后最高价=" + _price(g.state["highest_high"])
        + "｜移动止损启动价=" + _price(activation)
        + "｜本周期止损=" + _price(stop_before)
        + "｜下一周期止损=" + _price(stop_after)
        + "｜移动止损=" + ("已启动" if float(g.state["trail_stop"] or 0.0) > 0 else "未启动")
        + "｜当前杠杆收益率=" + _percent(current_pnl)
        + "｜止损杠杆收益率=" + _percent(stop_pnl)
        + "｜判断=" + decision
    )


def _exit_pending(position):
    if not g.state or not g.state.get("exit_ref"):
        return False
    status = str(get_order_status(g.state["exit_ref"]).get("status") or "unknown")
    if status in ("rejected", "failed", "cancelled", "canceled", "expired"):
        log("多单平仓指令失败｜状态=" + status + "｜下一根K线重新判断")
        g.state["exit_ref"] = ""
        return False
    log("等待多单平仓成交｜状态=" + status + "｜剩余数量=" + _price(abs(float(position.amount or 0.0))))
    return True


def handle_data(context, data):
    instrument = _instrument(context)
    if not instrument:
        log("多单持仓检查停止｜原因=实例没有注入接管品种")
        return
    atr_period = _integer(context, "atr_period", 14, 2)
    bars = data.history(
        instrument,
        count=max(atr_period + 5, 30),
        fields=["open", "high", "low", "close", "volume"],
    )
    if len(bars) < atr_period + 1:
        log("多单持仓检查跳过｜品种=" + instrument + "｜原因=ATR历史K线不足｜现有=" + str(len(bars)))
        return
    # 该策略实例已由 direction_mode=long_only 限定为多头，运行器会把
    # 接管仓位登记为实例的默认仓位，因此这里不能再按双向持仓腿查询。
    position = get_position(instrument)
    if abs(float(position.amount or 0.0)) <= 1e-12:
        if g.state is not None:
            reason = consume_last_exit_reason(instrument)
            log("多单仓位已归零，结束管理｜品种=" + instrument + "｜原因=" + str(reason or "外部平仓或已成交"))
            g.state = None
        return
    atr_value = _atr(bars, atr_period)
    if atr_value <= 0:
        log("多单持仓检查跳过｜品种=" + instrument + "｜原因=ATR无效")
        return
    if g.state is None:
        _initialize_position(context, instrument, position, bars, atr_value)
    if _exit_pending(position):
        return

    stop_before = _active_stop()
    if float(bars["low"].iloc[-1]) <= stop_before:
        _log_cycle(context, instrument, position, bars, atr_value, stop_before, stop_before, "最低价触及止损，提交多单平仓")
        g.state["exit_ref"] = order_target_value(
            instrument,
            0.0,
            position_side="long",
            reason="atr_long_position_stop",
        )
        return

    g.state["highest_high"] = max(float(g.state["highest_high"]), float(bars["high"].iloc[-1]))
    activation = float(g.state["entry_price"]) + _number(context, "trail_activation_r", 2.0, 0.5) * float(g.state["risk"])
    if float(g.state["highest_high"]) >= activation:
        candidate = max(
            float(g.state["initial_stop"]),
            float(g.state["highest_high"]) - _number(context, "trail_atr_mult", 3.5, 0.5) * atr_value,
        )
        previous = float(g.state["trail_stop"] or 0.0)
        g.state["trail_stop"] = candidate if previous <= 0 else max(previous, candidate)
    stop_after = _active_stop()
    decision = "继续持有"
    if stop_after > stop_before:
        decision = "继续持有，多单止损上移"
    _log_cycle(context, instrument, position, bars, atr_value, stop_before, stop_after, decision)
