# ruff: noqa: F821
# 本文件由安全执行器注入 df、indicators、params，并预加载 pd，不作为普通模块导入。
# 本模板只整理观察事实，不计算最终状态，不固化旧讨论稿的形态规则。
# df 为已收盘行情；indicators 为按模板清单调用项目指标库得到的完整序列。
window = int(params['window'])
recent = int(params['recent'])
if window < 2 or recent < 2 or len(df) < max(window, recent):
    raise ValueError('模板所需行情不足')

output = {'candles': df[['time', 'open', 'high', 'low', 'close']].tail(window).to_dict(orient='records')}
for name, values in indicators.items():
    tail = values.tail(recent)
    if tail.isna().to_numpy().any():
        raise ValueError('指标尚未形成：' + name)
    if isinstance(tail, pd.DataFrame):
        output[name] = tail.to_dict(orient='list')
    else:
        output[name] = [float(value) for value in tail]

closes = df['close'].tail(window)
span = float(closes.max() - closes.min())
output['position_score'] = float((closes.iloc[-1] - closes.min()) / span) if span else 0.5
output['position_note'] = '观察区间无波动' if not span else '按观察区间最高最低收盘价归一化'
