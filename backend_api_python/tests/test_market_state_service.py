"""单次编排及真实隔离计算测试；不调用行情网络、模型或生产数据库。"""
from dataclasses import replace
from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from app.market_state.recipe import default_recipe
from app.market_state.service import AnalysisService, ask_model, closed_frame, compute_indicator, execute_script, fetch_bars, validate_answer


NOW = datetime(2026, 9, 17, 0, 5, tzinfo=timezone.utc)


def candles(count=181):
    end = int(NOW.timestamp() // 3600) * 3600
    return [dict(time=end - (count - 1 - i) * 3600, open=100.0 + i, high=102.0 + i,
                 low=99.0 + i, close=101.0 + i, volume=10.0) for i in range(count)]


def answer():
    return dict(trend='UP', structure='CONTINUATION', ma_state='BULL_ALIGNED', position='HIGH',
                momentum='UP', phase='ADVANCE', confidence=4, reason='整体上行但仍有不确定性',
                evidence=['价格向上推进'], counter_evidence=[], fact_refs=['candles'])


@pytest.fixture
def setup():
    task = dict(id=1, user_id=1, market='Crypto', symbol='BTC/USDT', timeframe='1h', exchange_id='binance',
                market_type='swap', instrument_id='BTCUSDT', enabled=True, deleted_at=None, revision=1)
    repo = Mock()
    repo.get_task.return_value = task
    repo.get_result_for_bar.return_value = None
    repo.save_result.side_effect = lambda user, task, revision, close_at, result: {'id': 9, **result}
    model = Mock(return_value=(json.dumps(answer()), {'requested_model': 'fake'}))
    fetch = Mock(return_value=candles())
    execute = Mock(return_value={'success': True, 'result': {'output': {'candles': [1]}}})
    indicator = Mock(return_value=pd.Series([1.0]))
    service = AnalysisService(repo, fetch=fetch, execute=execute, indicator=indicator, model=model, clock=lambda: NOW)
    return service, repo, fetch, indicator, execute, model


def test_closed_window_excludes_open_and_preserves_order():
    frame, close_at = closed_frame(list(reversed(candles())), '1h', 180, NOW)
    assert len(frame) == 180 and frame.iloc[-1]['close'] == 280
    assert close_at.hour == 0 and close_at.minute == 0
    milliseconds = [{**row, 'time': row['time'] * 1000} for row in candles()]
    assert closed_frame(milliseconds, '1h', 180, NOW)[0].equals(frame)


def test_manual_run_stopped_task_keeps_state_and_uses_same_pipeline(setup):
    service, repo, fetch, indicator, execute, model = setup
    repo.get_task.return_value['enabled'] = False
    repo.save_result.side_effect = lambda *args, **kwargs: {'id': 9}
    assert service.run_once(1, 1, allow_stopped=True) == {'id': 9}
    assert repo.get_task.return_value['enabled'] is False
    assert repo.save_result.call_args.kwargs == {'allow_stopped': True}
    model.assert_called_once()
    repo.change_task.assert_not_called()


def test_manual_run_still_rejects_revision_change(setup):
    service, repo, fetch, indicator, execute, model = setup
    original = {**repo.get_task.return_value, 'enabled': False}
    repo.get_task.side_effect = [original, {**original, 'revision': original['revision'] + 1}]
    with pytest.raises(ValueError, match='变更'):
        service.run_once(1, 1, allow_stopped=True)
    model.assert_not_called()


@pytest.mark.parametrize('change', ['gap', 'duplicate', 'price', 'ohlc', 'interval', 'stale', 'few'])
def test_bad_market_data_rejected(change):
    bars = candles()
    if change == 'gap':
        del bars[80]
    elif change == 'duplicate':
        bars.insert(40, bars[40])
    elif change == 'price':
        bars[80]['close'] = float('nan')
    elif change == 'ohlc':
        bars[80]['high'] = 1
    elif change == 'interval':
        bars[80]['time'] += 60
    elif change == 'stale':
        for bar in bars:
            bar['time'] -= 7200
    elif change == 'few':
        bars = bars[-20:]
    with pytest.raises(ValueError):
        closed_frame(bars, '1h', 180, NOW)


def test_only_requested_indicators_and_recipe_snapshot(setup):
    service, repo, fetch, indicator, execute, model = setup
    recipe = replace(default_recipe(), indicators={'custom': {'name': 'RSI', 'params': {'timeperiod': 9}}},
                     code="output = {'candles': [1]}", params={'x': 2}, prompt='替换提示词')
    result = service.run_once(1, 1, recipe)
    assert indicator.call_count == 1
    assert indicator.call_args.args[0] == 'RSI' and indicator.call_args.args[2] == {'timeperiod': 9}
    assert execute.call_args.args[0] == recipe.code
    assert len(execute.call_args.args[1]['df']) == 180
    assert result['details']['recipe']['code'] == recipe.code
    assert result['details']['recipe']['params'] == {'x': 2}
    assert result['details']['reason'] == answer()['reason']
    assert '上涨阶段' in result['summary']
    assert '替换提示词' in model.call_args.args[0][0]['content']
    assert repo.save_result.call_args.args[:3] == (1, 1, 1)
    assert repo.save_result.call_args.args[3].minute == 0


def test_existing_result_skips_calculation_and_model(setup):
    service, repo, _, indicator, execute, model = setup
    repo.get_result_for_bar.return_value = {'id': 8}
    assert service.run_once(1, 1) == {'id': 8}
    indicator.assert_not_called()
    execute.assert_not_called()
    model.assert_not_called()


def test_queued_revision_and_bar_expiry_before_model(setup):
    service, repo, fetch, _, _, model = setup
    with pytest.raises(ValueError, match='任务已变更'):
        service.run_once(1, 1, expected_revision=9)
    fetch.assert_not_called()
    with pytest.raises(ValueError, match='周期已过期'):
        service.run_once(1, 1, expected_bar=datetime(2026, 9, 16, 23, tzinfo=timezone.utc))
    model.assert_not_called()
    repo.save_result.assert_not_called()


@pytest.mark.parametrize('change', ['missing', 'stopped', 'noncrypto'])
def test_task_checks_before_fetch(setup, change):
    service, repo, fetch, _, _, model = setup
    if change == 'missing':
        repo.get_task.return_value = None
    elif change == 'stopped':
        repo.get_task.return_value['enabled'] = False
    else:
        repo.get_task.return_value['market'] = 'USStock'
    with pytest.raises(ValueError):
        service.run_once(2, 1)
    repo.get_task.assert_called_with(2, 1)
    fetch.assert_not_called()
    model.assert_not_called()


def test_stop_during_calculation_skips_model(setup):
    service, repo, _, _, _, model = setup
    task = repo.get_task.return_value
    repo.get_task.side_effect = [task, {**task, 'revision': 2, 'enabled': False}]
    with pytest.raises(ValueError, match='变更'):
        service.run_once(1, 1)
    model.assert_not_called()
    repo.save_result.assert_not_called()


def test_stop_during_model_uses_repository_revision_guard(setup):
    service, repo, _, _, _, model = setup
    repo.save_result.side_effect = ValueError('任务已停止')
    with pytest.raises(ValueError, match='停止'):
        service.run_once(1, 1)
    assert model.call_count == 1
    assert repo.save_result.call_args.args[2] == 1


@pytest.mark.parametrize('output', [None, [], {}, {'x': float('inf')}, {'x': 'a' * 256_001}])
def test_invalid_script_output_never_calls_model(setup, output):
    service, repo, _, _, execute, model = setup
    execute.return_value = {'success': True, 'result': {'output': output}}
    with pytest.raises(ValueError):
        service.run_once(1, 1)
    model.assert_not_called()
    repo.save_result.assert_not_called()


def test_script_error_is_not_replaced_with_fake_result(setup):
    service, repo, _, _, execute, model = setup
    execute.return_value = {'success': False, 'error': 'timeout'}
    with pytest.raises(ValueError, match='timeout'):
        service.run_once(1, 1)
    model.assert_not_called()
    repo.save_result.assert_not_called()


@pytest.mark.parametrize('change', [dict(phase='BUY'), dict(confidence=True), dict(confidence=6),
                                   dict(fact_refs=['missing']), dict(evidence=[]), dict(reason=''),
                                   dict(error='数据不足')])
def test_model_contract_rejects_invalid_fields(change):
    with pytest.raises(ValueError):
        validate_answer(json.dumps({**answer(), **change}), {'candles': []})


def test_model_contract_accepts_existing_prose_wrapped_json_parser():
    raw = '<think>先分析行情结构。</think>\n' + json.dumps(answer(), ensure_ascii=False)
    assert validate_answer(raw, {'candles': []}) == answer()


def test_invalid_json_and_failed_model_not_saved(setup):
    service, repo, _, _, _, model = setup
    model.return_value = ('```json\n{}\n```', {})
    with pytest.raises(ValueError):
        service.run_once(1, 1)
    repo.save_result.assert_not_called()
    model.side_effect = RuntimeError('model unavailable')
    with pytest.raises(RuntimeError):
        service.run_once(1, 1)
    repo.save_result.assert_not_called()


def test_real_library_and_isolated_default_script(setup):
    service, repo, _, _, _, _ = setup
    service.indicator = compute_indicator
    service.execute = execute_script
    result = service.run_once(1, 1)
    facts = result['details']['facts']
    assert len(facts['candles']) == 120
    assert len(facts['ma90']) == 6
    assert facts['ma7'][-1] == pytest.approx(277)
    assert facts['atr14'][-1] == pytest.approx(3)
    assert facts['position_score'] == 1


def test_script_replacement_runs_without_indicator_dependencies(setup):
    service, _, _, indicator, _, _ = setup
    service.execute = execute_script
    recipe = replace(default_recipe(), indicators={}, code="output = {'candles': [float(close.iloc[-1])]}")
    assert service.run_once(1, 1, recipe)['details']['facts'] == {'candles': [280.0]}
    indicator.assert_not_called()


def test_dangerous_script_rejected_before_model(setup):
    service, repo, _, _, _, model = setup
    service.execute = execute_script
    recipe = replace(default_recipe(), indicators={}, code="import os\noutput = {'candles': os.environ}")
    with pytest.raises(ValueError, match='计算失败'):
        service.run_once(1, 1, recipe)
    model.assert_not_called()
    repo.save_result.assert_not_called()


def test_source_adapter_binds_identity_and_uppercase_timeframe(setup, monkeypatch):
    service = Mock()
    service.get_kline.return_value = candles()
    factory = Mock(return_value=service)
    monkeypatch.setattr('app.services.kline.KlineService', factory)
    task = setup[1].get_task.return_value
    assert fetch_bars(task, 181) == candles()
    factory.assert_called_once_with()
    service.get_kline.assert_called_once_with(
        market='Crypto', symbol='BTC/USDT', timeframe='1H', limit=181,
        exchange_id='binance', market_type='swap', instrument_id='BTCUSDT')
    with pytest.raises(ValueError, match='行情来源身份'):
        fetch_bars({**task, 'exchange_id': 'unknown'}, 181)


def test_model_adapter_locks_provider_and_disables_fallback(monkeypatch):
    from app.services.llm import LLMProvider
    llm = Mock(provider=LLMProvider.OPENAI)
    llm.get_default_model.return_value = 'test-model'
    llm.call_llm_api.return_value = '{}'
    monkeypatch.setattr('app.services.llm.LLMService', lambda: llm)
    raw, info = ask_model([{'role': 'user', 'content': 'test'}])
    assert raw == '{}' and info == {'provider': 'openai', 'requested_model': 'test-model'}
    kwargs = llm.call_llm_api.call_args.kwargs
    assert kwargs['provider'] == LLMProvider.OPENAI
    assert kwargs['model'] == 'test-model'
    assert not kwargs['use_fallback'] and not kwargs['try_alternative_providers']


def test_missing_volume_is_optional_for_default_template(setup):
    service, _, fetch, _, _, _ = setup
    rows = candles()
    rows[10]['volume'] = None
    fetch.return_value = rows
    service.indicator, service.execute = compute_indicator, execute_script
    result = service.run_once(1, 1)
    assert result['details']['input'][10]['volume'] is None
    assert len(result['details']['input_sha256']) == 64


def test_multi_output_indicator_reuses_existing_library(setup):
    service = setup[0]
    service.indicator, service.execute = compute_indicator, execute_script
    recipe = replace(default_recipe(), indicators={'macd': {'name': 'MACD', 'params': {}}})
    facts = service.run_once(1, 1, recipe)['details']['facts']
    assert set(facts['macd']) == {'macd', 'macdsignal', 'macdhist'}
    assert len(facts['macd']['macd']) == 6
    assert 'ma7' not in facts
