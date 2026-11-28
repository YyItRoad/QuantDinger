"""Independent order dispatch and exchange reconciliation loops."""
from app.utils.logger import get_logger

logger = get_logger(__name__)


class PendingOrderLoops:
    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"PendingOrderWorker tick error: {e}")
            self._stop_event.wait(self.poll_interval_sec)

    def _run_sync_loop(self) -> None:
        while not self._stop_event.is_set():
            for sync in (self._sync_discipline_exits, self._sync_quick_trade_orders,
                         self._sync_alpaca_sent_orders, self._sync_live_sent_orders, self._maybe_sync_positions,
                         self._sync_discipline_notices):
                if self._stop_event.is_set() or (self.lease_guard and not self.lease_guard()):
                    break
                try:
                    sync()
                except Exception:
                    logger.warning("Order reconciliation failed: %s", sync.__name__, exc_info=True)
            self._stop_event.wait(self.poll_interval_sec)

    def _run_batch_loop(self) -> None:
        from app.services.pending_orders.rebalance_batches import reconcile_order_batches

        while not self._stop_event.is_set():
            try:
                if not self.lease_guard or self.lease_guard():
                    reconcile_order_batches(self._notifier)
            except Exception:
                logger.warning("Order batch reconciliation failed", exc_info=True)
            self._stop_event.wait(self.poll_interval_sec)

    def _sync_discipline_exits(self) -> None:
        # Trading Worker 接手 Scheduler 持久化的退出请求，执行前持续检查当前租约。
        if self.lease_guard is None:
            return
        from app.trade_discipline.exit import process_discipline_exits

        process_discipline_exits(still_active=lambda: not self._stop_event.is_set() and self.lease_guard())

    def _sync_discipline_notices(self) -> None:
        # 只登记通知与补入有限任务队列，通知渠道的网络请求不在交易循环中运行。
        if self.lease_guard is None:
            return
        from app.trade_discipline.notifications import process_discipline_notices

        process_discipline_notices(still_active=lambda: not self._stop_event.is_set() and self.lease_guard())

    def _tick(self) -> None:
        if self.lease_guard and not self.lease_guard():
            return
        orders = self._fetch_pending_orders(limit=self.batch_size)
        if not orders:
            return

        for o in orders:
            if self._stop_event.is_set() or (self.lease_guard and not self.lease_guard()):
                break
            oid = o.get("id")
            if not oid:
                continue

            # Mark processing (best-effort)
            if not self._mark_processing(order_id=int(oid)):
                continue

            try:
                self._dispatch_one(o)
            except Exception as e:
                self._mark_failed(order_id=int(oid), error=str(e))

