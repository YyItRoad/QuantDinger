"""分析任务和结果接口；业务实现留在独立模块。"""
import os
from functools import lru_cache, wraps

from flask import g, jsonify, request
from app.openapi.blueprint import HumanBlueprint
from app.utils.auth import login_required
from .demo_repository import DemoRepository

blp = HumanBlueprint('market_state', __name__)


@lru_cache(maxsize=4)
def repository(path):
    return DemoRepository(path)


def reply(data=None, msg='success', status=200):
    return jsonify({'code': 1 if status < 400 else 0, 'msg': msg,
                    'data': data, 'mode': 'demo' if status != 503 else 'unavailable'}), status


def demo_endpoint(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        path = os.getenv('MARKET_STATE_DEMO_DB', '').strip()
        if os.getenv('MARKET_STATE_DEMO_ENABLED', '').lower() != 'true' or not path:
            return reply(msg='分析模块演示模式未开启，真实分析功能尚未接入', status=503)
        g.market_state_repository = repository(path)
        g.market_state_repository.seed(g.user_id)
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return reply(msg=str(exc), status=400)
    return login_required(wrapped)


def integer_arg(name, default, upper):
    try:
        value = int(request.args.get(name, default))
    except (ValueError, TypeError):
        raise ValueError(f'{name} 必须为整数')
    if value < 1 or value > upper:
        raise ValueError(f'{name} 超出允许范围')
    return value


def listing(kind):
    timeframe = request.args.get('timeframe', '')
    if timeframe not in ('', '1h', '4h', '1d'):
        raise ValueError('不支持的分析周期')
    return reply(g.market_state_repository.list(
        kind, g.user_id, integer_arg('page', 1, 1000000), integer_arg('page_size', 10, 100),
        request.args.get('symbol', '').strip().upper(), timeframe))


@blp.route('/records', methods=['GET'])
@demo_endpoint
def list_records():
    return listing('records')


@blp.route('/records/<int:record_id>', methods=['GET'])
@demo_endpoint
def get_record(record_id):
    row = g.market_state_repository.get_record(g.user_id, record_id)
    return reply(row) if row else reply(msg='分析记录不存在', status=404)


@blp.route('/tasks', methods=['GET'])
@demo_endpoint
def list_tasks():
    return listing('tasks')


@blp.route('/tasks', methods=['POST'])
@demo_endpoint
def create_task():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError('请求内容必须为对象')
    value = {}
    for key in ('market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'timeframe'):
        raw = body.get(key, '')
        if not isinstance(raw, str) or len(raw) > 120:
            raise ValueError(f'{key} 格式不正确')
        value[key] = raw.strip()
    if value['market'] not in ('Crypto', 'CNStock', 'HKStock', 'USStock', 'Forex', 'Futures', 'MOEX'):
        raise ValueError('不支持的市场')
    value['symbol'] = value['symbol'].upper()
    value['exchange_id'] = value['exchange_id'].lower()
    if not value['symbol'] or value['timeframe'] not in ('1d', '4h', '1h'):
        raise ValueError('请选择品种和分析周期')
    if value['market'] == 'Crypto':
        if value['exchange_id'] not in ('binance', 'bitget', 'bybit', 'okx', 'gate', 'htx') or value['market_type'] not in ('spot', 'swap'):
            raise ValueError('请选择有效交易所和品种类型')
    else:
        value['exchange_id'] = ''
        value['market_type'] = 'spot'
    try:
        task = g.market_state_repository.create_task(g.user_id, value)
    except ValueError as exc:
        return reply(msg=str(exc), status=409)
    return reply(task, status=201)


@blp.route('/tasks/<int:task_id>', methods=['PATCH', 'DELETE'])
@demo_endpoint
def update_task(task_id):
    delete = request.method == 'DELETE'
    enabled = False
    if not delete:
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise ValueError('仅允许设置布尔类型的 enabled')
        enabled = body['enabled']
    task = g.market_state_repository.change_task(g.user_id, task_id, enabled=enabled, delete=delete)
    return reply(task) if task else reply(msg='分析任务不存在', status=404)
