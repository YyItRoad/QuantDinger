"""Real PostgreSQL checks for frozen versions, source ownership and review snapshots."""
from copy import deepcopy
from uuid import uuid4
import pytest
from psycopg2.extras import RealDictCursor
from tests.test_trade_plan_app import full_app, login
from tests.test_trade_plan_management import card


def setup_plan(full_app, snapshot=None):
    client,admin,password,_=full_app
    headers=login(client,'plan-test',password)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_exchange_credentials(id,user_id,name,exchange_id,encrypted_config) VALUES(901,1,'fixture','binance','not-a-real-secret'),(902,2,'other','binance','not-a-real-secret')")
    if snapshot is None:
        snapshot=card(instrument={'market':'Crypto','symbol':'BTC/USDT','market_type':'spot','exchange_id':'binance','instrument_id':'BTCUSDT','settlement_ccy':'USDT'})
    created=client.post('/api/trade-plans',headers=headers,json={'request_id':str(uuid4()),'snapshot':snapshot})
    assert created.status_code==200,created.get_json()
    pid=created.get_json()['data']['id']
    def read():
        response=client.get('/api/trade-plans/'+pid,headers=headers)
        assert response.status_code==200,response.get_json()
        return response.get_json()['data']
    def write(action,**body):
        response=client.post('/api/trade-plans/'+pid+'/'+action,headers=headers,json={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],**body})
        assert response.status_code==200,response.get_json()
        return read()
    write('attempts',version_id=read()['current_version_id'],credential_id=901,execution_mode='live')
    return client,admin,headers,pid,read,write


def seed(admin,kind='open_long',quantity='2',price='100',owner=1,credential=901,event=True):
    with admin.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO qd_strategies_trading(user_id,strategy_name,symbol,market_category,market_type,execution_mode,status) VALUES(%s,'fixture','BTC/USDT','Crypto','spot','live','stopped') RETURNING id",(owner,))
        sid=cur.fetchone()['id'];eid=0
        if event:
            cur.execute("INSERT INTO qd_execution_events(event_key,user_id,credential_id,exchange_id,market_type,symbol,quantity,price,occurred_at) VALUES(%s,%s,%s,'binance','spot','BTC/USDT',%s,%s,NOW()) RETURNING id",(str(uuid4()),owner,credential,quantity,price))
            eid=cur.fetchone()['id']
        cur.execute("INSERT INTO qd_strategy_trades(user_id,strategy_id,symbol,market_type,type,amount,price,credential_id,inst_id,execution_event_id,commission_quote,fee_status,close_reason) VALUES(%s,%s,'BTC/USDT','spot',%s,%s,%s,%s,'BTCUSDT',%s,1,'actual','fixture_exit') RETURNING id",(owner,sid,kind,quantity,price,credential,eid))
        return cur.fetchone()['id']


def link(read,write,source_id,source_kind='trade'):
    return write('links',attempt_id=read()['attempts'][0]['id'],source_kind=source_kind,source_id=source_id,attribution='confirmed',reason='verified fixture')


def assessment():
    return {'market_judgment':{'value':'matches','reason':'structure held'},'strategy_rules':{'value':'applicable','reason':'rules explicit'},'execution':{'value':'deviated','reason':'execution difference'},'improvement':'wait for confirmation'}


def test_frozen_basis_closed_returns_and_review_history(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    attempt=read()['attempts'][0];snapshot=read()['snapshot']
    write('versions',snapshot={**snapshot,'risk_inputs':{**snapshot['risk_inputs'],'quantity':'10'}},change_reason='larger future reference')
    assert read()['checks']['one_r']=='50'
    assert read()['attempts'][0]['risk_snapshot']['one_r']=='10'
    link(read,write,seed(admin));row=link(read,write,seed(admin,'close_long',price='110'))
    summary=row['attempts'][0]['summary']
    assert summary['trade_status']=='closed' and summary['pretrade_evidence']=='verified'
    assert float(summary['net_profit'])==18 and float(summary['net_r'])==1.8
    write('reviews',attempt_id=attempt['id'],assessment=assessment(),change_reason='initial review')
    old=deepcopy(read()['attempts'][0]['reviews'][0])
    assert old['evidence_snapshot']['version_no']==1
    active=read()['attempts'][0]['links'][0]
    write('links/revoke',attempt_id=attempt['id'],link_id=active['id'],reason='correct mistaken allocation')
    assert read()['attempts'][0]['summary']['trade_status']=='unknown'
    assert read()['attempts'][0]['reviews'][0]==old
    write('reviews',attempt_id=attempt['id'],assessment=assessment(),change_reason='updated evidence')
    assert [r['review_no'] for r in read()['attempts'][0]['reviews']]==[2,1]
    with admin.cursor() as cur:
        for table in ('qd_trade_plan_attempts','qd_trade_plan_reviews'):
            with pytest.raises(Exception):cur.execute('UPDATE '+table+' SET source=source')


def test_partial_exit_archive_dedup_and_deleted_source(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    opening=seed(admin);link(read,write,opening)
    row=link(read,write,seed(admin,'close_long',quantity='1',price='110'))
    assert row['attempts'][0]['summary']['trade_status']=='partially_closed'
    assert row['attempts'][0]['summary']['net_r'] is None
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_position_management_trade_history(source_trade_id,source_strategy_id,user_id,market_category,market_type,symbol,trade_type,execution_mode,credential_id,exchange_id,inst_id,filled_amount) VALUES(%s,1,1,'Crypto','spot','BTC/USDT','open_long','live',901,'binance','BTCUSDT',2) RETURNING id",(opening,))
        hid=cur.fetchone()[0]
    body={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'attempt_id':read()['attempts'][0]['id'],'source_kind':'history','source_id':hid,'attribution':'confirmed','reason':'duplicate archive'}
    response=client.post('/api/trade-plans/'+pid+'/links',headers=headers,json=body)
    assert response.status_code==409 and response.get_json()['msg']=='record_already_linked'
    old=read()['attempts'][0]['links'][0]
    with admin.cursor() as cur:cur.execute('DELETE FROM qd_strategy_trades WHERE id=%s',(opening,))
    write('links/refresh',attempt_id=read()['attempts'][0]['id'],link_id=old['id'],reason='source removed')
    record=read()['attempts'][0]
    assert len(record['links'])==3 and record['summary']['trade_status']=='unknown'
    assert any(not l['snapshot'].get('available',True) for l in record['links'])
    old_stored=next(l for l in record['links'] if l['id']==old['id'])
    assert old_stored['revoked'] and old_stored['snapshot']==old['snapshot']


def test_scope_replay_missing_time_and_viewer(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    own=seed(admin,event=False);other=seed(admin,owner=2,credential=902)
    row=link(read,write,own)
    assert row['attempts'][0]['summary']['pretrade_evidence']=='unverifiable'
    assert row['attempts'][0]['summary']['net_r'] is None
    body={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'attempt_id':read()['attempts'][0]['id'],'source_kind':'trade','source_id':other,'attribution':'confirmed','reason':'denied'}
    assert client.post('/api/trade-plans/'+pid+'/links',headers=headers,json=body).status_code==404
    agent={'Authorization':'Bearer '+full_app[3],'Idempotency-Key':'second-attempt'}
    body={'request_id':'second-attempt','expected_revision':read()['row_revision'],'version_id':read()['current_version_id'],'credential_id':901,'execution_mode':'live'}
    url='/api/agent/v1/trade-plans/'+pid+'/attempts'
    first=client.post(url,headers=agent,json=body)
    assert first.status_code==200,first.get_json()
    assert client.post(url,headers=agent,json=body).get_json()==first.get_json()
    assert len(read()['attempts'])==2
    with admin.cursor() as cur:cur.execute("UPDATE qd_agent_tokens SET instruments='ETH/USDT'")
    assert client.post(url,headers=agent,json=body).status_code==404
    viewer=login(client,'plan-viewer',full_app[2])
    assert client.post('/api/trade-plans/'+pid+'/attempts',headers=viewer,json=body).status_code==403


def test_source_preview_account_scope_and_refresh(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    own=seed(admin);seed(admin,owner=2,credential=902)
    url='/api/trade-plans/'+pid+'/sources'
    response=client.get(url,headers=headers,query_string={'credential_id':901,'source_kind':'trade'})
    assert response.status_code==200,response.get_json()
    data=response.get_json()['data'];assert data['total']==1 and data['items'][0]['source_id']==own
    assert 'not-a-real-secret' not in str(data) and 'encrypted_config' not in str(data)
    assert client.get(url,headers=headers,query_string={'credential_id':902}).status_code==404
    link(read,write,own);record=read()['attempts'][0];old=record['links'][0]
    write('links/refresh',attempt_id=record['id'],link_id=old['id'],reason='updated facts')
    assert len(read()['attempts'][0]['links'])==2


def test_orders_and_strategy_are_references_not_synthetic_fills(full_app):
    from psycopg2.extras import Json
    client,admin,headers,pid,read,write=setup_plan(full_app)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_strategies_trading(user_id,strategy_name,symbol,market_category,market_type,execution_mode,trading_config,exchange_config) VALUES(1,'managed','BTC/USDT','Crypto','spot','live',%s,%s) RETURNING id",(Json({'position_management':{'enabled':True}}),Json({'credentialId':901})))
        sid=cur.fetchone()[0]
        cur.execute("INSERT INTO pending_orders(user_id,strategy_id,symbol,signal_type,market_type,execution_mode,status,credential_id,exchange_id,inst_id,idempotency_key,amount,filled) VALUES(1,%s,'BTC/USDT','open_long','spot','live','cancelled',901,'binance','BTCUSDT',%s,2,1) RETURNING id",(sid,str(uuid4())))
        oid=cur.fetchone()[0]
        cur.execute("INSERT INTO qd_quick_trades(user_id,credential_id,symbol,market_type,exchange_id,status,filled_amount,raw_result) VALUES(1,901,'BTC/USDT','spot','binance','filled',2,%s) RETURNING id",(Json({'secret':'must-never-return'}),))
        qid=cur.fetchone()[0]
    for kind in ('strategy','order','quick_order'):
        response=client.get('/api/trade-plans/'+pid+'/sources',headers=headers,query_string={'credential_id':901,'source_kind':kind})
        assert response.status_code==200,response.get_json()
        assert response.get_json()['data']['total']==1
        assert 'must-never-return' not in str(response.get_json())
    row=link(read,write,oid,'order')
    assert row['attempts'][0]['summary']['opened_quantity']=='0'
    assert row['attempts'][0]['summary']['trade_status']=='unknown'
    write('links',attempt_id=read()['attempts'][0]['id'],source_kind='quick_order',source_id=qid,attribution='pending',reason='environment unknown')
    assert read()['attempts'][0]['summary']['net_r'] is None
    # A confirmed reference cannot bypass missing environment or native identity.
    data={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'attempt_id':read()['attempts'][0]['id'],'source_kind':'strategy','source_id':sid,'attribution':'confirmed','reason':'insufficient native identity'}
    assert client.post('/api/trade-plans/'+pid+'/links',headers=headers,json=data).status_code==422


def test_same_fact_concurrency_is_claimed_only_once(full_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.trade_plans.service import PlanService
    client,admin,headers,pid,read,write=setup_plan(full_app)
    tid=seed(admin)
    p2=client.post('/api/trade-plans',headers=headers,json={'request_id':str(uuid4()),'snapshot':read()['snapshot']}).get_json()['data']['id']
    service=PlanService()
    row2=service.read(1,p2)
    service.mutate(1,'attempt',{'request_id':str(uuid4()),'expected_revision':row2['row_revision'],'version_id':row2['current_version_id'],'credential_id':901,'execution_mode':'live'},p2)
    rows=[read(),service.read(1,p2)];barrier=Barrier(2)
    def worker(row):
        from app.trade_plans.validation import PlanError
        barrier.wait()
        try:
            service.mutate(1,'link',{'request_id':str(uuid4()),'expected_revision':row['row_revision'],'attempt_id':row['attempts'][0]['id'],'source_kind':'trade','source_id':tid,'attribution':'confirmed','reason':'ownership claim'},row['id'])
            return 'linked'
        except PlanError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,rows))
    assert sorted(results)==['linked','record_already_linked']


def test_missing_fees_and_late_data_are_not_evaluable(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    opening=seed(admin);closing=seed(admin,'close_long',price='110')
    with admin.cursor() as cur:
        cur.execute("UPDATE qd_strategy_trades SET fee_status='pending' WHERE id=%s",(closing,))
        cur.execute("UPDATE qd_execution_events SET occurred_at=NOW()-interval '1 day' WHERE id=(SELECT execution_event_id FROM qd_strategy_trades WHERE id=%s)",(opening,))
    link(read,write,opening);row=link(read,write,closing)
    summary=row['attempts'][0]['summary']
    assert summary['trade_status']=='closed' and summary['pretrade_evidence']=='retrospective'
    assert summary['net_r'] is None and 'fees_incomplete' in summary['gaps']


def test_record_migration_preserves_g1_and_user_delete_is_compatible(full_app):
    from pathlib import Path
    client,admin,headers,pid,read,write=setup_plan(full_app)
    link(read,write,seed(admin))
    write('reviews',attempt_id=read()['attempts'][0]['id'],assessment=assessment(),change_reason='test before deletion')
    before=deepcopy(read())
    migration=(Path(__file__).resolve().parents[1]/'migrations/20261024_trade_plan_records.sql').read_text()
    with admin.cursor() as cur:
        cur.execute(migration)
        cur.execute(migration)
    assert read()==before
    from app.services.user_service import UserService
    assert UserService().delete_user(1)
    with admin.cursor() as cur:
        for table in ('qd_trade_plan_attempts','qd_trade_plan_links','qd_trade_plan_reviews'):
            cur.execute('SELECT COUNT(*) FROM '+table+' WHERE user_id=1')
            assert cur.fetchone()[0]==0
        cur.execute('SELECT COUNT(*) FROM qd_users WHERE id=2')
        assert cur.fetchone()[0]==1


def test_unresolved_attempt_blocks_completion_and_errors_name_fields(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    write('state',lifecycle='observing',reason='complete card')
    link(read,write,seed(admin))
    response=client.post('/api/trade-plans/'+pid+'/state',headers=headers,json={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'lifecycle':'completed','reason':'premature'})
    assert response.status_code==422 and response.get_json()['msg']=='record_attempts_unresolved'
    snapshot=deepcopy(read()['snapshot'])
    snapshot['risk_inputs']['quantity']='NaN'
    response=client.post('/api/trade-plans/'+pid+'/versions',headers=headers,json={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'snapshot':snapshot,'change_reason':'bad input'})
    assert response.status_code==400
    assert response.get_json()['data']['details']['fields']==['risk_inputs.quantity']


def test_owned_wrong_account_and_unsupported_profit_model(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_exchange_credentials(id,user_id,name,exchange_id,encrypted_config) VALUES(903,1,'other owned account','binance','{}')")
    source_id=seed(admin,credential=903)
    body={'request_id':str(uuid4()),'expected_revision':read()['row_revision'],'attempt_id':read()['attempts'][0]['id'],'source_kind':'trade','source_id':source_id,'attribution':'pending','reason':'wrong account'}
    response=client.post('/api/trade-plans/'+pid+'/links',headers=headers,json=body)
    assert response.status_code==422 and response.get_json()['msg']=='record_identity_mismatch'
    from app.trade_plans.record_summary import summarize
    sample=deepcopy(read()['attempts'][0])
    snapshot=deepcopy(sample['plan_snapshot'])
    snapshot['instrument']['market_type']='swap'
    result=summarize(sample,[],snapshot)
    assert result['net_r'] is None and 'funding_or_contract_costs_unverified' in result['gaps']


def test_restricted_scope_hides_links_with_unknown_market(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    tid=seed(admin)
    with admin.cursor() as cur:
        cur.execute("UPDATE qd_strategies_trading SET market_category='' WHERE id=(SELECT strategy_id FROM qd_strategy_trades WHERE id=%s)",(tid,))
    write('links',attempt_id=read()['attempts'][0]['id'],source_kind='trade',source_id=tid,attribution='pending',reason='market identity unavailable')
    agent={'Authorization':'Bearer '+full_app[3]}
    with admin.cursor() as cur:
        cur.execute("UPDATE qd_agent_tokens SET markets='Crypto'")
    assert client.get('/api/agent/v1/trade-plans/'+pid,headers=agent).status_code==404
    listing=client.get('/api/agent/v1/trade-plans',headers=agent)
    assert listing.get_json()['data']['total']==0


def test_strategy_reference_can_span_attempts_without_sharing_fills(full_app):
    from psycopg2.extras import Json
    client,admin,headers,pid,read,write=setup_plan(full_app)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_strategies_trading(user_id,strategy_name,symbol,market_category,market_type,execution_mode,trading_config,exchange_config) VALUES(1,'shared reference','BTC/USDT','Crypto','spot','live',%s,%s) RETURNING id",(Json({'position_management':{'enabled':True,'side':'long'}}),Json({'credentialId':901})))
        sid=cur.fetchone()[0]
    first=read()['attempts'][0]['id']
    write('links',attempt_id=first,source_kind='strategy',source_id=sid,attribution='pending',reason='reference only, not fill ownership')
    write('attempts',version_id=read()['current_version_id'],credential_id=901,execution_mode='live')
    second=read()['attempts'][1]['id']
    row=write('links',attempt_id=second,source_kind='strategy',source_id=sid,attribution='pending',reason='same strategy, separate attempt')
    assert all(a['summary']['trade_status']=='unknown' for a in row['attempts'])
    assert [len(a['links']) for a in row['attempts']]==[1,1]


def test_lookup_existing_plan_from_trade_and_archive_is_tenant_scoped(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    trade_id=seed(admin)
    link(read,write,trade_id)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_position_management_trade_history(source_trade_id,source_strategy_id,user_id,market_category,market_type,symbol,trade_type,credential_id) VALUES(%s,1,1,'Crypto','spot','BTC/USDT','open_long',901) RETURNING id",(trade_id,))
        history_id=cur.fetchone()[0]
    for kind,source_id in [('trade',trade_id),('history',history_id)]:
        result=client.get('/api/trade-plans',headers=headers,query_string={'source_kind':kind,'source_id':source_id})
        assert result.status_code==200,result.get_json()
        assert result.get_json()['data']['total']==1
        assert result.get_json()['data']['items'][0]['id']==pid
    other=login(client,'plan-viewer',full_app[2])
    assert client.get('/api/trade-plans',headers=other,query_string={'source_kind':'trade','source_id':trade_id}).get_json()['data']['total']==0
    current=read()['attempts'][0]
    write('links/revoke',attempt_id=current['id'],link_id=current['links'][0]['id'],reason='corrected ownership')
    assert client.get('/api/trade-plans',headers=headers,query_string={'source_kind':'trade','source_id':trade_id}).get_json()['data']['total']==0


def test_exact_source_preview_filters_before_pagination(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app)
    first=seed(admin)
    seed(admin)
    response=client.get('/api/trade-plans/'+pid+'/sources',headers=headers,query_string={'credential_id':901,'source_kind':'trade','source_id':first,'page_size':1})
    assert response.status_code==200,response.get_json()
    assert response.get_json()['data']['total']==1
    assert response.get_json()['data']['items'][0]['source_id']==first
    for filters in [{'source_kind':'trade'},{'source_id':first},{'source_kind':'trade','source_id':'not-an-id'}]:
        assert client.get('/api/trade-plans',headers=headers,query_string=filters).status_code==400


def managed_strategy_fixture(admin, account_key='credential_id', **position_overrides):
    from psycopg2.extras import Json
    with admin.cursor() as cur:
        cur.execute("""INSERT INTO qd_strategies_trading
            (user_id,strategy_name,symbol,market_category,market_type,execution_mode,trading_config,exchange_config)
            VALUES(1,'managed fixture','NEAR/USDT','Crypto','swap','live',%s,%s) RETURNING id""",
            (Json({'position_management':{'enabled':True,'side':'short'},
                   'instrument_products':[{'instrument_id':'BTCUSDT'}]}),
             Json({account_key:901,'exchange_id':'binance','api_key':'must-not-be-projected'})))
        sid=cur.fetchone()[0]
        values={'user_id':1,'symbol':'NEAR/USDT','market_type':'swap','side':'short',
                'credential_id':901,'inst_id':'NEARUSDT'}
        values.update(position_overrides)
        cur.execute("""INSERT INTO qd_strategy_positions
            (strategy_id,user_id,symbol,market_type,side,credential_id,inst_id,size)
            VALUES(%s,%s,%s,%s,%s,%s,%s,48)""",
            (sid,values['user_id'],values['symbol'],values['market_type'],values['side'],
             values['credential_id'],values['inst_id']))
    return sid


def near_card():
    return card(direction='short',retrospective=True,instrument={'market':'Crypto','symbol':'NEAR/USDT',
        'market_type':'swap','exchange_id':'binance','instrument_id':'NEARUSDT','settlement_ccy':'USDT'})


@pytest.mark.parametrize('account_key',['credential_id','credentialId'])
def test_managed_strategy_identity_preview_and_confirm_refresh(full_app,account_key):
    client,admin,headers,pid,read,write=setup_plan(full_app,near_card())
    sid=managed_strategy_fixture(admin,account_key)
    response=client.get('/api/trade-plans/'+pid+'/sources',headers=headers,
        query_string={'credential_id':901,'source_kind':'strategy','source_id':sid})
    data=response.get_json()['data']
    assert data['total']==1
    fact=data['items'][0]
    assert fact['credential_id']==901 and fact['exchange_id']=='binance'
    assert fact['inst_id']=='NEARUSDT'
    assert 'must-not-be-projected' not in str(data)
    assert client.get('/api/trade-plans/'+pid+'/sources',headers=headers,
        query_string={'credential_id':902,'source_kind':'strategy'}).status_code==404
    write('links',attempt_id=read()['attempts'][0]['id'],source_kind='strategy',source_id=sid,
        attribution='pending',reason='existing position research')
    old=deepcopy(read()['attempts'][0]['links'][0])
    row=write('links/refresh',attempt_id=read()['attempts'][0]['id'],link_id=old['id'],
        attribution='confirmed',reason='identity verified from owned position')
    links=row['attempts'][0]['links']
    prior=next(x for x in links if x['id']==old['id'])
    current=next(x for x in links if not x['revoked'])
    assert prior['revoked'] and prior['snapshot']==old['snapshot']
    assert current['attribution']=='confirmed' and current['snapshot']['inst_id']=='NEARUSDT'
    assert row['attempts'][0]['summary']['opened_quantity']=='0'
    assert row['attempts'][0]['summary']['net_r'] is None


@pytest.mark.parametrize('wrong_identity',[
    {'user_id':2},{'credential_id':902},{'symbol':'BTC/USDT'},
    {'market_type':'spot'},{'side':'long'}, {'inst_id':''},
])
def test_managed_strategy_does_not_borrow_unmatched_position_identity(full_app,wrong_identity):
    client,admin,headers,pid,read,write=setup_plan(full_app,near_card())
    sid=managed_strategy_fixture(admin,**wrong_identity)
    response=client.post('/api/trade-plans/'+pid+'/links',headers=headers,json={
        'request_id':str(uuid4()),'expected_revision':read()['row_revision'],
        'attempt_id':read()['attempts'][0]['id'],'source_kind':'strategy','source_id':sid,
        'attribution':'confirmed','reason':'must retain incomplete identity guard'})
    assert response.status_code==422
    assert response.get_json()['msg']=='record_identity_incomplete'
    assert 'inst_id' in response.get_json()['data']['details']['fields']
    assert not read()['attempts'][0]['links']



def test_managed_strategy_ambiguous_native_identity_stays_unconfirmed(full_app):
    client,admin,headers,pid,read,write=setup_plan(full_app,near_card())
    sid=managed_strategy_fixture(admin)
    with admin.cursor() as cur:
        cur.execute("""INSERT INTO qd_strategy_positions
            (strategy_id,user_id,symbol,market_type,side,credential_id,inst_id,size)
            VALUES(%s,1,'near/usdt','swap','short',901,'OTHER_NATIVE_ID',1)""",(sid,))
    response=client.get('/api/trade-plans/'+pid+'/sources',headers=headers,
        query_string={'credential_id':901,'source_kind':'strategy','source_id':sid})
    assert response.get_json()['data']['items'][0]['inst_id'] is None
    response=client.post('/api/trade-plans/'+pid+'/links',headers=headers,json={
        'request_id':str(uuid4()),'expected_revision':read()['row_revision'],
        'attempt_id':read()['attempts'][0]['id'],'source_kind':'strategy','source_id':sid,
        'attribution':'confirmed','reason':'ambiguous identity must not be guessed'})
    assert response.status_code==422 and not read()['attempts'][0]['links']
