"""Archive reads through real Agent authentication and the MCP boundary."""
from unittest.mock import Mock
import asyncio
import importlib
import json
import sys

import pytest
from app.routes.agent_v1 import position_management_history as routes
from app.services.live_trading import position_management_history as history
from app.utils import agent_auth

BASE = '/api/agent/v1/trading/position-management/trade-history'
HEADERS = {'Authorization': 'Bearer qd_agent_history_test'}


@pytest.fixture
def reader(monkeypatch):
    token = {'id': 51, 'user_id': 7, 'scopes': 'R', 'status': 'active', 'rate_limit_per_min': 60}
    audit = Mock()
    for name, fn in {
        '_ensure_schema': lambda: None, '_lookup_token': lambda _: token,
        '_audit': audit, '_touch_token_last_used': lambda *a: None,
        '_check_rate_limit': lambda *a: {'allowed': True, 'limit': 60, 'remaining': 59, 'reset': 123},
    }.items():
        monkeypatch.setattr(agent_auth, name, fn)
    listing = Mock(return_value={'items': [{'id': 11, 'credential_id': 2, 'entry_reason': 'saved',
                                            'user_id': 7, 'encrypted_config': 'hidden'}],
                                 'total': 1, 'page': 1, 'page_size': 20})
    detail = Mock(return_value={'id': 11, 'credential_id': 2, 'fee_status': 'pending',
                                'exit_reason': 'saved', 'user_id': 7, 'new_private_field': 'hidden'})
    monkeypatch.setattr(history, 'list_position_management_trade_history', listing)
    monkeypatch.setattr(history, 'get_position_management_trade_history', detail)
    return token, listing, detail, audit


@pytest.fixture
def archive_client(client):
    return client


def test_account_list_and_detail_use_identity_and_public_fields(archive_client, reader):
    r = archive_client.get(BASE, query_string={'credential_id': 2, 'symbol': 'BTC/USDT',
        'market_type': 'swap', 'position_side': 'long', 'source_strategy_id': 5,
        'start_time': '2026-10-01T00:00:00Z', 'user_id': 99}, headers=HEADERS)
    assert r.status_code == 200 and r.json['code'] == 0
    reader[1].assert_called_once_with(user_id=7, credential_id=2, page=1, page_size=20,
        symbol='BTC/USDT', market_type='swap', position_side='long', source_strategy_id=5,
        start_time='2026-10-01T00:00:00Z')
    assert r.json['data']['items'][0]['entry_reason'] == 'saved'
    assert 'hidden' not in r.text and 'user_id' not in r.text
    r = archive_client.get(BASE+'/11?credential_id=2', headers=HEADERS)
    assert r.status_code == 200 and r.json['data']['fee_status'] == 'pending'
    reader[2].assert_called_once_with(user_id=7, credential_id=2, history_id=11)
    assert 'hidden' not in r.text and 'user_id' not in r.text
    assert reader[3].call_count == 2


@pytest.mark.parametrize('path', [BASE, BASE+'/11'])
@pytest.mark.parametrize('scope', ['W', 'T', 'N', 'B'])
def test_read_permission_required(archive_client, reader, path, scope):
    reader[0]['scopes'] = scope
    assert archive_client.get(path+'?credential_id=2', headers=HEADERS).status_code == 403
    reader[1].assert_not_called()
    reader[2].assert_not_called()


def test_authentication_and_rate_limit(archive_client, reader, monkeypatch):
    assert archive_client.get(BASE+'?credential_id=2').status_code == 401
    monkeypatch.setattr(agent_auth, '_check_rate_limit', lambda *a: {'allowed': False, 'limit': 60, 'reset': 123})
    assert archive_client.get(BASE+'?credential_id=2', headers=HEADERS).status_code == 429
    reader[1].assert_not_called()


@pytest.mark.parametrize('query', ['', 'credential_id=0', 'credential_id=abc', 'credential_id=-1',
    'credential_id=2&page=0', 'credential_id=2&page_size=101', 'credential_id=2&page_size=oops',
    'credential_id=2&market_type=invalid', 'credential_id=2&position_side=both',
    'credential_id=2&symbol=', 'credential_id=2&source_strategy_id=0'])
def test_bad_list_parameters(archive_client, reader, query):
    assert archive_client.get(BASE+'?'+query, headers=HEADERS).status_code == 400
    reader[1].assert_not_called()


def test_detail_requires_account_and_uniform_missing_response(archive_client, reader):
    assert archive_client.get(BASE+'/11', headers=HEADERS).status_code == 400
    reader[2].return_value = None
    for cid in (2, 99):
        r = archive_client.get(BASE+f'/11?credential_id={cid}', headers=HEADERS)
        assert r.status_code == 404 and r.json['message'] == 'Trade history not found'


def test_safe_database_failure(archive_client, reader):
    for index, path in ((1, BASE), (2, BASE+'/11')):
        reader[index].side_effect = RuntimeError('database-password')
        r = archive_client.get(path+'?credential_id=2', headers=HEADERS)
        assert r.status_code == 500 and 'database-password' not in r.text


def test_mcp_through_http_gateway(archive_client, reader, monkeypatch):
    pytest.importorskip('mcp')
    import httpx
    monkeypatch.setenv('QUANTDINGER_BASE_URL', 'http://fixture.invalid')
    monkeypatch.setenv('QUANTDINGER_AGENT_TOKEN', 'qd_agent_history_test')
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'static')
    sys.modules.pop('quantdinger_mcp.server', None)
    server = importlib.import_module('quantdinger_mcp.server')

    def respond(req):
        result = archive_client.get(str(req.url).split('fixture.invalid', 1)[1], headers=dict(req.headers))
        return httpx.Response(result.status_code, json=result.json)

    with httpx.Client(base_url='http://fixture.invalid', headers=HEADERS,
                      transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(server, '_client', transport)
        for tool, args in [('list_position_management_trade_history', {'credential_id': 2}),
                           ('get_position_management_trade_history', {'credential_id': 2, 'history_id': 11})]:
            result = asyncio.run(server.mcp.call_tool(tool, args))
            assert 'hidden' not in result[0].text and json.loads(result[0].text)
        reader[2].return_value = None
        denied = asyncio.run(server.mcp.call_tool('get_position_management_trade_history',
                             {'credential_id': 99, 'history_id': 11}))
        assert denied.isError and 'Trade history not found' in denied.content[0].text
    assert reader[1].call_count == 1 and reader[2].call_count == 2
