"""分析任务和结果接口；业务实现留在独立模块。"""
from functools import wraps

from flask import g, jsonify, request
from psycopg2 import Error as DatabaseError
from app.openapi.blueprint import HumanBlueprint
from app.utils.auth import login_required
from app.utils.logger import get_logger
from .constants import SUPPORTED_TIMEFRAMES
from .errors import RequestValidationError, TaskConflictError
from .repository import AnalysisRepository
from .validation import validate_task_input

blp = HumanBlueprint('market_state', __name__)
logger = get_logger(__name__)


def reply(data=None, msg='success', status=200):
    return jsonify({'code': 1 if status < 400 else 0, 'msg': msg, 'data': data}), status


def analysis_endpoint(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        g.market_state_repository = AnalysisRepository()
        try:
            return fn(*args, **kwargs)
        except RequestValidationError as exc:
            return reply(msg=str(exc), status=400)
        except DatabaseError:
            logger.exception('分析模块存储请求失败')
            return reply(msg='分析存储暂不可用，请确认数据库连接及迁移已完成', status=503)
        except Exception:
            logger.exception('分析模块请求失败 endpoint=%s', request.endpoint)
            return reply(msg='分析请求失败，请检查服务日志', status=500)
    return login_required(wrapped)


def integer_arg(name, default, upper):
    try:
        value = int(request.args.get(name, default))
    except (ValueError, TypeError):
        raise RequestValidationError(f'{name} 必须为整数')
    if value < 1 or value > upper:
        raise RequestValidationError(f'{name} 超出允许范围')
    return value


def listing(kind):
    timeframe = request.args.get('timeframe', '')
    if timeframe and timeframe not in SUPPORTED_TIMEFRAMES:
        raise RequestValidationError('不支持的分析周期')
    return reply(g.market_state_repository.list(
        kind, g.user_id, integer_arg('page', 1, 1000000), integer_arg('page_size', 10, 100),
        request.args.get('symbol', '').strip().upper(), timeframe))


@blp.route('/records', methods=['GET'])
@analysis_endpoint
def list_records():
    return listing('records')


@blp.route('/records/<int:record_id>', methods=['GET'])
@analysis_endpoint
def get_record(record_id):
    row = g.market_state_repository.get_record(g.user_id, record_id)
    return reply(row) if row else reply(msg='分析记录不存在', status=404)


@blp.route('/tasks', methods=['GET'])
@analysis_endpoint
def list_tasks():
    return listing('tasks')


@blp.route('/tasks', methods=['POST'])
@analysis_endpoint
def create_task():
    value = validate_task_input(request.get_json(silent=True))
    try:
        task = g.market_state_repository.create_task(g.user_id, value)
    except TaskConflictError as exc:
        return reply(msg=str(exc), status=409)
    return reply(task, status=201)


@blp.route('/tasks/<int:task_id>', methods=['PATCH', 'DELETE'])
@analysis_endpoint
def update_task(task_id):
    delete = request.method == 'DELETE'
    enabled = False
    if not delete:
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise RequestValidationError('仅允许设置布尔类型的 enabled')
        enabled = body['enabled']
    task = g.market_state_repository.change_task(g.user_id, task_id, enabled=enabled, delete=delete)
    return reply(task) if task else reply(msg='分析任务不存在', status=404)


@blp.route('/tasks/<int:task_id>/run', methods=['POST'])
@analysis_endpoint
def run_task(task_id):
    """提交一次后台分析；不开启周期调度，不自动重试模型调用。"""
    from .scheduling import ScheduleRepository, latest_bar_close, utc_now
    from .tasks import submit_manual_analysis
    repo = ScheduleRepository()
    task = repo.get_task(g.user_id, task_id)
    if not task:
        return reply(msg='分析任务不存在', status=404)
    if task['market'] != 'Crypto':
        return reply(msg='当前仅支持数字货币单次分析', status=400)
    reservation = repo.reserve_manual(g.user_id, task_id, utc_now())
    if not reservation:
        return reply(msg='任务正在分析或已变更，请稍后重试', status=409)
    payload = {
        'id': task_id,
        'user_id': g.user_id,
        'revision': reservation['revision'],
        'lease_token': reservation['lease_token'],
    }
    try:
        submit_manual_analysis(payload)
    except Exception:
        repo.release(g.user_id, task_id, reservation['lease_token'])
        logger.exception('手动分析提交失败 task_id=%s user_id=%s', task_id, g.user_id)
        return reply(msg='分析任务提交失败，请检查后台任务服务', status=503)
    return reply({
        'task_id': task_id,
        'status': 'queued',
        'expected_bar_close_at': latest_bar_close(utc_now(), task['timeframe']).isoformat(),
    }, msg='分析任务已提交', status=202)
