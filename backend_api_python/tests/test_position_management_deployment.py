"""持仓管理实例现有部署与更新行为。"""

import json

from app.services import strategy_v2
from app.services.strategy import StrategyService
from app.services.strategy_v2 import deployment
from app.services.strategy_v2.deployment import StrategyV2DeploymentService


GENERIC_SOURCE = """
def initialize(context):
    context.set_universe(["Crypto:BTC/USDT@swap"])
    context.subscribe(frequency="1h")
    context.allow_leverage(max_leverage=125)
    context.set_metadata(direction_mode="short_only", position_management_generic=True)

def handle_data(context, data):
    pass
"""


class _Cursor:
    lastrowid = 41
    rowcount = 1

    def __init__(self):
        self.params = ()

    def execute(self, _query, params=()):
        self.params = params

    def close(self):
        return None


class _Db:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        return None


class _Sources:
    @staticmethod
    def get_source(_source_id, user_id=None):
        return {"id": 9, "name": "Generic short manager", "code": GENERIC_SOURCE}

    @staticmethod
    def get_latest_version(_source_id, user_id=None):
        return {"id": 19, "code": GENERIC_SOURCE, "metadata": {}}


def test_deployment_persists_management_config_symbol_and_timeframe(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(deployment, "get_script_source_service", lambda: _Sources())
    monkeypatch.setattr(deployment, "get_db_connection", lambda: _Db(cursor))
    payload = {
        "sourceId": 9,
        "name": "Generic short manager",
        "initialCapital": 1_000,
        "executionMode": "signal",
        "positionManagement": {
            "enabled": True,
            "auto_stop_when_flat": True,
            "instrument": "Crypto:M/USDT@swap",
            "side": "short",
            "timeframe": "4h",
        },
    }

    StrategyV2DeploymentService().save(user_id=7, payload=payload)
    trading_config = json.loads(cursor.params[-2])

    assert cursor.params[5] == "M/USDT"
    assert cursor.params[9] == "swap"
    assert cursor.params[6] == "4h"
    assert trading_config["symbol"] == "M/USDT"
    assert trading_config["market_type"] == "swap"
    assert trading_config["position_management"]["auto_stop_when_flat"] is True
    assert trading_config["strategy_manifest"]["drivingFrequency"] == "4h"
    assert trading_config["strategy_manifest"]["subscriptions"][0]["frequency"] == "4h"


def test_updating_managed_strategy_preserves_position_management_marker(monkeypatch):
    service = StrategyService()
    marker = {"enabled": True, "auto_stop_when_flat": True}
    existing = {
        "id": 44,
        "user_id": 3,
        "strategy_name": "[持仓] KAITO/USDC",
        "initial_capital": 1000,
        "execution_mode": "live",
        "leverage": 5,
        "trading_config": {
            "script_source_id": 8,
            "params": {"atr_period": 14},
            "position_management": marker,
        },
    }
    saved = []

    class _DeploymentService:
        def save(self, **kwargs):
            saved.append(kwargs)
            return 44

    monkeypatch.setattr(service, "get_strategy", lambda *_args, **_kwargs: existing)
    monkeypatch.setattr(
        strategy_v2,
        "get_strategy_v2_deployment_service",
        lambda: _DeploymentService(),
    )

    assert service.update_strategy(44, {"params": {"atr_period": 21}}, user_id=3) is True
    assert saved[0]["strategy_id"] == 44
    assert saved[0]["payload"]["positionManagement"] == marker
