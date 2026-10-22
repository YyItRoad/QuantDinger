from app.routes.settings import _effective_schema
from app.services import ai_calibration
from app.services.settings.capabilities import get_feature_flags


def test_feature_flags_parse_explicit_values():
    flags = get_feature_flags({
        "BILLING_ENABLED": "false",
        "MARKETPLACE_ENABLED": "0",
        "ENABLE_REGISTRATION": "yes",
    })

    assert flags == {
        "billing_enabled": False,
        "marketplace_enabled": False,
        "registration_enabled": True,
    }


def test_fresh_install_disables_commercial_features():
    flags = get_feature_flags({})

    assert flags["billing_enabled"] is False
    assert flags["marketplace_enabled"] is False
    assert flags["registration_enabled"] is True


def test_billing_configuration_is_hidden_until_billing_is_enabled():
    disabled_schema = _effective_schema({"BILLING_ENABLED": "false"})
    enabled_schema = _effective_schema({"BILLING_ENABLED": "true"})

    assert "features" in disabled_schema
    assert "billing" not in disabled_schema
    assert "billing" in enabled_schema
    feature_keys = {item["key"] for item in disabled_schema["features"]["items"]}
    assert feature_keys == {
        "BILLING_ENABLED",
        "MARKETPLACE_ENABLED",
        "ENABLE_REGISTRATION",
    }


def test_agent_and_trading_tuning_is_kept_behind_advanced_settings():
    schema = _effective_schema({})
    agent = {item["key"]: item for item in schema["agent"]["items"]}
    trading = {item["key"]: item for item in schema["trading"]["items"]}

    assert agent["AGENT_LIVE_TRADING_ENABLED"]["is_advanced"] is False
    assert agent["ENABLE_REFLECTION_WORKER"]["is_advanced"] is False
    assert agent["ENABLE_CONFIDENCE_CALIBRATION"]["is_advanced"] is False
    assert agent["ENABLE_AI_ENSEMBLE"]["is_advanced"] is False
    assert agent["QUANTDINGER_DEPLOYMENT_MODE"]["is_advanced"] is True
    assert agent["QUANTDINGER_DEPLOYMENT_MODE"]["default"] == "self_hosted"
    assert agent["AI_ENSEMBLE_MODELS"]["is_advanced"] is True
    assert trading["ALLOW_LOCAL_DESKTOP_BROKERS"]["is_advanced"] is False
    assert all(
        item["is_advanced"]
        for key, item in trading.items()
        if key != "ALLOW_LOCAL_DESKTOP_BROKERS"
    )


def test_calibration_worker_uses_every_configured_market(monkeypatch):
    calls = []

    def fake_calibrate(self, *, market, lookback_days, min_samples):
        del self
        calls.append((market, lookback_days, min_samples))
        return None

    monkeypatch.setenv("ENABLE_CONFIDENCE_CALIBRATION", "true")
    monkeypatch.setenv("AI_CALIBRATION_MARKETS", "Crypto,USStock")
    monkeypatch.setenv("AI_CALIBRATION_LOOKBACK_DAYS", "45")
    monkeypatch.setenv("AI_CALIBRATION_MIN_SAMPLES", "120")
    monkeypatch.setattr(ai_calibration.AICalibrationService, "calibrate_market", fake_calibrate)

    ai_calibration.start_ai_calibration_worker()

    assert calls == [("Crypto", 45, 120), ("USStock", 45, 120)]
