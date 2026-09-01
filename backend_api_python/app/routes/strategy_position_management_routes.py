"""持仓管理接口。"""

from flask import g, jsonify, request

from app.routes.strategy_blueprint import strategy_blp
from app.utils.auth import login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)


@strategy_blp.route('/account/managed-positions', methods=['GET'])
@login_required
def get_managed_account_positions():
    """只读返回当前凭证已登记在标准策略实例下的持仓。"""
    credential_id = request.args.get('credential_id', type=int)
    if not credential_id:
        return jsonify({
            'code': 0,
            'msg': 'Missing credential_id',
            'data': {'items': []},
        }), 400
    try:
        from app.services.live_trading.position_management import (
            list_managed_positions_for_account,
        )

        rows = list_managed_positions_for_account(
            user_id=int(g.user_id),
            credential_id=int(credential_id),
        )
        return jsonify({'code': 1, 'msg': 'success', 'data': {'items': rows}})
    except Exception:
        logger.exception("get_managed_account_positions failed")
        return jsonify({
            'code': 0,
            'msg': '加载持仓策略归属失败',
            'data': {'items': []},
        }), 500


@strategy_blp.route('/account/managed-strategies', methods=['POST'])
@login_required
def create_managed_account_strategy():
    """用标准策略创建流程接管交易所当前整笔仓位。"""
    payload = request.get_json(silent=True) or {}
    position_ref = payload.get("position")
    strategy_payload = payload.get("strategy")
    if not isinstance(position_ref, dict) or not isinstance(strategy_payload, dict):
        return jsonify({'code': 0, 'msg': '缺少仓位或策略配置', 'data': None}), 400
    try:
        from app.services.live_trading.position_management import (
            PositionManagementError,
            create_managed_strategy,
        )

        result = create_managed_strategy(
            user_id=int(g.user_id),
            position_ref=position_ref,
            strategy_payload=strategy_payload,
        )
        return jsonify({'code': 1, 'msg': '已创建持仓管理策略', 'data': result}), 201
    except PositionManagementError as exc:
        return jsonify({'code': 0, 'msg': str(exc), 'data': None}), exc.status_code
    except Exception:
        logger.exception("create_managed_account_strategy failed")
        return jsonify({'code': 0, 'msg': '创建持仓管理策略失败', 'data': None}), 500
