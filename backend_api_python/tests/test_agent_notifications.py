"""Send-only gateway tests; no real notifications are sent."""
import ast
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import requests
from app.services import agent_notifications as service, signal_notifier
from app.utils import agent_auth

PATH = '/api/agent/v1/notifications/send'
HEADERS = {'Authorization': 'Bearer qd_agent_notification_test', 'Idempotency-Key': 'event-1'}
BODY = {'title': 'Alert', 'message': 'Private analysis'}

@pytest.mark.parametrize('dialect', ['feishu', 'dingtalk', 'wecom', 'slack'])
@pytest.mark.parametrize('level', ['info', 'warning', 'critical'])
def test_vendor_notification_preserves_title_level_and_message(dialect, level):
    payload = {'event': 'agent.notification', 'title': 'NEAR 入场条件成立',
               'message': '请人工核对行情依据。', 'level': level}
    adapted = signal_notifier._adapt_payload_for_dialect(dialect, service._webhook_payload(payload))
    if dialect == 'feishu':
        text = adapted['content']['text']
    elif dialect == 'slack':
        text = adapted['text']
    else:
        text = adapted['text']['content']
    assert text == f"[{level.upper()}] {payload['title']}\n{payload['message']}"


def test_generic_notification_preserves_structured_payload():
    payload = {'event': 'agent.notification', 'title': 'Alert',
               'message': 'Body', 'level': 'critical'}
    adapted = signal_notifier._adapt_payload_for_dialect('generic', service._webhook_payload(payload))
    assert all(adapted[key] == value for key, value in payload.items())
    assert adapted['display']['plain'] == '[CRITICAL] Alert\nBody'


@pytest.fixture
def sender(monkeypatch):
    token = {'id': 51, 'user_id': 7, 'scopes': 'N', 'status': 'active', 'rate_limit_per_min': 60}
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
    reservations = {}
    def reserve(token_id, key):
        fingerprint = agent_auth._request_fingerprint()
        existing = reservations.get((token_id, key))
        if existing:
            return ('mismatch' if fingerprint != existing['request_hash'] else existing['status']), existing
        reservations[(token_id, key)] = {'request_hash': fingerprint, 'status': 'in_progress'}
        return 'reserved', None
    def complete(token_id, key, response):
        reservations[(token_id, key)].update(status='completed', response_body=response.get_json(),
                                             response_status=response.status_code)
    monkeypatch.setattr(agent_auth, '_reserve_idempotency', reserve)
    monkeypatch.setattr(agent_auth, '_complete_idempotency', complete)
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


@pytest.mark.parametrize('url,text_path', [
    ('https://open.feishu.cn/open-apis/bot/v2/hook/test', ('content', 'text')),
    ('https://oapi.dingtalk.com/robot/send?access_token=test', ('text', 'content')),
    ('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test', ('text', 'content')),
    ('https://hooks.slack.com/services/test', ('text',)),
])
def test_vendor_delivery_uses_existing_display_contract(client, sender, monkeypatch, url, text_path):
    monkeypatch.setattr(service, 'get_notification_settings', lambda uid: {
        'default_channels': ['webhook'], 'webhook_url': url,
    })
    result = client.post(PATH, headers=HEADERS, json={**BODY, 'level': 'warning'})
    assert result.status_code == 200
    text = sender[1].call_args.kwargs['json']
    for key in text_path:
        text = text[key]
    assert text == '[WARNING] Alert\nPrivate analysis'


@pytest.mark.parametrize('scopes', ['R', 'W', 'T', 'R,W,B,C,T'])
def test_denied_scopes(client, sender, scopes):
    sender[0]['scopes'] = scopes
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 403
    sender[1].assert_not_called()

@pytest.mark.parametrize('change', [{'title': ''}, {'message': ' '}, {'title': 4}, {'title': 'x'*201}, {'message': 'x'*8001}, {'level': []}, {'level': 'urgent'}, {'idempotency_key': 'body-key'}, {'url': 'https://evil.invalid'}, {'headers': {}}, {'token': 'private'}])
def test_bad_input(client, sender, change):
    assert client.post(PATH, headers=HEADERS, json={**BODY, **change}).status_code == 400
    sender[1].assert_not_called()

def test_existing_idempotency_replays_delivery(client, sender):
    first = client.post(PATH, headers=HEADERS, json=BODY)
    replay = client.post(PATH, headers=HEADERS, json=BODY)
    assert first.status_code == replay.status_code == 200
    assert replay.json == first.json
    assert replay.headers['Idempotent-Replayed'] == 'true'
    assert sender[1].call_count == 1


def test_existing_idempotency_rejects_changed_body(client, sender):
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 200
    result = client.post(PATH, headers=HEADERS, json={**BODY, 'message': 'Changed'})
    assert result.status_code == 409
    assert sender[1].call_count == 1


def test_existing_rate_limit_blocks_delivery(client, sender, monkeypatch):
    monkeypatch.setattr(agent_auth, '_check_rate_limit', lambda *a: {
        'allowed': False, 'limit': 60, 'remaining': 0, 'reset': 123})
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 429
    sender[1].assert_not_called()


def test_existing_idempotency_blocks_inflight_request(client, sender, monkeypatch):
    monkeypatch.setattr(agent_auth, '_reserve_idempotency', lambda *a: ('in_progress', None))
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 409
    sender[1].assert_not_called()


def test_existing_sender_retries_transient_failure(client, sender, monkeypatch):
    monkeypatch.setenv('SIGNAL_NOTIFY_TIMEOUT_SEC', '4.5')
    sender[1].side_effect = [Mock(status_code=503, text='{}'), Mock(status_code=200, text='{}')]
    result = client.post(PATH, headers=HEADERS, json=BODY)
    assert result.status_code == 200 and result.json['data']['status'] == 'delivered'
    assert sender[1].call_count == 2
    assert all(call.kwargs['timeout'] == 4.5 for call in sender[1].call_args_list)


def test_configuration_storage_failure_is_safe(client, sender, monkeypatch):
    monkeypatch.setattr(service, 'get_notification_settings', Mock(side_effect=RuntimeError('private')))
    result = client.post(PATH, headers=HEADERS, json=BODY)
    assert result.status_code == 503
    assert 'private' not in result.text
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

@pytest.mark.parametrize('key', [None, '', 'x'*121])
def test_existing_idempotency_requires_valid_header(client, sender, key):
    headers = {'Authorization': HEADERS['Authorization']}
    if key is not None:
        headers['Idempotency-Key'] = key
    assert client.post(PATH, headers=headers, json=BODY).status_code == 400
    sender[1].assert_not_called()


def test_existing_idempotency_fails_closed(client, sender, monkeypatch):
    monkeypatch.setattr(agent_auth, '_reserve_idempotency', Mock(side_effect=RuntimeError('unavailable')))
    assert client.post(PATH, headers=HEADERS, json=BODY).status_code == 503
    sender[1].assert_not_called()


def test_failed_delivery_replays_original_failure(client, sender):
    sender[1].side_effect = requests.Timeout('timeout')
    first = client.post(PATH, headers=HEADERS, json=BODY)
    replay = client.post(PATH, headers=HEADERS, json=BODY)
    assert first.status_code == replay.status_code == 502
    assert replay.json == first.json
    assert replay.headers['Idempotent-Replayed'] == 'true'
    assert sender[1].call_count == 1


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
        agent_auth._audit('N', 200, {'status': 'delivered'}, 1)
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
        result = asyncio.run(server.mcp.call_tool('send_notification', {**BODY, 'idempotency_key': 'mcp-event-1'}))
    import json
    assert json.loads(result[0].text)['status'] == 'delivered'
    assert sender[1].call_count == 1


def test_notification_send_uses_existing_scope_and_idempotency_contract():
    operation = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/agent/agent-openapi.json").read_text(encoding="utf-8")
    )["paths"]["/api/agent/v1/notifications/send"]["post"]
    assert operation["x-scope-class"] == "N"
    assert any(
        item["in"] == "header" and item["name"] == "Idempotency-Key" and item["required"]
        for item in operation["parameters"]
    )
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"title", "message", "level"}
    route_source = Path(__file__).resolve().parents[2] / "backend_api_python/app/routes/agent_v1/notification_send.py"
    route = next(
        node for node in ast.parse(route_source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "send_notification"
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name) and decorator.func.id == "agent_required"
        and isinstance(decorator.args[0], ast.Name) and decorator.args[0].id == "SCOPE_N"
        for decorator in route.decorator_list
    )
