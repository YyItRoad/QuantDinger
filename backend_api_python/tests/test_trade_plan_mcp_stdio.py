"""Real MCP stdio -> loopback HTTP -> authenticated Gateway -> PostgreSQL."""
import asyncio
import json
import os
import sys
import threading
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from werkzeug.serving import make_server

from tests.test_trade_plan_app import full_app, login
from tests.test_trade_plan_management import card


def test_real_stdio_plan_attempt_review_and_web_share_one_store(full_app):
    client, admin, password, raw_token = full_app
    headers = login(client, 'plan-test', password)
    with admin.cursor() as cur:
        cur.execute("INSERT INTO qd_exchange_credentials(id,user_id,name,exchange_id,encrypted_config) VALUES(901,1,'stdio fixture','binance','{}')")
    http = make_server('127.0.0.1', 0, client.application)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, 'QUANTDINGER_BASE_URL': f'http://127.0.0.1:{http.server_port}',
           'QUANTDINGER_AGENT_TOKEN': raw_token, 'QUANTDINGER_MCP_TRANSPORT': 'stdio',
           'QUANTDINGER_MCP_AUTH_MODE': 'static', 'QUANTDINGER_MCP_AUTH_TOKEN': ''}
    params = StdioServerParameters(command=sys.executable, args=['-m', 'quantdinger_mcp.server'], env=env)

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                assert {'create_trade_plan', 'get_trade_plan', 'create_trade_plan_attempt', 'save_trade_plan_review'} <= names
                order_tool = next(tool for tool in tools if tool.name == 'place_quick_order')
                assert 'entry_reason' in order_tool.inputSchema['properties']

                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    payload = json.loads(''.join(part.text for part in response.content if part.type == 'text'))
                    assert not payload.get('error'), payload
                    return payload.get('data', payload)

                snapshot = card(title='stdio acceptance', instrument={'market':'Crypto', 'symbol':'BTC/USDT', 'market_type':'spot', 'exchange_id':'binance'})
                request = {'snapshot':snapshot, 'idempotency_key':str(uuid4())}
                created = await call('create_trade_plan', request)
                replay = await call('create_trade_plan', request)
                assert replay['id'] == created['id']
                plan_id = created['id']
                detail = await call('get_trade_plan', {'plan_id':plan_id})
                await call('create_trade_plan_attempt', {'plan_id':plan_id, 'version_id':detail['current_version_id'], 'credential_id':901, 'execution_mode':'live', 'expected_revision':detail['row_revision'], 'idempotency_key':str(uuid4())})
                detail = await call('get_trade_plan', {'plan_id':plan_id})
                attempt = detail['attempts'][0]
                assessment = {key:{'value':'not_evaluable', 'reason':'synthetic fixture'} for key in ('market_judgment','strategy_rules','execution')}
                assessment['improvement'] = 'retain evidence before real use'
                await call('save_trade_plan_review', {'plan_id':plan_id, 'attempt_id':attempt['id'], 'assessment':assessment, 'change_reason':'stdio acceptance review', 'expected_revision':detail['row_revision'], 'idempotency_key':str(uuid4())})
                observed = client.get('/api/trade-plans/'+plan_id, headers=headers)
                assert observed.status_code == 200
                assert len(observed.get_json()['data']['attempts'][0]['reviews']) == 1
                assert observed.get_json()['data']['attempts'][0]['links'] == []

    try:
        asyncio.run(asyncio.wait_for(exercise(), timeout=30))
    finally:
        http.shutdown()
        thread.join(timeout=5)
        http.server_close()
