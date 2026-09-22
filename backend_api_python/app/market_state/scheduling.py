"""独立调度存储：队列租约及每根收盘 K 线最多一次自动尝试。"""
from contextlib import closing
from datetime import datetime, timezone
import os
from uuid import uuid4

from app.market_state.constants import TIMEFRAME_SECONDS
from app.market_state.repository import AnalysisRepository, public
from app.utils.db import get_db_transaction


def execution_enabled():
    return os.getenv('MARKET_STATE_EXECUTION_ENABLED', '').lower() in ('1', 'true', 'yes', 'on')


def latest_bar_close(now, timeframe):
    """返回 24 小时市场当前最近一个已完成周期的 UTC 收盘边界。"""
    step = TIMEFRAME_SECONDS.get(timeframe)
    if step is None or not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('无法确定分析周期收盘时间')
    timestamp = int(now.timestamp() // step) * step
    return datetime.fromtimestamp(timestamp, timezone.utc)


class ScheduleRepository(AnalysisRepository):
    def reserve_manual(self, user_id, task_id, now):
        """手动执行复用调度租约，不更改启停状态或自动尝试记录。"""
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('''
                UPDATE qd_market_state_tasks SET lease_token=%s,
                    lease_until=%s::timestamptz+INTERVAL '30 minutes'
                WHERE id=%s AND user_id=%s AND deleted_at IS NULL
                    AND market='Crypto'
                    AND (lease_until IS NULL OR lease_until<=%s)
                RETURNING id, user_id, revision, lease_token::text
            ''', (str(uuid4()), now, task_id, user_id, now))
            return public(cur.fetchone())

    def reserve_due(self, now, limit=100):
        """短事务领取队列租约；多个 Beat 同时扫描也不重复派发。"""
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('''
                WITH due AS (
                    SELECT t.id, to_timestamp(floor(extract(epoch FROM %s::timestamptz) /
                        CASE t.timeframe WHEN '1h' THEN 3600 WHEN '4h' THEN 14400 ELSE 86400 END) *
                        CASE t.timeframe WHEN '1h' THEN 3600 WHEN '4h' THEN 14400 ELSE 86400 END) AS bar
                    FROM qd_market_state_tasks t
                    WHERE t.enabled AND t.deleted_at IS NULL AND t.market='Crypto'
                        AND t.exchange_id<>'' AND (t.lease_until IS NULL OR t.lease_until<=%s)
                ), candidates AS (
                    SELECT t.id, due.bar FROM qd_market_state_tasks t JOIN due ON due.id=t.id
                    WHERE due.bar<=%s::timestamptz-INTERVAL '30 seconds'
                        AND (t.lease_until IS NULL OR t.lease_until<=%s)
                        AND (t.last_attempt_bar IS NULL OR t.last_attempt_bar<due.bar)
                        AND NOT EXISTS (SELECT 1 FROM qd_market_state_results r WHERE r.task_id=t.id AND r.bar_close_at=due.bar)
                    ORDER BY t.id LIMIT %s FOR UPDATE OF t SKIP LOCKED
                )
                UPDATE qd_market_state_tasks t SET lease_token=%s, lease_until=%s::timestamptz+INTERVAL '30 minutes'
                FROM candidates c WHERE t.id=c.id
                RETURNING t.id, t.user_id, t.revision, t.lease_token::text, c.bar AS bar_close_at
            ''', (now, now, now, now, limit, str(uuid4()), now))
            return [public(row) for row in cur.fetchall()]

    def start_reserved(self, user_id, task_id, revision, token, bar, now):
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('''
                UPDATE qd_market_state_tasks t
                SET last_attempt_bar=%s, lease_until=%s::timestamptz+INTERVAL '15 minutes'
                WHERE id=%s AND user_id=%s AND revision=%s AND lease_token=%s
                    AND lease_until>%s AND enabled AND deleted_at IS NULL
                    AND (last_attempt_bar IS NULL OR last_attempt_bar<%s)
                    AND %s=to_timestamp(floor(extract(epoch FROM %s::timestamptz) /
                        CASE timeframe WHEN '1h' THEN 3600 WHEN '4h' THEN 14400 ELSE 86400 END) *
                        CASE timeframe WHEN '1h' THEN 3600 WHEN '4h' THEN 14400 ELSE 86400 END)
                    AND %s<=%s::timestamptz-INTERVAL '30 seconds'
                    AND NOT EXISTS (SELECT 1 FROM qd_market_state_results r WHERE r.task_id=t.id AND r.bar_close_at=%s)
                RETURNING id
            ''', (bar, now, task_id, user_id, revision, token, now, bar, bar, now, bar, now, bar))
            return cur.fetchone() is not None

    def release(self, user_id, task_id, token):
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('UPDATE qd_market_state_tasks SET lease_token=NULL, lease_until=NULL '
                        'WHERE id=%s AND user_id=%s AND lease_token=%s', (task_id, user_id, token))

    def release_pending(self, user_id, task_id, token, bar):
        # 发布异常可能只是确认丢失；已开始的消息不能被发布者解除运行租约。
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('UPDATE qd_market_state_tasks SET lease_token=NULL, lease_until=NULL '
                        'WHERE id=%s AND user_id=%s AND lease_token=%s '
                        'AND (last_attempt_bar IS NULL OR last_attempt_bar<%s)', (task_id, user_id, token, bar))


def utc_now():
    return datetime.now(timezone.utc)
