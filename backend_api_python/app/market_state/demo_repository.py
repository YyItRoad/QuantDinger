"""隔离的演示数据存储；不连接业务数据库，不执行分析或调度。

SQLite只作为可替换的假数据适配器，便于多个API进程共享演示操作。
这不是正式任务表/结果表迁移；后续接入PostgreSQL时保持接口契约不变。
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class DemoRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS demo_users (id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS demo_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS demo_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def unpack(row):
        return {**json.loads(row['payload']), 'id': row['id']}

    @staticmethod
    def insert(db, table, user_id, value):
        cursor = db.execute(f'INSERT INTO {table}(user_id,payload) VALUES (?,?)',
                            (user_id, json.dumps(value, ensure_ascii=False)))
        return {**value, 'id': cursor.lastrowid}

    def seed(self, user_id):
        """每个用户仅初始化一次；删除全部任务后也不重新播种。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT id FROM demo_users WHERE id=?', (user_id,)).fetchone():
                return
            base = datetime(2026, 9, 16, 0, tzinfo=timezone.utc)
            tasks = []
            for i, (symbol, timeframe) in enumerate([
                ('BTC/USDT', '1d'), ('BTC/USDT', '4h'), ('SOL/USDT', '1h')
            ]):
                tasks.append(self.insert(db, 'demo_tasks', user_id, {
                    'market': 'Crypto', 'symbol': symbol, 'exchange_id': 'binance',
                    'market_type': 'swap', 'instrument_id': symbol.replace('/', ''),
                    'timeframe': timeframe, 'enabled': True, 'deleted_at': None,
                    'created_at': (base - timedelta(days=2, minutes=i)).isoformat(),
                    'updated_at': base.isoformat(),
                }))
            # 超过一页，支持实际检查后端分页及跨页币种筛选。
            for i in range(24):
                task = tasks[i % len(tasks)]
                closed = base - timedelta(days=i) if task['timeframe'] == '1d' else base - timedelta(hours=4*i)
                self.insert(db, 'demo_records', user_id, {
                    **{k: task[k] for k in ('market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'timeframe')},
                    'task_id': task['id'], 'bar_close_at': closed.isoformat(),
                    'created_at': (closed + timedelta(minutes=5)).isoformat(),
                    'trend': 'UP', 'structure': 'PULLBACK', 'ma_state': 'BULL_ALIGNED',
                    'position': 'HIGH', 'momentum': 'DOWN', 'phase': 'ADVANCE',
                    'confidence': 4,
                    'summary': '示例：上涨结构保持，当前回调尚未破坏关键低点。',
                    'details': {
                        'evidence': ['示例：有效波段高点和低点仍抬高。', '示例：关键波段低点尚未被收盘有效跌破。'],
                        'counter_evidence': ['示例：短期动能向下，后续能否恢复上涨仍待确认。'],
                        'demo': True,
                    },
                })
            db.execute('INSERT INTO demo_users(id) VALUES (?)', (user_id,))

    def list(self, kind, user_id, page, page_size, symbol='', timeframe=''):
        table = 'demo_tasks' if kind == 'tasks' else 'demo_records'
        with self.connect() as db:
            rows = [self.unpack(r) for r in db.execute(f'SELECT * FROM {table} WHERE user_id=?', (user_id,))]
        if kind == 'tasks':
            rows = [r for r in rows if not r['deleted_at']]
        symbols = sorted({r['symbol'] for r in rows})
        rows = [r for r in rows if (not symbol or r['symbol'] == symbol) and (not timeframe or r['timeframe'] == timeframe)]
        rows.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
        total = len(rows)
        page = min(page, max(1, (total + page_size - 1) // page_size))
        items = rows[(page - 1)*page_size:page*page_size]
        if kind == 'records':
            items = [{k: v for k, v in r.items() if k != 'details'} for r in items]
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size, 'symbols': symbols}

    def get_record(self, user_id, record_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM demo_records WHERE user_id=? AND id=?', (user_id, record_id)).fetchone()
        return self.unpack(row) if row else None

    def create_task(self, user_id, value):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # 搜索来源可能不提供原生 instrument_id；不能因此重复创建同一任务。
            keys = ('market', 'symbol', 'exchange_id', 'market_type', 'timeframe')
            existing = [self.unpack(r) for r in db.execute('SELECT * FROM demo_tasks WHERE user_id=?', (user_id,))]
            if any(not r['deleted_at'] and all(r[k] == value[k] for k in keys) for r in existing):
                raise ValueError('该品种及周期已存在分析任务，请启动已有任务')
            return self.insert(db, 'demo_tasks', user_id, {
                **value, 'enabled': True, 'deleted_at': None,
                'created_at': now_iso(), 'updated_at': now_iso(),
            })

    def change_task(self, user_id, task_id, *, enabled=None, delete=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM demo_tasks WHERE user_id=? AND id=?', (user_id, task_id)).fetchone()
            if not row:
                return None
            value = self.unpack(row)
            if value['deleted_at']:
                return None
            value['enabled'] = False if delete else enabled
            value['updated_at'] = now_iso()
            if delete:
                value['deleted_at'] = now_iso()
            db.execute('UPDATE demo_tasks SET payload=? WHERE user_id=? AND id=?',
                       (json.dumps(value, ensure_ascii=False), user_id, task_id))
            return value
