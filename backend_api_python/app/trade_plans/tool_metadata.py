"""Tool discovery metadata; no transport or execution side effects."""
TOOL_ROUTES = {'list_trade_plans': '/api/agent/v1/trade-plans',
 'get_trade_plan': '/api/agent/v1/trade-plans/{plan_id}',
 'list_trade_plan_versions': '/api/agent/v1/trade-plans/{plan_id}/versions',
 'get_trade_plan_version': '/api/agent/v1/trade-plans/{plan_id}/versions/{version_id}',
 'list_trade_plan_events': '/api/agent/v1/trade-plans/{plan_id}/events',
 'get_trade_plan_request': '/api/agent/v1/trade-plans/requests/{request_id}',
 'create_trade_plan': '/api/agent/v1/trade-plans',
 'revise_trade_plan': '/api/agent/v1/trade-plans/{plan_id}/versions',
 'set_trade_plan_state': '/api/agent/v1/trade-plans/{plan_id}/state',
 'copy_trade_plan': '/api/agent/v1/trade-plans/{plan_id}/copy',
 'record_trade_plan_decision': '/api/agent/v1/trade-plans/{plan_id}/events',
 'list_trade_plan_accounts': '/api/agent/v1/trade-plans/accounts',
 'list_trade_plan_sources': '/api/agent/v1/trade-plans/{plan_id}/sources',
 'create_trade_plan_attempt': '/api/agent/v1/trade-plans/{plan_id}/attempts',
 'link_trade_plan_record': '/api/agent/v1/trade-plans/{plan_id}/links',
 'revoke_trade_plan_link': '/api/agent/v1/trade-plans/{plan_id}/links/revoke',
 'refresh_trade_plan_link': '/api/agent/v1/trade-plans/{plan_id}/links/refresh',
 'save_trade_plan_review': '/api/agent/v1/trade-plans/{plan_id}/reviews'}
WRITE_TOOLS = ('copy_trade_plan',
 'create_trade_plan',
 'record_trade_plan_decision',
 'revise_trade_plan',
 'set_trade_plan_state',
 'create_trade_plan_attempt',
 'link_trade_plan_record',
 'revoke_trade_plan_link',
 'refresh_trade_plan_link',
 'save_trade_plan_review')


def tool_definitions(tool_type):
    return tuple(tool_type(
        id='mcp.' + name, category='research', label_zh=name.replace('_', ' '),
        label_en=name.replace('_', ' '), description_zh='交易计划研究记录，不执行交易。',
        description_en='Trade plan records only; never executes trading.', route=route,
        produces=('tool_result',), risk_level='write_config' if name in WRITE_TOOLS else 'read',
        read_only=name not in WRITE_TOOLS, priority=80,
        safety='Requires W and a stable idempotency key; no execution side effects.' if name in WRITE_TOOLS else ''
    ) for name, route in TOOL_ROUTES.items())
