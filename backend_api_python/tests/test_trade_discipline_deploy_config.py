"""执行发布脚本中的真实配置校验，确保不能误启实盘或遗漏退出时间边界。"""

import sys
from pathlib import Path

import pytest


SCRIPT = (Path(__file__).resolve().parents[2] / "ops/deploy/deploy-remote.sh").read_text()
VERIFY = SCRIPT.split('"$DISCIPLINE_EXIT_FROM" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
PREFLIGHT = SCRIPT.split('backend python - "$DISCIPLINE_EXIT_FROM" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
CUTOFF = "2026-11-26T14:56:19.694Z"


def setup_environment(monkeypatch, mode):
    # 不加载本地凭证，只设置本用例用到的非敏感能力开关。
    monkeypatch.setenv("QUANTDINGER_ENV_FILE", "/dev/null")
    values = {
        "TRADE_DISCIPLINE_USER_ID": "1",
        "TRADE_DISCIPLINE_OBSERVATION_ENABLED": "true",
        "TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL": "true",
        "TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED": "false",
        "TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED": "false",
        "TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS": "24",
        "TRADE_DISCIPLINE_AUTO_EXIT_ENABLED": mode,
        "TRADE_DISCIPLINE_EXIT_FROM": CUTOFF if mode == "true" else "",
        "LIVE_TRADING_ENABLED": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["-", "1", mode, CUTOFF if mode == "true" else ""])


@pytest.mark.parametrize("mode", ["false", "true"])
def test_valid_observation_and_production_modes(monkeypatch, mode):
    setup_environment(monkeypatch, mode)
    exec(compile(VERIFY, "deploy-config-verification", "exec"), {})


@pytest.mark.parametrize("change", [
    {"LIVE_TRADING_ENABLED": "false"},
    {"TRADE_DISCIPLINE_EXIT_FROM": ""},
    {"TRADE_DISCIPLINE_USER_ID": "2"},
    {"TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED": "true"},
    {"TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED": "true"},
    {"TRADE_DISCIPLINE_AUTO_EXIT_ENABLED": "false"},
])
def test_production_configuration_rejects_unsafe_mismatch(monkeypatch, change):
    setup_environment(monkeypatch, "true")
    for key, value in change.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        exec(compile(VERIFY, "deploy-config-verification", "exec"), {})


@pytest.mark.parametrize("cutoff,live,valid", [
    (CUTOFF, "true", True),
    ("2026-11-26T22:56:19.694+08:00", "true", True),
    ("2026-11-26T22:56:19", "true", False),
    ("", "true", False),
    (CUTOFF, "false", False),
])
def test_preflight_checks_timezone_and_preserves_global_capability(monkeypatch, cutoff, live, valid):
    setup_environment(monkeypatch, "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", live)
    monkeypatch.setattr(sys, "argv", ["-", cutoff])
    if valid:
        exec(compile(PREFLIGHT, "deploy-exit-preflight", "exec"), {})
    else:
        with pytest.raises(SystemExit):
            exec(compile(PREFLIGHT, "deploy-exit-preflight", "exec"), {})
    assert __import__("os").environ["LIVE_TRADING_ENABLED"] == live
