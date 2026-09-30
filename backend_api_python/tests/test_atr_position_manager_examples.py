from pathlib import Path
import unittest

import pandas as pd

from app.services.strategy_v2.runtime import StrategyV2LiveSession, compile_strategy_v2
from app.services.strategy_v2.position_management_timeframe import (
    position_management_candidate,
)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs" / "examples"
SHORT_SOURCE = EXAMPLES / "strategy_v2_atr_short_position_manager_generic_1h.py"
LONG_SOURCE = EXAMPLES / "strategy_v2_atr_long_position_manager_generic_1h.py"
INSTRUMENT = "Crypto:M/USDT@binance:swap"
MANAGED_INSTRUMENT = "Crypto:M/USDT@swap"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frame(*, periods: int = 30, final_high: float | None = None, final_low: float | None = None, final_close: float | None = None) -> pd.DataFrame:
    index = pd.date_range("2026-08-01", periods=periods, freq="1h", tz="UTC")
    rows = []
    for offset in range(periods):
        close = 100.0 + (offset - periods / 2) * 0.02
        rows.append({
            "open": close - 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        })
    if final_high is not None:
        rows[-1]["high"] = final_high
    if final_low is not None:
        rows[-1]["low"] = final_low
    if final_close is not None:
        rows[-1]["close"] = final_close
        rows[-1]["high"] = max(rows[-1]["high"], final_close)
        rows[-1]["low"] = min(rows[-1]["low"], final_close)
    return pd.DataFrame(rows, index=index)


def _position(side: str) -> dict:
    return {
        INSTRUMENT: {
            "side": side,
            "position_side": "",
            "amount": 10.0,
            "avg_cost": 100.0,
            "last_price": 100.0,
        }
    }


def _session(path: Path, frame: pd.DataFrame, *, fixed_stop_price: float = 0) -> StrategyV2LiveSession:
    return StrategyV2LiveSession(
        code=_source(path),
        frames={INSTRUMENT: frame},
        initial_capital=1000,
        params={"managed_instrument": MANAGED_INSTRUMENT, "leverage": 3, "fixed_stop_price": fixed_stop_price},
    )


class AtrPositionManagerExamplesTest(unittest.TestCase):
    def test_managed_position_candidate_uses_instance_instrument(self):
        candidate = position_management_candidate({
            "position_management": {
                "enabled": True,
                "instrument": MANAGED_INSTRUMENT,
                "side": "short",
            }
        })

        self.assertEqual(candidate, {
            "key": MANAGED_INSTRUMENT,
            "market": "Crypto",
            "symbol": "M/USDT",
            "exchange_id": "",
            "market_type": "swap",
            "instrument_id": MANAGED_INSTRUMENT,
        })

    def test_generic_sources_compile(self):
        for path, direction in (
            (SHORT_SOURCE, "short_only"),
            (LONG_SOURCE, "long_only"),
        ):
            with self.subTest(path=path.name):
                manifest = compile_strategy_v2(_source(path)).manifest
                self.assertEqual(manifest.driving_frequency, "1h")
                self.assertEqual(manifest.direction_mode, direction)
                self.assertEqual(manifest.max_leverage, 125)
                self.assertIs(
                    manifest.metadata()["metadata"]["position_management_generic"],
                    True,
                )

    def test_no_registered_position_never_creates_an_order(self):
        frame = _frame()
        for path in (SHORT_SOURCE, LONG_SOURCE):
            with self.subTest(path=path.name):
                intents, messages, _ = _session(path, frame).process({INSTRUMENT: frame})
                self.assertEqual(intents, [])
                self.assertEqual(messages, [])

    def test_registered_position_logs_key_decisions(self):
        frame = _frame()
        for path, side, log_prefix in (
            (SHORT_SOURCE, "short", "1H空单持仓检查"),
            (LONG_SOURCE, "long", "1H多单持仓检查"),
        ):
            with self.subTest(path=path.name):
                session = _session(path, frame)
                session.synchronize_positions(_position(side))
                intents, messages, _ = session.process({INSTRUMENT: frame})
                self.assertEqual(intents, [])
                cycle = next(message for message in messages if log_prefix in message)
                for field in (
                    "开仓均价=",
                    "ATR=",
                    "固定止损=",
                    "移动止损启动价=",
                    "本周期止损=",
                    "下一周期止损=",
                    "当前杠杆收益率=",
                    "止损杠杆收益率=",
                    "判断=继续持有",
                ):
                    self.assertIn(field, cycle)

    def test_short_atr_stop_requires_close_above_stop(self):
        initial = _frame()
        session = _session(SHORT_SOURCE, initial)
        session.synchronize_positions(_position("short"))
        self.assertEqual(session.process({INSTRUMENT: initial})[0], [])

        triggered = _frame(periods=31, final_high=120.0)
        session.synchronize_positions(_position("short"))
        self.assertEqual(session.process({INSTRUMENT: triggered})[0], [])

        triggered = _frame(periods=32, final_high=120.0, final_close=120.0)
        session.synchronize_positions(_position("short"))
        intents, messages, _ = session.process({INSTRUMENT: triggered})
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "target_value")
        self.assertEqual(intents[0].value, 0)
        self.assertEqual(intents[0].position_side, "short")
        self.assertEqual(intents[0].reason, "atr_short_position_stop")
        self.assertTrue(any("收盘价触及ATR止损，提交空单平仓" in message for message in messages))

    def test_long_atr_stop_requires_close_below_stop(self):
        initial = _frame()
        session = _session(LONG_SOURCE, initial)
        session.synchronize_positions(_position("long"))
        self.assertEqual(session.process({INSTRUMENT: initial})[0], [])

        triggered = _frame(periods=31, final_low=80.0)
        session.synchronize_positions(_position("long"))
        self.assertEqual(session.process({INSTRUMENT: triggered})[0], [])

        triggered = _frame(periods=32, final_low=80.0, final_close=80.0)
        session.synchronize_positions(_position("long"))
        intents, messages, _ = session.process({INSTRUMENT: triggered})
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "target_value")
        self.assertEqual(intents[0].value, 0)
        self.assertEqual(intents[0].position_side, "long")
        self.assertEqual(intents[0].reason, "atr_long_position_stop")
        self.assertTrue(any("收盘价触及ATR止损，提交多单平仓" in message for message in messages))

    def test_fixed_stop_is_independent_from_atr_stop(self):
        initial = _frame()
        for path, side, stop, close, reason in (
            (LONG_SOURCE, "long", 99.0, 98.5, "fixed_long_position_stop"),
            (SHORT_SOURCE, "short", 101.0, 101.5, "fixed_short_position_stop"),
        ):
            with self.subTest(side=side):
                session = _session(path, initial, fixed_stop_price=stop)
                session.synchronize_positions(_position(side))
                self.assertEqual(session.process({INSTRUMENT: initial})[0], [])
                state = dict(session.program.state.state)
                self.assertNotEqual(state["initial_stop"], stop)

                triggered = _frame(periods=31, final_close=close)
                session.synchronize_positions(_position(side))
                intents, messages, _ = session.process({INSTRUMENT: triggered})
                self.assertEqual(len(intents), 1)
                self.assertEqual(intents[0].reason, reason)
                self.assertEqual(intents[0].position_side, side)
                self.assertTrue(any("收盘价触及固定止损" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
