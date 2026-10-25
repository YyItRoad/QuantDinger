"""Full route registration and real authentication on an opt-in isolated database."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import pytest

from tests.test_trade_plan_management import card


@pytest.fixture
def full_app(monkeypatch):
    dsn = os.getenv('QD_PLAN_TEST_DSN')
    if not dsn:
        pytest.skip('Set QD_PLAN_TEST_DSN to a disposable PostgreSQL test server')
    schema = 'plan_app_' + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        cur.execute("SET TIME ZONE 'UTC'")
        migrations = Path(__file__).resolve().parents[1] / 'migrations'
        for name in ('init.sql', '20260917_market_state.sql', '20260917_market_state_schedule.sql'):
            cur.execute((migrations / name).read_text())

    # Replace only connection acquisition; all SQL, auth, scopes and audit remain real.
    class IsolatedPool:
        def getconn(self):
            return psycopg2.connect(dsn, options='-csearch_path=' + schema + ' -ctimezone=UTC')

        def putconn(self, conn, close=False):
            conn.close()

    from app.utils import db_postgres, agent_auth
    import app as app_module
    monkeypatch.setattr(db_postgres, '_get_connection_pool', lambda: IsolatedPool())
    monkeypatch.setattr(app_module, '_bootstrap_database', lambda: None)
    monkeypatch.setattr(agent_auth, '_schema_ready', False)
    monkeypatch.setenv('SKIP_STARTUP_HOOKS', '1')
    monkeypatch.setenv('SINGLE_USER_MODE', 'false')
    monkeypatch.setenv('TURNSTILE_SITE_KEY', '')
    monkeypatch.setenv('TURNSTILE_SECRET_KEY', '')
    from app.services.user_service import UserService
    from app.services import security_service
    monkeypatch.setattr(security_service, '_security_service', None)
    password = 'isolated-plan-test-password'
    hashed = UserService().hash_password(password)
    agent_raw = 'qd_agent_isolated_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_users(id,username,password_hash,role,password_changed_at) VALUES (1,'plan-test',%s,'user',NOW()),(2,'plan-viewer',%s,'viewer',NOW()),(3,'plan-admin',%s,'admin',NOW())", (hashed, hashed, hashed))
        cur.execute("INSERT INTO qd_agent_tokens(user_id,name,token_prefix,token_hash,scopes,rate_limit_per_min) VALUES (1,'isolated-plan','qd_agent_isolated',%s,'R,W',999)", (agent_auth._hash_token(agent_raw),))
    app = app_module.create_app('testing')
    app.config['TESTING'] = True
    try:
        yield app.test_client(), admin, password, agent_raw
    finally:
        admin.cursor().execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def login(client, username, password):
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result['code'] == 1, result
    return {'Authorization': 'Bearer ' + result['data']['token']}


def test_full_app_login_web_agent_roundtrip_and_viewer(full_app):
    client, admin, password, agent_raw = full_app
    from app.openapi import get_openapi_api
    from app.openapi.register import enrich_spec
    spec = enrich_spec(get_openapi_api(client.application).spec.to_dict())
    assert 'TradePlans' in {tag['name'] for tag in spec['tags']}
    assert spec['paths']['/api/trade-plans']['post']['responses']['200']['content']['application/json']['schema']['properties']['data']['$ref'].endswith('/TradePlanMutationResult')
    user = login(client, 'plan-test', password)
    viewer = login(client, 'plan-viewer', password)
    response = client.post('/api/trade-plans', headers=user, json={'request_id': 'real-web', 'snapshot': card()})
    assert response.status_code == 200, response.get_json()
    created = response.get_json()['data']
    pid = created['id']
    assert client.post('/api/trade-plans', headers=viewer, json={'request_id': 'viewer-write', 'snapshot': {'title': 'deny'}}).status_code == 403
    assert client.get('/api/trade-plans/' + pid, headers=viewer).status_code == 404
    assert client.get('/api/trade-plans', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    agent = {'Authorization': 'Bearer ' + agent_raw, 'Idempotency-Key': 'real-agent'}
    url = '/api/agent/v1/trade-plans/' + pid
    body = {'request_id': 'real-agent', 'expected_revision': 1, 'snapshot': card(title='Agent revision'), 'change_reason': 'roundtrip'}
    updated = client.post(url + '/versions', headers=agent, json=body)
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()['data']['version_no'] == 2
    assert client.get('/api/trade-plans/' + pid, headers=user).get_json()['data']['snapshot']['title'] == 'Agent revision'
    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM qd_agent_audit WHERE route=%s AND method='POST'", (url + '/versions',))
        audit = cur.fetchone()
        assert audit and audit['user_id'] == 1 and audit['scope_class'] == 'W' and audit['status_code'] == 200
        cur.execute("UPDATE qd_agent_tokens SET scopes='R'")
    assert client.post(url + '/copy', headers={**agent, 'Idempotency-Key': 'read-only'}, json={'request_id': 'read-only', 'expected_revision': 2}).status_code == 403
    with admin.cursor() as cur:
        cur.execute("UPDATE qd_agent_tokens SET scopes='R,W',instruments='ETH/USDT'")
    assert client.get(url, headers=agent).status_code == 404
    assert client.get('/api/agent/v1/trade-plans', headers=agent).get_json()['data']['total'] == 0


def test_existing_account_deletion_and_disabled_module(full_app, monkeypatch):
    client, admin, password, agent_raw = full_app
    user = login(client, 'plan-test', password)
    administrator = login(client, 'plan-admin', password)
    created = client.post('/api/trade-plans', headers=user, json={'request_id': 'to-delete', 'snapshot': card()}).get_json()['data']
    client.post('/api/trade-plans/' + created['id'] + '/copy', headers=user, json={'request_id': 'copy-delete', 'expected_revision': 1})
    from app.services.user_service import UserService
    assert UserService().delete_user(1)
    with admin.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM qd_trade_plan_versions WHERE user_id=1')
        assert cur.fetchone()[0] == 0
        cur.execute('SELECT COUNT(*) FROM qd_users WHERE id=3')
        assert cur.fetchone()[0] == 1
    assert client.get('/api/trade-plans', headers=user).status_code == 401
    monkeypatch.setenv('TRADE_PLAN_MANAGEMENT_ENABLED', 'false')
    assert client.get('/api/trade-plans', headers=administrator).status_code == 503
    assert client.get('/api/auth/info', headers=administrator).status_code == 200


def test_existing_analysis_lists_and_plan_evidence_roundtrip(full_app):
    client, admin, password, _ = full_app
    user = login(client, 'plan-test', password)
    for path in ('records', 'tasks'):
        response = client.get('/api/market-state/' + path, headers=user)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()['data']['items'] == []
    # Seed a stopped task and an explicitly synthetic result; do not run an analyzer.
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_market_state_tasks(user_id,market,symbol,market_type,timeframe,enabled) VALUES (1,'Crypto','BTC/USDT','spot','1d',FALSE) RETURNING id")
        task_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO qd_market_state_results
            (user_id,task_id,market,symbol,market_type,timeframe,bar_close_at,
             trend,structure,ma_state,position,momentum,phase,confidence,summary)
            VALUES (1,%s,'Crypto','BTC/USDT','spot','1d',NOW()-INTERVAL '1 day',
             'test','test','test','test','test','BASE',1,'Synthetic integration evidence')
            RETURNING id""", (task_id,))
        record_id = cur.fetchone()[0]
    records = client.get('/api/market-state/records?page_size=20', headers=user).get_json()['data']
    assert records['total'] == 1 and records['items'][0]['id'] == record_id
    created = client.post('/api/trade-plans', headers=user, json={
        'request_id': 'real-analysis-ref',
        'snapshot': card(research_refs=[{'type': 'market_state_result', 'id': record_id}]),
    })
    assert created.status_code == 200, created.get_json()
    plan = client.get('/api/trade-plans/' + created.get_json()['data']['id'], headers=user).get_json()['data']
    assert plan['snapshot']['research_refs'][0]['evidence_snapshot']['summary'] == 'Synthetic integration evidence'
