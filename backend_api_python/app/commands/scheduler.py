"""Scheduler process entrypoint."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def main() -> None:
    # 交易纪律的常驻入口由 scheduler-worker 启动。网页请求和 CLI 各走自己的入口。
    # 完整调用顺序及 VS Code 断点见 docs/trading/交易纪律代码导读.md。
    os.environ["QD_PROCESS_ROLE"] = "scheduler"

    from app import create_app
    from app.runtime.process import ShutdownSignal
    from app.startup import _start_scheduler_services
    from app.services.strategy_command_repository import StrategyCommandRepository
    from app.trade_discipline.observation import hourly_observer_from_env
    from app.utils.logger import get_logger
    from app.workers.trading import build_worker_id

    app = create_app(register_http_routes=False)
    shutdown = ShutdownSignal()
    shutdown.install()
    repository = StrategyCommandRepository()
    worker_id = build_worker_id()
    lease_key = "scheduler-global-services"
    lease_seconds = max(10, int(os.getenv("SCHEDULER_LEASE_SEC", "30")))
    leader = False
    observer = None
    logger = get_logger(__name__)
    with app.app_context():
        try:
            while not shutdown.event.is_set():
                if not leader:
                    leader = repository.acquire_process_lease(
                        lease_key=lease_key,
                        owner_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
                    # 拿到数据库租约后才创建观察器。同一时刻由一个 Scheduler 负责定时观察。
                    if leader:
                        _start_scheduler_services()
                        try:
                            observer = hourly_observer_from_env(datetime.now(timezone.utc))
                        except ValueError as exc:
                            logger.error("Trade discipline observation configuration invalid: %s", exc)
                else:
                    leader = repository.renew_process_lease(
                        lease_key=lease_key,
                        owner_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
                    if not leader:
                        break
                repository.record_worker_heartbeat(
                    worker_id=worker_id,
                    role="scheduler",
                    metadata={"leader": leader},
                )
                # Scheduler 每 10 秒检查是否到点，实际观察由后台线程执行。
                if observer is not None:
                    observer.tick(datetime.now(timezone.utc))
                shutdown.event.wait(10)
        finally:
            if observer is not None:
                observer.stop()
            if leader:
                repository.release_process_lease(lease_key=lease_key, owner_id=worker_id)
            repository.mark_worker_stopped(worker_id)


if __name__ == "__main__":
    main()
