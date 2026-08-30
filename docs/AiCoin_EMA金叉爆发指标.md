# AiCoin EMA 金叉爆发指标

本脚本把 QuantDinger 的 `EMA金叉爆发风控图` 移植为 AiCoin Script v2 指标。

它只负责绘制 EMA、虚拟入场、止损与退出标记，并提供预警条件，不包含 `enterLong`、`enterShort` 等交易指令。虚拟入场采用信号确认后的下一根 K 线开盘价。

## 信号含义

- `合格金叉`：EMA25 从下向上穿越 EMA90，并且当根 K 线的 EMA7 同时高于 EMA25 和 EMA90。
- `爆发K线`：合格金叉出现后的指定窗口内，当前阳线实体涨幅严格大于此前 10 根 K 线的最大实体涨幅，并满足倍率和最小涨幅要求。
- `虚拟入场`：爆发K线出现后的下一根 K 线开盘，只模拟一笔多头持仓。
- `初始止损`：结构低点减 ATR 缓冲，与入场价减 `2.5×ATR` 二者取更低值。
- `ATR追踪止盈`：达到 `2R` 后，从下一根 K 线启用“最高价减 `3.5×ATR`”追踪线。
- 其他退出：3根内形态失败、8根无 `0.5R` 跟随且 EMA7 转弱、跌破10周期低点或 EMA25 死叉 EMA90。

## AiCoin Script v2

```javascript
// EMA 金叉后爆发 K 线指标
// 只画图和提供预警条件，不自动交易
// @version=2

// ===== 可配置参数 =====
leadEmaPeriod = 7
crossFastPeriod = 25
crossSlowPeriod = 90
comparisonBars = 10
nearbyBars = 3
burstMultiplier = 1.0
minBodyRisePct = 0.0
atrPeriod = 14
initialAtrMult = 2.5
structureBufferAtr = 0.2
failureBars = 3
stagnationBars = 8
stagnationMinR = 0.5
trailActivationR = 2.0
trailAtrMult = 3.5
trendExitBars = 10

// ===== EMA =====
emaLead = ema(close, leadEmaPeriod)
emaFast = ema(close, crossFastPeriod)
emaSlow = ema(close, crossSlowPeriod)

// ===== 合格金叉 =====
periodOrderValid = leadEmaPeriod < crossFastPeriod && crossFastPeriod < crossSlowPeriod
emaReady = bar_index >= crossSlowPeriod - 1
goldenCross = crossup(emaFast, emaSlow) && emaReady
qualifiedCross = goldenCross && emaLead > emaFast && emaLead > emaSlow && periodOrderValid

// nearbyBars 支持 0～10；当前 K 线及金叉后的 N 根 K 线都属于“附近”
crossNearby = qualifiedCross || (nearbyBars >= 1 && qualifiedCross[1]) || (nearbyBars >= 2 && qualifiedCross[2]) || (nearbyBars >= 3 && qualifiedCross[3]) || (nearbyBars >= 4 && qualifiedCross[4]) || (nearbyBars >= 5 && qualifiedCross[5]) || (nearbyBars >= 6 && qualifiedCross[6]) || (nearbyBars >= 7 && qualifiedCross[7]) || (nearbyBars >= 8 && qualifiedCross[8]) || (nearbyBars >= 9 && qualifiedCross[9]) || (nearbyBars >= 10 && qualifiedCross[10])

// ===== 爆发阳线 =====
bodyRisePct = open != 0 ? ((close - open) / open) * 100 : 0.0
previousMaxBodyRisePct = highest(bodyRisePct[1], comparisonBars)
comparisonReady = bar_index >= comparisonBars
burstCandle = comparisonReady && bodyRisePct > 0 && bodyRisePct >= minBodyRisePct && bodyRisePct > previousMaxBodyRisePct * burstMultiplier
emaBurst = crossNearby && burstCandle

// ===== ATR 与趋势退出条件 =====
atrValue = atr(close, atrPeriod, 1, 'SMMA')
channelLow = lowest(low[1], trendExitBars)
deadCross = crossdown(emaFast, emaSlow)

// ===== 虚拟持仓状态 =====
var setupActive = false
var setupLow = nan
var pendingEntry = false
var pendingSetupLow = nan
var pendingBurstLow = nan
var inTrade = false
var entryPrice = nan
var initialStop = nan
var riskR = nan
var highestHigh = nan
var trailActive = false
var trailPrice = nan
var barsHeld = 0

entryNow = false
initialStopExit = false
atrTrailExit = false
patternFailureExit = false
stagnationExit = false
trendExit = false

// 爆发K线在收盘后确认，所以延迟到下一根 K 线开盘虚拟入场
if (!inTrade && pendingEntry && !na(atrValue)) {
    entryPrice := open
    structureStop = pendingSetupLow - structureBufferAtr * atrValue
    volatilityStop = entryPrice - initialAtrMult * atrValue
    initialStop := min(structureStop, volatilityStop)
    riskR := entryPrice - initialStop
    highestHigh := high
    trailActive := false
    trailPrice := nan
    barsHeld := 0
    inTrade := true
    pendingEntry := false
    entryNow := true
}

// 保存本根 K 线实际使用的止损；本根达到 2R 后，追踪线从下一根开始生效
lineVisible = inTrade
lineEntryPrice = entryPrice
lineInitialStop = initialStop
trailWasActive = trailActive
trailForBar = trailPrice
activeStop = trailWasActive ? trailForBar : initialStop

if (inTrade) {
    if (low <= activeStop) {
        initialStopExit := !trailWasActive
        atrTrailExit := trailWasActive
        inTrade := false
    } else if (barsHeld <= failureBars && close < pendingBurstLow) {
        patternFailureExit := true
        inTrade := false
    } else if (barsHeld >= stagnationBars && max(highestHigh, high) < entryPrice + stagnationMinR * riskR && emaLead < emaFast) {
        stagnationExit := true
        inTrade := false
    } else if (close < channelLow || deadCross) {
        trendExit := true
        inTrade := false
    } else {
        highestHigh := max(highestHigh, high)
        if (highestHigh >= entryPrice + trailActivationR * riskR) {
            trailCandidate = max(initialStop, highestHigh - trailAtrMult * atrValue)
            trailPrice := trailActive ? max(trailPrice, trailCandidate) : trailCandidate
            trailActive := true
        }
        barsHeld := barsHeld + 1
    }
}

// 从合格金叉开始记录结构最低价，爆发K线出现后为下一根 K 线准备入场
if (qualifiedCross) {
    setupActive := true
    setupLow := low
}
if (setupActive) {
    setupLow := min(setupLow, low)
}
if (emaBurst) {
    if (!inTrade && !pendingEntry) {
        pendingSetupLow := setupLow
        pendingBurstLow := low
        pendingEntry := true
    }
    setupActive := false
}

// ===== 主图绘制 =====
plot(emaLead, title='EMA7领先线', color='#f59e0b', lineWidth=1, lineDash=[0])
plot(emaFast, title='EMA25金叉快线', color='#22c55e', lineWidth=1, lineDash=[0])
plot(emaSlow, title='EMA90金叉慢线', color='#3b82f6', lineWidth=1, lineDash=[0])
plot(lineVisible ? lineEntryPrice : nan, title='虚拟入场', color='#06b6d4', lineWidth=1, lineDash=[0])
plot(lineVisible ? lineInitialStop : nan, title='初始止损', color='#ef4444', lineWidth=1, lineDash=[0])
plot(lineVisible && trailWasActive ? trailForBar : nan, title='ATR追踪止盈', color='#f97316', lineWidth=2, lineDash=[0])

plotText(qualifiedCross, title='合格金叉', text='合格金叉', refSeries=low, bgColor='#a855f7', color='white', fontSize=12, placement='bottom', display=true)
plotText(emaBurst, title='爆发K线', text='爆发K线', refSeries=low, bgColor='#ef4444', color='white', fontSize=12, placement='bottom', display=true)
plotText(entryNow, title='虚拟入场', text='虚拟入场', refSeries=low, bgColor='#06b6d4', color='white', fontSize=12, placement='bottom', display=true)
plotText(initialStopExit, title='初始止损', text='初始止损', refSeries=high, bgColor='#dc2626', color='white', fontSize=12, placement='top', display=true)
plotText(atrTrailExit, title='ATR追踪止盈', text='ATR追踪止盈', refSeries=high, bgColor='#ea580c', color='white', fontSize=12, placement='top', display=true)
plotText(patternFailureExit, title='形态失败', text='形态失败', refSeries=high, bgColor='#f97316', color='white', fontSize=12, placement='top', display=true)
plotText(stagnationExit, title='动能停滞', text='动能停滞', refSeries=high, bgColor='#64748b', color='white', fontSize=12, placement='top', display=true)
plotText(trendExit, title='趋势退出', text='趋势退出', refSeries=high, bgColor='#7c3aed', color='white', fontSize=12, placement='top', display=true)

// 这里只注册预警条件，不会自动创建预警，也不会下单
alertcondition(qualifiedCross, title='合格金叉', direction='buy')
alertcondition(emaBurst, title='爆发K线', direction='buy')
alertcondition(entryNow, title='虚拟入场', direction='buy')
alertcondition(initialStopExit, title='初始止损', direction='sell')
alertcondition(atrTrailExit, title='ATR追踪止盈', direction='sell')
alertcondition(patternFailureExit, title='形态失败', direction='sell')
alertcondition(stagnationExit, title='动能停滞', direction='sell')
alertcondition(trendExit, title='趋势退出', direction='sell')
```

## 默认参数说明

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `leadEmaPeriod` | 7 | 领先 EMA |
| `crossFastPeriod` | 25 | 金叉快线 |
| `crossSlowPeriod` | 90 | 金叉慢线 |
| `comparisonBars` | 10 | 与此前多少根 K 线比较实体涨幅 |
| `nearbyBars` | 3 | 金叉当根及之后多少根 K 线内信号有效 |
| `burstMultiplier` | 1.0 | 当前实体涨幅必须严格大于此前最大值的倍数 |
| `minBodyRisePct` | 0.0 | 当前阳线实体的最低涨幅门槛 |
| `atrPeriod` | 14 | ATR周期 |
| `initialAtrMult` | 2.5 | 初始波动止损的ATR倍数 |
| `structureBufferAtr` | 0.2 | 结构低点下方的ATR缓冲 |
| `failureBars` | 3 | 形态失败观察窗口 |
| `stagnationBars` | 8 | 停滞检查等待周期 |
| `stagnationMinR` | 0.5 | 停滞前至少需要达到的最大有利波动 |
| `trailActivationR` | 2.0 | 启动追踪止盈需要达到的R倍数 |
| `trailAtrMult` | 3.5 | ATR追踪距离 |
| `trendExitBars` | 10 | 趋势退出低点通道周期 |

如果希望“明显涨幅”的条件更严格，可以先尝试：

- `burstMultiplier = 1.2`
- `minBodyRisePct = 0.8`

## 在 AiCoin 中使用

1. 在“自定义指标/回测/实盘”中点击“新建”。
2. 选择编程创建，并将脚本粘贴到编辑器。
3. 选择“主图展示”，保存名称可用 `EMA Cross Burst`。
4. 点击“显示到K线”，检查三条 EMA、虚拟入场线、初始止损线、ATR追踪线和退出文字。
5. 如需通知，再点击“开启预警”，选择对应的入场或退出条件。预警需要单独创建；脚本本身不会自动开启。

AiCoin 版采用顶部常量配置，没有使用不同客户端版本兼容性不一致的 `input()`。首次粘贴后应先点击“显示到K线”完成编译；该脚本尚未代表真实成交，也不会统计收益。

## 官方语法依据

- [AiCoin 用户手册：快速入门](https://www.aicoin.com/article/365084.html)
- [AiCoin 指标函数](https://www.aicoin.com/article/356118.html)
- [AiCoin 计算函数](https://www.aicoin.com/article/356119.html)
- [AiCoin 画图、预警与条件表达式](https://www.aicoin.com/article/356120.html)
- [AiCoin version2 新特性](https://www.aicoin.com/article/365085.html)
