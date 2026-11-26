import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.trade_discipline import inventory_cli as trade_discipline_inventory
from app.trade_discipline import position_evidence
from app.trade_plans.repository import PlanRepository
from app.trade_plans.service import PlanService

_ORDER_TIME = datetime(2026, 11, 21, 2, tzinfo=timezone.utc)
_INSTRUMENT = {
    "market": "Crypto", "symbol": "BTC/USDT", "exchange_id": "binance",
    "market_type": "swap", "instrument_id": "BTCUSDT", "settlement_ccy": "USDT",
}


def candidate(*, expires=_ORDER_TIME + timedelta(hours=1), **instrument_changes):
    return {
        "plan_id": "historical-plan", "version_id": "version-2", "version_no": 2,
        "snapshot": {"instrument": dict(_INSTRUMENT, **instrument_changes), "expires_at": expires.isoformat() if expires else None},
        "version_created_at": (_ORDER_TIME - timedelta(hours=3)).isoformat(),
        "observing_since": (_ORDER_TIME - timedelta(hours=6)).isoformat(),
    }


@pytest.fixture
def lookup(monkeypatch):
    cursor = Mock()

    @contextmanager
    def read_transaction(self):
        yield cursor

    monkeypatch.setattr(PlanRepository, "transaction", read_transaction)
    return cursor


def match(**overrides):
    return PlanService(now=lambda: _ORDER_TIME + timedelta(days=7)).match_at_order_time(
        7, **dict(instrument=_INSTRUMENT, direction="long", submitted_at=_ORDER_TIME.isoformat(), **overrides),
    )


def test_historical_plan_query_uses_order_time_for_version_state_and_expiry(lookup):
    lookup.fetchall.return_value = [candidate(symbol="BTC/USDT:USDT", exchange_id="binanceusdm")]

    result = match()

    assert result["status"] == "matched"
    assert (result["plan_id"], result["version_id"], result["version_no"]) == ("historical-plan", "version-2", 2)
    sql, binds = lookup.execute.call_args.args
    assert binds == (_ORDER_TIME, _ORDER_TIME, 7, _ORDER_TIME, "long")
    sql = " ".join(sql.split())
    assert sql.startswith("SELECT")
    assert "plan_id=p.id AND created_at<=%s ORDER BY created_at DESC, version_no DESC LIMIT 1" in sql
    assert "event_type='state_changed' AND recorded_at<=%s ORDER BY recorded_at DESC, id DESC LIMIT 1" in sql
    assert "state ON state.lifecycle='observing'" in sql
    assert "WHERE p.user_id=%s AND p.created_at<=%s AND v.snapshot->>'direction'=%s" in sql
    assert "p.current_version_id" not in sql and "p.lifecycle" not in sql
    assert "occurred_at" not in sql


@pytest.mark.parametrize("changes", [
    {"symbol": "ETH/USDT"}, {"market": "USStock"}, {"exchange_id": "okx"},
    {"market_type": "spot"}, {"market_type": "futures"}, {"instrument_id": "OTHER"},
    {"settlement_ccy": "USDC"},
])
def test_different_instrument_identity_is_no_plan(lookup, changes):
    lookup.fetchall.return_value = [candidate(**changes)]

    assert match()["status"] == "no_plan"


@pytest.mark.parametrize("expires", [_ORDER_TIME, _ORDER_TIME - timedelta(minutes=1)])
def test_expiry_at_or_before_order_is_no_plan(lookup, expires):
    lookup.fetchall.return_value = [candidate(expires=expires)]

    assert match()["status"] == "no_plan"


def test_retrospective_or_empty_result_is_no_plan(lookup):
    retrospective = candidate()
    retrospective["snapshot"]["retrospective"] = True
    lookup.fetchall.side_effect = [[retrospective], []]

    assert match()["status"] == "no_plan"
    assert match()["status"] == "no_plan"


def test_unconfigured_lookback_and_missing_identity_are_unavailable(lookup):
    lookup.fetchall.side_effect = [[candidate(expires=None)], [candidate(instrument_id="")],
                                    [candidate(expires=None), candidate()]]
    assert match() == {"status": "unavailable", "reason": "plan_lookback_not_configured"}
    assert match() == {"status": "unavailable", "reason": "plan_identity_incomplete"}
    assert match()["status"] == "matched"


def test_lookback_counts_from_most_recent_version_or_observing_entry(lookup):
    row = candidate(expires=None)
    lookup.fetchall.return_value = [row]
    assert match(lookback_hours=3)["status"] == "matched"
    assert match(lookback_hours=2)["status"] == "no_plan"
    row["observing_since"] = (_ORDER_TIME - timedelta(hours=1)).isoformat()
    assert match(lookback_hours=1)["status"] == "matched"


def test_query_failure_is_distinct_from_no_plan(lookup):
    lookup.execute.side_effect = RuntimeError("mock database timeout")

    assert match() == {"status": "unavailable", "reason": "plan_query_failed"}


def test_order_time_must_include_timezone_without_querying_database(lookup):
    result = PlanService().match_at_order_time(
        7, instrument=_INSTRUMENT, direction="long", submitted_at="2026-11-21T02:00:00",
    )

    assert result == {"status": "unavailable", "reason": "order_submission_time_unavailable"}
    lookup.execute.assert_not_called()


def test_cli_uses_real_submission_time_once_per_entry_order(lookup, monkeypatch, capsys):
    def event(order_id, action, side, submitted_at):
        return {"order_id": order_id, "action": action, "side": side, "submitted_at": submitted_at}

    lookup.fetchall.side_effect = [[candidate()], []]
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "7")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "4")
    monkeypatch.setenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS", "48")
    monkeypatch.setattr("sys.argv", ["trade_discipline_inventory", "--evidence-symbol", "BTC/USDT", "--check-plan"])
    monkeypatch.setattr(trade_discipline_inventory, "read_account_position_inventory", Mock(return_value={
        "user_id": 7, "credential_id": 4, "status": "ok", "exchange_id": "binance", "market_type": "swap",
    }))
    second_time = _ORDER_TIME + timedelta(seconds=30)
    monkeypatch.setattr(position_evidence, "read_position_evidence", Mock(return_value={
        "status": "partial", "instrument": "BTCUSDT",
        "events": [
            event("1", "open", "buy", _ORDER_TIME.isoformat()),
            event("1", "add", "buy", _ORDER_TIME.isoformat()),
            event("2", "close", "sell", _ORDER_TIME.isoformat()),
            event("3", "reverse", "sell", second_time.isoformat()),
            event("4", "add", "sell", None),
        ],
    }))

    assert trade_discipline_inventory.main() == 1
    result = json.loads(capsys.readouterr().out)["plan_checks"]

    assert [(check["order_id"], check["direction"], check["status"]) for check in result] == [
        ("1", "long", "matched"), ("3", "short", "no_plan"), ("4", "short", "unavailable"),
    ]
    assert result[0]["version_id"] == "version-2"
    assert result[2]["reason"] == "order_submission_time_unavailable"
    assert [call.args[1] for call in lookup.execute.call_args_list] == [
        (_ORDER_TIME, _ORDER_TIME, 7, _ORDER_TIME, "long"),
        (second_time, second_time, 7, second_time, "short"),
    ]
