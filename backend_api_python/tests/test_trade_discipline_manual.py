import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.trade_discipline import manual as manual
from app.trade_discipline import observation as observation
from app.trade_discipline import origin
from app.services.live_trading import position_management
from app.trade_discipline import position_evidence
from app.services.llm import LLMService
from app.services.signal_notifier import SignalNotifier
from app.trade_plans.repository import PlanRepository

INVENTORY = dict(user_id=3, credential_id=7, exchange_id="binance", environment="demo",
                 market_type="swap", exit_recheck=True)
POSITION = dict(symbol="BTC/USDT", size=0.5, side="long")
NOW = datetime.fromisoformat("2026-11-21T12:00:00+08:00")
REPORT = dict(instrument="BTCUSDT", symbol="BTC/USDT", side="long", opening_fill_id="10", opened_at=NOW.isoformat())
EVENTS = [dict(order_id="1", action="open", side="buy"), dict(order_id="2", action="add", side="buy")]


@pytest.mark.parametrize("origins, outcome", [
    (("unknown", "unknown"), "needs_review"),
    (("strategy", "strategy"), "skipped"),
    (("system", "system"), "skipped"),
    (("manual", "strategy"), "notify_only"),
    (("strategy", "manual"), "notify_only"),
])
def test_origin_gates_discipline(monkeypatch, origins, outcome):
    monkeypatch.setattr(manual, "opening_order_origin", Mock(side_effect=origins))
    plans = Mock()
    monkeypatch.setattr(observation, "check_plans", plans)
    result = manual.assess_manual_opening(INVENTORY, POSITION, EVENTS, REPORT, NOW, None)
    assert result["outcome"] == outcome
    plans.assert_not_called()
    if outcome == "notify_only":
        sender = Mock(return_value=(True, ""))
        monkeypatch.setattr(SignalNotifier, "_notify_browser", sender)
        manual.apply_poll_window(INVENTORY, result, None)
        assert result["notification_delivered"]
        assert sender.call_args.kwargs["user_id"] == 3


def test_attestation_is_exact_and_cannot_override_system_binding(monkeypatch):
    binding = Mock(return_value=([], False))
    monkeypatch.setattr(origin, "read_system_orders", binding)
    assert manual.opening_order_origin(INVENTORY, "BTCUSDT", "1") == "unknown"
    monkeypatch.setenv("TRADE_DISCIPLINE_MANUAL_ORDERS_JSON", json.dumps([[3, 7, "binance", "swap", "BTCUSDT", "1"]]))
    assert manual.opening_order_origin(INVENTORY, "BTCUSDT", "1") == "manual"
    assert manual.opening_order_origin(dict(INVENTORY, environment="live"), "BTCUSDT", "1") == "unknown"
    assert manual.opening_order_origin(dict(INVENTORY, credential_id=8), "BTCUSDT", "1") == "unknown"
    binding.return_value = ([dict(id=1, source="binding", strategy_id=42)], False)
    assert manual.opening_order_origin(INVENTORY, "BTCUSDT", "1") == "strategy"


@pytest.mark.parametrize("client_id", ["ios_im_manual_123", "web_manual_123", "android_manual_123"])
def test_external_order_uses_confirmed_account_policy(monkeypatch, client_id):
    monkeypatch.setattr(origin, "read_system_orders", Mock(return_value=([], False)))
    monkeypatch.setenv("TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL", "true")
    assert manual.opening_order_origin(dict(INVENTORY, environment="live"), "BTCUSDT", "1", client_id) == "manual"
    assert manual.opening_order_origin(dict(INVENTORY, environment="live"), "BTCUSDT", "1") == "unknown"
    monkeypatch.setattr(origin, "read_system_orders", Mock(side_effect=RuntimeError("synthetic read failure")))
    assert manual.opening_order_origin(INVENTORY, "BTCUSDT", "1", client_id) == "unknown"


def test_unresolved_system_submission_never_becomes_manual(monkeypatch):
    monkeypatch.setenv("TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL", "true")
    monkeypatch.setattr(origin, "read_system_orders", Mock(return_value=([], True)))
    assert manual.opening_order_origin(INVENTORY, "BTCUSDT", "1", "ios_im_test") == "unknown"


@pytest.mark.parametrize("plan, ema, outcome, reason", [
    ("no_plan", "allowed", "proposed_exit", "no_plan"),
    ("unavailable", "allowed", "needs_review", "plan_evidence_unavailable"),
    ("matched", "blocked", "proposed_exit", "ema25_direction_blocked"),
    ("matched", "unavailable", "needs_review", "ema_evidence_unavailable"),
    ("matched", "allowed", "opening_passed", None),
])
def test_plan_before_ema_and_opening_only(monkeypatch, plan, ema, outcome, reason):
    monkeypatch.setattr(manual, "opening_order_origin", Mock(return_value="manual"))
    plans, candles = Mock(return_value=[{"status": plan}]), Mock(return_value=[{"status": ema}])
    monkeypatch.setattr(observation, "check_plans", plans)
    monkeypatch.setattr(observation, "check_ema", candles)
    result = manual.assess_manual_opening(INVENTORY, POSITION, EVENTS, REPORT, NOW, None)
    assert (result["outcome"], result.get("reason")) == (outcome, reason)
    assert plans.call_args.args[0]["evidence"]["events"] == EVENTS[:1]
    assert candles.call_count == int(plan == "matched")


def test_scheduler_reads_each_credential_and_continues_on_failure(monkeypatch):
    monkeypatch.setenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED", "true")
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setattr(position_management, "list_position_inventory_accounts", Mock(return_value=[{"id": 7}, {"id": 8}]))
    observe = Mock(side_effect=[RuntimeError("fixture"), {"user_id": 3, "credential_id": 8, "positions": []}])
    monkeypatch.setattr(observation, "observe_account", observe)
    now = datetime.fromisoformat("2026-11-21T12:00:00+08:00")
    observation.hourly_observer_from_env(now)._run(now)
    assert [call.kwargs["credential_id"] for call in observe.call_args_list] == [7, 8]
    assert all(call.kwargs["manual_only"] for call in observe.call_args_list)


@pytest.mark.parametrize("model_output, accepted", [
    ('{"fixed_stop_price":"90"}', True),
    ('{"fixed_stop_price":"89"}', False),
    ('{"fixed_stop_price":"90","leverage":50}', False),
])
def test_llm_output_must_match_pinned_plan_stop(monkeypatch, model_output, accepted):
    @contextmanager
    def transaction(self):
        yield None

    monkeypatch.setattr(PlanRepository, "transaction", transaction)
    monkeypatch.setattr(PlanRepository, "versions", Mock(return_value=[
        {"id": "v2", "snapshot": {"direction": "long", "risk_inputs": {"stop_price": "80"}}},
        {"id": "v1", "snapshot": {"direction": "long", "risk_inputs": {"stop_price": "90"}}},
    ]))
    llm = Mock(return_value=model_output)
    monkeypatch.setattr(LLMService, "call_llm_api", llm)
    args = (3, {"plan_id": "p", "version_id": "v1"}, {"params": {"x": 1}}, "long")
    if accepted:
        assert manual.load_plan_parameters(*args) == {"fixed_stop_price": 90.0, "x": 1}
    else:
        with pytest.raises(ValueError):
            manual.load_plan_parameters(*args)
    assert json.loads(llm.call_args.args[0][1]["content"])["risk_inputs"]["stop_price"] == "90"


@pytest.mark.parametrize("quote, price, accepted", [
    ("止损设在90", "90", True),
    ("止损设在90", "89", False),
    ("止损设在89", "89", False),
    (None, "90", False),
])
def test_llm_can_extract_stop_from_pinned_plan_text(monkeypatch, quote, price, accepted):
    @contextmanager
    def transaction(self):
        yield None

    monkeypatch.setattr(PlanRepository, "transaction", transaction)
    monkeypatch.setattr(PlanRepository, "versions", Mock(return_value=[
        {"id": "v1", "snapshot": {"direction": "long", "risk_inputs": {},
                                 "invalidation": "跌破支撑退出，止损设在90。"}},
    ]))
    llm = Mock(return_value=json.dumps({"fixed_stop_price": price, "stop_evidence": quote}))
    monkeypatch.setattr(LLMService, "call_llm_api", llm)
    if accepted:
        assert manual.load_plan_parameters(3, {"plan_id": "p", "version_id": "v1"}, {}, "long") == {"fixed_stop_price": 90.0}
    else:
        with pytest.raises(ValueError):
            manual.load_plan_parameters(3, {"plan_id": "p", "version_id": "v1"}, {}, "long")
    assert llm.call_args.args[0][0]["content"] == manual.PLAN_MANAGEMENT_PROMPT


def test_saved_pass_does_not_rejudge_plan_or_count_same_round(monkeypatch, caplog):
    monkeypatch.setattr(manual, "opening_order_origin", Mock(return_value="manual"))
    plans, ema = Mock(), Mock()
    monkeypatch.setattr(observation, "check_plans", plans)
    monkeypatch.setattr(observation, "check_ema", ema)
    saved = {"plan_id": "p", "version_id": "v1", "passed_round": NOW.astimezone(timezone.utc)}
    @contextmanager
    def transaction(self):
        yield None

    monkeypatch.setattr(PlanRepository, "transaction", transaction)
    monkeypatch.setattr(PlanRepository, "discipline_opening_pass", Mock(return_value=saved))
    append = Mock()
    monkeypatch.setattr(PlanRepository, "record_discipline_opening_pass", append)
    caplog.set_level("INFO", logger=manual.__name__)
    inventory = dict(INVENTORY, exit_recheck=False)
    adoption = {"from": NOW, "templates": {"long": {}}}
    for now, outcome in [(NOW, "waiting_for_next_poll"), (NOW + timedelta(hours=1), "proposed_adoption")]:
        position = manual.assess_manual_opening(inventory, POSITION, EVENTS, REPORT, now, None)
        manual.apply_poll_window(inventory, position, now, adoption=adoption)
        assert position["outcome"] == outcome
    assert position["approved_plan"] == {"plan_id": "p", "version_id": "v1"}
    plans.assert_not_called()
    ema.assert_not_called()
    append.assert_not_called()
    steps = [json.loads(record.message.removeprefix("trade_discipline "))
             for record in caplog.records if record.message.startswith("trade_discipline ")]
    assert {row["step"] for row in steps} >= {"opening_origin", "addition_ownership", "poll_window"}
    assert all(row["credential_id"] == 7 and row["opening_fill_id"] == "10" and row["round_at"] for row in steps)


def test_observation_without_auto_adoption_does_not_write_pass(monkeypatch):
    append = Mock()
    monkeypatch.setattr(PlanRepository, "record_discipline_opening_pass", append)
    position = dict(REPORT, outcome="opening_passed", _opening_pass_event_id="internal")

    manual.apply_poll_window(INVENTORY, position, NOW)

    assert position["outcome"] == "opening_passed"
    assert "_opening_pass_event_id" not in position
    append.assert_not_called()
