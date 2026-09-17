"""调度入口测试，不启动 Broker、Worker 或真实分析。"""
from unittest.mock import Mock
import pytest

from app.market_state import tasks
from app.market_state.scheduling import execution_enabled


ITEM = dict(id=1, user_id=2, revision=1, lease_token='00000000-0000-0000-0000-000000000001',
            bar_close_at='2026-09-17T00:00:00+00:00')


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('MARKET_STATE_EXECUTION_ENABLED', 'true')
    monkeypatch.delenv('MARKET_STATE_DEMO_ENABLED', raising=False)
    repo = Mock()
    repo.reserve_due.return_value = [ITEM]
    repo.start_reserved.return_value = True
    service = Mock()
    service.run_once.return_value = {'id': 10}
    monkeypatch.setattr(tasks, 'ScheduleRepository', lambda: repo)
    monkeypatch.setattr(tasks, 'AnalysisService', lambda _: service)
    publish = Mock()
    monkeypatch.setattr(tasks.execute_analysis, 'apply_async', publish)
    return repo, service, publish


def test_default_disabled_and_demo_never_runs(setup, monkeypatch):
    repo, service, publish = setup
    monkeypatch.delenv('MARKET_STATE_EXECUTION_ENABLED')
    assert not execution_enabled()
    assert tasks.dispatch_analysis.run() == {'skipped': True}
    assert tasks.execute_analysis.run(**ITEM) == {'skipped': True}
    monkeypatch.setenv('MARKET_STATE_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('MARKET_STATE_DEMO_ENABLED', 'true')
    assert not execution_enabled()
    tasks.dispatch_analysis.run()
    repo.reserve_due.assert_not_called()
    service.run_once.assert_not_called()
    publish.assert_not_called()


def test_dispatch_uses_reserved_identity(setup):
    _, _, publish = setup
    assert tasks.dispatch_analysis.run() == {'dispatched': 1}
    publish.assert_called_once_with(kwargs=ITEM, expires=1800)


def test_publish_failure_only_releases_pending_lease(setup):
    repo, _, publish = setup
    publish.side_effect = RuntimeError('broker unavailable')
    assert tasks.dispatch_analysis.run() == {'dispatched': 0}
    repo.release_pending.assert_called_once_with(2, 1, ITEM['lease_token'], ITEM['bar_close_at'])
    repo.release.assert_not_called()


def test_worker_passes_revision_and_bar_and_releases(setup):
    repo, service, _ = setup
    assert tasks.execute_analysis.run(**ITEM) == {'record_id': 10}
    assert service.run_once.call_args.kwargs['expected_revision'] == 1
    assert service.run_once.call_args.kwargs['expected_bar'].isoformat() == ITEM['bar_close_at']
    repo.release.assert_called_once_with(2, 1, ITEM['lease_token'])


def test_duplicate_worker_does_not_release_running_lease(setup):
    repo, service, _ = setup
    repo.start_reserved.return_value = False
    assert tasks.execute_analysis.run(**ITEM) == {'skipped': True}
    service.run_once.assert_not_called()
    repo.release.assert_not_called()


def test_analysis_failure_propagates_without_retry(setup):
    repo, service, _ = setup
    service.run_once.side_effect = ValueError('行情不足')
    with pytest.raises(ValueError):
        tasks.execute_analysis.run(**ITEM)
    repo.release.assert_called_once()
    assert service.run_once.call_count == 1


def test_celery_registration_uses_existing_queues():
    from app.celery_app import celery_app
    assert 'app.market_state.tasks' in celery_app.conf.imports
    assert celery_app.conf.task_routes['quantdinger.tasks.market_state_execute']['queue'] == 'ai'
    assert celery_app.conf.task_routes['quantdinger.tasks.market_state_tick']['queue'] == 'maintenance'
    assert celery_app.conf.beat_schedule['market-state-scan']['schedule'] == 60
    assert tasks.execute_analysis.time_limit == 600
    assert tasks.execute_analysis.soft_time_limit == 540
