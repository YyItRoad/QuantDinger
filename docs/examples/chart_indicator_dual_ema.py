# QuantDinger chart-only indicator example.
# Paste into Indicator IDE. This file does not backtest or trade.

# @param fast_len int 12 Fast EMA period
# @param slow_len int 26 Slow EMA period
# @param confirm_next_bar bool false Show markers one bar after confirmation
# @param cloud_opacity float 0.10 Trend cloud opacity range=0.03:0.24:0.01

my_indicator_name = "Dual EMA Viewer"
my_indicator_description = "Chart-only EMA crossover indicator with visual event markers."

df = df.copy()

fast_len = int(params.get("fast_len", 12))
slow_len = int(params.get("slow_len", 26))
confirm_next_bar = bool(params.get("confirm_next_bar", False))
cloud_opacity = min(0.24, max(0.03, float(params.get("cloud_opacity", 0.10))))

close = df["close"]
high = df["high"]
low = df["low"]

def edge(condition):
    s = condition.fillna(False).astype(bool)
    previous = s.shift(1, fill_value=False).astype(bool)
    return s & ~previous

def to_plot_list(series):
    return [None if pd.isna(v) else float(v) for v in series]

ema_fast = close.ewm(span=fast_len, adjust=False).mean()
ema_slow = close.ewm(span=slow_len, adjust=False).mean()

trend_cloud = []
for index in range(len(df)):
    fast_value = ema_fast.iloc[index]
    slow_value = ema_slow.iloc[index]
    if pd.isna(fast_value) or pd.isna(slow_value):
        trend_cloud.append(None)
        continue
    trend_cloud.append(
        {
            "upper": float(max(fast_value, slow_value)),
            "lower": float(min(fast_value, slow_value)),
            "color": "#22c55e" if fast_value >= slow_value else "#f43f5e",
        }
    )

golden = edge(ema_fast > ema_slow)
death = edge(ema_fast < ema_slow)

if confirm_next_bar:
    golden = golden.shift(1, fill_value=False).astype(bool)
    death = death.shift(1, fill_value=False).astype(bool)

buy_marks = [
    float(low.iloc[i] * 0.995) if bool(golden.iloc[i]) else None
    for i in range(len(df))
]
sell_marks = [
    float(high.iloc[i] * 1.005) if bool(death.iloc[i]) else None
    for i in range(len(df))
]

output = {
    "name": my_indicator_name,
    "plots": [
        {
            "name": "EMA Cloud",
            "title": False,
            "data": trend_cloud,
            "color": "#22c55e",
            "type": "cloud",
            "overlay": True,
            "opacity": cloud_opacity,
        },
        {
            "name": "EMA Fast",
            "data": to_plot_list(ema_fast),
            "color": "#22c55e",
            "type": "line",
            "overlay": True,
            "lineWidth": 2,
        },
        {
            "name": "EMA Slow",
            "data": to_plot_list(ema_slow),
            "color": "#3b82f6",
            "type": "line",
            "overlay": True,
            "lineWidth": 2,
        },
    ],
    "signals": [
        {"type": "buy", "text": "Golden", "color": "#22c55e", "data": buy_marks},
        {"type": "sell", "text": "Death", "color": "#ef4444", "data": sell_marks},
    ],
    "layers": [],
}
