"""Feature-owned attempts, source snapshots and append-only reviews."""
from uuid import uuid4
from copy import deepcopy
from psycopg2.extras import Json
from psycopg2.errors import UniqueViolation
from . import execution_sources as sources
from .repository import public
from .validation import PlanError, check, text
from .record_summary import summarize

RECORD_FIELDS = {
    'attempt': {'version_id', 'credential_id', 'execution_mode', 'note'},
    'link': {'attempt_id', 'source_kind', 'source_id', 'attribution', 'reason'},
    'unlink': {'attempt_id', 'link_id', 'reason'},
    'refresh_link': {'attempt_id', 'link_id', 'reason', 'attribution'},
    'review': {'attempt_id', 'assessment', 'change_reason'},
}


class RecordManager:
    def __init__(self, service):
        self.service = service

    def visible(self, cur, user_id, plan_id):
        scope = self.service.scope
        if not scope._restricted(scope.markets) and not scope._restricted(scope.instruments):
            return True
        cur.execute('SELECT snapshot FROM qd_trade_plan_links WHERE user_id=%s AND plan_id=%s', (user_id, plan_id))
        return all(self.source_visible(r['snapshot']) for r in cur.fetchall())

    def source_visible(self, fact):
        scope = self.service.scope
        if scope._restricted(scope.markets) and not fact.get('market'):
            return False
        if scope._restricted(scope.instruments) and not fact.get('symbol'):
            return False
        return self.service.visible({'instrument': fact})

    def version(self, cur, user_id, row, version_id):
        version = next((v for v in self.service.repo.versions(cur, user_id, row['id'])
                        if v['id'] == version_id and self.service.visible(v['snapshot'])), None)
        if not version:
            raise PlanError('version_not_found', 404)
        return version

    def detail(self, cur, user_id, row, allowed_ids):
        cur.execute('SELECT * FROM qd_trade_plan_attempts WHERE user_id=%s AND plan_id=%s ORDER BY attempt_no', (user_id, row['id']))
        attempts = [public(r) for r in cur.fetchall() if str(r['plan_version_id']) in allowed_ids]
        cur.execute('SELECT * FROM qd_trade_plan_links WHERE user_id=%s AND plan_id=%s ORDER BY recorded_at,id', (user_id, row['id']))
        links = [public(r) for r in cur.fetchall()]
        cur.execute('SELECT * FROM qd_trade_plan_reviews WHERE user_id=%s AND plan_id=%s ORDER BY review_no DESC', (user_id, row['id']))
        reviews = [public(r) for r in cur.fetchall()]
        versions = {v['id']: v for v in self.service.repo.versions(cur, user_id, row['id'])}
        for attempt in attempts:
            attempt['links'] = [r for r in links if r['attempt_id'] == attempt['id']]
            attempt['reviews'] = [r for r in reviews if r['attempt_id'] == attempt['id']]
            version = versions[attempt['plan_version_id']]
            attempt['version_no'] = version['version_no']
            attempt['plan_snapshot'] = version['snapshot']
            attempt['summary'] = summarize(attempt, attempt['links'], version['snapshot'], self.service.now())
        return attempts

    def source_list(self, user_id, plan_id, filters):
        allowed = {'version_id', 'credential_id', 'source_kind', 'source_id', 'page', 'page_size', '_t'}
        if set(filters) - allowed:
            raise PlanError('unknown_filters')
        with self.service.repo.transaction() as cur:
            row = self.service.require_plan(cur, user_id, plan_id)
            version = self.version(cur, user_id, row, filters.get('version_id') or row['current_version_id'])
            credential_id = self.service.integer(filters.get('credential_id'), 1, 2**63-1)
            sources.account(cur, user_id, credential_id)
            result = sources.list_sources(cur, user_id, filters.get('source_kind', 'history'), credential_id,
                version['snapshot']['instrument'].get('symbol', ''),
                self.service.integer(filters.get('page', 1), 1, 1000000),
                self.service.integer(filters.get('page_size', 20), 1, 100), self.service.scope,
                self.service.integer(filters['source_id'], 1, 2**63-1) if filters.get('source_id') else None)
            result['items'] = [fact for fact in result['items'] if self.source_visible(fact)]
            # Source queries restrict symbol/account; market mismatches are checked before linkage.
            return result

    def account_list(self, user_id):
        with self.service.repo.transaction() as cur:
            return {'items': sources.accounts(cur, user_id)}

    def compatible(self, attempt, plan, fact):
        if not self.source_visible(fact):
            raise PlanError('record_source_not_found', 404)
        instrument = plan['instrument']
        unknown = []
        pairs = [('credential_id', attempt['account_snapshot']['credential_id']),
                 ('exchange_id', attempt['account_snapshot'].get('exchange_id')),
                 ('execution_mode', attempt['execution_mode']),
                 ('symbol', instrument.get('symbol')), ('market', instrument.get('market')),
                 ('market_type', instrument.get('market_type')), ('inst_id', instrument.get('instrument_id'))]
        for key, expected in pairs:
            actual = fact.get(key)
            if expected and actual and str(expected).upper() != str(actual).upper():
                raise PlanError('record_identity_mismatch', 422, {'fields': [key]})
            if expected and not actual:
                unknown.append(key)
        kind = str(fact.get('trade_type') or '')
        direction = fact.get('position_side') or (kind.rsplit('_', 1)[-1] if kind.endswith(('_long', '_short')) else '')
        if not direction and fact.get('market_type') == 'spot' and (fact.get('side') in ('buy', 'sell') or kind in ('buy', 'sell')):
            direction = 'long'
        if direction and direction != plan['direction']:
            raise PlanError('record_identity_mismatch', 422, {'fields': ['direction']})
        if not direction:
            unknown.append('direction')
        return unknown

    def _append_link(self, cur, user_id, row, attempt, fact, attribution, reason):
        version = self.version(cur, user_id, row, attempt['plan_version_id'])
        unknown = self.compatible(attempt, version['snapshot'], fact)
        if unknown and attribution == 'confirmed':
            raise PlanError('record_identity_incomplete', 422, {'fields': unknown})
        try:
            cur.execute('''INSERT INTO qd_trade_plan_links
              (id,user_id,plan_id,attempt_id,source_kind,source_id,source_key,trade_id,execution_event_id,fill_identity,snapshot,attribution,reason)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
              (str(uuid4()), user_id, row['id'], attempt['id'], fact['source_kind'], fact['source_id'], fact['source_key'],
               fact.get('trade_id'), fact.get('execution_event_id'), fact.get('fill_identity'), Json(fact), attribution, reason))
        except UniqueViolation:
            raise PlanError('record_already_linked', 409) from None

    def guard_replay(self, cur, user_id, row, action, body):
        if action == 'attempt':
            return self.version(cur, user_id, row, body.get('version_id'))
        else:
            cur.execute('SELECT plan_version_id FROM qd_trade_plan_attempts WHERE user_id=%s AND plan_id=%s AND id=%s', (user_id, row['id'], self.uuid(body.get('attempt_id'))))
            attempt = cur.fetchone()
            if not attempt:
                raise PlanError('record_attempt_not_found', 404)
            return self.version(cur, user_id, row, str(attempt['plan_version_id']))

    def mutate(self, cur, user_id, row, action, body, source, actor):
        if action == 'attempt':
            version = self.version(cur, user_id, row, body.get('version_id'))
            snapshot = version['snapshot']
            if not snapshot.get('instrument', {}).get('symbol') or snapshot.get('direction') not in ('long', 'short'):
                raise PlanError('record_plan_identity_required', 422)
            account = sources.account(cur, user_id, self.service.integer(body.get('credential_id'), 1, 2**63-1))
            mode = body.get('execution_mode')
            if mode not in ('live', 'paper'):
                raise PlanError('invalid_record_environment')
            cur.execute('SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM qd_trade_plan_attempts WHERE user_id=%s AND plan_id=%s', (user_id, row['id']))
            number = cur.fetchone()['n']
            attempt_id = str(uuid4())
            cur.execute('''INSERT INTO qd_trade_plan_attempts
              (id,user_id,plan_id,plan_version_id,attempt_no,account_snapshot,execution_mode,risk_snapshot,note,source,actor_id)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
              (attempt_id, user_id, row['id'], version['id'], number, Json(account), mode, Json({**check(snapshot), 'recorded_after_terminal': self.service.effective_state(row) not in ('draft', 'observing')}),
               text(body.get('note', ''), 2000, field='note'), source, actor))
            payload = {'attempt_id': attempt_id, 'adopted_version_id': version['id']}
        else:
            cur.execute('SELECT * FROM qd_trade_plan_attempts WHERE user_id=%s AND plan_id=%s AND id=%s', (user_id, row['id'], self.uuid(body.get('attempt_id'))))
            attempt = public(cur.fetchone())
            if not attempt:
                raise PlanError('record_attempt_not_found', 404)
            self.version(cur, user_id, row, attempt['plan_version_id'])
            payload = {'attempt_id': attempt['id'], 'adopted_version_id': attempt['plan_version_id']}
            reason = text(body.get('reason'), 2000, True, 'reason') if action in ('link', 'unlink', 'refresh_link') else ''
            if action == 'link':
                fact = sources.read_source(cur, user_id, body.get('source_kind'), self.service.integer(body.get('source_id'), 1, 2**63-1))
                if not fact:
                    raise PlanError('record_source_not_found', 404)
                attribution = body.get('attribution')
                if attribution not in ('confirmed', 'pending'):
                    raise PlanError('invalid_record_attribution')
                self._append_link(cur, user_id, row, attempt, fact, attribution, reason)
            elif action in ('unlink', 'refresh_link'):
                cur.execute('SELECT * FROM qd_trade_plan_links WHERE user_id=%s AND attempt_id=%s AND id=%s AND NOT revoked',
                            (user_id, attempt['id'], self.uuid(body.get('link_id'))))
                link = public(cur.fetchone())
                if not link:
                    raise PlanError('record_link_not_found', 404)
                cur.execute('UPDATE qd_trade_plan_links SET revoked=TRUE WHERE id=%s', (link['id'],))
                payload['link_id'] = link['id']
                if action == 'refresh_link':
                    fact = sources.read_source(cur, user_id, link['source_kind'], link['source_id'])
                    if fact is None:
                        fact = deepcopy(link['snapshot'])
                        fact['available'] = False
                    attribution = body.get('attribution', link['attribution'])
                    if attribution not in ('confirmed', 'pending'):
                        raise PlanError('invalid_record_attribution')
                    self._append_link(cur, user_id, row, attempt, fact, attribution, reason)
            elif action == 'review':
                assessment = self.assessment(body.get('assessment'))
                reason = text(body.get('change_reason'), 500, True, 'change_reason')
                allowed_ids = {attempt['plan_version_id']}
                detailed = next(a for a in self.detail(cur, user_id, row, allowed_ids) if a['id'] == attempt['id'])
                evidence = {key: detailed[key] for key in ('plan_version_id', 'version_no', 'plan_snapshot', 'risk_snapshot', 'summary', 'links')}
                evidence['data_as_of'] = self.service.now().isoformat()
                cur.execute('SELECT COALESCE(MAX(review_no),0)+1 AS n FROM qd_trade_plan_reviews WHERE attempt_id=%s', (attempt['id'],))
                n = cur.fetchone()['n']
                cur.execute('''INSERT INTO qd_trade_plan_reviews(id,user_id,plan_id,attempt_id,review_no,assessment,evidence_snapshot,change_reason,source,actor_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (str(uuid4()), user_id, row['id'], attempt['id'], n, Json(assessment), Json(evidence), reason, source, actor))
                payload['review_no'] = n
            payload['reason'] = reason
        self.service.repo.update(cur, user_id, row['id'], row['lifecycle'])
        event_row = {**row, 'current_version_id': payload['adopted_version_id']}
        self.service.add_event(cur, user_id, event_row, 'record_' + action, payload, source, actor)

    @staticmethod
    def uuid(value):
        from uuid import UUID
        try:
            return str(UUID(str(value)))
        except (ValueError, TypeError):
            raise PlanError('invalid_record_id') from None

    @staticmethod
    def assessment(raw):
        choices = {'market_judgment': ('matches', 'differs', 'not_evaluable'),
                   'strategy_rules': ('applicable', 'not_applicable', 'not_evaluable'),
                   'execution': ('followed', 'deviated', 'not_evaluable')}
        if not isinstance(raw, dict) or set(raw) != set(choices) | {'improvement'}:
            raise PlanError('invalid_review_assessment')
        result = {'improvement': text(raw['improvement'], 2000, True, 'improvement')}
        for key, values in choices.items():
            item = raw[key]
            if not isinstance(item, dict) or set(item) != {'value', 'reason'} or item.get('value') not in values:
                raise PlanError('invalid_review_assessment', details={'fields': [key]})
            result[key] = {'value': item['value'], 'reason': text(item.get('reason'), 2000, True, key)}
        return result
