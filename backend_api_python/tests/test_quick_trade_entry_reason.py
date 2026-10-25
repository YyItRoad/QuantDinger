"""Entry reasons use the existing order ledger, without real exchange calls."""
from types import SimpleNamespace

import pytest
from psycopg2.extras import RealDictCursor

from app.routes import quick_trade
from app.routes.agent_v1 import quick_trade as agent_orders
from app.services.quick_trade.history import normalize_entry_reason, parse_quick_trade_metadata
from tests.test_trade_plan_app import full_app, login


@pytest.fixture
def opening(full_app, monkeypatch):
    client, admin, password, _ = full_app
    headers = login(client, 'plan-test', password)
    from app.services.live_trading import account_configuration, execution, spot_sizing
    from app.services.quick_trade import credentials, orders
    from app.services.execution_streams.repository import ExecutionEventRepository
    from app.services.live_trading import fee_quote
    config = {'exchange_id': 'binance'}
    calls = []
    reason = '市场震荡，回踩支撑未破。\n判断有反弹机会，尝试做多。'
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_exchange_credentials(id,user_id,name,exchange_id,encrypted_config) VALUES(901,1,'fixture','binance','{}')")

    def execute_order(**kwargs):
        with admin.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM qd_quick_trades WHERE user_id=1 ORDER BY id DESC LIMIT 1')
            before = cur.fetchone()
        assert before['status'] == 'pending'
        assert parse_quick_trade_metadata(before['raw_result'])['entry_reason'] == reason
        calls.append(before['id'])
        return SimpleNamespace(exchange_order_id='fake-order', filled=2, avg_price=100, raw={})

    fake = SimpleNamespace(place_limit_order=execute_order)
    monkeypatch.setattr(quick_trade, '_build_product_aware_config', lambda *a, **k: (config, None))
    monkeypatch.setattr(quick_trade, 'create_exchange_client', lambda *a, **k: fake)
    monkeypatch.setattr(credentials, 'build_exchange_config', lambda *a, **k: config)
    monkeypatch.setattr(credentials, 'create_exchange_client', lambda *a, **k: fake)
    monkeypatch.setattr(quick_trade, '_convert_usdt_to_base_qty', lambda *a, **k: 2)
    monkeypatch.setattr(quick_trade, 'fetch_balance_raw', lambda *a, **k: {'available': 100000})
    monkeypatch.setattr(spot_sizing, 'normalize_spot_base_quantity', lambda *a, quantity, **k: quantity)
    monkeypatch.setattr(spot_sizing, 'normalize_spot_quote_amount', lambda *a, quote_amount, **k: quote_amount)
    monkeypatch.setattr(account_configuration, 'configure_derivatives_account', lambda *a, **k: None)
    monkeypatch.setattr(execution, 'place_order_from_signal', execute_order)
    monkeypatch.setattr(ExecutionEventRepository, 'register_binding', lambda *a, **k: None)
    monkeypatch.setattr(fee_quote, 'fee_to_quote', lambda *a, **k: 0)
    for module in (quick_trade, orders):
        monkeypatch.setattr(module, 'enrich_fill', lambda *a, **k: {'filled': 2, 'avg_price': 100, 'status': 'filled'})
        monkeypatch.setattr(module, 'attach_quick_trade_protection', lambda *a, **k: [])
        monkeypatch.setattr(module, 'limit_order_kwargs', lambda *a, **k: {})
    body = dict(credential_id=901, symbol='BTC/USDT', side='buy', amount=200, market_type='spot', entry_reason='  '+reason+'  ')
    return client, admin, headers, body, calls, reason


@pytest.mark.parametrize('market_type,side,leverage', [('spot','buy',1), ('swap','sell',1), ('perp','buy',1), ('','sell',5)])
def test_blank_reason_blocks_entry_before_any_exchange_access(full_app, monkeypatch, market_type, side, leverage):
    client, _, password, _ = full_app
    headers = login(client, 'plan-test', password)
    monkeypatch.setattr(quick_trade, '_build_product_aware_config', lambda *a, **k: pytest.fail('must reject before accessing credentials'))
    response = client.post('/api/quick-trade/place-order', headers=headers, json={
        'credential_id': 901, 'symbol':'BTC/USDT', 'side':side, 'market_type':market_type,
        'leverage':leverage, 'amount':200, 'entry_reason':' \n\t ',
    })
    assert response.status_code == 400
    assert response.get_json()['msg'] == 'quickTrade.entryReasonRequired'


@pytest.mark.parametrize('market_type,side,order_type', [('spot','buy','market'), ('spot','buy','limit'), ('swap','sell','market'), ('swap','buy','limit')])
def test_reason_commits_before_order_and_remains_on_same_history_record(opening, market_type, side, order_type):
    client, admin, headers, body, calls, reason = opening
    body.update(market_type=market_type, side=side, order_type=order_type, price=100)
    response = client.post('/api/quick-trade/place-order', headers=headers, json=body)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['code'] == 1
    assert calls == [response.get_json()['data']['trade_id']]
    history = client.get('/api/quick-trade/history?credential_id=901&symbol=BTC/USDT&market_type='+market_type, headers=headers).get_json()['data']['trades']
    assert len(history) == 1
    assert history[0]['entry_reason'] == reason
    assert history[0]['status'] == 'filled'
    viewer = login(client, 'plan-viewer', 'isolated-plan-test-password')
    assert client.get('/api/quick-trade/history', headers=viewer).get_json()['data']['trades'] == []


def test_reason_save_failure_prevents_order_submission(opening, monkeypatch):
    client, _, headers, body, calls, _ = opening
    monkeypatch.setattr(quick_trade, '_record_quick_trade', lambda **k: None)
    response = client.post('/api/quick-trade/place-order', headers=headers, json=body)
    assert response.status_code == 503
    assert response.get_json()['msg'] == 'quickTrade.entryReasonSaveFailed'
    assert calls == []


@pytest.mark.parametrize('market_type', ['spot', 'perp'])
def test_uncertain_order_retains_reason_without_duplicate_history(opening, monkeypatch, market_type):
    client, admin, headers, body, calls, reason = opening
    from app.services.live_trading import execution
    def uncertain(**kwargs):
        raise RuntimeError('synthetic submission timeout')
    monkeypatch.setattr(execution, 'place_order_from_signal', uncertain)
    body['market_type'] = market_type
    response = client.post('/api/quick-trade/place-order', headers=headers, json=body)
    assert response.status_code == 500
    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM qd_quick_trades WHERE user_id=1')
        rows = cur.fetchall()
    assert len(rows) == 1
    assert parse_quick_trade_metadata(rows[0]['raw_result'])['entry_reason'] == reason
    assert rows[0]['status'] == 'pending'
    assert rows[0]['market_type'] == ('swap' if market_type == 'perp' else 'spot')
    assert rows[0]['client_order_id']
    assert rows[0]['raw_result']['_quick_trade']['requested_base_qty'] == 2
    assert rows[0]['amount'] == 200


def test_spot_exit_does_not_require_reason(full_app, monkeypatch):
    client, _, password, _ = full_app
    headers = login(client, 'plan-test', password)
    reached = []
    def stop_after_validation(*a, **k):
        reached.append(True)
        raise ValueError('fixture stops before real credentials')
    monkeypatch.setattr(quick_trade, '_build_product_aware_config', stop_after_validation)
    response = client.post('/api/quick-trade/place-order', headers=headers, json={
        'credential_id':901, 'symbol':'BTC/USDT', 'side':'sell', 'market_type':'spot', 'quantity':2,
    })
    assert reached == [True]
    assert response.get_json()['msg'] != 'quickTrade.entryReasonRequired'


def test_live_agent_uses_the_same_required_reason_and_ledger(opening, full_app):
    _, admin, _, body, calls, reason = opening
    with pytest.raises(ValueError, match='entryReasonRequired'):
        agent_orders._place_live_order(body={**body, 'market':'Crypto', 'qty':2, 'entry_reason':''}, user_id=1)
    assert calls == []
    result = agent_orders._place_live_order(body={**body, 'market':'Crypto', 'qty':2}, user_id=1)
    assert result['trade_id'] == calls[0]
    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM qd_quick_trades WHERE user_id=1')
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]['client_order_id']
    assert parse_quick_trade_metadata(rows[0]['raw_result'])['entry_reason'] == reason
    client, _, _, raw_token = full_app
    response = client.get('/api/agent/v1/trading/quick-trades', headers={'Authorization':'Bearer '+raw_token})
    assert response.status_code == 200
    item = response.get_json()['data']['items'][0]
    assert item['entry_reason'] == reason
    assert 'raw_result' not in item


@pytest.mark.parametrize('value', [None, 12, [], {}, 'a'*501])
def test_reason_rejects_non_text_and_overlong_input(value):
    with pytest.raises(ValueError, match='entryReasonInvalid'):
        normalize_entry_reason(value, required=True)


def test_old_records_remain_readable_without_fabricated_reason():
    for raw in (None, '', 'broken', {}, {'_quick_trade':None}):
        assert parse_quick_trade_metadata(raw)['entry_reason'] == ''


@pytest.mark.parametrize('surface', ['web', 'agent'])
def test_result_update_failure_keeps_the_saved_entry_record_identity(opening, monkeypatch, surface):
    client, admin, headers, body, calls, reason = opening
    original = quick_trade._record_quick_trade
    def fail_only_completion(**kwargs):
        return None if kwargs.get('record_id') else original(**kwargs)
    monkeypatch.setattr(quick_trade, '_record_quick_trade', fail_only_completion)
    if surface == 'web':
        response = client.post('/api/quick-trade/place-order', headers=headers, json=body)
        assert response.status_code == 200
        result = response.get_json()['data']
    else:
        result = agent_orders._place_live_order(body={**body, 'market':'Crypto', 'qty':2}, user_id=1)
    assert result['trade_id'] == calls[0]
    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM qd_quick_trades WHERE user_id=1')
        rows = cur.fetchall()
    assert len(rows) == 1
    assert parse_quick_trade_metadata(rows[0]['raw_result'])['entry_reason'] == reason
