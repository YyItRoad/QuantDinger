"""Contracts for research-only execution links and reviews."""
from copy import deepcopy

UUID = {'type': 'string', 'format': 'uuid'}
INTEGER = {'type': 'integer', 'minimum': 1}
TEXT = {'type': 'string', 'maxLength': 2000}
CHOICES = {'market_judgment': ['matches', 'differs', 'not_evaluable'],
           'strategy_rules': ['applicable', 'not_applicable', 'not_evaluable'],
           'execution': ['followed', 'deviated', 'not_evaluable']}
ASSESSMENT = {'type': 'object', 'additionalProperties': False,
    'required': [*CHOICES, 'improvement'], 'properties': {
        'improvement': TEXT, **{key: {'type': 'object', 'additionalProperties': False,
            'required': ['value', 'reason'], 'properties': {'value': {'type': 'string', 'enum': values}, 'reason': TEXT}}
            for key, values in CHOICES.items()}}}
SCHEMAS = {'TradePlanReviewAssessment': ASSESSMENT}
ROOT = '/api/agent/v1/trade-plans'


def body(properties, required):
    return {'type': 'object', 'additionalProperties': False,
        'required': ['request_id', 'expected_revision', *required],
        'properties': {'request_id': {'type': 'string', 'maxLength': 120}, 'expected_revision': INTEGER, **properties}}


ROUTES = [
    {'method': 'get', 'path': ROOT + '/accounts', 'id': 'list_trade_plan_accounts'},
    {'method': 'get', 'path': ROOT + '/{plan_id}/sources', 'id': 'list_trade_plan_sources', 'query': [
        {'name': key, 'in': 'query', 'required': key == 'credential_id', 'schema': schema}
        for key, schema in {'credential_id': INTEGER, 'source_kind': {'type': 'string', 'enum': ['strategy', 'order', 'quick_order', 'trade', 'history']},
                            'version_id': UUID, 'source_id': INTEGER, 'page': INTEGER, 'page_size': {'type': 'integer', 'minimum': 1, 'maximum': 100}}.items()]},
    {'method': 'post', 'path': ROOT + '/{plan_id}/attempts', 'id': 'create_trade_plan_attempt',
     'body': body({'version_id': UUID, 'credential_id': INTEGER, 'execution_mode': {'type': 'string', 'enum': ['live', 'paper']}, 'note': TEXT},
                  ['version_id', 'credential_id', 'execution_mode'])},
    {'method': 'post', 'path': ROOT + '/{plan_id}/links', 'id': 'link_trade_plan_record',
     'body': body({'attempt_id': UUID, 'source_kind': {'type': 'string', 'enum': ['strategy', 'order', 'quick_order', 'trade', 'history']},
                   'source_id': INTEGER, 'attribution': {'type': 'string', 'enum': ['confirmed', 'pending']}, 'reason': TEXT},
                  ['attempt_id', 'source_kind', 'source_id', 'attribution', 'reason'])},
    {'method': 'post', 'path': ROOT + '/{plan_id}/links/revoke', 'id': 'revoke_trade_plan_link',
     'body': body({'attempt_id': UUID, 'link_id': UUID, 'reason': TEXT}, ['attempt_id', 'link_id', 'reason'])},
    {'method': 'post', 'path': ROOT + '/{plan_id}/links/refresh', 'id': 'refresh_trade_plan_link',
     'body': body({'attempt_id': UUID, 'link_id': UUID, 'reason': TEXT, 'attribution': {'type': 'string', 'enum': ['confirmed', 'pending']}}, ['attempt_id', 'link_id', 'reason'])},
    {'method': 'post', 'path': ROOT + '/{plan_id}/reviews', 'id': 'save_trade_plan_review',
     'body': body({'attempt_id': UUID, 'assessment': {'$ref': '#/components/schemas/TradePlanReviewAssessment'},
                   'change_reason': {'type': 'string', 'maxLength': 500}}, ['attempt_id', 'assessment', 'change_reason'])},
]
