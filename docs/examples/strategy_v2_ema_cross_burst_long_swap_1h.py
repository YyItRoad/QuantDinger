"""EMA金叉爆发永续合约多头策略
扫描静态USDT永续合约池，在EMA合格金叉附近寻找爆发K线，并在下一根K线开多。
使用结构与ATR初始风险、形态失败、动能停滞、趋势退出和ATR追踪管理持仓。
"""

# 品种属于策略清单的一部分。修改后需要重新保存、校验、回测和部署。
CONTRACT_POOL = [
    "Crypto:BTC/USDT@swap",
    "Crypto:ETH/USDT@swap",
    "Crypto:SOL/USDT@swap",
]

PERSIST_RUNTIME_STATE = True

# @param lead_ema_period int 7 EMA领先线周期 range=2:30:1
# @param cross_fast_period int 25 金叉快线EMA周期 range=5:100:1
# @param cross_slow_period int 90 金叉慢线EMA周期 range=20:300:1
# @param comparison_bars int 10 爆发K线对比的前序周期数 range=2:50:1
# @param nearby_bars int 3 金叉后允许出现爆发K线的周期数 range=0:10:1
# @param burst_multiplier float 1.0 爆发K线相对近期最大实体涨幅的倍数 range=1.0:5.0:0.1
# @param min_body_rise_pct float 0.0 阳线实体最小涨幅百分比 range=0.0:20.0:0.1
# @param atr_period int 14 ATR周期 range=2:100:1
# @param initial_atr_mult float 2.5 初始止损ATR倍数 range=0.5:10.0:0.25
# @param structure_buffer_atr float 0.2 结构低点下方的ATR缓冲倍数 range=0.0:3.0:0.1
# @param failure_bars int 3 形态失败检查周期数 range=1:20:1
# @param trail_activation_r float 2.0 ATR追踪启动盈利R倍数 range=0.5:10.0:0.25
# @param trail_atr_mult float 3.5 ATR追踪距离倍数 range=0.5:10.0:0.25
# @param trend_exit_bars int 10 趋势退出使用的前序低点周期数 range=2:100:1
# @param stagnation_bars int 8 动能停滞检查周期数 range=2:100:1
# @param stagnation_min_r float 0.5 动能停滞要求的最小有利波动R倍数 range=0.0:5.0:0.25
# @param risk_per_trade_pct float 0.5 单笔最大风险占账户权益百分比 range=0.1:5.0:0.1
# @param max_positions int 3 最大同时持仓数量 range=1:20:1
# @param max_position_pct float 30.0 单品种最大名义仓位占账户权益百分比 range=1.0:100.0:1.0
# @param daily_loss_limit_pct float 2.0 当日停止新开仓的权益回撤百分比 range=0.5:20.0:0.5


def initialize(context):
    g.symbols = list(CONTRACT_POOL)
    g.states = {}
    g.daily_date = ""
    g.daily_start_equity = 0.0
    context.set_universe(g.symbols)
    context.subscribe(
        frequency="1h",
        fields=["open", "high", "low", "close", "volume"],
    )
    context.set_warmup(220)
    context.allow_leverage(max_leverage=3)
    context.set_metadata(direction_mode="long_only")


def _number(params, name, default, minimum):
    return max(minimum, float(params.get(name, default)))


def _integer(params, name, default, minimum):
    return max(minimum, int(params.get(name, default)))


def _runtime_params(context):
    params = context.params
    return {
        "lead_ema_period": _integer(params, "lead_ema_period", 7, 1),
        "cross_fast_period": _integer(params, "cross_fast_period", 25, 1),
        "cross_slow_period": _integer(params, "cross_slow_period", 90, 2),
        "comparison_bars": _integer(params, "comparison_bars", 10, 1),
        "nearby_bars": _integer(params, "nearby_bars", 3, 0),
        "burst_multiplier": _number(params, "burst_multiplier", 1.0, 1.0),
        "min_body_rise_pct": _number(params, "min_body_rise_pct", 0.0, 0.0),
        "atr_period": _integer(params, "atr_period", 14, 2),
        "initial_atr_mult": _number(params, "initial_atr_mult", 2.5, 0.5),
        "structure_buffer_atr": _number(params, "structure_buffer_atr", 0.2, 0.0),
        "failure_bars": _integer(params, "failure_bars", 3, 1),
        "trail_activation_r": _number(params, "trail_activation_r", 2.0, 0.5),
        "trail_atr_mult": _number(params, "trail_atr_mult", 3.5, 0.5),
        "trend_exit_bars": _integer(params, "trend_exit_bars", 10, 2),
        "stagnation_bars": _integer(params, "stagnation_bars", 8, 2),
        "stagnation_min_r": _number(params, "stagnation_min_r", 0.5, 0.0),
        "risk_per_trade": _number(params, "risk_per_trade_pct", 0.5, 0.0) / 100.0,
        "max_positions": _integer(params, "max_positions", 3, 1),
        "max_position_pct": _number(params, "max_position_pct", 30.0, 1.0) / 100.0,
        "daily_loss_limit": _number(params, "daily_loss_limit_pct", 2.0, 0.0) / 100.0,
    }


def _empty_state():
    return {
        "phase": "flat",
        "entry_ref": "",
        "exit_ref": "",
        "setup_low": 0.0,
        "burst_low": 0.0,
        "signal_atr": 0.0,
        "entry_price": 0.0,
        "initial_stop": 0.0,
        "risk": 0.0,
        "highest_high": 0.0,
        "atr_trail": 0.0,
        "bars_held": 0,
    }


def _state(symbol):
    if symbol not in g.states:
        g.states[symbol] = _empty_state()
    return g.states[symbol]


def _reset_state(symbol):
    g.states[symbol] = _empty_state()


def _client_order_id(context, symbol, action):
    stamp = str(context.current_dt).replace(" ", "").replace(":", "").replace("+", "")
    asset = symbol.split(":", 1)[-1].split("/", 1)[0].lower()
    return ("ema-burst-" + asset + "-" + action + "-" + stamp)[-100:]


def _signal_snapshot(bars, config):
    lead_period = config["lead_ema_period"]
    fast_period = config["cross_fast_period"]
    slow_period = config["cross_slow_period"]
    comparison_bars = config["comparison_bars"]
    nearby_bars = config["nearby_bars"]
    required = max(slow_period + 1, comparison_bars + nearby_bars + 2)
    if len(bars) < required or not (lead_period < fast_period < slow_period):
        return None

    open_price = bars["open"]
    high = bars["high"]
    low = bars["low"]
    close = bars["close"]
    ema_lead = close.ewm(span=lead_period, adjust=False).mean()
    ema_fast = close.ewm(span=fast_period, adjust=False).mean()
    ema_slow = close.ewm(span=slow_period, adjust=False).mean()
    qualified = (
        (ema_fast > ema_slow)
        & (ema_fast.shift(1) <= ema_slow.shift(1))
        & (ema_lead > ema_fast)
        & (ema_lead > ema_slow)
    )

    first_nearby = max(0, len(bars) - nearby_bars - 1)
    cross_index = -1
    for index in range(first_nearby, len(bars)):
        if index >= slow_period - 1 and bool(qualified.iloc[index]):
            cross_index = index
    if cross_index < 0:
        return None

    current_open = float(open_price.iloc[-1])
    current_close = float(close.iloc[-1])
    if current_open <= 0:
        return None
    body_rise = close / open_price.replace(0, float("nan")) - 1.0
    previous = body_rise.iloc[-comparison_bars - 1 : -1]
    if len(previous) < comparison_bars:
        return None
    current_rise = float(body_rise.iloc[-1])
    previous_max = float(previous.max())
    is_burst = (
        current_rise > 0
        and current_rise >= config["min_body_rise_pct"] / 100.0
        and current_rise > previous_max * config["burst_multiplier"]
    )
    if not is_burst:
        return None

    previous_close = close.shift(1)
    true_range = (high - low).to_frame("range")
    true_range["high_gap"] = (high - previous_close).abs()
    true_range["low_gap"] = (low - previous_close).abs()
    atr = true_range.max(axis=1).ewm(
        alpha=1.0 / config["atr_period"],
        adjust=False,
    ).mean()
    atr_value = float(atr.iloc[-1])
    if atr_value <= 0:
        return None
    return {
        "close": current_close,
        "setup_low": float(low.iloc[cross_index:].min()),
        "burst_low": float(low.iloc[-1]),
        "atr": atr_value,
        "ema_lead": float(ema_lead.iloc[-1]),
        "ema_fast": float(ema_fast.iloc[-1]),
        "ema_slow": float(ema_slow.iloc[-1]),
    }


def _atr_and_emas(bars, config):
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    previous_close = close.shift(1)
    true_range = (high - low).to_frame("range")
    true_range["high_gap"] = (high - previous_close).abs()
    true_range["low_gap"] = (low - previous_close).abs()
    atr = true_range.max(axis=1).ewm(
        alpha=1.0 / config["atr_period"],
        adjust=False,
    ).mean()
    ema_lead = close.ewm(span=config["lead_ema_period"], adjust=False).mean()
    ema_fast = close.ewm(span=config["cross_fast_period"], adjust=False).mean()
    ema_slow = close.ewm(span=config["cross_slow_period"], adjust=False).mean()
    return float(atr.iloc[-1]), ema_lead, ema_fast, ema_slow


def _equity(context):
    value = float(context.portfolio.total_value or 0.0)
    if value <= 0:
        value = float(context.portfolio.starting_cash or 0.0)
    return max(0.0, value)


def _update_daily_guard(context, config):
    day = str(context.current_dt.date())
    equity = _equity(context)
    if g.daily_date != day or g.daily_start_equity <= 0:
        g.daily_date = day
        g.daily_start_equity = equity
    if g.daily_start_equity <= 0 or config["daily_loss_limit"] <= 0:
        return False
    return equity <= g.daily_start_equity * (1.0 - config["daily_loss_limit"])


def _open_or_reserved_positions():
    count = 0
    for symbol in g.symbols:
        position = get_position(symbol, position_side="long")
        phase = _state(symbol)["phase"]
        if abs(float(position.amount or 0.0)) > 1e-12 or phase == "pending_entry":
            count += 1
    return count


def _prepare_open_state(state, position, bars, config):
    entry_price = float(position.avg_cost or bars["open"].iloc[-1])
    atr_value = float(state["signal_atr"] or _atr_and_emas(bars, config)[0])
    setup_low = float(state["setup_low"] or bars["low"].iloc[-1])
    initial_stop = min(
        setup_low - config["structure_buffer_atr"] * atr_value,
        entry_price - config["initial_atr_mult"] * atr_value,
    )
    state["phase"] = "open"
    state["entry_price"] = entry_price
    state["initial_stop"] = initial_stop
    state["risk"] = max(1e-12, entry_price - initial_stop)
    state["highest_high"] = float(bars["high"].iloc[-1])
    state["atr_trail"] = 0.0
    state["bars_held"] = 0


def _submit_exit(context, symbol, state, reason):
    state["exit_ref"] = order_target_value(
        symbol,
        0.0,
        position_side="long",
        reason=reason,
        client_order_id=_client_order_id(context, symbol, "exit"),
    )
    state["phase"] = "exit_pending"
    log("退出信号：" + symbol + "，原因=" + reason)


def _manage_open_position(context, symbol, state, position, bars, config, entered_now):
    if not entered_now:
        state["bars_held"] = int(state["bars_held"]) + 1
    atr_value, ema_lead, ema_fast, ema_slow = _atr_and_emas(bars, config)
    current_low = float(bars["low"].iloc[-1])
    current_high = float(bars["high"].iloc[-1])
    current_close = float(bars["close"].iloc[-1])
    active_trail = float(state["atr_trail"] or 0.0)
    active_stop = active_trail if active_trail > 0 else float(state["initial_stop"])

    reason = ""
    if current_low <= active_stop:
        reason = "atr_trailing_exit" if active_trail > 0 else "initial_stop_exit"
    elif int(state["bars_held"]) <= config["failure_bars"] and current_close < float(state["burst_low"]):
        reason = "pattern_failure_exit"
    else:
        trend_break = False
        if len(bars) > config["trend_exit_bars"]:
            prior_low = float(bars["low"].iloc[-config["trend_exit_bars"] - 1 : -1].min())
            trend_break = current_close < prior_low
        dead_cross = bool(
            ema_fast.iloc[-1] < ema_slow.iloc[-1]
            and ema_fast.iloc[-2] >= ema_slow.iloc[-2]
        )
        if trend_break or dead_cross:
            reason = "trend_exit"
        elif (
            int(state["bars_held"]) >= config["stagnation_bars"]
            and max(float(state["highest_high"]), current_high)
            < float(state["entry_price"]) + config["stagnation_min_r"] * float(state["risk"])
            and ema_lead.iloc[-1] < ema_fast.iloc[-1]
        ):
            reason = "stagnation_exit"

    if reason:
        _submit_exit(context, symbol, state, reason)
        return

    state["highest_high"] = max(float(state["highest_high"]), current_high)
    activation_price = float(state["entry_price"]) + config["trail_activation_r"] * float(state["risk"])
    if float(state["highest_high"]) >= activation_price:
        candidate = max(
            float(state["initial_stop"]),
            float(state["highest_high"]) - config["trail_atr_mult"] * atr_value,
        )
        state["atr_trail"] = candidate if active_trail <= 0 else max(active_trail, candidate)


def handle_data(context, data):
    config = _runtime_params(context)
    daily_blocked = _update_daily_guard(context, config)
    slots_used = _open_or_reserved_positions()

    history_count = max(
        config["cross_slow_period"] + config["nearby_bars"] + 5,
        config["comparison_bars"] + config["nearby_bars"] + 5,
        config["trend_exit_bars"] + 5,
        config["atr_period"] + 5,
    )

    for symbol in g.symbols:
        bars = data.history(
            symbol,
            count=history_count,
            fields=["open", "high", "low", "close", "volume"],
        )
        if len(bars) < 3:
            continue
        state = _state(symbol)
        position = get_position(symbol, position_side="long")
        has_position = abs(float(position.amount or 0.0)) > 1e-12
        terminal_failure = ("rejected", "failed", "cancelled", "canceled", "expired")

        if state["phase"] in ("open", "exit_pending") and not has_position:
            exit_reason = consume_last_exit_reason(symbol)
            if exit_reason:
                log("持仓已退出：" + symbol + "，原因=" + str(exit_reason))
            _reset_state(symbol)
            state = _state(symbol)

        entered_now = False
        if state["phase"] == "pending_entry":
            entry_status = str(
                get_order_status(state["entry_ref"]).get("status") or "unknown"
            )
            if has_position and entry_status == "filled":
                _prepare_open_state(state, position, bars, config)
                entered_now = True
                log("持仓确认：" + symbol + "，入场价=" + str(state["entry_price"]))
            elif has_position and entry_status in terminal_failure:
                # 订单状态和持仓快照冲突时优先保护已经存在的策略持仓。
                _prepare_open_state(state, position, bars, config)
                entered_now = True
                log("异常持仓接管：" + symbol + "，订单状态=" + entry_status)
            elif not has_position and entry_status in terminal_failure:
                _reset_state(symbol)
                state = _state(symbol)
            else:
                # queued/submitted/open/partial 必须等待完整成交与持仓同步。
                continue

        if state["phase"] == "exit_pending" and has_position:
            exit_status = str(
                get_order_status(state["exit_ref"]).get("status") or "unknown"
            )
            if exit_status in terminal_failure:
                state["phase"] = "open"
                state["exit_ref"] = ""
                log("退出订单未完成，恢复风险跟踪：" + symbol + "，状态=" + exit_status)
            else:
                # 部分成交或已提交时等待持仓真正归零，禁止重复退出。
                continue

        if has_position and state["phase"] == "flat":
            _prepare_open_state(state, position, bars, config)
            entered_now = True
            log("恢复未记录持仓：" + symbol + "，入场价=" + str(state["entry_price"]))

        if has_position and state["phase"] == "open":
            _manage_open_position(context, symbol, state, position, bars, config, entered_now)
            continue

        if has_position or state["phase"] != "flat" or daily_blocked:
            continue
        if slots_used >= config["max_positions"]:
            continue

        signal = _signal_snapshot(bars, config)
        if signal is None:
            continue
        planned_stop = min(
            signal["setup_low"] - config["structure_buffer_atr"] * signal["atr"],
            signal["close"] - config["initial_atr_mult"] * signal["atr"],
        )
        stop_distance = signal["close"] - planned_stop
        equity = _equity(context)
        if stop_distance <= 0 or equity <= 0:
            continue
        risk_budget = equity * config["risk_per_trade"]
        target_value = risk_budget / stop_distance * signal["close"]
        target_value = min(target_value, equity * config["max_position_pct"])
        if target_value <= 0:
            continue
        emergency_stop_pct = max(0.001, min(0.5, stop_distance / signal["close"]))

        state["phase"] = "pending_entry"
        state["setup_low"] = signal["setup_low"]
        state["burst_low"] = signal["burst_low"]
        state["signal_atr"] = signal["atr"]
        state["entry_ref"] = order_target_value(
            symbol,
            target_value,
            position_side="long",
            reason="ema_burst_long_entry",
            stop_loss_pct=emergency_stop_pct,
            client_order_id=_client_order_id(context, symbol, "entry"),
        )
        slots_used += 1
        log("入场信号：" + symbol + "，计划名义金额=" + str(round(target_value, 4)))
