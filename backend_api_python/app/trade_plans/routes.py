"""Recording-only trade plan HTTP endpoints."""
from flask import g, request
from app.trade_plans.http import endpoint, mutate
from app.utils.auth import login_required
from app.openapi.blueprint import HumanBlueprint
from .contracts import human_contract

blp = HumanBlueprint("trade_plans", __name__)


@blp.route('', methods=['GET'])
@blp.doc(**human_contract('list_trade_plans'))
@login_required
@endpoint(agent=False)
def list_trade_plans():
    return g.plan_service.listing(g.plan_user_id, request.args.to_dict())


@blp.route('', methods=['POST'])
@blp.doc(**human_contract('create_trade_plan'))
@login_required
@endpoint(agent=False)
def create_trade_plan():
    return mutate('create')


@blp.route('/<uuid:plan_id>', methods=['GET'])
@blp.doc(**human_contract('get_trade_plan'))
@login_required
@endpoint(agent=False)
def get_trade_plan(plan_id):
    return g.plan_service.read(g.plan_user_id, str(plan_id))


@blp.route('/<uuid:plan_id>/versions', methods=['GET'])
@blp.doc(**human_contract('list_trade_plan_versions'))
@login_required
@endpoint(agent=False)
def list_trade_plan_versions(plan_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'versions')


@blp.route('/<uuid:plan_id>/versions', methods=['POST'])
@blp.doc(**human_contract('revise_trade_plan'))
@login_required
@endpoint(agent=False)
def revise_trade_plan(plan_id):
    return mutate('revise', plan_id)


@blp.route('/<uuid:plan_id>/versions/<uuid:version_id>', methods=['GET'])
@blp.doc(**human_contract('get_trade_plan_version'))
@login_required
@endpoint(agent=False)
def get_trade_plan_version(plan_id, version_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'versions', str(version_id))



@blp.route('/<uuid:plan_id>/state', methods=['POST'])
@blp.doc(**human_contract('set_trade_plan_state'))
@login_required
@endpoint(agent=False)
def set_trade_plan_state(plan_id):
    return mutate('state', plan_id)


@blp.route('/<uuid:plan_id>/copy', methods=['POST'])
@blp.doc(**human_contract('copy_trade_plan'))
@login_required
@endpoint(agent=False)
def copy_trade_plan(plan_id):
    return mutate('copy', plan_id)


@blp.route('/<uuid:plan_id>/events', methods=['GET'])
@blp.doc(**human_contract('list_trade_plan_events'))
@login_required
@endpoint(agent=False)
def list_trade_plan_events(plan_id):
    return g.plan_service.history(g.plan_user_id, str(plan_id), 'events')


@blp.route('/<uuid:plan_id>/events', methods=['POST'])
@blp.doc(**human_contract('record_trade_plan_decision'))
@login_required
@endpoint(agent=False)
def record_trade_plan_decision(plan_id):
    return mutate('decision', plan_id)


@blp.route('/requests/<request_id>', methods=['GET'])
@blp.doc(**human_contract('get_trade_plan_request'))
@login_required
@endpoint(agent=False)
def get_trade_plan_request(request_id):
    return g.plan_service.request_result(g.plan_user_id, request_id)


@blp.route('/accounts', methods=['GET'])
@blp.doc(**human_contract('list_trade_plan_accounts'))
@login_required
@endpoint(agent=False)
def list_trade_plan_accounts():
    return g.plan_service.records.account_list(g.plan_user_id)


@blp.route('/<uuid:plan_id>/sources', methods=['GET'])
@blp.doc(**human_contract('list_trade_plan_sources'))
@login_required
@endpoint(agent=False)
def list_trade_plan_sources(plan_id):
    return g.plan_service.records.source_list(g.plan_user_id, str(plan_id), request.args.to_dict())


@blp.route('/<uuid:plan_id>/attempts', methods=['POST'])
@blp.doc(**human_contract('create_trade_plan_attempt'))
@login_required
@endpoint(agent=False)
def create_trade_plan_attempt(plan_id):
    return mutate('attempt', plan_id)


@blp.route('/<uuid:plan_id>/links', methods=['POST'])
@blp.doc(**human_contract('link_trade_plan_record'))
@login_required
@endpoint(agent=False)
def link_trade_plan_record(plan_id):
    return mutate('link', plan_id)


@blp.route('/<uuid:plan_id>/links/revoke', methods=['POST'])
@blp.doc(**human_contract('revoke_trade_plan_link'))
@login_required
@endpoint(agent=False)
def revoke_trade_plan_link(plan_id):
    return mutate('unlink', plan_id)


@blp.route('/<uuid:plan_id>/links/refresh', methods=['POST'])
@blp.doc(**human_contract('refresh_trade_plan_link'))
@login_required
@endpoint(agent=False)
def refresh_trade_plan_link(plan_id):
    return mutate('refresh_link', plan_id)


@blp.route('/<uuid:plan_id>/reviews', methods=['POST'])
@blp.doc(**human_contract('save_trade_plan_review'))
@login_required
@endpoint(agent=False)
def save_trade_plan_review(plan_id):
    return mutate('review', plan_id)
