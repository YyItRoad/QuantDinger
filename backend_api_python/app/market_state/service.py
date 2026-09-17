"""单次分析编排。只由内部显式调用，不注册路由、调度或交易能力。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math

import pandas as pd

from app.market_state.recipe import default_recipe


SECONDS = {'1h': 3600, '4h': 14400, '1d': 86400}
STATES = {
    'trend': 'STRONG_UP UP TURNING_UP SIDEWAYS TURNING_DOWN DOWN STRONG_DOWN'.split(),
    'structure': 'RANGE BREAKOUT BREAKDOWN RETEST PULLBACK REBOUND CONTINUATION REVERSAL FAILED_BREAKOUT FAILED_BREAKDOWN UNCLEAR'.split(),
    'ma_state': 'BEAR_ALIGNED BEAR_CONVERGING BOTTOMING BULL_TRANSITION BULL_ALIGNED BULL_CONVERGING BEAR_TRANSITION MIXED'.split(),
    'position': ['LOW', 'MID', 'HIGH'],
    'momentum': 'STRONG_UP UP NEUTRAL DOWN STRONG_DOWN'.split(),
    'phase': 'BASE TRANSITION_UP ADVANCE TOP TRANSITION_DOWN DECLINE'.split(),
}
LABELS = dict(zip(
    'STRONG_UP UP TURNING_UP SIDEWAYS TURNING_DOWN DOWN STRONG_DOWN RANGE BREAKOUT BREAKDOWN RETEST PULLBACK REBOUND CONTINUATION REVERSAL FAILED_BREAKOUT FAILED_BREAKDOWN UNCLEAR BEAR_ALIGNED BEAR_CONVERGING BOTTOMING BULL_TRANSITION BULL_ALIGNED BULL_CONVERGING BEAR_TRANSITION MIXED LOW MID HIGH NEUTRAL BASE TRANSITION_UP ADVANCE TOP TRANSITION_DOWN DECLINE'.split(),
    '强势上行 上行 转强 横盘 转弱 下行 强势下行 区间整理 向上突破 向下跌破 回测 回调 反弹 延续 反转 突破失败 跌破失败 结构不明确 空头排列 空头收敛 底部粘合 多头转换 多头排列 多头收敛 空头转换 均线混乱 低位 中位 高位 中性 筑底 转强阶段 上涨阶段 顶部阶段 转弱阶段 下跌阶段'.split(),
))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def closed_frame(bars, timeframe, count, now):
    """首版仅用于 24 小时交易的 Crypto；不把交易日历缺口误判为缺数据。"""
    step = SECONDS[timeframe]
    if now.tzinfo is None:
        raise ValueError('当前时间必须带时区')
    rows = []
    for bar in bars:
        ts = bar.get('time')
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ValueError('K 线时间必须是秒或毫秒时间戳')
        ts = ts / 1000 if ts > 10_000_000_000 else ts
        if ts != int(ts) or ts % step:
            raise ValueError('K 线时间与任务周期不匹配')
        if ts + step > now.timestamp():
            continue
        row = {'time': int(ts)}
        for key in ('open', 'high', 'low', 'close'):
            value = bar.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('K 线价格无效')
            row[key] = float(value)
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise ValueError('K 线高低价关系无效')
        volume = bar.get('volume')
        row['volume'] = float(volume) if isinstance(volume, (int, float)) and not isinstance(volume, bool) and math.isfinite(volume) and volume >= 0 else None
        rows.append(row)
    rows.sort(key=lambda row: row['time'])
    times = [row['time'] for row in rows]
    if len(times) != len(set(times)):
        raise ValueError('K 线时间重复')
    rows = rows[-count:]
    if len(rows) != count or any(b['time'] - a['time'] != step for a, b in zip(rows, rows[1:])):
        raise ValueError('已收盘行情不足或不连续')
    close_at = rows[-1]['time'] + step
    if close_at != int(now.timestamp() // step) * step:
        raise ValueError('行情未更新至最近已收盘周期')
    return pd.DataFrame(rows), datetime.fromtimestamp(close_at, timezone.utc)


def fetch_bars(task, limit):
    # 绑定交易所的现有只读来源不跨交易所降级，也不转用代币对应股票行情。
    from app.data_sources.crypto import CryptoDataSource
    source = CryptoDataSource.for_exchange(task['exchange_id'], task['market_type'])
    timeframe = {'1h': '1H', '4h': '4H', '1d': '1D'}[task['timeframe']]
    if not source._ensure_markets_loaded():
        raise ValueError('无法核验行情来源品种')
    symbol = source._symbol_for_scoped_market(task['symbol'])
    product = source.exchange.market(symbol)
    if not product.get(task['market_type']) or (task.get('instrument_id') and str(product['id']) != task['instrument_id']):
        raise ValueError('行情来源与任务品种标识不一致')
    # 不接受不完整低周期聚合；其完整性应在以后单独验收。
    supported = getattr(source.exchange, 'timeframes', None) or {}
    if supported and task['timeframe'] not in supported:
        raise ValueError('当前来源不支持原生任务周期')
    return source.get_kline(task['symbol'], timeframe, limit)


def compute_indicator(name, frame, params):
    from app.services.factors import compute_talib_indicator
    return compute_talib_indicator(name, frame, params)


def execute_script(code, inputs):
    from app.utils.safe_exec import safe_exec_isolated
    return safe_exec_isolated(code, input_data=inputs, timeout=20)


def ask_model(messages):
    from app.services.llm import LLMService
    llm = LLMService()
    provider = llm.provider
    model = llm.get_default_model(provider)
    answer = llm.call_llm_api(messages, model=model, provider=provider, temperature=0.2,
                              use_fallback=False, try_alternative_providers=False, use_json_mode=True)
    # 原服务会规范化模型名；只记录请求配置，不伪称供应商响应中的实际模型。
    return answer, {'provider': provider.value, 'requested_model': model}


def validate_answer(raw, facts):
    if not isinstance(raw, str) or len(raw.encode()) > 64_000:
        raise ValueError('模型输出无效或过大')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('模型未能给出有效分析')
    for field, choices in STATES.items():
        if value.get(field) not in choices:
            raise ValueError('模型状态不在约定字典内：' + field)
    if type(value.get('confidence')) is not int or not 1 <= value['confidence'] <= 5:
        raise ValueError('置信度必须为 1～5 的整数')
    if not isinstance(value.get('reason'), str) or not value['reason'].strip():
        raise ValueError('缺少分析理由')
    for key in ('evidence', 'counter_evidence', 'fact_refs'):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > 30 or any(not isinstance(x, str) or not x.strip() for x in items):
            raise ValueError('模型证据格式无效：' + key)
    if not value['evidence'] or not value['fact_refs'] or any(key not in facts for key in value['fact_refs']):
        raise ValueError('缺少证据或引用了不存在的事实')
    return value


class AnalysisService:
    def __init__(self, repository, *, fetch=fetch_bars, indicator=compute_indicator,
                 execute=execute_script, model=ask_model, clock=None):
        self.repository = repository
        self.fetch, self.indicator, self.execute, self.model = fetch, indicator, execute, model
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(self, user_id, task_id, recipe=None):
        recipe = deepcopy(recipe or default_recipe())
        task = self.repository.get_task(user_id, task_id)
        if not task or not task['enabled'] or task.get('deleted_at'):
            raise ValueError('分析任务不存在、已停止或已删除')
        if task['market'] != 'Crypto' or task['timeframe'] not in SECONDS:
            raise ValueError('单次执行首版仅支持 Crypto 的 1h、4h、1d；其他市场待交易日历接入')
        if not task.get('exchange_id') or task['market_type'] not in ('spot', 'swap'):
            raise ValueError('分析任务必须绑定交易所和市场类型')
        if type(recipe.bars) is not int or not 2 <= recipe.bars <= 1000 or len(recipe.indicators) > 32:
            raise ValueError('模板计算规模超出限制')
        now = self.clock()
        bars = self.fetch(task, recipe.bars + 1)
        frame, close_at = closed_frame(bars, task['timeframe'], recipe.bars, now)
        previous = self.repository.get_result_for_bar(user_id, task_id, close_at)
        if previous:
            return previous
        indicators = {key: self.indicator(spec['name'], frame.copy(), dict(spec.get('params', {})))
                      for key, spec in recipe.indicators.items()}
        execution = self.execute(recipe.code, {'df': frame.copy(), 'params': recipe.params, 'indicators': indicators})
        if not execution.get('success'):
            raise ValueError('分析计算失败：' + str(execution.get('error', '未知错误')))
        facts = (execution.get('result') or {}).get('output')
        if not isinstance(facts, dict) or not facts or any(not isinstance(key, str) for key in facts):
            raise ValueError('计算脚本必须输出非空事实对象')
        facts_json = json_text(facts)
        if len(facts_json.encode()) > 256_000:
            raise ValueError('计算结果过大')
        current = self.repository.get_task(user_id, task_id)
        if not current or not current['enabled'] or current.get('deleted_at') or current['revision'] != task['revision']:
            raise ValueError('分析任务已停止、删除或发生变更')
        identity = {key: task[key] for key in ('market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'timeframe')}
        messages = [
            {'role': 'system', 'content': recipe.prompt + '\n状态字典：' + json_text(STATES)},
            {'role': 'user', 'content': json_text({'instrument': identity, 'bar_close_at': close_at.isoformat(), 'facts': facts})},
        ]
        raw, model_info = self.model(messages)
        answer = validate_answer(raw, facts)
        input_rows = frame.astype(object).where(pd.notna(frame), None).to_dict(orient='records')
        result = {key: answer[key] for key in (*STATES, 'confidence')}
        result['summary'] = (f"{task['symbol']} {task['timeframe']}处于{LABELS[answer['phase']]}，"
                             + '，'.join(LABELS[answer[key]] for key in ('trend', 'structure', 'ma_state', 'position', 'momentum')) + '。')
        result['details'] = {
            'reason': answer['reason'],
            'evidence': answer['evidence'], 'counter_evidence': answer['counter_evidence'],
            'fact_refs': answer['fact_refs'], 'facts': facts, 'model': model_info,
            'recipe': {'name': recipe.name, 'version': recipe.version, 'bars': recipe.bars,
                       'indicators': recipe.indicators, 'params': recipe.params,
                       'code': recipe.code, 'prompt': recipe.prompt,
                       'sha256': digest({'code': recipe.code, 'prompt': recipe.prompt,
                                         'indicators': recipe.indicators, 'params': recipe.params, 'bars': recipe.bars})},
            'input': input_rows, 'input_sha256': digest(input_rows),
        }
        return self.repository.save_result(user_id, task_id, task['revision'], close_at, result)
