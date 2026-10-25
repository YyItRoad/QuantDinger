"""Feature-owned HTTP contracts, shared by registration and spec generation."""
from copy import deepcopy
import re

SCHEMAS = {'TradePlanSnapshot': {'type': 'object',
                       'required': ['title'],
                       'additionalProperties': False,
                       'properties': {'title': {'type': 'string', 'maxLength': 120},
                                      'primary_strategy': {'type': 'string', 'maxLength': 120},
                                      'hypothesis': {'type': 'string', 'maxLength': 2000},
                                      'entry_trigger': {'type': 'string', 'maxLength': 2000},
                                      'invalidation': {'type': 'string', 'maxLength': 2000},
                                      'exit_rules': {'type': 'string', 'maxLength': 2000},
                                      'wait_conditions': {'type': 'string', 'maxLength': 2000},
                                      'evidence': {'type': 'string', 'maxLength': 4000},
                                      'counter_evidence': {'type': 'string', 'maxLength': 4000},
                                      'schema_version': {'type': 'integer', 'enum': [1]},
                                      'instrument': {'type': 'object',
                                                     'additionalProperties': False,
                                                     'properties': {'market': {'type': 'string', 'maxLength': 100},
                                                                    'symbol': {'type': 'string', 'maxLength': 100},
                                                                    'exchange_id': {'type': 'string',
                                                                                    'maxLength': 100},
                                                                    'market_type': {'type': 'string',
                                                                                    'maxLength': 100},
                                                                    'instrument_id': {'type': 'string',
                                                                                      'maxLength': 100},
                                                                    'settlement_ccy': {'type': 'string',
                                                                                       'maxLength': 100}}},
                                      'direction': {'type': 'string', 'enum': ['', 'long', 'short', 'watch']},
                                      'analysis_timeframe': {'type': 'string'},
                                      'decision_timeframe': {'type': 'string'},
                                      'expires_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                                      'retrospective': {'type': 'boolean'},
                                      'research_refs': {'type': 'array',
                                                        'maxItems': 20,
                                                        'items': {'type': 'object'}},
                                      'risk_inputs': {'type': 'object',
                                                      'additionalProperties': False,
                                                      'properties': {'model': {'type': 'string'},
                                                                     'entry_price': {'type': 'string'},
                                                                     'stop_price': {'type': 'string'},
                                                                     'target_price': {'type': 'string'},
                                                                     'quantity': {'type': 'string'},
                                                                     'risk_budget': {'type': 'string'},
                                                                     'equity': {'type': 'string'},
                                                                     'currency': {'type': 'string'},
                                                                     'equity_currency': {'type': 'string'},
                                                                     'equity_at': {'type': 'string'}},
                                                      'not': {'required': ['quantity', 'risk_budget']}}}},
 'TradePlanMutationResult': {'type': 'object',
                             'required': ['id', 'version_id', 'version_no', 'row_revision', 'request_id'],
                             'additionalProperties': False,
                             'properties': {'id': {'type': 'string', 'format': 'uuid'},
                                            'version_id': {'type': 'string', 'format': 'uuid'},
                                            'version_no': {'type': 'integer', 'minimum': 1},
                                            'row_revision': {'type': 'integer', 'minimum': 1},
                                            'request_id': {'type': 'string', 'maxLength': 120}}}}

# Keep route-specific fields explicit; the envelope and permission rules are shared.
_ROUTES = [{'method': 'get',
  'path': '/api/agent/v1/trade-plans',
  'id': 'list_trade_plans',
  'query': [{'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 1000000}},
            {'name': 'page_size', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
            {'name': 'q', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'lifecycle', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'direction', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'primary_strategy', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'retrospective', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'source_kind', 'in': 'query', 'schema': {'type': 'string', 'enum': ['strategy', 'order', 'quick_order', 'trade', 'history']}},
            {'name': 'source_id', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}}]},
 {'method': 'post',
  'path': '/api/agent/v1/trade-plans',
  'id': 'create_trade_plan',
  'body': {'type': 'object',
           'properties': {'request_id': {'type': 'string', 'maxLength': 120},
                          'snapshot': {'$ref': '#/components/schemas/TradePlanSnapshot'}},
           'required': ['request_id', 'snapshot'],
           'additionalProperties': False}},
 {'method': 'get', 'path': '/api/agent/v1/trade-plans/{plan_id}', 'id': 'get_trade_plan'},
 {'method': 'get', 'path': '/api/agent/v1/trade-plans/{plan_id}/versions', 'id': 'list_trade_plan_versions'},
 {'method': 'post',
  'path': '/api/agent/v1/trade-plans/{plan_id}/versions',
  'id': 'revise_trade_plan',
  'body': {'type': 'object',
           'properties': {'request_id': {'type': 'string', 'maxLength': 120},
                          'expected_revision': {'type': 'integer', 'minimum': 1},
                          'snapshot': {'$ref': '#/components/schemas/TradePlanSnapshot'},
                          'change_reason': {'type': 'string', 'maxLength': 500}},
           'required': ['request_id', 'expected_revision', 'snapshot', 'change_reason'],
           'additionalProperties': False}},
 {'method': 'get',
  'path': '/api/agent/v1/trade-plans/{plan_id}/versions/{version_id}',
  'id': 'get_trade_plan_version'},
 {'method': 'post',
  'path': '/api/agent/v1/trade-plans/{plan_id}/state',
  'id': 'set_trade_plan_state',
  'body': {'type': 'object',
           'properties': {'request_id': {'type': 'string', 'maxLength': 120},
                          'expected_revision': {'type': 'integer', 'minimum': 1},
                          'lifecycle': {'type': 'string',
                                        'enum': ['draft', 'observing', 'cancelled', 'completed']},
                          'reason': {'type': 'string', 'maxLength': 500}},
           'required': ['request_id', 'expected_revision', 'lifecycle', 'reason'],
           'additionalProperties': False}},
 {'method': 'post',
  'path': '/api/agent/v1/trade-plans/{plan_id}/copy',
  'id': 'copy_trade_plan',
  'body': {'type': 'object',
           'properties': {'request_id': {'type': 'string', 'maxLength': 120},
                          'expected_revision': {'type': 'integer', 'minimum': 1}},
           'required': ['request_id', 'expected_revision'],
           'additionalProperties': False}},
 {'method': 'get', 'path': '/api/agent/v1/trade-plans/{plan_id}/events', 'id': 'list_trade_plan_events'},
 {'method': 'post',
  'path': '/api/agent/v1/trade-plans/{plan_id}/events',
  'id': 'record_trade_plan_decision',
  'body': {'type': 'object',
           'properties': {'request_id': {'type': 'string', 'maxLength': 120},
                          'expected_revision': {'type': 'integer', 'minimum': 1},
                          'status': {'type': 'string', 'enum': ['unconfirmed', 'confirmed', 'withdrawn']},
                          'reason': {'type': 'string', 'maxLength': 2000},
                          'observed_at': {'type': 'string', 'format': 'date-time'},
                          'assessment_author': {'type': 'string',
                                                'enum': ['human', 'ai_suggestion'],
                                                'default': 'human'}},
           'required': ['request_id', 'expected_revision', 'status', 'reason', 'observed_at'],
           'additionalProperties': False}},
 {'method': 'get', 'path': '/api/agent/v1/trade-plans/requests/{request_id}', 'id': 'get_trade_plan_request'}]


def _operation(route):
    write = route['method'] == 'post'
    result_schema = {'$ref': '#/components/schemas/TradePlanMutationResult'} if write else {'type': 'object'}
    result = {
        'operationId': route['id'], 'tags': ['TradePlans'],
        'summary': 'Recording-only trade plan management; never executes trades',
        'security': [{'AgentToken': []}], 'x-required-scope': 'W' if write else 'R',
        'x-scope-class': 'W' if write else 'R',
        'description': 'Recording-only plans; revisions preserve history. Retry writes with the same key and body. Never executes trading.',
        'responses': {
            '200': {'description': 'Success', 'content': {'application/json': {'schema': {
                'type': 'object', 'properties': {'code': {'type': 'integer', 'enum': [0]},
                    'message': {'type': 'string'}, 'data': result_schema}, 'required': ['code', 'data']}}}},
            **{code: {'description': description} for code, description in {
                '400': 'Invalid input', '401': 'Unauthenticated', '403': 'Scope or instrument forbidden',
                '404': 'Not found', '409': 'Revision or idempotency conflict',
                '422': 'Invalid lifecycle transition, immutable instrument change, or mismatched analysis reference', '503': 'Storage unavailable',
            }.items()},
        },
        'parameters': [{
            'name': name, 'in': 'path', 'required': True,
            'schema': {'type': 'string', **({'format': 'uuid'} if name != 'request_id' else {})},
        } for name in re.findall(r'{(\w+)}', route['path'])] + deepcopy(route.get('query', [])),
    }
    if route['id'] == 'revise_trade_plan':
        result['description'] += (' Once a symbol is selected, instrument identity cannot change in revisions. '
                                  'A title-only draft may select its instrument for the first time. '
                                  'Analysis references must match the plan instrument when identity fields are known.')
    if write:
        result['parameters'].append({'name': 'Idempotency-Key', 'in': 'header', 'required': True,
            'schema': {'type': 'string', 'maxLength': 120},
            'description': 'Must equal body.request_id; retries must reuse both.'})
        result['requestBody'] = {'required': True, 'content': {'application/json': {'schema': route['body']}}}
    return result


from .record_contracts import ROUTES as RECORD_ROUTES, SCHEMAS as RECORD_SCHEMAS
_ROUTES.extend(RECORD_ROUTES)
SCHEMAS.update(RECORD_SCHEMAS)

OPERATIONS = {}
for _route in _ROUTES:
    OPERATIONS.setdefault(_route['path'], {})[_route['method']] = _operation(_route)


def human_contract(operation_id):
    operation = next(op for methods in OPERATIONS.values() for op in methods.values()
                     if op['operationId'] == operation_id)
    result = deepcopy(operation)
    result['security'] = [{'HumanJWT': []}]
    result.pop('x-required-scope', None)
    result.pop('x-scope-class', None)
    result['x-visibility'] = 'internal'
    result['parameters'] = [p for p in result.get('parameters', []) if p['in'] != 'header']
    response = result['responses']['200']['content']['application/json']['schema']
    response['properties']['code']['enum'] = [1]
    response['properties']['msg'] = response['properties'].pop('message')
    return result


def register_human_api(api):
    from .routes import blp
    api.spec.options.setdefault('tags', []).append({
        'name': 'TradePlans', 'description': 'Recording-only trade plans and decision history.',
    })
    for name, schema in SCHEMAS.items():
        api.spec.components.schema(name, component=deepcopy(schema))
    api.register_blueprint(blp, url_prefix='/api/trade-plans')
