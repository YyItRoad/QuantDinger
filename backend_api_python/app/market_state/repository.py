"""正式分析存储，复用项目 PostgreSQL 连接与事务；不负责分析和调度。"""
from contextlib import closing
from datetime import datetime, timezone

from psycopg2.errors import UniqueViolation
from psycopg2.extras import Json

from app.utils.db import get_db_transaction


IDENTITY = ('market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'timeframe')
RESULT_FIELDS = ('trend', 'structure', 'ma_state', 'position', 'momentum', 'phase', 'confidence', 'summary')


def public(row):
    if row is None:
        return None
    return {
        key: value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value
        for key, value in dict(row).items()
    }


class AnalysisRepository:
    def list(self, kind, user_id, page, page_size, symbol='', timeframe=''):
        if kind not in ('tasks', 'records'):
            raise ValueError('不支持的列表类型')
        table = 'qd_market_state_tasks' if kind == 'tasks' else 'qd_market_state_results'
        base = 'user_id = %s' + (' AND deleted_at IS NULL' if kind == 'tasks' else '')
        where, args = base, [user_id]
        for key, value in (('symbol', symbol), ('timeframe', timeframe)):
            if value:
                where += f' AND {key} = %s'
                args.append(value)
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute(f'SELECT DISTINCT symbol FROM {table} WHERE {base} ORDER BY symbol', (user_id,))
            symbols = [row['symbol'] for row in cur.fetchall()]
            cur.execute(f'SELECT COUNT(*) AS total FROM {table} WHERE {where}', tuple(args))
            total = cur.fetchone()['total']
            page = min(page, max(1, (total + page_size - 1) // page_size))
            columns = '*' if kind == 'tasks' else ', '.join(
                ('id', 'user_id', 'task_id', *IDENTITY, 'bar_close_at', 'created_at', *RESULT_FIELDS))
            cur.execute(
                f'SELECT {columns} FROM {table} WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s',
                (*args, page_size, (page - 1) * page_size))
            items = [public(row) for row in cur.fetchall()]
        return dict(items=items, symbols=symbols, total=total, page=page, page_size=page_size)

    def get_record(self, user_id, record_id):
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute('SELECT * FROM qd_market_state_results WHERE user_id=%s AND id=%s', (user_id, record_id))
            return public(cur.fetchone())

    def create_task(self, user_id, value):
        try:
            with get_db_transaction() as db, closing(db.cursor()) as cur:
                cur.execute(
                    'INSERT INTO qd_market_state_tasks (user_id, ' + ', '.join(IDENTITY) + ') '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *',
                    (user_id, *(value[key] for key in IDENTITY)))
                return public(cur.fetchone())
        except UniqueViolation as exc:
            if exc.diag.constraint_name == 'uq_market_state_active_task':
                raise ValueError('该品种及周期已存在分析任务，请启动已有任务') from exc
            raise

    def change_task(self, user_id, task_id, *, enabled=None, delete=False):
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute(
                'UPDATE qd_market_state_tasks SET enabled=%s, revision=revision+1, updated_at=NOW(), '
                'deleted_at=CASE WHEN %s THEN NOW() ELSE deleted_at END '
                'WHERE user_id=%s AND id=%s AND deleted_at IS NULL RETURNING *',
                (False if delete else enabled, delete, user_id, task_id))
            return public(cur.fetchone())

    def save_result(self, user_id, task_id, expected_revision, bar_close_at, result):
        """供后续单次分析调用，不提供外部写入接口。旧任务执行结果不得覆盖新状态。"""
        if not isinstance(bar_close_at, datetime) or bar_close_at.tzinfo is None:
            raise ValueError('K 线收盘时间必须带时区')
        if bar_close_at > datetime.now(timezone.utc):
            raise ValueError('不能保存未收盘 K 线的结果')
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute(
                'SELECT * FROM qd_market_state_tasks WHERE user_id=%s AND id=%s FOR UPDATE',
                (user_id, task_id))
            task = cur.fetchone()
            if not task or task['deleted_at'] or not task['enabled'] or task['revision'] != expected_revision:
                raise ValueError('分析任务已停止、删除或发生变更')
            columns = ('user_id', 'task_id', *IDENTITY, 'bar_close_at', *RESULT_FIELDS, 'details')
            values = (user_id, task_id, *(task[key] for key in IDENTITY), bar_close_at,
                      *(result[key] for key in RESULT_FIELDS), Json(result.get('details', {})))
            cur.execute(
                'INSERT INTO qd_market_state_results (' + ', '.join(columns) + ') VALUES (' +
                ', '.join(['%s'] * len(columns)) + ') ON CONFLICT (task_id, bar_close_at) DO NOTHING RETURNING *',
                values)
            row = cur.fetchone()
            if row is None:
                cur.execute('SELECT * FROM qd_market_state_results WHERE user_id=%s AND task_id=%s AND bar_close_at=%s',
                            (user_id, task_id, bar_close_at))
                row = cur.fetchone()
            return public(row)
