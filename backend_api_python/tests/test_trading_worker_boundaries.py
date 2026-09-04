"""Trading worker ownership and command boundary tests."""

from __future__ import annotations

from app.services.strategy_command_repository import StrategyCommand
from app.workers.trading import TradingWorker


class FakeExecutor:
    def __init__(self) -> None:
        self.running_strategies = {}
        self.lock = __import__("threading").Lock()
        self.stopped = []

    def stop_strategy(self, strategy_id, persist_status=False):
        del persist_status
        self.stopped.append(int(strategy_id))
        self.running_strategies.pop(int(strategy_id), None)
        return True

    def start_strategy(self, strategy_id):
        self._last_start_failure = f"temporary network failure for {strategy_id}"
        return False


class FakeRepository:
    def __init__(self) -> None:
        self.completed = []
        self.failed = []
        self.released = []

    def complete(self, command_id, result=None):
        self.completed.append((command_id, result))

    def fail(self, command_id, error, retry_delay_seconds=None):
        self.failed.append((command_id, error, retry_delay_seconds))

    def release_strategy_lease(self, *, strategy_id, owner_id):
        self.released.append((strategy_id, owner_id))

    def acquire_strategy_lease(self, *, strategy_id, owner_id, lease_seconds):
        del strategy_id, owner_id, lease_seconds
        return 1


def _command(command_type: str) -> StrategyCommand:
    return StrategyCommand(
        id=1,
        strategy_id=55,
        user_id=1,
        command_type=command_type,
        status="processing",
        idempotency_key="test-command",
        payload={},
        attempts=1,
    )


def test_stop_command_is_executed_by_trading_worker():
    repository = FakeRepository()
    executor = FakeExecutor()
    worker = TradingWorker(executor, repository)

    worker._execute(_command("stop"))

    assert executor.stopped == [55]
    assert repository.released == [(55, worker.worker_id)]
    assert repository.completed[0][1]["status"] == "stopped"


def test_failed_command_is_retried_with_backoff(monkeypatch):
    repository = FakeRepository()
    worker = TradingWorker(FakeExecutor(), repository)
    monkeypatch.setattr(worker, "_start", lambda _strategy_id: (_ for _ in ()).throw(RuntimeError("boom")))

    worker._execute(_command("start"))

    assert repository.failed == [(1, "boom", 1)]


def test_restore_renews_ownership_around_each_strategy_without_stopping_desired_state(monkeypatch):
    class FakeStrategyService:
        status_updates = []

        def get_running_strategies_with_type(self):
            return [{"id": 10}, {"id": 20}]

        def update_strategy_status(self, strategy_id, status):
            self.status_updates.append((strategy_id, status))

    monkeypatch.setattr("app.services.strategy.StrategyService", FakeStrategyService)
    repository = FakeRepository()
    worker = TradingWorker(FakeExecutor(), repository)
    maintenance_calls = []
    monkeypatch.setattr(worker, "_maintain_ownership", lambda: maintenance_calls.append(True))

    worker.restore_desired_strategies()

    assert len(maintenance_calls) == 4
    assert repository.released == [(10, worker.worker_id), (20, worker.worker_id)]
    assert FakeStrategyService.status_updates == []
