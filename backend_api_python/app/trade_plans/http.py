"""Shared HTTP adapters for JWT and Agent plan surfaces."""
import os
from functools import wraps
from flask import g, jsonify, request
from psycopg2 import Error as DatabaseError
from app.utils.logger import get_logger
from .service import PlanService
from .validation import PlanError

logger = get_logger(__name__)


def agent_scope():
    from app.utils.agent_auth import current_token, parse_csv_list
    from .permissions import PlanScope
    token = current_token()
    return PlanScope(tuple(parse_csv_list(token.get('markets'))),
                     tuple(parse_csv_list(token.get('instruments'))))


def endpoint(agent=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                if os.getenv("TRADE_PLAN_MANAGEMENT_ENABLED", "true").lower() in {"false", "0", "no"}:
                    raise PlanError("plan_feature_disabled", 503)
                if request.content_length and request.content_length > 131072:
                    raise PlanError('request_too_large', 413)
                if agent:
                    from app.utils.agent_auth import current_user_id, current_token
                    user_id, actor = current_user_id(), str(current_token()['id'])
                else:
                    user_id, actor = int(g.user_id), str(g.user_id)
                if not agent and request.method not in {'GET', 'HEAD', 'OPTIONS'} and g.user_role == 'viewer':
                    raise PlanError('read_only_role', 403)
                g.plan_service = PlanService(scope=agent_scope() if agent else None)
                g.plan_user_id, g.plan_actor, g.plan_source = user_id, actor, 'mcp' if agent else 'web'
                data = fn(*args, **kwargs)
                return jsonify({'code': 0, 'message': 'success', 'data': data} if agent else {'code': 1, 'msg': 'success', 'data': data})
            except PlanError as exc:
                body = {'code': exc.status, 'message': exc.code, 'details': exc.details, 'retriable': exc.code == 'request_in_progress'} if agent else {'code': 0, 'msg': exc.code, 'data': {'error': exc.code, 'details': exc.details}}
                return jsonify(body), exc.status
            except DatabaseError:
                logger.exception('Trade plan storage unavailable')
                return jsonify({'code': 503, 'message': 'plan_storage_unavailable', 'retriable': True} if agent else {'code': 0, 'msg': 'plan_storage_unavailable', 'data': None}), 503
        return wrapped
    return decorate


def mutate(action, plan_id=None):
    body = request.get_json(silent=True)
    from .validation import canonical_json
    try:
        canonical_json(body)
    except (ValueError, TypeError):
        raise PlanError('invalid_json') from None
    if g.plan_source == 'mcp' and isinstance(body, dict) and body.get('request_id') != request.headers.get('Idempotency-Key'):
        raise PlanError('request_id_must_match_idempotency_key')
    return g.plan_service.mutate(g.plan_user_id, action, body, str(plan_id) if plan_id else None, g.plan_source, g.plan_actor)
