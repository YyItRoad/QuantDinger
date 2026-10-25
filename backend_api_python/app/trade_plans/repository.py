"""PostgreSQL storage; aggregate writes and request replay share a transaction."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from psycopg2.extras import Json
from app.utils.db import get_db_transaction


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


class PlanRepository:
    @contextmanager
    def transaction(self):
        with get_db_transaction() as db, closing(db.cursor()) as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            yield cur

    def reserve(self, cur, uid, request_id, operation, fingerprint):
        cur.execute('''INSERT INTO qd_trade_plan_requests(user_id,request_id,operation,request_hash)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id''',
            (uid, request_id, operation, fingerprint))
        inserted = bool(cur.fetchone())
        cur.execute('SELECT * FROM qd_trade_plan_requests WHERE user_id=%s AND request_id=%s', (uid, request_id))
        return inserted, public(cur.fetchone())

    def finish(self, cur, uid, request_id, response):
        cur.execute('UPDATE qd_trade_plan_requests SET response=%s WHERE user_id=%s AND request_id=%s',
                    (Json(response), uid, request_id))

    def request(self, cur, uid, request_id):
        cur.execute('SELECT request_id,operation,response,created_at FROM qd_trade_plan_requests WHERE user_id=%s AND request_id=%s', (uid, request_id))
        return public(cur.fetchone())

    def plan(self, cur, uid, plan_id, lock=False):
        # Lock the aggregate before joining its replaceable current-version pointer.
        # A concurrent pointer update can otherwise make PostgreSQL recheck an old join.
        if lock:
            cur.execute('SELECT id FROM qd_trade_plans WHERE user_id=%s AND id=%s FOR UPDATE', (uid, plan_id))
            if not cur.fetchone():
                return None
        cur.execute('''SELECT p.*,v.snapshot,v.version_no,v.content_hash,v.created_at AS version_created_at
            FROM qd_trade_plans p JOIN qd_trade_plan_versions v ON v.id=p.current_version_id
            WHERE p.user_id=%s AND p.id=%s''', (uid, plan_id))
        return public(cur.fetchone())

    def listing(self, cur, uid, scope, filters, now, page, size):
        visible_sql, params = scope.sql()
        conditions = ['p.user_id=%s', visible_sql]
        params = [uid, *params]
        link_visible, link_params = scope.sql("jsonb_build_object('instrument', l.snapshot)")
        if scope._restricted(scope.markets):
            link_visible += " AND COALESCE(l.snapshot->>'market','') <> ''"
        if scope._restricted(scope.instruments):
            link_visible += " AND COALESCE(l.snapshot->>'symbol','') <> ''"
        conditions.append('NOT EXISTS (SELECT 1 FROM qd_trade_plan_links l WHERE l.user_id=p.user_id AND l.plan_id=p.id AND NOT (' + link_visible + '))')
        params.extend(link_params)
        if filters.get('source_kind'):
            kind, source_id = filters['source_kind'], int(filters['source_id'])
            identity = '(linked.source_kind=%s AND linked.source_id=%s)'
            source_params = [kind, source_id]
            if kind == 'trade':
                identity += ' OR linked.trade_id=%s'
                source_params.append(source_id)
            elif kind == 'history':
                identity += ' OR linked.trade_id=(SELECT h.source_trade_id FROM qd_position_management_trade_history h WHERE h.user_id=%s AND h.id=%s)'
                source_params.extend([uid, source_id])
            conditions.append('EXISTS (SELECT 1 FROM qd_trade_plan_links linked WHERE linked.user_id=p.user_id AND linked.plan_id=p.id AND NOT linked.revoked AND (' + identity + '))')
            params.extend(source_params)
        if filters.get('lifecycle'):
            conditions.append("CASE WHEN p.lifecycle IN ('draft','observing') AND (v.snapshot->>'expires_at')::timestamptz <= %s THEN 'expired' ELSE p.lifecycle END = %s")
            params.extend([now, filters['lifecycle']])
        if filters.get('direction'):
            conditions.append("v.snapshot->>'direction' = %s")
            params.append(filters['direction'])
        for key, expression in (
            ('q', "CONCAT(v.snapshot->>'title', ' ', v.snapshot->'instrument'->>'symbol')"),
            ('primary_strategy', "v.snapshot->>'primary_strategy'"),
        ):
            if filters.get(key):
                conditions.append(f"POSITION(%s IN LOWER({expression})) > 0")
                params.append(filters[key].strip().lower())
        if filters.get('retrospective') in ('true', 'false'):
            conditions.append("COALESCE((v.snapshot->>'retrospective')::boolean, FALSE) = %s")
            params.append(filters['retrospective'] == 'true')
        base = ' FROM qd_trade_plans p JOIN qd_trade_plan_versions v ON v.id=p.current_version_id WHERE ' + ' AND '.join(conditions)
        cur.execute('SELECT COUNT(*) AS total' + base, tuple(params))
        total = cur.fetchone()['total']
        cur.execute("""SELECT p.*, v.snapshot, v.version_no, v.content_hash,
            v.created_at AS version_created_at,
            COALESCE((SELECT e.payload->>'status' FROM qd_trade_plan_events e
                WHERE e.user_id=p.user_id AND e.plan_id=p.id
                  AND e.plan_version_id=p.current_version_id AND e.event_type='signal_assessment'
                ORDER BY e.recorded_at DESC,e.id DESC LIMIT 1), 'unassessed') AS signal_status
            """ + base + ' ORDER BY p.updated_at DESC,p.id DESC LIMIT %s OFFSET %s',
            tuple([*params, size, (page-1)*size]))
        return [public(row) for row in cur.fetchall()], total

    def versions(self, cur, uid, plan_id):
        cur.execute('SELECT * FROM qd_trade_plan_versions WHERE user_id=%s AND plan_id=%s ORDER BY version_no DESC', (uid, plan_id))
        return [public(row) for row in cur.fetchall()]

    def events(self, cur, uid, plan_id):
        cur.execute('SELECT * FROM qd_trade_plan_events WHERE user_id=%s AND plan_id=%s ORDER BY recorded_at,id', (uid, plan_id))
        return [public(row) for row in cur.fetchall()]

    def new_plan(self, cur, uid, plan_id, parent_id):
        cur.execute('INSERT INTO qd_trade_plans(id,user_id,parent_plan_id) VALUES (%s,%s,%s)', (plan_id, uid, parent_id))

    def new_version(self, cur, uid, plan_id, vid, version_no, snapshot, digest, reason, source, actor):
        cur.execute('''INSERT INTO qd_trade_plan_versions
            (id,user_id,plan_id,version_no,snapshot,content_hash,change_reason,source,actor_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (vid, uid, plan_id, version_no, Json(snapshot), digest, reason, source, actor))
        cur.execute('UPDATE qd_trade_plans SET current_version_id=%s WHERE user_id=%s AND id=%s', (vid, uid, plan_id))

    def update(self, cur, uid, plan_id, lifecycle):
        cur.execute('UPDATE qd_trade_plans SET lifecycle=%s,row_revision=row_revision+1,updated_at=NOW() WHERE user_id=%s AND id=%s', (lifecycle, uid, plan_id))

    def event(self, cur, eid, uid, plan_id, vid, kind, payload, occurred_at, source, actor):
        cur.execute('''INSERT INTO qd_trade_plan_events
            (id,user_id,plan_id,plan_version_id,event_type,payload,occurred_at,source,actor_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (eid, uid, plan_id, vid, kind, Json(payload), occurred_at, source, actor))

    def research(self, cur, uid, rid):
        cur.execute('''SELECT id,market,symbol,exchange_id,market_type,instrument_id,timeframe,
            bar_close_at,trend,structure,summary FROM qd_market_state_results WHERE user_id=%s AND id=%s''', (uid, rid))
        return public(cur.fetchone())
