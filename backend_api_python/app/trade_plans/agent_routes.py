"""Recording-only trade plan HTTP endpoints."""
from flask import g, request
from app.trade_plans.http import endpoint, mutate
from app.utils.agent_auth import agent_required, SCOPE_R, SCOPE_W
from app.routes.agent_v1 import agent_v1_bp


@agent_v1_bp.route('/trade-plans', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def list_trade_plans():
    return g.plan_service.listing(g.plan_user_id, request.args.to_dict())


@agent_v1_bp.route('/trade-plans', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def create_trade_plan():
    return mutate('create')


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def get_trade_plan(plan_id):
    return g.plan_service.read(g.plan_user_id, str(plan_id))


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/versions', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def list_trade_plan_versions(plan_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'versions')


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/versions', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def revise_trade_plan(plan_id):
    return mutate('revise', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/versions/<uuid:version_id>', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def get_trade_plan_version(plan_id, version_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'versions', str(version_id))


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/state', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def set_trade_plan_state(plan_id):
    return mutate('state', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/copy', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def copy_trade_plan(plan_id):
    return mutate('copy', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/events', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def list_trade_plan_events(plan_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'events')


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/events', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def record_trade_plan_decision(plan_id):
    return mutate('decision', plan_id)


@agent_v1_bp.route('/trade-plans/requests/<request_id>', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def get_trade_plan_request(request_id):
    return g.plan_service.request_result(g.plan_user_id, request_id)


@agent_v1_bp.route('/trade-plans/accounts', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def list_trade_plan_accounts():
    return g.plan_service.records.account_list(g.plan_user_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/sources', methods=['GET'])
@agent_required(SCOPE_R)
@endpoint(agent=True)
def list_trade_plan_sources(plan_id):
    return g.plan_service.records.source_list(g.plan_user_id, str(plan_id), request.args.to_dict())


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/attempts', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def create_trade_plan_attempt(plan_id):
    return mutate('attempt', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/links', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def link_trade_plan_record(plan_id):
    return mutate('link', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/links/revoke', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def revoke_trade_plan_link(plan_id):
    return mutate('unlink', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/links/refresh', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def refresh_trade_plan_link(plan_id):
    return mutate('refresh_link', plan_id)


@agent_v1_bp.route('/trade-plans/<uuid:plan_id>/reviews', methods=['POST'])
@agent_required(SCOPE_W, domain_idempotency=True)
@endpoint(agent=True)
def save_trade_plan_review(plan_id):
    return mutate('review', plan_id)
