"""真实 PostgreSQL 集成测试；仅显式配置专用测试库时运行。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.market_state.repository import AnalysisRepository


@pytest.fixture
def postgres_repo(monkeypatch):
    url = os.getenv('MARKET_STATE_TEST_DATABASE_URL')
    if not url:
        pytest.skip('需要显式配置专用 MARKET_STATE_TEST_DATABASE_URL')
    schema = 'analysis_test_' + uuid4().hex
    migration = (Path(__file__).parents[1] / 'migrations/20260917_market_state.sql').read_text()
    schedule_migration = (Path(__file__).parents[1] / 'migrations/20260917_market_state_schedule.sql').read_text()
    with psycopg2.connect(url) as db:
        with db.cursor() as cur:
            cur.execute(f'CREATE SCHEMA {schema}')
            cur.execute(f'SET search_path TO {schema}')
            cur.execute('CREATE TABLE qd_users(id INTEGER PRIMARY KEY)')
            cur.execute('INSERT INTO qd_users VALUES (1), (2)')
            cur.execute(migration)
            cur.execute(migration)  # 迁移幂等。
            cur.execute(schedule_migration)
            cur.execute(schedule_migration)

    @contextmanager
    def transaction():
        connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET LOCAL search_path TO {schema}')
                yield connection
        finally:
            connection.close()

    monkeypatch.setattr('app.market_state.repository.get_db_transaction', transaction)
    monkeypatch.setattr('app.market_state.scheduling.get_db_transaction', transaction)
    yield AnalysisRepository(), transaction
    with psycopg2.connect(url) as db:
        with db.cursor() as cur:
            # 仅清理本测试创建的唯一 schema，不触及其他表。
            cur.execute(f'DROP SCHEMA {schema} CASCADE')


def task_value(symbol='BTC/USDT'):
    return dict(market='Crypto', symbol=symbol, exchange_id='binance', market_type='swap', instrument_id='', timeframe='4h')


def test_manual_reservation_stopped_scope_and_save(postgres_repo):
    from app.market_state.scheduling import ScheduleRepository
    repo, _ = postgres_repo
    schedule = ScheduleRepository()
    task = repo.create_task(1, task_value())
    stopped = repo.change_task(1, task['id'], enabled=False)
    now = datetime.now(timezone.utc)
    assert schedule.reserve_manual(2, task['id'], now) is None
    reservation = schedule.reserve_manual(1, task['id'], now)
    assert reservation
    assert schedule.reserve_manual(1, task['id'], now) is None
    record = repo.save_result(1, task['id'], stopped['revision'], now - timedelta(hours=1), result_value(), allow_stopped=True)
    assert record['id']
    assert repo.get_task(1, task['id'])['enabled'] is False
    schedule.release(1, task['id'], reservation['lease_token'])
    assert schedule.reserve_manual(1, task['id'], now)


def result_value():
    return dict(trend='UP', structure='PULLBACK', ma_state='BULL_ALIGNED', position='HIGH', momentum='DOWN',
                phase='ADVANCE', confidence=4, summary='集成测试结果',
                details={'evidence': ['测试证据'], 'counter_evidence': []})


def test_lifecycle_scope_and_unique_identity(postgres_repo):
    repo, _ = postgres_repo
    assert repo.list('records', 1, 1, 10)['total'] == 0
    task = repo.create_task(1, task_value())
    assert repo.get_task(1, task['id']) == task
    assert repo.get_task(2, task['id']) is None
    assert repo.change_task(2, task['id'], delete=True) is None
    with pytest.raises(ValueError):
        repo.create_task(1, {**task_value(), 'instrument_id': 'BTCUSDT'})
    stopped = repo.change_task(1, task['id'], enabled=False)
    assert not stopped['enabled'] and stopped['revision'] == 2
    assert repo.change_task(1, task['id'], enabled=True)['revision'] == 3
    repo.change_task(1, task['id'], delete=True)
    assert repo.get_task(1, task['id']) is None
    assert repo.list('tasks', 1, 1, 10)['total'] == 0
    assert repo.change_task(1, task['id'], enabled=True) is None
    assert repo.create_task(1, task_value())['id'] != task['id']


def test_results_snapshot_idempotency_pagination_and_retention(postgres_repo):
    repo, transaction = postgres_repo
    btc = repo.create_task(1, task_value())
    sol = repo.create_task(1, task_value('SOL/USDT'))
    bar = datetime.now(timezone.utc) - timedelta(hours=4)
    first = repo.save_result(1, btc['id'], 1, bar, result_value())
    assert repo.get_result_for_bar(1, btc['id'], bar) == first
    assert repo.get_result_for_bar(2, btc['id'], bar) is None
    assert repo.save_result(1, btc['id'], 1, bar, {**result_value(), 'summary': '不可覆盖'})['summary'] == '集成测试结果'
    assert repo.get_record(2, first['id']) is None
    repo.save_result(1, sol['id'], 1, bar, result_value())
    page = repo.list('records', 1, 2, 1)
    assert page['total'] == 2 and page['items'][0]['id'] == first['id']
    assert 'details' not in page['items'][0]
    assert repo.list('records', 1, 1, 10, 'BTC/USDT')['symbols'] == ['BTC/USDT', 'SOL/USDT']
    assert repo.list('records', 1, 99, 10, 'MISSING')['page'] == 1
    repo.change_task(1, btc['id'], delete=True)
    assert repo.get_record(1, first['id'])['symbol'] == 'BTC/USDT'
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with transaction() as db, db.cursor() as cur:
            cur.execute('DELETE FROM qd_market_state_tasks WHERE id=%s', (btc['id'],))
    assert repo.list('records', 1, 1, 10)['total'] == 2


def test_stale_execution_and_invalid_results_rejected(postgres_repo):
    repo, _ = postgres_repo
    task = repo.create_task(1, task_value())
    bar = datetime.now(timezone.utc) - timedelta(hours=4)
    repo.change_task(1, task['id'], enabled=False)
    with pytest.raises(ValueError):
        repo.save_result(1, task['id'], 1, bar, result_value())
    repo.change_task(1, task['id'], enabled=True)
    with pytest.raises(ValueError):
        repo.save_result(1, task['id'], 1, bar, result_value())
    with pytest.raises(ValueError):
        repo.save_result(2, task['id'], 3, bar, result_value())
    with pytest.raises(ValueError):
        repo.save_result(1, task['id'], 3, datetime.now(timezone.utc) + timedelta(days=1), result_value())
    with pytest.raises(psycopg2.errors.CheckViolation):
        repo.save_result(1, task['id'], 3, bar, {**result_value(), 'confidence': 99})
    assert repo.list('records', 1, 1, 10)['total'] == 0


def test_concurrent_create_is_unique(postgres_repo):
    repo, _ = postgres_repo
    def create(_):
        try:
            return repo.create_task(1, task_value())['id']
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create, range(4)))
    assert len([value for value in ids if value]) == 1
    assert repo.list('tasks', 1, 1, 10)['total'] == 1


def test_routes_use_database_by_default(postgres_repo, monkeypatch):
    from flask import Flask
    from flask_smorest import Api
    from app.market_state.routes import blp
    monkeypatch.setattr('app.utils.auth.verify_token', lambda _: {'user_id': 1, '_verified_user_role': 'user'})
    app = Flask(__name__)
    app.config.update(TESTING=True, API_TITLE='test', API_VERSION='1', OPENAPI_VERSION='3.0.3')
    Api(app).register_blueprint(blp, url_prefix='/api/market-state')
    client = app.test_client()
    headers = {'Authorization': 'Bearer test'}
    response = client.get('/api/market-state/records', headers=headers)
    assert response.json['data']['total'] == 0
    assert client.post('/api/market-state/tasks', json=task_value(), headers=headers).status_code == 201
    assert client.get('/api/market-state/tasks', headers=headers).json['data']['total'] == 1


def test_single_analysis_to_database_and_duplicate_reuse(postgres_repo):
    import json
    from app.market_state.service import AnalysisService
    repo, _ = postgres_repo
    task = repo.create_task(1, {**task_value(), 'timeframe': '1h'})
    now = datetime(2026, 9, 17, 0, 5, tzinfo=timezone.utc)
    end = int(now.timestamp() // 3600) * 3600
    rows = [dict(time=end - (180 - i) * 3600, open=100.0 + i, high=102.0 + i,
                 low=99.0 + i, close=101.0 + i, volume=10.0) for i in range(181)]
    calls = []
    def model(messages):
        calls.append(messages)
        return json.dumps(dict(trend='UP', structure='CONTINUATION', ma_state='BULL_ALIGNED',
                               position='HIGH', momentum='UP', phase='ADVANCE', confidence=4,
                               reason='集成测试', evidence=['均线向上'], counter_evidence=[], fact_refs=['ma7'])), {'requested_model': 'fake'}
    service = AnalysisService(repo, fetch=lambda *_: rows, model=model, clock=lambda: now)
    record = service.run_once(1, task['id'])
    assert repo.get_record(1, record['id']) == record
    assert len(record['details']['input']) == 180
    assert service.run_once(1, task['id']) == record
    assert len(calls) == 1
    with pytest.raises(ValueError):
        service.run_once(2, task['id'])
    repo.change_task(1, task['id'], enabled=False)
    with pytest.raises(ValueError):
        service.run_once(1, task['id'])
    assert len(calls) == 1


def test_schedule_reservation_concurrency_and_start_once(postgres_repo):
    from app.market_state.scheduling import ScheduleRepository
    repo, _ = postgres_repo
    task = repo.create_task(1, {**task_value(), 'timeframe': '1h'})
    schedule = ScheduleRepository()
    now = datetime(2026, 9, 17, 0, 1, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: schedule.reserve_due(now), range(4)))
    items = [item for batch in results for item in batch]
    assert len(items) == 1
    item = items[0]
    bar = datetime.fromisoformat(item['bar_close_at'])
    def start(_):
        return schedule.start_reserved(1, task['id'], 1, item['lease_token'], bar, now)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(start, range(4))) == 1
    schedule.release_pending(1, task['id'], item['lease_token'], bar)
    assert repo.get_task(1, task['id'])['lease_token'] is not None
    schedule.release(1, task['id'], item['lease_token'])
    assert schedule.reserve_due(now) == []
    assert len(schedule.reserve_due(now + timedelta(hours=1))) == 1


def test_schedule_grace_expiry_and_revision(postgres_repo):
    from app.market_state.scheduling import ScheduleRepository
    repo, _ = postgres_repo
    task = repo.create_task(1, {**task_value(), 'timeframe': '1h'})
    schedule = ScheduleRepository()
    now = datetime(2026, 9, 17, 0, 0, 10, tzinfo=timezone.utc)
    assert schedule.reserve_due(now) == []
    now += timedelta(seconds=30)
    item = schedule.reserve_due(now)[0]
    bar = datetime.fromisoformat(item['bar_close_at'])
    assert not schedule.start_reserved(2, task['id'], 1, item['lease_token'], bar, now)
    assert schedule.reserve_due(now + timedelta(minutes=29)) == []
    next_item = schedule.reserve_due(now + timedelta(minutes=31))[0]
    assert next_item['lease_token'] != item['lease_token']
    assert not schedule.start_reserved(1, task['id'], 1, item['lease_token'], bar, now + timedelta(minutes=31))
    schedule.release(1, task['id'], item['lease_token'])
    assert str(repo.get_task(1, task['id'])['lease_token']) == next_item['lease_token']
    repo.change_task(1, task['id'], enabled=False)
    repo.change_task(1, task['id'], enabled=True)
    assert not schedule.start_reserved(1, task['id'], 1, next_item['lease_token'], bar, now + timedelta(minutes=31))


def test_schedule_ignores_completed_noncrypto_and_stopped(postgres_repo):
    from app.market_state.scheduling import ScheduleRepository
    repo, _ = postgres_repo
    a = repo.create_task(1, task_value())
    b = repo.create_task(1, task_value('ETH/USDT'))
    repo.create_task(1, {**task_value('AAPL'), 'market': 'USStock', 'exchange_id': ''})
    repo.change_task(1, b['id'], enabled=False)
    bar = datetime(2026, 9, 17, 0, tzinfo=timezone.utc)
    repo.save_result(1, a['id'], 1, bar, result_value())
    assert ScheduleRepository().reserve_due(bar + timedelta(minutes=1)) == []


def test_celery_consumption_to_record_api_with_duplicate_delivery(postgres_repo, monkeypatch):
    """真实 Celery 消费线程及 JSON 序列化，内存 Broker、真实测试数据库、假模型。"""
    import json
    from threading import Event
    from celery.contrib.testing.worker import start_worker
    from celery.signals import task_postrun
    from flask import Flask
    from flask_smorest import Api
    from app.celery_app import celery_app, FlaskContextTask
    from app.market_state import tasks
    from app.market_state.routes import blp
    from app.market_state.service import AnalysisService

    repo, _ = postgres_repo
    task = repo.create_task(1, {**task_value(), 'timeframe': '1h'})
    now = datetime(2026, 9, 17, 0, 5, tzinfo=timezone.utc)
    end = int(now.timestamp() // 3600) * 3600
    bars = [dict(time=end - (180 - i) * 3600, open=100.0 + i, high=102.0 + i,
                 low=99.0 + i, close=101.0 + i, volume=10.0) for i in range(181)]
    calls = []
    def model(messages):
        calls.append(messages)
        return json.dumps(dict(trend='UP', structure='CONTINUATION', ma_state='BULL_ALIGNED',
                               position='HIGH', momentum='UP', phase='ADVANCE', confidence=4,
                               reason='消费链路验证', evidence=['价格持续推进'], counter_evidence=[],
                               fact_refs=['candles'])), {'requested_model': 'test-only'}

    application = Flask('analysis-consumption-test')
    application.config.update(TESTING=True, API_TITLE='test', API_VERSION='1', OPENAPI_VERSION='3.0.3')
    Api(application).register_blueprint(blp, url_prefix='/api/market-state')
    monkeypatch.setattr(FlaskContextTask, '_flask_app', application)
    monkeypatch.setattr(tasks, 'utc_now', lambda: now)
    monkeypatch.setattr(tasks, 'AnalysisService', lambda store: AnalysisService(
        store, fetch=lambda *_: bars, model=model, clock=lambda: now))
    monkeypatch.setattr('app.utils.auth.verify_token', lambda _: {'user_id': 1, '_verified_user_role': 'user'})
    monkeypatch.setenv('MARKET_STATE_EXECUTION_ENABLED', 'true')
    monkeypatch.setitem(celery_app.conf, 'broker_url', 'memory://')
    monkeypatch.setitem(celery_app.conf, 'result_backend', 'cache+memory://')
    done = Event()
    consumed = []
    def completed(sender=None, kwargs=None, state=None, **unused):
        if sender.name == 'quantdinger.tasks.market_state_execute':
            consumed.append((kwargs, state))
            done.set()
    task_postrun.connect(completed, weak=False)
    try:
        with start_worker(celery_app, pool='solo', queues=['maintenance', 'ai'],
                          perform_ping_check=False, shutdown_timeout=15):
            tasks.dispatch_analysis.apply_async()
            assert done.wait(15), '未消费分析任务'
            assert consumed[0][1] == 'SUCCESS'
            assert len(calls) == 1
            record = repo.list('records', 1, 1, 10)['items'][0]
            assert record['task_id'] == task['id']
            client = application.test_client()
            headers = {'Authorization': 'Bearer test'}
            detail = client.get(f"/api/market-state/records/{record['id']}", headers=headers)
            assert detail.status_code == 200 and detail.json['data']['details']['facts']['ma7']
            assert client.get('/api/market-state/tasks', headers=headers).status_code == 200
            done.clear()
            tasks.execute_analysis.apply_async(kwargs=consumed[0][0])
            assert done.wait(15), '未消费重复消息'
            assert consumed[-1][1] == 'SUCCESS'
            assert len(calls) == 1
            assert repo.list('records', 1, 1, 10)['total'] == 1
    finally:
        task_postrun.disconnect(completed)
