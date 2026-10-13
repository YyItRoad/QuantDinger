"""Send-only gateway tests; no real notifications are sent."""
from unittest.mock import Mock
import pytest
import requests
from app.services import agent_notifications as service, signal_notifier
from app.utils import agent_auth

PATH = '/api/agent/v1/notifications/send'
HEADERS = {'Authorization': 'Bearer qd_agent_notification_test'}
BODY = {'title': 'Alert', 'message': 'Private analysis', 'idempotency_key': 'event-1'}

@pytest.fixture
def sender(monkeypatch):
    token = {'id': 51, 'user_id': 7, 'scopes': 'NS', 'status': 'active', 'rate_limit_per_min': 60}
    for name, fn in {
        '_ensure_schema': lambda: None, '_lookup_token': lambda _: token,
        '_audit': lambda *a: None, '_touch_token_last_used': lambda *a: None,
        '_check_rate_limit': lambda *a: {'allowed': True, 'limit': 60, 'remaining': 59, 'reset': 123},
    }.items():
        monkeypatch.setattr(agent_auth, name, fn)
    monkeypatch.setattr(service, 'get_notification_settings', lambda uid: {
        'default_channels': ['webhook'], 'webhook_url': 'https://fixture.invalid/hook?secret=hidden',
        'webhook_token': 'hidden-bearer',
    })
    monkeypatch.setattr(service, '_reserve', lambda *a: ('reserved', 'hash'))
    monkeypatch.setattr(service, '_finish', Mock())
    post = Mock(return_value=Mock(status_code=200, text='{}'))
    monkeypatch.setattr(signal_notifier.requests, 'post', post)
    monkeypatch.setattr(signal_notifier.time, 'sleep', lambda _: None)
    return token, post

def test_delivery(client, sender):
    r = client.post(PATH, headers=HEADERS, json=BODY)
    assert r.status_code == 200 and r.json['data']['status'] == 'delivered'
    assert sender[1].call_args.kwargs['json']['message'] == BODY['message']
    assert sender[1].call_args.kwargs['headers']['Authorization'] == 'Bearer hidden-bearer'
    assert 'hidden' not in r.text

@pytest.mark.parametrize('scopes', ['R', 'N', 'W', 'T', 'R,W,B,N,C,T'])
def test_denied_scopes(client, sender, scopes):
    sender[0]['scopes'] = scopes
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 403
    sender[1].assert_not_called()

def test_no_alert_management(client, sender):
    for method, suffix in [('post', ''), ('delete', '/1')]:
        assert getattr(client, method)('/api/agent/v1/notifications/signal-alerts'+suffix, headers=HEADERS, json={}).status_code == 403

@pytest.mark.parametrize('change', [{'title': ''}, {'message': ' '}, {'title': 4}, {'title': 'x'*201}, {'message': 'x'*8001}, {'level': []}, {'level': 'urgent'}, {'idempotency_key': ''}, {'idempotency_key': 'x'*121}, {'url': 'https://evil.invalid'}, {'headers': {}}, {'token': 'private'}])
def test_bad_input(client, sender, change):
    assert client.post(PATH, headers=HEADERS, json={**BODY, **change}).status_code == 400
    sender[1].assert_not_called()

@pytest.mark.parametrize('state,code', [('duplicate', 200), ('conflict', 409), ('rate_limited', 429)])
def test_reservations(client, sender, monkeypatch, state, code):
    monkeypatch.setattr(service, '_reserve', lambda *a: (state, 'key'))
    r = client.post(PATH, headers=HEADERS, json=BODY)
    assert r.status_code == code
    assert r.json['data']['status'] == ('duplicate' if code == 200 else 'failed')
    sender[1].assert_not_called()

def test_no_config(client, sender, monkeypatch):
    monkeypatch.setattr(service, 'get_notification_settings', lambda _: {})
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 409
    sender[1].assert_not_called()

@pytest.mark.parametrize('failure', ['timeout', 'connection', 'upstream', 'unexpected'])
def test_safe_failures(client, sender, caplog, failure):
    if failure == 'upstream':
        sender[1].return_value = Mock(status_code=503, text='hidden-bearer Private analysis')
    else:
        sender[1].side_effect = {'timeout': requests.Timeout, 'connection': requests.ConnectionError, 'unexpected': RuntimeError}[failure]('hidden-bearer Private analysis')
    r = client.post(PATH, headers=HEADERS, json=BODY)
    assert r.status_code == 502
    assert 'hidden-bearer' not in caplog.text + r.text
    assert 'Private analysis' not in caplog.text + r.text

def test_optional_key(client, sender):
    assert client.post(PATH, headers=HEADERS, json={'title': 'hi', 'message': 'body'}).status_code == 200

def test_storage_fails_closed(client, sender, monkeypatch):
    monkeypatch.setattr(service, '_reserve', Mock(side_effect=RuntimeError('private')))
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 503
    sender[1].assert_not_called()


def test_audit_omits_all_caller_content(monkeypatch):
    from contextlib import contextmanager
    from flask import Flask, g
    cursor = Mock()
    db = Mock()
    db.cursor.return_value = cursor
    @contextmanager
    def connect():
        yield db
    monkeypatch.setattr(agent_auth, 'get_db_connection', connect)
    app = Flask(__name__)
    with app.test_request_context(PATH+'?private=secret-analysis', method='POST', json=BODY):
        g.agent_token = {'id': 1, 'user_id': 1}
        agent_auth._audit('NS', 200, {'status': 'delivered'}, 1)
    values = str(cursor.execute.call_args)
    assert 'Private analysis' not in values
    assert 'secret-analysis' not in values
    assert '<omitted>' in values


def test_mcp_to_gateway_in_process(client, sender, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio
    import importlib
    import sys
    import httpx
    monkeypatch.setenv('QUANTDINGER_BASE_URL', 'http://fixture.invalid')
    monkeypatch.setenv('QUANTDINGER_AGENT_TOKEN', 'qd_agent_notification_test')
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'static')
    sys.modules.pop('quantdinger_mcp.server', None)
    server = importlib.import_module('quantdinger_mcp.server')
    def respond(req):
        result = client.post(req.url.path, data=req.content, headers=dict(req.headers))
        return httpx.Response(result.status_code, json=result.json)
    with httpx.Client(base_url='http://fixture.invalid', headers=HEADERS, transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(server, '_client', transport)
        result = asyncio.run(server.mcp.call_tool('send_notification', BODY))
    import json
    assert json.loads(result[0].text)['status'] == 'delivered'
    assert sender[1].call_count == 1
