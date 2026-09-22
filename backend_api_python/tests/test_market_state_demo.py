"""分析模块第一步：隔离假数据、接口契约及用户边界，不调用行情/模型。"""
import pytest
from flask import Flask
from flask_smorest import Api

from app.market_state.demo_repository import DemoRepository
from app.market_state.errors import RequestValidationError
from app.market_state.routes import blp
from app.market_state.validation import validate_task_input


@pytest.fixture
def demo(tmp_path):
    store = DemoRepository(tmp_path / 'demo.sqlite')
    store.seed(1)
    return store


def test_seed_paging_filter_and_sort(demo):
    result = demo.list('records', 1, 1, 10)
    assert result['total'] == 24
    assert len(result['items']) == 10
    assert result['symbols'] == ['BTC/USDT', 'SOL/USDT']
    assert 'details' not in result['items'][0]
    dates = [r['created_at'] for r in result['items']]
    assert dates == sorted(dates, reverse=True)
    second = demo.list('records', 1, 2, 10)
    assert not {r['id'] for r in result['items']} & {r['id'] for r in second['items']}
    filtered = demo.list('records', 1, 1, 10, 'SOL/USDT', '1h')
    assert filtered['total'] == 8
    assert filtered['symbols'] == result['symbols']
    demo.seed(1)
    assert demo.list('records', 1, 1, 10)['total'] == 24


def test_stop_delete_preserve_records_and_do_not_reseed(demo):
    for task in demo.list('tasks', 1, 1, 100)['items']:
        assert demo.change_task(1, task['id'], enabled=False)['enabled'] is False
        assert demo.change_task(1, task['id'], enabled=True)['enabled'] is True
        demo.change_task(1, task['id'], delete=True)
        assert demo.change_task(1, task['id'], enabled=True) is None
    demo.seed(1)
    assert demo.list('tasks', 1, 1, 10)['total'] == 0
    assert demo.list('records', 1, 1, 10)['total'] == 24


def test_tenant_isolation_and_persistence(demo):
    row = demo.list('records', 1, 1, 10)['items'][0]
    task = demo.list('tasks', 1, 1, 10)['items'][0]
    demo.seed(2)
    assert demo.get_record(2, row['id']) is None
    assert demo.change_task(2, task['id'], delete=True) is None
    other = DemoRepository(demo.path)
    assert other.get_record(1, row['id'])['details']['demo'] is True


def test_duplicate_identity_and_recreation_after_delete(demo):
    task = demo.list('tasks', 1, 1, 10)['items'][0]
    value = {key: task[key] for key in ('market', 'symbol', 'exchange_id', 'market_type', 'timeframe')}
    value['instrument_id'] = ''
    with pytest.raises(ValueError):
        demo.create_task(1, value)
    demo.change_task(1, task['id'], delete=True)
    assert demo.create_task(1, value)['id'] != task['id']


def test_task_input_reuses_canonical_market_context():
    value = validate_task_input({
        'market': 'Crypto', 'symbol': ' btc/usdt:usdt ', 'exchange_id': 'okex',
        'market_type': 'perpetual', 'instrument_id': 'BTC-USDT-SWAP', 'timeframe': '4h',
    })
    assert value == {
        'market': 'Crypto', 'symbol': 'BTC/USDT', 'exchange_id': 'okx',
        'market_type': 'swap', 'instrument_id': 'BTC-USDT-SWAP', 'timeframe': '4h',
    }
    with pytest.raises(RequestValidationError):
        validate_task_input({'market': 'Unknown', 'symbol': 'BTC/USDT', 'timeframe': '4h'})


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import app.utils.auth as auth
    import app.market_state.routes as routes
    monkeypatch.setattr(auth, 'verify_token', lambda token: {'user_id': int(token), '_verified_username': 'demo', '_verified_user_role': 'user'})
    store = DemoRepository(tmp_path / 'api.sqlite')
    store.seed(1)
    monkeypatch.setattr(routes, 'AnalysisRepository', lambda: store)
    application = Flask(__name__)
    application.config.update(TESTING=True, API_TITLE='test', API_VERSION='1', OPENAPI_VERSION='3.0.3')
    Api(application).register_blueprint(blp, url_prefix='/api/market-state')
    yield application.test_client()


def headers(user=1):
    return {'Authorization': f'Bearer {user}'}


def test_manual_run_requires_login(api_client):
    assert api_client.post('/api/market-state/tasks/1/run').status_code == 401


@pytest.mark.parametrize('case, status', [('ok', 202), ('missing', 404), ('busy', 409), ('unsupported', 400), ('failed', 503)])
def test_manual_run_scope_lease_and_failure(api_client, monkeypatch, case, status):
    from unittest.mock import Mock
    repo = Mock()
    repo.get_task.return_value = None if case == 'missing' else {
        'market': 'USStock' if case == 'unsupported' else 'Crypto',
        'timeframe': '4h',
        'enabled': False,
    }
    repo.reserve_manual.return_value = None if case == 'busy' else {'revision': 3, 'lease_token': 'token'}
    submit = Mock(return_value='celery')
    if case == 'failed':
        submit.side_effect = RuntimeError('private broker error')
    monkeypatch.setattr('app.market_state.scheduling.ScheduleRepository', lambda: repo)
    monkeypatch.setattr('app.market_state.tasks.submit_manual_analysis', submit)
    response = api_client.post('/api/market-state/tasks/5/run', headers=headers(2))
    assert response.status_code == status
    repo.get_task.assert_called_once_with(2, 5)
    repo.change_task.assert_not_called()
    if case in ('ok', 'failed'):
        submit.assert_called_once_with({'id': 5, 'user_id': 2, 'revision': 3, 'lease_token': 'token'})
    else:
        submit.assert_not_called()
    if case == 'failed':
        repo.release.assert_called_once_with(2, 5, 'token')
    else:
        repo.release.assert_not_called()
    if case == 'ok':
        assert response.json['data']['status'] == 'queued'
        assert response.json['data']['expected_bar_close_at'].endswith('+00:00')
    assert 'private broker error' not in response.get_data(as_text=True)


def test_api_requires_login(api_client):
    assert api_client.get('/api/market-state/records').status_code == 401


def test_api_pagination_validation_detail_and_ownership(api_client):
    root = '/api/market-state'
    response = api_client.get(root + '/records?page=2&page_size=10', headers=headers())
    assert response.json['data']['page'] == 2
    record_id = response.json['data']['items'][0]['id']
    assert api_client.get(f'{root}/records/{record_id}', headers=headers()).json['data']['details']['demo']
    assert api_client.get(f'{root}/records/{record_id}', headers=headers(2)).status_code == 404
    for query in ('page=0', 'page=abc', 'page_size=101', 'timeframe=5m'):
        assert api_client.get(root + '/records?' + query, headers=headers()).status_code == 400


def test_api_task_lifecycle(api_client):
    root = '/api/market-state/tasks'
    value = dict(market='Crypto', symbol='ETH/USDT', exchange_id='binance', market_type='swap', timeframe='4h')
    result = api_client.post(root, json={**value, 'user_id': 999}, headers=headers())
    assert result.status_code == 201
    task_id = result.json['data']['id']
    assert api_client.post(root, json=value, headers=headers()).status_code == 409
    assert api_client.patch(f'{root}/{task_id}', json={'enabled': 'false'}, headers=headers()).status_code == 400
    assert api_client.patch(f'{root}/{task_id}', json={'enabled': False}, headers=headers(2)).status_code == 404
    assert api_client.patch(f'{root}/{task_id}', json={'enabled': False}, headers=headers()).json['data']['enabled'] is False
    assert api_client.delete(f'{root}/{task_id}', headers=headers()).status_code == 200
    assert api_client.delete(f'{root}/{task_id}', headers=headers()).status_code == 404
    assert api_client.get('/api/market-state/records', headers=headers()).json['data']['total'] == 24


@pytest.mark.parametrize('body', [[], {}, {'market': 'Crypto', 'symbol': 'BTC', 'timeframe': '5m'}])
def test_api_rejects_invalid_creation(api_client, body):
    assert api_client.post('/api/market-state/tasks', json=body, headers=headers()).status_code == 400


def test_database_failure_returns_unavailable_without_demo_fallback(api_client, monkeypatch):
    from psycopg2 import OperationalError
    import app.market_state.routes as routes
    class FailingRepository:
        def list(self, *args):
            raise OperationalError('test database unavailable')
    monkeypatch.setattr(routes, 'AnalysisRepository', FailingRepository)
    response = api_client.get('/api/market-state/records', headers=headers())
    assert response.status_code == 503
    assert response.json['data'] is None


def test_schema_registered_after_base_schema(monkeypatch):
    from unittest.mock import MagicMock
    from app.utils import db
    names = []
    monkeypatch.setattr(db, 'get_db_connection', MagicMock())
    def component(*args, **kwargs):
        names.append(kwargs['name'])
        if kwargs['name'] == 'market-state':
            assert kwargs['path'].is_file()
    monkeypatch.setattr(db, '_apply_migration_component', component)
    db._apply_init_sql(MagicMock(), strict=True)
    assert names.index('market-state') > names.index('schema-init')
    assert names.index('market-state-schedule') == names.index('market-state') + 1
