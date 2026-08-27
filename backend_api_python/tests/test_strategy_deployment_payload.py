import pytest

from app.services import strategy_v2
from app.services.strategy import StrategyService


def test_deployment_payload_accepts_direction_mode():
    payload = StrategyService._deployment_payload(
        {
            "sourceId": 9,
            "name": "Dual strategy",
            "initialCapital": 1_000,
            "executionMode": "live",
            "directionMode": "both",
            "positionSide": "neutral",
        }
    )

    assert payload["directionMode"] == "both"
    assert payload["positionSide"] == "neutral"


def test_deployment_payload_still_rejects_unknown_fields():
    with pytest.raises(ValueError, match="strategyV2.unsupportedFields"):
        StrategyService._deployment_payload({"sourceId": 9, "unknown": True})


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

    class DeploymentService:
        def save(self, **kwargs):
            saved.append(kwargs)
            return 44

    monkeypatch.setattr(service, "get_strategy", lambda *_args, **_kwargs: existing)
    monkeypatch.setattr(strategy_v2, "get_strategy_v2_deployment_service", lambda: DeploymentService())

    assert service.update_strategy(44, {"params": {"atr_period": 21}}, user_id=3) is True
    assert saved[0]["strategy_id"] == 44
    assert saved[0]["payload"]["positionManagement"] == marker
