"""Recording-only plan management. This module has no execution dependencies."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from .repository import PlanRepository
from .permissions import PlanScope
from .validation import INSTRUMENT_KEYS, PlanError, canonical_json, check, text, timestamp, validate_snapshot

ACTIVE = {'draft', 'observing'}


def uid():
    return str(uuid4())


class PlanService:
    def __init__(self, repository=None, scope=None, now=None):
        self.repo = repository or PlanRepository()
        self.scope = scope or PlanScope()
        self.visible = self.scope.allows
        self.now = now or (lambda: datetime.now(timezone.utc))
        from .records import RecordManager
        self.records = RecordManager(self)

    def effective_state(self, row):
        expires = row['snapshot'].get('expires_at')
        return 'expired' if row['lifecycle'] in ACTIVE and expires and timestamp(expires) <= self.now() else row['lifecycle']

    def require_plan(self, cur, user_id, plan_id, lock=False):
        row = self.repo.plan(cur, user_id, plan_id, lock)
        if not row or not self.visible(row['snapshot']) or not self.records.visible(cur, user_id, plan_id):
            raise PlanError('plan_not_found', 404)
        return row

    def present(self, cur, user_id, row):
        versions = self.repo.versions(cur, user_id, row['id'])
        allowed_ids = {v['id'] for v in versions if self.visible(v['snapshot'])}
        events = [e for e in self.repo.events(cur, user_id, row['id']) if e['plan_version_id'] in allowed_ids]
        signal = next((e for e in reversed(events) if e['plan_version_id'] == row['current_version_id'] and e['event_type'] == 'signal_assessment'), None)
        attempts = self.records.detail(cur, user_id, row, allowed_ids)
        statuses = {a['summary']['trade_status'] for a in attempts}
        trade_status = next((s for s in ('unknown', 'holding', 'partially_closed', 'closed', 'no_fills') if s in statuses), 'not_linked')
        return dict(row, effective_lifecycle=self.effective_state(row), checks=check(row['snapshot']),
                    signal_status=signal['payload']['status'] if signal else 'unassessed',
                    trade_status=trade_status, events=events, attempts=attempts)

    def read(self, user_id, plan_id):
        with self.repo.transaction() as cur:
            return self.present(cur, user_id, self.require_plan(cur, user_id, plan_id))

    def listing(self, user_id, filters):
        page = self.integer(filters.get('page', 1), 1, 1000000)
        size = self.integer(filters.get('page_size', 20), 1, 100)
        if set(filters) - {'page', 'page_size', 'q', 'lifecycle', 'direction', 'primary_strategy', 'retrospective', 'source_kind', 'source_id', '_t'}:
            raise PlanError('unknown_filters')
        kind, source_id = filters.get('source_kind', ''), filters.get('source_id', '')
        if bool(kind) != bool(source_id) or (kind and kind not in ('strategy', 'order', 'quick_order', 'trade', 'history')):
            raise PlanError('invalid_filter')
        if source_id:
            self.integer(source_id, 1, 2**63-1)
        for key in ('q', 'primary_strategy'):
            text(filters.get(key, ''), 120)
        if filters.get('lifecycle', '') not in {'', 'draft', 'observing', 'cancelled', 'expired', 'completed'} or filters.get('direction', '') not in {'', 'long', 'short', 'watch'} or filters.get('retrospective', '') not in {'', 'true', 'false'}:
            raise PlanError('invalid_filter')
        with self.repo.transaction() as cur:
            rows, total = self.repo.listing(cur, user_id, self.scope, filters, self.now(), page, size)
            items = []
            for row in rows:
                snapshot = row['snapshot']
                # Lists need only card summaries; history is fetched in the detail endpoint.
                summary = {key: snapshot.get(key) for key in
                           ('title', 'instrument', 'direction', 'primary_strategy', 'retrospective')}
                items.append(dict(row, snapshot=summary, effective_lifecycle=self.effective_state(row),
                                  checks=check(snapshot)))
            return {'items': items, 'total': total, 'page': page, 'page_size': size}

    @staticmethod
    def integer(value, low, high):
        try:
            if isinstance(value, bool) or str(value) != str(int(value)) or not low <= int(value) <= high:
                raise ValueError()
            return int(value)
        except (TypeError, ValueError):
            raise PlanError('invalid_integer') from None

    def history(self, user_id, plan_id, kind, version_id=None):
        with self.repo.transaction() as cur:
            self.require_plan(cur, user_id, plan_id)
            versions = [v for v in self.repo.versions(cur, user_id, plan_id) if self.visible(v['snapshot'])]
            if version_id:
                row = next((v for v in versions if v['id'] == version_id), None)
                if not row:
                    raise PlanError('version_not_found', 404)
                return row
            if kind == 'versions':
                return {'items': versions}
            ids = {v['id'] for v in versions}
            return {'items': [e for e in self.repo.events(cur, user_id, plan_id) if e['plan_version_id'] in ids]}

    def request_result(self, user_id, request_id):
        with self.repo.transaction() as cur:
            row = self.repo.request(cur, user_id, request_id)
            if not row or not row['response']:
                raise PlanError('request_not_found', 404)
            self.guard_result(cur, user_id, row['response'])
            row['response'] = self.mutation_result(row['response'], request_id)
            return row

    def guard_result(self, cur, user_id, result):
        self.require_plan(cur, user_id, result['id'])
        adopted = result.get('version_id') or result.get('current_version_id')
        version = next((v for v in self.repo.versions(cur, user_id, result['id']) if v['id'] == adopted), None)
        if not version or not self.visible(version['snapshot']):
            raise PlanError('plan_not_found', 404)

    @staticmethod
    def mutation_result(row, request_id):
        return {'id': row['id'], 'version_id': row.get('version_id') or row['current_version_id'],
                'version_no': row['version_no'], 'row_revision': row['row_revision'],
                'request_id': request_id}

    def prepare_snapshot(self, cur, user_id, raw):
        snapshot = validate_snapshot(raw)
        if not self.visible(snapshot):
            raise PlanError('instrument_forbidden', 403)
        for ref in snapshot['research_refs']:
            if ref['type'] == 'market_state_result':
                record = self.repo.research(cur, user_id, ref['id'])
                if not record or not self.visible({'instrument': record}):
                    raise PlanError('research_not_found', 404)
                instrument = snapshot['instrument']
                mismatches = [key for key in ('market', 'symbol', 'exchange_id', 'market_type', 'instrument_id')
                              if record.get(key) and instrument.get(key)
                              and str(record[key]).upper() != str(instrument[key]).upper()]
                if mismatches:
                    raise PlanError('research_instrument_mismatch', 422, {'fields': mismatches})
                ref['summary'] = record['summary']
                ref['data_as_of'] = record['bar_close_at']
                ref['evidence_snapshot'] = record
                ref['recorded_at'] = self.now().isoformat()
        return snapshot

    def add_event(self, cur, user_id, row, kind, payload, source, actor, occurred_at=None):
        self.repo.event(cur, uid(), user_id, row['id'], row['current_version_id'], kind,
                        payload, occurred_at, source, actor)

    def mutate(self, user_id, action, body, plan_id=None, source='web', actor=None):
        if not isinstance(body, dict):
            raise PlanError('body_required')
        fields = {'request_id'} | ({'expected_revision'} if action != 'create' else set())
        from .records import RECORD_FIELDS
        extras = {'create': {'snapshot'}, 'revise': {'snapshot', 'change_reason'},
                  'state': {'lifecycle', 'reason'}, 'copy': set(),
                  'decision': {'status', 'reason', 'observed_at', 'assessment_author'}, **RECORD_FIELDS}
        if action not in extras or set(body) - fields - extras[action]:
            raise PlanError('unknown_fields')
        request_id = text(body.get('request_id'), 120, True)
        actor = str(actor or user_id)
        fingerprint = sha256(canonical_json({'action': action, 'plan_id': plan_id, 'body': body}).encode()).hexdigest()
        with self.repo.transaction() as cur:
            inserted, previous = self.repo.reserve(cur, user_id, request_id, action, fingerprint)
            if not inserted:
                if previous['request_hash'] != fingerprint:
                    raise PlanError('idempotency_mismatch', 409)
                if not previous['response']:
                    raise PlanError('request_in_progress', 409)
                self.guard_result(cur, user_id, previous['response'])
                if action in RECORD_FIELDS:
                    row = self.require_plan(cur, user_id, plan_id)
                    self.records.guard_replay(cur, user_id, row, action, body)
                return self.mutation_result(previous['response'], request_id)
            row = None
            if action != 'create':
                row = self.require_plan(cur, user_id, plan_id, True)
                expected = self.integer(body.get('expected_revision'), 1, 2**63-1)
                if row['row_revision'] != expected:
                    raise PlanError('revision_conflict', 409, {'current_revision': row['row_revision'], 'current_version_id': row['current_version_id']})
            if action in RECORD_FIELDS:
                self.records.mutate(cur, user_id, row, action, body, source, actor)
            elif action in {'create', 'copy'}:
                # Copies reuse trusted frozen evidence, even if its source was later removed.
                snapshot = (self.prepare_snapshot(cur, user_id, body.get('snapshot', {}))
                            if action == 'create' else self.copy_snapshot(row['snapshot']))
                pid, vid = uid(), uid()
                self.repo.new_plan(cur, user_id, pid, plan_id if action == 'copy' else None)
                self.repo.new_version(cur, user_id, pid, vid, 1, snapshot,
                    sha256(canonical_json(snapshot).encode()).hexdigest(), 'initial' if action == 'create' else 'copied', source, actor)
                row = self.require_plan(cur, user_id, pid)
                self.add_event(cur, user_id, row, 'created' if action == 'create' else 'copied', {}, source, actor)
            elif action == 'revise':
                if self.effective_state(row) not in ACTIVE:
                    raise PlanError('plan_terminal', 422)
                reason = text(body.get('change_reason'), 500, True, 'change_reason')
                snapshot = self.prepare_snapshot(cur, user_id, body.get('snapshot'))
                previous_instrument = row['snapshot']['instrument']
                if previous_instrument.get('symbol'):
                    changed = [key for key in sorted(INSTRUMENT_KEYS)
                               if (previous_instrument.get(key) or '') != (snapshot['instrument'].get(key) or '')]
                    if changed:
                        raise PlanError('plan_instrument_immutable', 422, {'fields': changed})
                if row['lifecycle'] == 'observing' and check(snapshot)['card_status'] != 'complete':
                    raise PlanError('card_incomplete', 422, check(snapshot)['missing_fields'])
                self.repo.new_version(cur, user_id, row['id'], uid(), row['version_no']+1, snapshot,
                    sha256(canonical_json(snapshot).encode()).hexdigest(), reason, source, actor)
                self.repo.update(cur, user_id, row['id'], row['lifecycle'])
                row = self.require_plan(cur, user_id, row['id'])
                self.add_event(cur, user_id, row, 'revised', {'reason': reason}, source, actor)
            elif action == 'state':
                target = body.get('lifecycle')
                state = self.effective_state(row)
                allowed = {'draft': {'observing', 'cancelled'}, 'observing': {'draft', 'cancelled', 'completed'}}
                if not isinstance(target, str) or target not in allowed.get(state, set()):
                    raise PlanError('invalid_state_transition', 422)
                if target == 'completed':
                    allowed_ids = {v['id'] for v in self.repo.versions(cur, user_id, row['id']) if self.visible(v['snapshot'])}
                    attempts = self.records.detail(cur, user_id, row, allowed_ids)
                    if any(a['summary']['trade_status'] in ('holding', 'partially_closed', 'unknown') for a in attempts):
                        raise PlanError('record_attempts_unresolved', 422)
                reason = text(body.get('reason'), 500, True, 'reason')
                if target == 'observing' and check(row['snapshot'])['card_status'] != 'complete':
                    raise PlanError('card_incomplete', 422, check(row['snapshot'])['missing_fields'])
                self.repo.update(cur, user_id, row['id'], target)
                self.add_event(cur, user_id, row, 'state_changed', {'from': state, 'to': target, 'reason': reason}, source, actor)
            elif action == 'decision':
                if self.effective_state(row) != 'observing':
                    raise PlanError('plan_not_observing', 422)
                status = body.get('status')
                if not isinstance(status, str) or status not in {'unconfirmed', 'confirmed', 'withdrawn'}:
                    raise PlanError('invalid_signal_status')
                author = body.get('assessment_author', 'human')
                if not isinstance(author, str) or author not in {'human', 'ai_suggestion'} or (author == 'ai_suggestion' and status == 'confirmed'):
                    raise PlanError('ai_cannot_confirm_signal')
                observed = timestamp(body.get('observed_at'))
                if observed > self.now():
                    raise PlanError('future_observation')
                payload = {'status': status, 'reason': text(body.get('reason'), 2000, True, 'reason'), 'assessment_author': author}
                self.repo.update(cur, user_id, row['id'], row['lifecycle'])
                self.add_event(cur, user_id, row, 'signal_assessment', payload, source, actor, observed)
            result = self.mutation_result(self.require_plan(cur, user_id, row['id']), request_id)
            if action in RECORD_FIELDS:
                adopted = self.records.guard_replay(cur, user_id, row, action, body)
                result.update(version_id=adopted['id'], version_no=adopted['version_no'])
            self.repo.finish(cur, user_id, request_id, result)
            return result

    @staticmethod
    def copy_snapshot(snapshot):
        from copy import deepcopy
        result = deepcopy(snapshot)
        result['expires_at'] = None
        return result
