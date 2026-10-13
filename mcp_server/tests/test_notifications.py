"""Send-only notification discovery and authorization."""
import asyncio
import importlib
import json
import sys
from unittest.mock import Mock
import pytest
from mcp.server.auth.provider import AccessToken
from quantdinger_mcp import oauth_auth
from quantdinger_mcp.tool_contract import WRITE_TOOLS

@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv('QUANTDINGER_BASE_URL', 'http://fixture.invalid')
    monkeypatch.setenv('QUANTDINGER_AGENT_TOKEN', 'qd_agent_fixture')
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'static')
    sys.modules.pop('quantdinger_mcp.server', None)
    return importlib.import_module('quantdinger_mcp.server')

def test_discovery(server, monkeypatch):
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == 'send_notification')
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
    assert set(tool.inputSchema['properties']) == {'title', 'message', 'level', 'idempotency_key'}
    post = Mock(return_value={'status': 'delivered'})
    monkeypatch.setattr(server, '_post', post)
    result = asyncio.run(server.mcp.call_tool('send_notification', {'title': 'Alert', 'message': 'Body'}))
    assert json.loads(result[0].text)['status'] == 'delivered'
    assert post.call_args.args == ('/api/agent/v1/notifications/send',)
    assert post.call_args.kwargs['json']['level'] == 'info'

@pytest.mark.parametrize('scope,denied', [('quantdinger.read', True), ('quantdinger.write', True), ('quantdinger.trade', True), ('quantdinger.notify', False)])
def test_scope(monkeypatch, scope, denied):
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'hybrid')
    monkeypatch.setattr(oauth_auth, 'get_access_token', lambda: AccessToken(token='private', client_id='test', scopes=[scope]))
    result = oauth_auth.authorize_tool_call('send_notification', WRITE_TOOLS)
    assert (result is not None) == denied
    if denied:
        assert result.structuredContent['status'] == 403
    else:
        for name in ['create_signal_alert', 'delete_signal_alert']:
            assert oauth_auth.authorize_tool_call(name, WRITE_TOOLS) is not None
    assert oauth_auth.required_scope_for_tool('get_price', WRITE_TOOLS) == 'quantdinger.read'

def test_no_token_logs(capsys):
    verifier = oauth_auth.HybridTokenVerifier(static_token='secret-static', oauth=Mock())
    assert asyncio.run(verifier.verify_token('secret-static')) is not None
    captured = capsys.readouterr()
    assert 'secret-static' not in captured.out + captured.err


def test_oauth_verification_preserves_claim_guards(monkeypatch, capsys):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = oauth_auth.HybridAuthConfig('qd_agent_oauth', 'https://issuer.invalid/', 'https://resource.invalid/mcp', 'https://issuer.invalid/jwks', 'owner')
    verifier = oauth_auth.OAuthJWTVerifier(config)
    verifier._jwks = Mock()
    verifier._jwks.get_signing_key_from_jwt.return_value = Mock(key=key.public_key())
    claims = {'iss': config.issuer, 'aud': config.audience, 'sub': 'owner', 'iat': int(time.time()), 'exp': int(time.time())+60, 'scope': 'quantdinger.read quantdinger.notify'}
    token = jwt.encode(claims, key, algorithm='RS256')
    result = verifier._verify_sync(token)
    assert result.subject == 'owner' and 'quantdinger.notify' in result.scopes
    for change in [{'sub': 'other'}, {'aud': 'wrong'}, {'iss': 'wrong'}, {'exp': 1}]:
        assert verifier._verify_sync(jwt.encode({**claims, **change}, key, algorithm='RS256')) is None
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_protocol_scope_denial_never_forwards(server, monkeypatch):
    monkeypatch.setenv('QUANTDINGER_MCP_AUTH_MODE', 'hybrid')
    monkeypatch.setattr(oauth_auth, 'get_access_token', lambda: AccessToken(token='private', client_id='test', scopes=['quantdinger.read']))
    post = Mock()
    monkeypatch.setattr(server, '_post', post)
    result = asyncio.run(server.mcp.call_tool('send_notification', {'title': 'Alert', 'message': 'Body'}))
    assert result.isError and result.structuredContent['status'] == 403
    post.assert_not_called()
