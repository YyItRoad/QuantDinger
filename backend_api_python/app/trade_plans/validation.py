"""Strict recording-only contracts and deterministic price-risk calculations."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re


class PlanError(ValueError):
    def __init__(self, code, status=400, details=None):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details


TEXT_LIMITS = dict(title=120, primary_strategy=120, hypothesis=2000,
                   entry_trigger=2000, invalidation=2000, exit_rules=2000,
                   wait_conditions=2000, evidence=4000, counter_evidence=4000)
TIMEFRAMES = {'1m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M'}
RISK_KEYS = {'model', 'entry_price', 'stop_price', 'target_price', 'quantity', 'risk_budget',
             'equity', 'currency', 'equity_currency', 'equity_at'}
INSTRUMENT_KEYS = {'market', 'symbol', 'exchange_id', 'market_type', 'instrument_id', 'settlement_ccy'}
REQUIRED_CARD = ('primary_strategy', 'hypothesis', 'entry_trigger', 'invalidation',
                 'exit_rules', 'wait_conditions', 'evidence', 'analysis_timeframe', 'decision_timeframe')


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PlanError('invalid_timestamp') from None


def text(value, limit, required=False, field=None):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise PlanError('invalid_text', details={'max_length': limit, 'fields': [field] if field else []})
    return value.strip()


def number(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result > Decimal('1e18') or result.as_tuple().exponent < -18:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise PlanError('invalid_risk_number') from None


def validate_snapshot(raw):
    if not isinstance(raw, dict):
        raise PlanError('snapshot_required')
    allowed = set(TEXT_LIMITS) | {'schema_version', 'instrument', 'direction', 'analysis_timeframe',
        'decision_timeframe', 'expires_at', 'risk_inputs', 'research_refs', 'retrospective'}
    if set(raw) - allowed:
        raise PlanError('unknown_fields', details=sorted(set(raw) - allowed))
    result = {key: text(raw.get(key, ''), limit, key == 'title', key) for key, limit in TEXT_LIMITS.items()}
    if type(raw.get('schema_version', 1)) is not int or raw.get('schema_version', 1) != 1:
        raise PlanError('unsupported_schema_version')
    result['schema_version'] = 1
    instrument = raw.get('instrument', {})
    if not isinstance(instrument, dict) or set(instrument) - INSTRUMENT_KEYS:
        raise PlanError('invalid_instrument')
    result['instrument'] = {key: text(value, 100) for key, value in instrument.items()}
    if result['instrument'].get('symbol'):
        result['instrument']['symbol'] = result['instrument']['symbol'].upper()
    if instrument.get('market_type') and instrument['market_type'] not in {'spot', 'swap', 'stock', 'futures', 'option'}:
        raise PlanError('invalid_market_type')
    direction = raw.get('direction', '')
    if not isinstance(direction, str) or direction not in {'', 'long', 'short', 'watch'}:
        raise PlanError('invalid_direction')
    result['direction'] = direction
    for key in ('analysis_timeframe', 'decision_timeframe'):
        value = raw.get(key, '')
        if not isinstance(value, str) or (value and value not in TIMEFRAMES):
            raise PlanError('unsupported_timeframe')
        result[key] = value
    try:
        result['expires_at'] = timestamp(raw['expires_at']).isoformat() if raw.get('expires_at') is not None else None
    except PlanError as exc:
        exc.details = {'fields': ['expires_at']}
        raise
    retrospective = raw.get('retrospective', False)
    if type(retrospective) is not bool:
        raise PlanError('invalid_retrospective')
    result['retrospective'] = retrospective
    risk = deepcopy(raw.get('risk_inputs', {}))
    if not isinstance(risk, dict) or set(risk) - RISK_KEYS:
        raise PlanError('invalid_risk_inputs')
    for key, value in list(risk.items()):
        if value in ('', None):
            del risk[key]
        elif key in {'model', 'currency', 'equity_currency'}:
            risk[key] = text(value, 32)
        else:
            try:
                risk[key] = timestamp(value).isoformat() if key == 'equity_at' else str(number(value))
            except PlanError as exc:
                exc.details = {'fields': ['risk_inputs.' + key]}
                raise
    if risk.get('quantity') and risk.get('risk_budget'):
        raise PlanError('quantity_or_budget_exclusive', details={'fields': ['quantity', 'risk_budget']})
    result['risk_inputs'] = risk
    refs = raw.get('research_refs', [])
    if not isinstance(refs, list) or len(refs) > 20:
        raise PlanError('invalid_research_refs')
    result['research_refs'] = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) - {'type', 'id', 'url', 'summary', 'data_as_of', 'evidence_snapshot', 'recorded_at'}:
            raise PlanError('invalid_research_ref')
        kind = ref.get('type', 'note')
        if not isinstance(kind, str) or kind not in {'note', 'url', 'market_state_result'}:
            raise PlanError('invalid_research_ref_type')
        entry = {'type': kind, 'summary': text(ref.get('summary', ''), 4000)}
        if kind == 'market_state_result':
            if type(ref.get('id')) is not int or ref['id'] <= 0:
                raise PlanError('invalid_research_ref_id')
            entry['id'] = ref['id']
        if kind == 'url':
            url = text(ref.get('url', ''), 2000, True)
            if not re.match(r'^https?://[^\s]+$', url):
                raise PlanError('invalid_research_url')
            entry['url'] = url
        if ref.get('data_as_of'):
            entry['data_as_of'] = timestamp(ref['data_as_of']).isoformat()
        result['research_refs'].append(entry)
    return result


def check(snapshot):
    missing = [key for key in REQUIRED_CARD if not snapshot.get(key)]
    instrument = snapshot.get('instrument', {})
    for key in ('market', 'symbol'):
        if not instrument.get(key):
            missing.append('instrument.' + key)
    if not snapshot.get('direction'):
        missing.append('direction')
    risk = snapshot.get('risk_inputs', {})
    output = {'calculation_version': 'price-risk-v1', 'card_status': 'incomplete' if missing else 'complete',
              'missing_fields': missing, 'risk_status': 'missing', 'risk_missing_fields': [],
              'one_r': None, 'risk_currency': risk.get('currency'), 'target_reward_risk': None,
              'quantity': None, 'equity_risk_ratio': None, 'excludes_costs': True}
    if snapshot.get('direction') == 'watch':
        return dict(output, risk_status='not_applicable')
    market_type = instrument.get('market_type')
    if instrument.get('market') != 'Crypto' or market_type not in ('spot', 'swap') or (market_type == 'spot' and snapshot.get('direction') == 'short') or (market_type == 'swap' and risk.get('model') != 'linear'):
        return dict(output, risk_status='unsupported')
    required = ['entry_price', 'stop_price', 'currency']
    output['risk_missing_fields'] = [key for key in required if not risk.get(key)]
    if not risk.get('quantity') and not risk.get('risk_budget'):
        output['risk_missing_fields'].append('quantity_or_risk_budget')
    if output['risk_missing_fields']:
        return output
    entry, stop = number(risk['entry_price']), number(risk['stop_price'])
    side = snapshot.get('direction')
    if side not in ('long', 'short') or (side == 'long' and stop >= entry) or (side == 'short' and stop <= entry):
        return dict(output, risk_status='invalid', risk_error='invalid_stop_direction')
    distance = abs(entry - stop)
    quantity = number(risk['quantity']) if risk.get('quantity') else number(risk['risk_budget']) / distance
    one_r = distance * quantity
    output.update(risk_status='calculable', quantity=str(quantity), one_r=str(one_r))
    if risk.get('target_price'):
        target = number(risk['target_price'])
        if (side == 'long' and target <= entry) or (side == 'short' and target >= entry):
            return dict(output, risk_status='invalid', risk_error='invalid_target_direction', one_r=None)
        output['target_reward_risk'] = str(abs(target - entry) / distance)
    if risk.get('equity') and risk.get('equity_currency') == risk['currency'] and risk.get('equity_at'):
        output['equity_risk_ratio'] = str(one_r / number(risk['equity']))
    if instrument.get('settlement_ccy') and instrument['settlement_ccy'] != risk['currency']:
        return dict(output, risk_status='invalid', risk_error='currency_mismatch', one_r=None)
    return output


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
