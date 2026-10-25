"""Web/Gateway contract and MCP integration against the same disposable store."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from flask import Flask
from tests.test_trade_plan_management import database, card
from app.utils import agent_auth, auth


@pytest.fixture
def plan_client(database, monkeypatch):
    service, connect, queries = database
    from app.trade_plans import http
    from app.trade_plans.routes import blp
    from app.routes.agent_v1 import register
    monkeypatch.setattr(http, 'PlanService', lambda scope=None: type(service)(scope=scope))
    monkeypatch.setattr(auth, 'verify_token', lambda token: {'user_id': 1, '_verified_user_role': 'user', '_verified_username': 'test'})
    token = {'id': 9, 'user_id': 1, 'scopes': 'R,W', 'markets': '*', 'instruments': '*', 'status': 'active', 'rate_limit_per_min': 999}
    monkeypatch.setattr(agent_auth, '_ensure_schema', lambda: None)
    monkeypatch.setattr(agent_auth, '_lookup_token', lambda raw: token)
    monkeypatch.setattr(agent_auth, '_check_rate_limit', lambda *args: {'allowed': True, 'limit': 999, 'remaining': 998, 'reset_at': 1})
    monkeypatch.setattr(agent_auth, '_touch_token_last_used', lambda *args: None)
    monkeypatch.setattr(agent_auth, '_audit', lambda *args: None)
    monkeypatch.setattr(agent_auth, 'get_db_connection', connect)
    with connect() as db:
        db.cursor().execute('''CREATE TABLE qd_agent_idempotency(agent_token_id INTEGER,method TEXT,route TEXT,
            idempotency_key TEXT,request_hash TEXT,status TEXT,response_body JSONB,response_status INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW(),updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(agent_token_id,method,route,idempotency_key))''')
    app = Flask(__name__); app.config['TESTING'] = True
    app.register_blueprint(blp, url_prefix='/api/trade-plans'); register(app)
    return app.test_client(), token


def headers(key=None, agent=False):
    return {'Authorization': 'Bearer qd_agent_test' if agent else 'Bearer test-jwt', **({'Idempotency-Key': key} if key else {})}


def test_web_agent_shared_versions_replay_and_permissions(plan_client):
    client, token = plan_client
    key = str(uuid4())
    row = client.post('/api/trade-plans', json={'request_id': key, 'snapshot': card()}, headers=headers()).get_json()['data']
    pid = row['id']; base = '/api/agent/v1/trade-plans/'+pid
    agent = client.get(base, headers=headers(agent=True)); assert agent.status_code == 200
    assert agent.get_json()['code'] == 0 and agent.get_json()['data']['id'] == pid
    key = str(uuid4()); body = {'request_id': key, 'snapshot': card(title='MCP revised'), 'change_reason': 'updated', 'expected_revision': 1}
    response = client.post(base+'/versions', json=body, headers=headers(key, True))
    assert response.status_code == 200
    assert client.post(base+'/versions', json=body, headers=headers(key, True)).get_json() == response.get_json()
    web = client.get('/api/trade-plans/'+pid, headers=headers()).get_json()['data']
    assert web['snapshot']['title'] == 'MCP revised' and web['version_no'] == 2
    token['scopes'] = 'R'
    assert client.post(base+'/copy', json={'request_id': 'denied', 'expected_revision': 2}, headers=headers('denied', True)).status_code == 403
    token['scopes'] = 'R,W'; token['instruments'] = 'ETH/USDT'
    assert client.get(base, headers=headers(agent=True)).status_code == 404
    assert client.get('/api/agent/v1/trade-plans', headers=headers(agent=True)).get_json()['data']['total'] == 0
    assert client.get('/api/agent/v1/trade-plans/requests/'+key, headers=headers(agent=True)).status_code == 404


def test_contract_errors_and_ai_signal(plan_client):
    client, token = plan_client
    assert client.get('/api/trade-plans').status_code == 401
    for snapshot in [{'title': 'x', 'direction': []}, {'title': 'x', 'risk_inputs': {'quantity': 'NaN'}}, {'title': 'x', 'analysis_timeframe': []}]:
        response = client.post('/api/trade-plans', json={'request_id': str(uuid4()), 'snapshot': snapshot}, headers=headers())
        assert response.status_code == 400
    assert client.get('/api/trade-plans?page_size=101', headers=headers()).status_code == 400
    assert client.get('/api/trade-plans?_t=123', headers=headers()).status_code == 200
    response = client.post('/api/agent/v1/trade-plans', json={'request_id': 'different', 'snapshot': {'title': 'x'}}, headers=headers('key', True))
    assert response.status_code == 400


@pytest.mark.parametrize('agent', [False, True])
def test_web_and_gateway_reject_selected_instrument_change(plan_client, agent):
    client, _ = plan_client
    base = '/api/agent/v1/trade-plans' if agent else '/api/trade-plans'
    key = str(uuid4())
    row = client.post(base, json={'request_id':key,'snapshot':card()}, headers=headers(key,agent)).get_json()['data']
    snapshot = card(); snapshot['instrument']['symbol'] = 'ETH/USDT'
    key = str(uuid4())
    response = client.post(base+'/'+row['id']+'/versions', json={
        'request_id':key,'expected_revision':row['row_revision'],'snapshot':snapshot,'change_reason':'wrong symbol',
    }, headers=headers(key,agent))
    assert response.status_code == 422
    assert (response.get_json().get('message') or response.get_json().get('msg')) == 'plan_instrument_immutable'
    stored = client.get(base+'/'+row['id'], headers=headers(agent=agent)).get_json()['data']
    assert stored['version_no'] == 1 and stored['snapshot']['instrument']['symbol'] == 'BTC/USDT'


def test_mcp_wire_to_gateway_and_oauth(plan_client, monkeypatch):
    import httpx
    import importlib
    import asyncio
    from mcp.server.auth.provider import AccessToken
    from quantdinger_mcp import oauth_auth
    from quantdinger_mcp.tool_contract import WRITE_TOOLS, PLAN_WRITE_TOOLS
    client, token = plan_client
    monkeypatch.setenv('QUANTDINGER_BASE_URL', 'http://test.invalid')
    monkeypatch.setenv('QUANTDINGER_AGENT_TOKEN', 'qd_agent_test')
    server = importlib.import_module('quantdinger_mcp.server')
    def forward(request):
        response = client.open(request.url.path, method=request.method, query_string=request.url.query.decode(), headers=dict(request.headers), data=request.content)
        return httpx.Response(response.status_code, json=response.get_json())
    monkeypatch.setattr(server, '_client', httpx.Client(base_url='http://test.invalid', headers={'Authorization': 'Bearer qd_agent_test'}, transport=httpx.MockTransport(forward)))
    # The real MCP transport helper and real Gateway reach the same service.
    result = server.create_trade_plan(card(), 'mcp-create')
    assert not result.get('error'), result
    # MCP helper unwraps data; preserve the actual helper's response shape.
    row = result.get('data', result)
    assert client.get('/api/trade-plans/'+row['id'], headers=headers()).get_json()['data']['id'] == row['id']
    changed = card(); changed['instrument']['symbol'] = 'ETH/USDT'
    rejected = server.revise_trade_plan(row['id'], changed, 'change instrument', row['row_revision'], 'mcp-wrong-instrument')
    assert rejected['error'] and rejected['status'] == 422
    assert rejected['body']['message'] == 'plan_instrument_immutable'
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'hybrid')
    monkeypatch.setattr(oauth_auth, 'get_access_token', lambda: AccessToken(token='fake', client_id='test', scopes=['quantdinger.read']))
    assert oauth_auth.authorize_tool_call('create_trade_plan', WRITE_TOOLS) is not None
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        if tool.name in PLAN_WRITE_TOOLS:
            assert tool.annotations.idempotentHint is True and tool.annotations.readOnlyHint is False


def test_disabled_plan_module_does_not_write(plan_client, monkeypatch):
    client, token = plan_client
    monkeypatch.setenv('TRADE_PLAN_MANAGEMENT_ENABLED', 'false')
    response = client.post('/api/trade-plans', json={'request_id':'disabled','snapshot':{'title':'unused'}}, headers=headers())
    assert response.status_code == 503 and response.get_json()['msg'] == 'plan_feature_disabled'


def test_storage_failure_is_explicit(plan_client, monkeypatch):
    import psycopg2
    from app.trade_plans.service import PlanService
    client, _ = plan_client
    def unavailable(*args):
        raise psycopg2.OperationalError('isolated test unavailable')
    monkeypatch.setattr(PlanService, 'listing', unavailable)
    response = client.get('/api/trade-plans', headers=headers())
    assert response.status_code == 503
    assert response.get_json()['msg'] == 'plan_storage_unavailable'
