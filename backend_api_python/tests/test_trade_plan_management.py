"""Strict contracts and opt-in disposable PostgreSQL tests (QD_PLAN_TEST_DSN)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import pytest
from app.trade_plans import repository
from app.trade_plans.service import PlanService
from app.trade_plans.permissions import PlanScope
from app.trade_plans.validation import PlanError, check, validate_snapshot

NOW = datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
MIGRATION = Path(__file__).resolve().parents[1] / 'migrations/20261023_trade_plan_management.sql'


def card(**changes):
    return {**dict(title='test plan', instrument={'market': 'Crypto', 'symbol': 'BTC/USDT', 'market_type': 'spot', 'settlement_ccy': 'USDT'}, direction='long', primary_strategy='retest', hypothesis='trend', entry_trigger='retest holds', invalidation='breakdown', exit_rules='target or stop', wait_conditions='no confirmation', evidence='closed bar', analysis_timeframe='1d', decision_timeframe='4h', risk_inputs={'entry_price': '100', 'stop_price': '95', 'quantity': '2', 'currency': 'USDT'}), **changes}


def test_risk_models_and_boundaries():
    value = check(validate_snapshot(card()))
    assert value['one_r'] == '10' and value['excludes_costs']
    short = card(direction='short', instrument={'market': 'Crypto', 'symbol': 'BTC/USDT', 'market_type': 'swap'}, risk_inputs={'model': 'linear', 'entry_price': '100', 'stop_price': '105', 'risk_budget': '10', 'target_price': '90', 'currency': 'USDT'})
    assert check(validate_snapshot(short))['target_reward_risk'] == '2'
    for patch, error in [({'stop_price': '101'}, 'invalid_stop_direction'), ({'target_price': '90'}, 'invalid_target_direction'), ({'currency': 'USD'}, 'currency_mismatch')]:
        sample = card(); sample['risk_inputs'].update(patch)
        assert check(validate_snapshot(sample))['risk_error'] == error
    assert check(validate_snapshot(card(direction='watch')))['risk_status'] == 'not_applicable'
    assert check(validate_snapshot(card(direction='short')))['risk_status'] == 'unsupported'


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0', True, '1e999', '0.0000000000000000001'])
def test_invalid_risk_numbers(value):
    sample = card(); sample['risk_inputs']['quantity'] = value
    with pytest.raises(PlanError): validate_snapshot(sample)


@pytest.mark.parametrize('changes', [{'title': ''}, {'title': 'x'*121}, {'unknown': True}, {'expires_at': '2026-10-23'}, {'retrospective': 'false'}, {'research_refs': [{'type': 'url', 'url': 'javascript:alert(1)'}]}])
def test_strict_contract(changes):
    with pytest.raises(PlanError): validate_snapshot(card(**changes))


def test_quantity_and_budget_are_exclusive():
    sample = card()
    sample['risk_inputs']['risk_budget'] = '10'
    with pytest.raises(PlanError, match='quantity_or_budget_exclusive'):
        validate_snapshot(sample)


@pytest.fixture
def database(monkeypatch):
    dsn = os.getenv('QD_PLAN_TEST_DSN')
    if not dsn: pytest.skip('Set QD_PLAN_TEST_DSN to a disposable PostgreSQL test server')
    schema, queries = 'plan_test_'+uuid4().hex, []
    admin = psycopg2.connect(dsn); admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        cur.execute('CREATE TABLE qd_users(id INTEGER PRIMARY KEY); INSERT INTO qd_users VALUES (1),(2)')
        cur.execute('CREATE TABLE qd_market_state_results(id INTEGER PRIMARY KEY,user_id INTEGER,market TEXT,symbol TEXT,exchange_id TEXT,market_type TEXT,instrument_id TEXT,timeframe TEXT,bar_close_at TIMESTAMPTZ,trend TEXT,structure TEXT,summary TEXT)')
        cur.execute(MIGRATION.read_text())
        cur.execute((MIGRATION.parent / '20261024_trade_plan_records.sql').read_text())
    class Cursor(RealDictCursor):
        def execute(self, query, vars=None):
            queries.append(str(query)); return super().execute(query, vars)
    @contextmanager
    def connect():
        db = psycopg2.connect(dsn, cursor_factory=Cursor)
        try:
            db.cursor().execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            yield db
            db.commit()
        except Exception: db.rollback(); raise
        finally: db.close()
    monkeypatch.setattr(repository, 'get_db_transaction', connect)
    try: yield PlanService(now=lambda: NOW), connect, queries
    finally:
        admin.cursor().execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema))); admin.close()


def create(service, snapshot=None, request_id=None, user=1):
    result = service.mutate(user, 'create', {'request_id': request_id or str(uuid4()), 'snapshot': snapshot or {'title': 'draft'}})
    return service.read(user, result['id'])


def change(service, row, action, **data):
    result = service.mutate(1, action, {'request_id': str(uuid4()), 'expected_revision': row['row_revision'], **data}, row['id'])
    return service.read(1, result['id'])


def legacy_version(service, row, snapshot):
    # Preserve coverage for mixed-identity records created before identity locking.
    with service.repo.transaction() as cur:
        service.repo.new_version(cur, 1, row['id'], str(uuid4()), row['version_no']+1,
                                 snapshot, 'legacy-fixture', 'legacy fixture', 'web', '1')
        service.repo.update(cur, 1, row['id'], row['lifecycle'])
    return service.read(1, row['id'])


@pytest.mark.parametrize('key,value', [('market', 'USStock'), ('symbol', 'ETH/USDT'),
    ('market_type', 'swap'), ('exchange_id', 'okx'), ('instrument_id', 'ETHUSDT'), ('settlement_ccy', 'USD')])
def test_selected_instrument_cannot_change_or_be_cleared(database, key, value):
    service, _, _ = database
    identity = {'market':'Crypto', 'symbol':'BTC/USDT', 'market_type':'spot',
                'exchange_id':'binance', 'instrument_id':'BTCUSDT', 'settlement_ccy':'USDT'}
    row = create(service, card(instrument=identity))
    for updated in (value, ''):
        snapshot = deepcopy(row['snapshot']); snapshot['instrument'][key] = updated
        with pytest.raises(PlanError, match='plan_instrument_immutable') as error:
            change(service, row, 'revise', snapshot=snapshot, change_reason='identity edit')
        assert error.value.status == 422
    assert service.read(1, row['id'])['version_no'] == 1


def test_title_only_draft_selects_instrument_once_and_can_revise_ideas(database):
    service, _, _ = database
    draft = create(service)
    row = change(service, draft, 'revise', snapshot=card(), change_reason='first selection')
    row = change(service, row, 'state', lifecycle='observing', reason='observe')
    snapshot = deepcopy(row['snapshot']); snapshot['hypothesis'] = 'updated idea'
    snapshot['instrument']['exchange_id'] = ''
    revised = change(service, row, 'revise', snapshot=snapshot, change_reason='new view')
    assert revised['snapshot']['hypothesis'] == 'updated idea'
    assert revised['version_no'] == 3 and revised['signal_status'] == 'unassessed'
    assert service.history(1, row['id'], 'versions')['items'][-1]['snapshot']['title'] == 'draft'


def test_analysis_reference_must_match_instrument(database):
    service, connect, _ = database
    with connect() as db:
        db.cursor().execute("INSERT INTO qd_market_state_results(id,user_id,market,symbol,exchange_id,market_type,summary) VALUES(5,1,'Crypto','ETH/USDT','okx','spot','ETH analysis')")
    for identity in ({'market':'Crypto','symbol':'BTC/USDT','exchange_id':'okx','market_type':'spot'},
                     {'market':'Crypto','symbol':'ETH/USDT','exchange_id':'binance','market_type':'spot'},
                     {'market':'Crypto','symbol':'ETH/USDT','exchange_id':'okx','market_type':'swap'}):
        with pytest.raises(PlanError, match='research_instrument_mismatch'):
            create(service, card(instrument=identity, research_refs=[{'type':'market_state_result','id':5}]))
    assert service.listing(1, {})['total'] == 0


def test_lifecycle_versions_signal_and_no_execution(database):
    service, _, queries = database
    row = create(service)
    assert row['checks']['card_status'] == 'incomplete' and row['trade_status'] == 'not_linked'
    with pytest.raises(PlanError, match='card_incomplete'): change(service, row, 'state', lifecycle='observing', reason='watch')
    row = change(service, row, 'revise', snapshot=card(), change_reason='complete card')
    first = service.history(1, row['id'], 'versions')['items'][-1]
    row = change(service, row, 'state', lifecycle='observing', reason='watch')
    row = change(service, row, 'decision', status='confirmed', reason='manual evidence', observed_at=NOW.isoformat())
    assert row['signal_status'] == 'confirmed'
    row = change(service, row, 'revise', snapshot=card(entry_trigger='new trigger'), change_reason='new condition')
    assert row['signal_status'] == 'unassessed'
    assert service.history(1, row['id'], 'versions', first['id']) == first
    row = change(service, row, 'state', lifecycle='cancelled', reason='view changed')
    with pytest.raises(PlanError, match='plan_terminal'): change(service, row, 'revise', snapshot=card(), change_reason='reopen')
    copied = change(service, row, 'copy')
    assert copied['id'] != row['id'] and copied['parent_plan_id'] == row['id'] and copied['lifecycle'] == 'draft'
    writes = [q.strip().upper() for q in queries if q.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    assert writes and all(any(n in q for n in ('QD_TRADE_PLANS', 'QD_TRADE_PLAN_')) for q in writes)
    assert not any(n in ' '.join(writes) for n in ('PENDING_ORDERS', 'QD_STRATEGY_COMMANDS', 'QD_STRATEGY_POSITIONS'))


def test_database_immutability_and_tenant_constraints(database):
    service, connect, _ = database
    row = create(service, card())
    for table in ('qd_trade_plan_versions', 'qd_trade_plan_events'):
        for query in ('UPDATE '+table+' SET source=source', 'DELETE FROM '+table):
            with pytest.raises(psycopg2.IntegrityError):
                with connect() as db: db.cursor().execute(query)
    with pytest.raises(psycopg2.IntegrityError):
        with connect() as db: db.cursor().execute('INSERT INTO qd_trade_plans(id,user_id,parent_plan_id) VALUES (%s,2,%s)', (str(uuid4()), row['id']))
    with pytest.raises(PlanError): service.read(2, row['id'])


def test_concurrent_revision_and_request_replay(database):
    service, _, _ = database
    row, barrier = create(service), Barrier(2)
    def revise(index):
        barrier.wait()
        try: return change(service, row, 'revise', snapshot={'title': f'v{index}'}, change_reason='race')['row_revision']
        except PlanError as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool: result = list(pool.map(revise, range(2)))
    assert sorted(map(str, result)) == ['2', 'revision_conflict']
    barrier = Barrier(4)
    def repeat(_):
        barrier.wait(); return create(service, {'title': 'same'}, 'concurrent-create')['id']
    with ThreadPoolExecutor(max_workers=4) as pool: ids = list(pool.map(repeat, range(4)))
    assert len(set(ids)) == 1 and service.listing(1, {})['total'] == 2
    assert len(service.history(1, row['id'], 'versions')['items']) == 2


def test_replay_conflict_rollback_and_lost_response(database, monkeypatch):
    service, _, _ = database
    row = create(service, {'title': 'one'}, 'stable')
    assert create(service, {'title': 'one'}, 'stable') == row
    with pytest.raises(PlanError, match='idempotency_mismatch'): create(service, {'title': 'other'}, 'stable')
    original = service.repo.finish
    def fail(*args): raise RuntimeError('before commit')
    monkeypatch.setattr(service.repo, 'finish', fail)
    with pytest.raises(RuntimeError): create(service, {'title': 'rollback'}, 'rollback')
    monkeypatch.setattr(service.repo, 'finish', original)
    assert service.listing(1, {})['total'] == 1
    with pytest.raises(PlanError): service.request_result(1, 'rollback')
    create(service, {'title': 'committed'}, 'lost-response')
    assert create(service, {'title': 'committed'}, 'lost-response')['id'] == service.request_result(1, 'lost-response')['response']['id']


def test_expiry_is_immediate_without_maintenance_or_read_writes(database):
    service, _, _ = database
    row = create(service, card(expires_at=(NOW-timedelta(minutes=1)).isoformat()))
    assert row['effective_lifecycle'] == 'expired' and row['lifecycle'] == 'draft'
    with pytest.raises(PlanError, match='plan_terminal'): change(service, row, 'revise', snapshot=card(), change_reason='revive')
    assert service.listing(1, {'lifecycle': 'expired'})['total'] == 1
    assert service.listing(1, {'lifecycle': 'draft'})['total'] == 0
    assert [e['event_type'] for e in service.history(1, row['id'], 'events')['items']] == ['created']


def test_policy_versions_events_requests_and_research(database):
    service, connect, _ = database
    original = create(service, card(), 'old-key')
    row = legacy_version(service, original, validate_snapshot(card(instrument={'market': 'Crypto', 'symbol': 'ETH/USDT', 'market_type': 'spot'})))
    limited = PlanService(scope=PlanScope(instruments=('ETH/USDT',)))
    assert len(limited.history(1, row['id'], 'versions')['items']) == 1
    assert limited.listing(1, {'q': 'eth'})['total'] == 1
    with pytest.raises(PlanError): limited.request_result(1, 'old-key')
    with pytest.raises(PlanError): create(limited, card())
    with connect() as db: db.cursor().execute("INSERT INTO qd_market_state_results(id,user_id,market,symbol,bar_close_at,summary) VALUES (5,1,'Crypto','BTC/USDT',%s,'real evidence')", (NOW,))
    row = create(service, card(research_refs=[{'type': 'market_state_result', 'id': 5, 'summary': 'spoofed'}]))
    assert row['snapshot']['research_refs'][0]['summary'] == 'real evidence'
    revised = change(service, row, 'revise', snapshot=deepcopy(row['snapshot']), change_reason='same source')
    assert revised['snapshot']['research_refs'][0]['evidence_snapshot']['id'] == 5
    with connect() as db:
        db.cursor().execute('DELETE FROM qd_market_state_results WHERE id=5')
    copied = change(service, revised, 'copy')
    assert copied['snapshot']['research_refs'] == revised['snapshot']['research_refs']
    with pytest.raises(PlanError, match='research_not_found'): create(service, card(research_refs=[{'type': 'market_state_result', 'id': 999}]))


def test_ai_and_future_signal_rejected(database):
    service, _, _ = database
    row = create(service, card())
    with pytest.raises(PlanError, match='plan_not_observing'): change(service, row, 'decision', status='confirmed', reason='test', observed_at=NOW.isoformat())
    row = change(service, row, 'state', lifecycle='observing', reason='watch')
    with pytest.raises(PlanError, match='ai_cannot_confirm_signal'): change(service, row, 'decision', status='confirmed', reason='AI', observed_at=NOW.isoformat(), assessment_author='ai_suggestion')
    with pytest.raises(PlanError, match='future_observation'): change(service, row, 'decision', status='confirmed', reason='future', observed_at=(NOW+timedelta(days=1)).isoformat())


def test_additive_repeat_migration_preserves_legacy_data(database):
    service, connect, _ = database
    create(service, card())
    with connect() as db:
        cur = db.cursor()
        cur.execute('CREATE TABLE legacy_trades(id INT, entry_reason TEXT); INSERT INTO legacy_trades VALUES (1,\'original\')')
        cur.execute(MIGRATION.read_text())
        cur.execute((MIGRATION.parent / '20261024_trade_plan_records.sql').read_text()); cur.execute(MIGRATION.read_text())
        cur.execute((MIGRATION.parent / '20261024_trade_plan_records.sql').read_text())
        cur.execute('SELECT * FROM legacy_trades')
        assert cur.fetchone()['entry_reason'] == 'original'
    assert service.listing(1, {})['total'] == 1


def test_account_delete_cascades_only_its_immutable_history(database):
    service, connect, _ = database
    original = create(service, card())
    change(service, original, 'copy')
    survivor = create(service, card(), user=2)
    with connect() as db:
        db.cursor().execute('DELETE FROM qd_users WHERE id=1')
    assert service.read(2, survivor['id'])['id'] == survivor['id']
    with connect() as db:
        for table in ('qd_trade_plans', 'qd_trade_plan_versions', 'qd_trade_plan_events', 'qd_trade_plan_requests'):
            cur = db.cursor()
            cur.execute('SELECT COUNT(*) AS n FROM '+table+' WHERE user_id=1')
            assert cur.fetchone()['n'] == 0


def test_sql_pagination_scope_and_compact_replay(database):
    service, connect, queries = database
    for index in range(23):
        create(service, card(title=f'page {index}', instrument={'market': 'Crypto', 'symbol': 'ETH/USDT'}))
    with connect() as db:
        db.cursor().execute("INSERT INTO qd_market_state_results(id,user_id,market,symbol,summary) VALUES (8,1,'Crypto','BTC/USDT','restricted evidence')")
    legacy = create(service, card(instrument={'market': 'Crypto', 'symbol': 'ETH/USDT'}))
    snapshot = deepcopy(legacy['snapshot'])
    snapshot['research_refs'] = [{'type':'market_state_result','id':8,'summary':'restricted evidence',
                                 'evidence_snapshot':{'market':'Crypto','symbol':'BTC/USDT'}}]
    legacy_version(service, legacy, snapshot)
    limited = PlanService(scope=PlanScope(instruments=('ETH/USDT',)))
    start = len(queries)
    first = limited.listing(1, {'page': '1', 'page_size': '20'})
    second = limited.listing(1, {'page': '2', 'page_size': '20'})
    assert first['total'] == second['total'] == 23
    assert len(first['items']) == 20 and len(second['items']) == 3
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    assert all('events' not in row and set(row['snapshot']) == {'title', 'instrument', 'direction', 'primary_strategy', 'retrospective'} for row in first['items'])
    assert sum('LIMIT' in query and 'OFFSET' in query for query in queries[start:]) == 2
    result = service.mutate(1, 'create', {'request_id': 'compact', 'snapshot': card()})
    replay = service.request_result(1, 'compact')['response']
    assert result == replay
    assert set(result) == {'id', 'version_id', 'version_no', 'row_revision', 'request_id'}


def test_title_only_revision_resets_signal(database):
    service, _, _ = database
    row = change(service, create(service, card()), 'state', lifecycle='observing', reason='watch')
    row = change(service, row, 'decision', status='confirmed', reason='observed', observed_at=NOW.isoformat())
    changed = change(service, row, 'revise', snapshot=card(title='renamed'), change_reason='rename')
    assert changed['signal_status'] == 'unassessed'
    assert any(e['payload'].get('status') == 'confirmed' for e in service.history(1, row['id'], 'events')['items'])
