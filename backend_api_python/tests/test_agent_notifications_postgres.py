"""Real PostgreSQL reservation tests in an isolated schema; no network push."""
import os
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.services import agent_notifications as service

@pytest.fixture
def database(monkeypatch):
    dsn = os.getenv('TEST_NOTIFICATION_DATABASE_URL')
    if not dsn:
        pytest.skip('Set TEST_NOTIFICATION_DATABASE_URL to an isolated test PostgreSQL')
    import psycopg2
    from psycopg2.extras import RealDictCursor
    schema = 'notify_test_' + uuid.uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute('CREATE TABLE qd_users (id INTEGER PRIMARY KEY)')
        cur.execute('INSERT INTO qd_users VALUES (1), (2)')
        cur.execute((Path(__file__).parents[1] / 'migrations/20261013_agent_notifications.sql').read_text())
    @contextmanager
    def connect():
        conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(service, 'get_db_connection', connect)
    yield connect
    with admin.cursor() as cur:
        cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.close()


def payload(key='event'):
    return service.validate_notification({'title': 'Title', 'message': 'Body', 'idempotency_key': key})


def test_concurrent_user_deduplication(database):
    with ThreadPoolExecutor(max_workers=5) as pool:
        states = list(pool.map(lambda _: service._reserve(1, payload())[0], range(5)))
    assert states.count('reserved') == 1
    assert states.count('duplicate') == 4
    assert service._reserve(2, payload())[0] == 'reserved'
    assert service._reserve(1, {**payload(), 'message': 'different'})[0] == 'conflict'


def test_rate_limit_and_ttl(database):
    for i in range(10):
        assert service._reserve(1, payload(str(i)))[0] == 'reserved'
    assert service._reserve(1, payload('overflow'))[0] == 'rate_limited'
    assert service._reserve(1, payload('0'))[0] == 'duplicate'
    with database() as db:
        with db.cursor() as cur:
            cur.execute("UPDATE qd_agent_notification_sends SET expires_at = NOW() - INTERVAL '1 second'")
        db.commit()
    assert service._reserve(1, payload('0'))[0] == 'reserved'


def test_failed_and_inflight_reservations_never_resend(database):
    _, key = service._reserve(1, payload())
    service._finish(1, key, 'failed')
    assert service._reserve(1, payload())[0] == 'duplicate'
    with database() as db:
        with db.cursor() as cur:
            cur.execute('SELECT * FROM qd_agent_notification_sends')
            row = cur.fetchone()
    assert row['status'] == 'failed'
    assert 'Body' not in str(row) and 'Title' not in str(row)
