"""复用现有 Beat / maintenance / ai 队列；默认关闭，无自动模型重试。"""
from datetime import datetime
import os
from threading import Thread

from app.celery_app import celery_app
from app.market_state.scheduling import ScheduleRepository, execution_enabled, utc_now
from app.market_state.service import AnalysisService
from app.utils.logger import get_logger

logger = get_logger(__name__)


def celery_tasks_enabled():
    return os.getenv('CELERY_TASKS_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')


def _run_manual_analysis(id, user_id, revision, lease_token):
    repo = ScheduleRepository()
    try:
        result = AnalysisService(repo).run_once(
            user_id, id, expected_revision=revision, allow_stopped=True)
        return {'record_id': result['id']}
    except Exception:
        logger.exception('手动分析执行失败 task_id=%s user_id=%s', id, user_id)
        raise
    finally:
        repo.release(user_id, id, lease_token)


def _run_manual_analysis_in_thread(payload):
    try:
        _run_manual_analysis(**payload)
    except Exception:
        # _run_manual_analysis 已记录完整异常；本地后台线程不再重复输出。
        pass


def submit_manual_analysis(payload):
    """生产走 Celery；未启用 Celery 的本地 F5 使用模块内后台线程。"""
    if celery_tasks_enabled():
        execute_analysis.apply_async(kwargs={**payload, 'manual': True}, expires=1800)
        return 'celery'
    Thread(
        target=_run_manual_analysis_in_thread,
        args=(dict(payload),),
        name=f"market-state-manual-{payload['id']}",
        daemon=True,
    ).start()
    return 'thread'


@celery_app.task(name='quantdinger.tasks.market_state_tick', ignore_result=True)
def dispatch_analysis():
    if not execution_enabled():
        return {'skipped': True}
    repo = ScheduleRepository()
    sent = 0
    for item in repo.reserve_due(utc_now()):
        try:
            execute_analysis.apply_async(kwargs=item, expires=1800)
            sent += 1
        except Exception:
            repo.release_pending(item['user_id'], item['id'], item['lease_token'], item['bar_close_at'])
            logger.exception('分析任务派发失败 task_id=%s', item['id'])
    return {'dispatched': sent}


@celery_app.task(name='quantdinger.tasks.market_state_execute', ignore_result=True,
                 soft_time_limit=540, time_limit=600)
def execute_analysis(id, user_id, revision, lease_token, bar_close_at=None, manual=False):
    if manual:
        return _run_manual_analysis(id, user_id, revision, lease_token)
    repo = ScheduleRepository()
    bar = datetime.fromisoformat(bar_close_at)
    if not execution_enabled():
        repo.release_pending(user_id, id, lease_token, bar)
        return {'skipped': True}
    started = False
    try:
        started = repo.start_reserved(user_id, id, revision, lease_token, bar, utc_now())
        if not started:
            repo.release_pending(user_id, id, lease_token, bar)
            return {'skipped': True}
        result = AnalysisService(repo).run_once(user_id, id, expected_revision=revision, expected_bar=bar)
        return {'record_id': result['id']}
    finally:
        # 失败仍保留 last_attempt_bar；丢失进程时租约到期自动释放，不重复当前周期的模型请求。
        if started:
            repo.release(user_id, id, lease_token)
