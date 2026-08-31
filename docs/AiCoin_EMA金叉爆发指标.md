# AiCoin EMA 金叉爆发指标

本脚本把 QuantDinger 的 `EMA金叉爆发单笔复盘` 移植为 AiCoin Script v2 指标。

它只负责绘制 EMA、模拟入场、止损与退出标记，并提供预警条件，不包含 `enterLong`、`enterShort` 等交易指令。自动模式采用信号确认后的下一根 K 线开盘价；手动模式可以用 `bar_index` 指定一根 K 线和入场价，单独观察这笔机会后续如何触发退出。

## 信号含义

- `合格金叉`：EMA25 从下向上穿越 EMA90，并且当根 K 线的 EMA7 同时高于 EMA25 和 EMA90。
- `爆发K线`：合格金叉出现后的指定窗口内，当前阳线实体涨幅严格大于此前 10 根 K 线的最大实体涨幅，并满足倍率和最小涨幅要求。
- `模拟入场`：爆发K线出现后的下一根 K 线开盘，只模拟一笔多头持仓。
- `初始止损`：结构低点减 ATR 缓冲，与入场价减 `2.5×ATR` 二者取更低值。
- `ATR追踪止盈`：达到 `2R` 后，从下一根 K 线启用“最高价减 `3.5×ATR`”追踪线。
- 其他退出：3根内形态失败、8根无 `0.5R` 跟随且 EMA7 转弱、跌破10周期低点或 EMA25 死叉 EMA90。

指标中的“退出”是基于 K 线的模拟触发，不等于交易所已经成交。初始止损和 ATR 追踪线按触发价标记；形态失败、动能停滞和趋势退出按收盘确认，只代表策略应在该根 K 线结束后提交退出订单。

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
entryMode = 0
manualEntryBarIndex = -1
manualEntryPrice = 0.0
manualStructureBars = 4
singleTradeOnly = false

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
var trailActivationPrice = nan
var tradeFinished = false

entryNow = false
initialStopExit = false
atrTrailExit = false
patternFailureExit = false
stagnationExit = false
trendExit = false

// entryMode=0 使用自动形态；entryMode=1 使用 manualEntryBarIndex 指定K线。
manualEntryNow = entryMode == 1 && bar_index == manualEntryBarIndex
autoEntryNow = entryMode == 0 && pendingEntry
if (!inTrade && !tradeFinished && (manualEntryNow || autoEntryNow) && !na(atrValue)) {
    entryPrice := manualEntryNow && manualEntryPrice > 0 ? manualEntryPrice : open
    manualSetupLow = lowest(low, manualStructureBars)
    effectiveSetupLow = manualEntryNow ? manualSetupLow : pendingSetupLow
    pendingBurstLow := manualEntryNow ? low : pendingBurstLow
    structureStop = effectiveSetupLow - structureBufferAtr * atrValue
    volatilityStop = entryPrice - initialAtrMult * atrValue
    initialStop := min(structureStop, volatilityStop)
    riskR := entryPrice - initialStop
    trailActivationPrice := entryPrice + trailActivationR * riskR
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
        tradeFinished := singleTradeOnly
    } else if (barsHeld <= failureBars && close < pendingBurstLow) {
        patternFailureExit := true
        inTrade := false
        tradeFinished := singleTradeOnly
    } else if (barsHeld >= stagnationBars && max(highestHigh, high) < entryPrice + stagnationMinR * riskR && emaLead < emaFast) {
        stagnationExit := true
        inTrade := false
        tradeFinished := singleTradeOnly
    } else if (close < channelLow || deadCross) {
        trendExit := true
        inTrade := false
        tradeFinished := singleTradeOnly
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
    if (entryMode == 0 && !inTrade && !pendingEntry && !tradeFinished) {
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
plot(lineVisible ? lineEntryPrice : nan, title='模拟入场', color='#06b6d4', lineWidth=1, lineDash=[0])
plot(lineVisible ? lineInitialStop : nan, title='初始止损', color='#ef4444', lineWidth=1, lineDash=[0])
plot(lineVisible ? trailActivationPrice : nan, title='2R追踪启动价（非固定止盈）', color='#8b5cf6', lineWidth=1, lineDash=[4, 4])
plot(lineVisible ? activeStop : nan, title='当前有效止损', color='#f43f5e', lineWidth=2, lineDash=[0])
plot(lineVisible && trailWasActive ? trailForBar : nan, title='ATR追踪止盈', color='#f97316', lineWidth=2, lineDash=[0])

plotText(qualifiedCross, title='合格金叉', text='合格金叉', refSeries=low, bgColor='#a855f7', color='white', fontSize=12, placement='bottom', display=true)
plotText(emaBurst, title='爆发K线', text='爆发K线', refSeries=low, bgColor='#ef4444', color='white', fontSize=12, placement='bottom', display=true)
plotText(entryNow, title='模拟入场', text='模拟入场', refSeries=low, bgColor='#06b6d4', color='white', fontSize=12, placement='bottom', display=true)
plotText(initialStopExit, title='初始止损', text='初始止损', refSeries=high, bgColor='#dc2626', color='white', fontSize=12, placement='top', display=true)
plotText(atrTrailExit, title='ATR追踪止盈', text='ATR追踪止盈', refSeries=high, bgColor='#ea580c', color='white', fontSize=12, placement='top', display=true)
plotText(patternFailureExit, title='形态失败', text='形态失败', refSeries=high, bgColor='#f97316', color='white', fontSize=12, placement='top', display=true)
plotText(stagnationExit, title='动能停滞', text='动能停滞', refSeries=high, bgColor='#64748b', color='white', fontSize=12, placement='top', display=true)
plotText(trendExit, title='趋势退出', text='趋势退出', refSeries=high, bgColor='#7c3aed', color='white', fontSize=12, placement='top', display=true)

// 这里只注册预警条件，不会自动创建预警，也不会下单
alertcondition(qualifiedCross, title='合格金叉', direction='buy')
alertcondition(emaBurst, title='爆发K线', direction='buy')
alertcondition(entryNow, title='模拟入场', direction='buy')
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
| `entryMode` | 0 | `0` 自动形态入场；`1` 手动指定K线入场 |
| `manualEntryBarIndex` | -1 | 手动模式的入场K线 `bar_index` |
| `manualEntryPrice` | 0.0 | 手动入场价；`0` 使用指定K线开盘价 |
| `manualStructureBars` | 4 | 手动模式计算结构低点的回看周期 |
| `singleTradeOnly` | false | `true` 表示首次模拟退出后不再寻找下一笔 |

如果希望“明显涨幅”的条件更严格，可以先尝试：

- `burstMultiplier = 1.2`
- `minBodyRisePct = 0.8`

## 在 AiCoin 中使用

1. 在“自定义指标/回测/实盘”中点击“新建”。
2. 选择编程创建，并将脚本粘贴到编辑器。
3. 选择“主图展示”，保存名称可用 `EMA Cross Burst`。
4. 点击“显示到K线”，检查三条 EMA、模拟入场线、初始止损线、2R追踪启动线、当前有效止损、ATR追踪线和退出文字。
5. 如需通知，再点击“开启预警”，选择对应的入场或退出条件。预警需要单独创建；脚本本身不会自动开启。

### 自动观察

保持：

```javascript
entryMode = 0
singleTradeOnly = false
```

指标会按形态信号依次模拟。出现爆发 K 线后，下一根 K 线开盘进入模拟持仓，并持续更新止损线。

### 手动复盘一笔机会

把目标 K 线的 `bar_index` 填入：

```javascript
entryMode = 1
manualEntryBarIndex = 1234
manualEntryPrice = 0.0
```

`manualEntryPrice=0.0` 时采用该根 K 线开盘价；如果要模拟自己的实际开仓价，可以改成真实价格。手动模式只会在指定 K 线上创建一笔模拟持仓。

AiCoin 版采用顶部常量配置，没有使用不同客户端版本兼容性不一致的 `input()`。首次粘贴后应先点击“显示到K线”完成编译；该脚本尚未代表真实成交，也不会统计收益。

## 官方语法依据

- [AiCoin 用户手册：快速入门](https://www.aicoin.com/article/365084.html)
- [AiCoin 指标函数](https://www.aicoin.com/article/356118.html)
- [AiCoin 计算函数](https://www.aicoin.com/article/356119.html)
- [AiCoin 画图、预警与条件表达式](https://www.aicoin.com/article/356120.html)
- [AiCoin version2 新特性](https://www.aicoin.com/article/365085.html)
