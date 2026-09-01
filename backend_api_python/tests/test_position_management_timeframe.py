import pandas as pd

from app.services.strategy_v2 import StrategyV2LiveSession, compile_strategy_v2
from app.services.strategy_v2.contract import StrategyV2ContractError
from app.services.strategy_v2.position_management_timeframe import (
    effective_position_management_program,
    normalize_position_management_timeframe,
)


GENERIC_SOURCE = """
def initialize(context):
    context.set_universe(["Crypto:BTC/USDT@swap"])
    context.subscribe(frequency="1h")
    context.set_metadata(direction_mode="long_only", position_management_generic=True)

def handle_data(context, data):
    pass
"""

ORDINARY_SOURCE = GENERIC_SOURCE.replace(
    ", position_management_generic=True",
    "",
)


def test_generic_position_manager_uses_instance_timeframe():
    program = effective_position_management_program(
        compile_strategy_v2(GENERIC_SOURCE),
        {"enabled": True, "timeframe": "15m"},
    )

    assert program.manifest.driving_frequency == "15m"
    assert program.manifest.metadata()["subscriptions"][0]["frequency"] == "15m"


def test_generic_position_manager_keeps_source_timeframe_without_override():
    program = effective_position_management_program(
        compile_strategy_v2(GENERIC_SOURCE),
        {"enabled": True},
    )

    assert program.manifest.driving_frequency == "1h"


def test_ordinary_strategy_cannot_override_source_timeframe():
    program = effective_position_management_program(
        compile_strategy_v2(ORDINARY_SOURCE),
        {"enabled": True, "timeframe": "1d"},
    )

    assert program.manifest.driving_frequency == "1h"


def test_position_management_timeframe_only_accepts_ui_options():
    for timeframe in ("15m", "1h", "4h", "1d"):
        assert normalize_position_management_timeframe(timeframe) == timeframe

    try:
        normalize_position_management_timeframe("5m")
    except StrategyV2ContractError as exc:
        assert str(exc) == "strategyV2.positionManagementTimeframeInvalid"
    else:
        raise AssertionError("5m should be rejected")


def test_live_session_uses_effective_program_instead_of_recompiling_source_timeframe():
    program = effective_position_management_program(
        compile_strategy_v2(GENERIC_SOURCE),
        {"enabled": True, "timeframe": "1d"},
    )
    index = pd.date_range("2026-01-01", periods=2, freq="1D")
    frames = {
        "Crypto:BTC/USDT@swap": pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "volume": [10.0, 11.0],
            },
            index=index,
        ),
    }

    session = StrategyV2LiveSession(
        code=GENERIC_SOURCE,
        program=program,
        frames=frames,
        frequency_frames={"1d": frames},
        initial_capital=1_000,
    )

    assert session.program.manifest.driving_frequency == "1d"
    assert session.portal.driving_frequency == "1d"
