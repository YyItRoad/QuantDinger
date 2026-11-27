import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.trade_discipline import inventory_cli as trade_discipline_inventory
from app.trade_discipline import position_evidence as evidence
from app.services.live_trading.binance import BinanceFuturesClient

_BASE = 1795226400000


def fill(i, delta, *, order=None, book="both", cumulative=0):
    return {
        "exchange_fill_id": str(i), "exchange_order_id": str(order or (100 + i)),
        "occurred_at": datetime.fromtimestamp((_BASE + i * 60000) / 1000, timezone.utc),
        "quantity": str(abs(delta)), "side": "buy" if delta > 0 else "sell",
        "position_side": book, "cumulative_quantity": str(cumulative),
    }


def remote(i, delta, *, order=None):
    return {"id": i, "orderId": order or 100 + i, "symbol": "BTCUSDT",
            "time": _BASE + i * 60000, "qty": str(abs(delta)),
            "side": "BUY" if delta > 0 else "SELL", "positionSide": "BOTH"}


def order_payload(order_id, i):
    return {"symbol": "BTCUSDT", "orderId": order_id, "time": _BASE + i * 60000 - 30000,
            "clientOrderId": f"ios_im_test_{order_id}"}


def order(i, source="pending_orders"):
    return {"exchange_order_id": str(100 + i), "source": source,
            "payload": json.dumps(order_payload(100 + i, i))}


def inventory(*positions):
    return {
        "user_id": 3, "credential_id": 7, "exchange_id": "binance", "market_type": "swap", "status": "ok",
        "fetched_at": _BASE // 1000 + 3600,
        "positions": [{"symbol": "BTC/USDT", "inst_id": "BTCUSDT", "side": side, "size": size}
                      for side, size in positions],
    }


@pytest.fixture
def inputs(monkeypatch, discipline_product_catalog):
    sources = SimpleNamespace(fills=[], orders=[], cursor=Mock(), exchange=Mock(timeout_sec=15))
    sources.cursor.fetchall.side_effect = lambda: (
        sources.fills if "qd_execution_events" in sources.cursor.execute.call_args.args[0] else sources.orders
    )

    @contextmanager
    def connection():
        yield Mock(cursor=Mock(return_value=sources.cursor))

    monkeypatch.setattr(evidence, "get_db_connection", connection)
    sources.resolve = Mock(return_value={"exchange_id": "binance"})
    sources.factory = Mock(return_value=sources.exchange)
    sources.exchange.get_user_trades.return_value = []
    sources.exchange.get_order.return_value = {}
    monkeypatch.setattr(evidence, "resolve_exchange_config", sources.resolve)
    monkeypatch.setattr(evidence, "create_client", sources.factory)
    return sources


@pytest.mark.parametrize("metadata_change", [
    {"symbol": "BTCUSDT"}, {"contractType": "CURRENT_QUARTER"}, {"marginAsset": "USDT"}, {},
])
def test_usdc_evidence_requires_exact_perpetual_product(inputs, metadata_change):
    snapshot = inventory(("long", 1))
    snapshot["positions"][0].update(symbol="BTC/USDC", inst_id="BTCUSDC")
    meta = dict(symbol="BTCUSDC", contractType="PERPETUAL", baseAsset="BTC", quoteAsset="USDC", marginAsset="USDC")
    inputs.exchange.get_symbol_filters.return_value = {"_meta": dict(meta, **metadata_change)}
    inputs.exchange.get_user_trades.return_value = [dict(remote(1, 1), symbol="BTCUSDC")]
    inputs.exchange.get_order.return_value = dict(order_payload(101, 1), symbol="BTCUSDC")

    report = evidence.read_position_evidence(snapshot, "BTCUSDC")

    if metadata_change:
        assert report["issues"] == ["product_identity_unavailable"]
        assert report["status"] == "unverified"
        inputs.exchange.get_user_trades.assert_not_called()
        return
    assert report["status"] == "reconciled"
    assert (report["product"]["symbol"], report["product"]["settlement_ccy"]) == ("BTC/USDC", "USDC")
    inputs.exchange.get_user_trades.assert_called_once_with(symbol="BTCUSDC", limit=1000)
    inputs.exchange.get_order.assert_called_once_with(symbol="BTCUSDC", order_id="101")


def test_client_identity_is_preserved_locally_and_backfilled_only_when_missing(inputs):
    inputs.fills = [fill(1, 1)]
    inputs.orders = [order(1)]
    result = evidence.read_position_evidence(inventory(("long", 1)), "BTCUSDT")
    assert result["events"][0]["client_order_id"] == "ios_im_test_101"
    inputs.exchange.get_order.assert_not_called()

    payload = order_payload(101, 1)
    payload.pop("clientOrderId")
    inputs.orders = [dict(order(1), payload=payload)]
    inputs.exchange.get_order.return_value = order_payload(101, 1)
    result = evidence.read_position_evidence(inventory(("long", 1)), "BTCUSDT")
    assert result["events"][0]["client_order_id"] == "ios_im_test_101"
    inputs.exchange.get_order.assert_called_once_with(symbol="BTCUSDT", order_id="101")


def test_conflicting_client_ids_make_evidence_incomplete(inputs):
    inputs.fills = [dict(fill(1, 1), client_order_id="system-before-submit")]
    inputs.orders = [order(1)]
    result = evidence.read_position_evidence(inventory(("long", 1)), "BTCUSDT")
    assert result["status"] == "partial"
    assert "order_client_id_conflict" in result["issues"]
    assert result["events"][0]["client_order_id"] == ""


def test_pre_submit_id_and_formatted_exchange_id_are_same_order(inputs):
    client = BinanceFuturesClient(api_key="fixture", secret_key="fixture")
    original_id = "qd_42_101_mkt"
    wire_id = client._format_client_order_id(original_id)
    inputs.exchange._format_client_order_id.side_effect = client._format_client_order_id
    inputs.fills = [dict(fill(1, 1), client_order_id=wire_id)]
    inputs.orders = [dict(order(1), client_order_id=original_id,
                         payload=dict(order_payload(101, 1), clientOrderId=wire_id))]

    result = evidence.read_position_evidence(inventory(("long", 1)), "BTCUSDT")
    assert result["status"] == "reconciled"
    assert result["events"][0]["client_order_id"] == wire_id
    inputs.exchange.get_order.assert_not_called()


def test_cli_replays_local_fills_and_reads_local_order_times(inputs, monkeypatch, capsys):
    inputs.fills = [fill(5, -3), fill(4, -2), fill(3, -1), fill(2, 2), fill(1, 1)]
    inputs.orders = [order(1, "qd_quick_trades"), *(order(i) for i in range(2, 6))]
    monkeypatch.setattr(trade_discipline_inventory, "read_account_position_inventory",
                        Mock(return_value=inventory(("short", 3))))
    monkeypatch.setenv("TRADE_DISCIPLINE_USER_ID", "3")
    monkeypatch.setenv("TRADE_DISCIPLINE_CREDENTIAL_ID", "7")
    monkeypatch.setenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap")
    monkeypatch.setattr("sys.argv", ["trade_discipline_inventory", "--evidence-symbol", "BTC/USDT"])

    assert trade_discipline_inventory.main() == 0
    report = json.loads(capsys.readouterr().out)["evidence"]

    assert report["status"] == "reconciled"
    assert [event["action"] for event in report["events"]] == ["open", "add", "reduce", "close", "open"]
    assert [event["signed_after"] for event in report["events"]] == ["1", "3", "2", "0", "-3"]
    first = report["events"][0]
    assert first["order_time_source"] == "qd_quick_trades"
    assert first["submitted_at"] < first["filled_at"]
    assert report["opened_at"] == {"short": report["events"][-1]["filled_at"]}
    inputs.factory.assert_not_called()
    calls = inputs.cursor.execute.call_args_list
    assert len(calls) == 2
    assert "qd_quick_trades" in calls[1].args[0] and "pending_orders" in calls[1].args[0]
    for call in calls:
        assert call.args[0].lstrip().startswith("SELECT ")
        assert call.args[1][:2] == (3, 7)


def test_backfill_deduplicates_and_splits_a_reversal(inputs):
    inputs.fills = [fill(1, 2)]
    inputs.orders = [order(1)]
    inputs.exchange.get_user_trades.return_value = [remote(1, 2), remote(2, -3)]
    inputs.exchange.get_order.return_value = order_payload(102, 2)

    report = evidence.read_position_evidence(inventory(("short", 1)), "BTCUSDT")

    assert report["status"] == "reconciled"
    opening, reversal = report["events"]
    assert opening["fill_source"] == "local_execution_event"
    assert reversal["action"] == "reverse"
    assert (reversal["filled_quantity"], reversal["closed_quantity"], reversal["opened_quantity"]) == ("3", "2", "1")
    assert (reversal["signed_before"], reversal["signed_after"]) == ("2", "-1")
    assert reversal["order_time_source"] == "exchange_order"
    assert report["opened_at"] == {"short": reversal["filled_at"]}
    inputs.exchange.get_user_trades.assert_called_once_with(symbol="BTCUSDT", limit=1000)
    inputs.exchange.get_order.assert_called_once_with(symbol="BTCUSDT", order_id="102")
    inputs.resolve.assert_called_once_with({"credential_id": 7}, user_id=3)


def test_cumulative_quantity_detects_missing_fills_even_when_net_matches(inputs):
    inputs.fills = [fill(2, 1, order=101, cumulative=3)]
    inputs.exchange.get_user_trades.return_value = [
        remote(1, 2, order=101), remote(2, 1, order=101), remote(3, -2),
    ]
    inputs.orders = [order(1), order(3)]

    report = evidence.read_position_evidence(inventory(("long", 1)), "BTCUSDT")

    assert report["status"] == "reconciled"
    assert [event["order_fill_number"] for event in report["events"]] == [1, 2, 1]
    assert report["opened_at"] == {"long": report["events"][0]["filled_at"]}
    inputs.exchange.get_user_trades.assert_called_once()


def test_hedged_books_are_reconciled_independently(inputs):
    inputs.fills = [fill(1, 2, book="long"), fill(2, -3, book="short"), fill(3, 1, book="short")]
    inputs.orders = [order(i) for i in range(1, 4)]

    report = evidence.read_position_evidence(inventory(("long", 2), ("short", 2)), "BTCUSDT")

    assert report["status"] == "reconciled"
    assert [(event["book"], event["action"]) for event in report["events"]] == [
        ("long", "open"), ("short", "open"), ("short", "reduce"),
    ]
    assert set(report["opened_at"]) == {"long", "short"}
    inputs.factory.assert_not_called()


def test_flat_position_reports_full_close_without_current_open_time(inputs):
    inputs.fills = [fill(1, 2), fill(2, -2)]
    inputs.orders = [order(1), order(2)]

    report = evidence.read_position_evidence(inventory(), "BTCUSDT")

    assert report["status"] == "reconciled"
    assert report["events"][-1]["action"] == "close"
    assert report["events"][-1]["signed_after"] == "0"
    assert report["opened_at"] == {}


def test_missing_order_time_does_not_relabel_fill_time(inputs):
    inputs.fills = [fill(1, 2)]
    inputs.orders = [{"exchange_order_id": "101", "source": "qd_quick_trades",
                      "payload": json.dumps({"created_at": _BASE, "updateTime": _BASE})}]

    report = evidence.read_position_evidence(inventory(("long", 2)), "BTCUSDT")

    assert report["status"] == "partial"
    assert report["issues"] == ["order_submission_time_unavailable"]
    assert report["events"][0]["filled_at"]
    assert report["events"][0]["submitted_at"] is None
    inputs.exchange.get_user_trades.assert_not_called()
    inputs.exchange.get_order.assert_called_once_with(symbol="BTCUSDT", order_id="101")


@pytest.mark.parametrize(("trades", "issue"), [
    ([remote(1, 2)], "position_quantity_mismatch"),
    ([remote(1, 3)], "conflicting_fill"),
    ([remote(10, 1)] * 1000, "exchange_history_limit"),
])
def test_incomplete_history_does_not_invent_an_open_time(inputs, trades, issue):
    inputs.fills = [fill(1, 2)]
    inputs.exchange.get_user_trades.return_value = trades

    report = evidence.read_position_evidence(inventory(("long", 3)), "BTCUSDT")

    assert report["status"] == "unverified"
    assert report["issues"] == [issue]
    assert report["opened_at"] == {}
    assert report["events"] == []
    inputs.exchange.get_order.assert_not_called()


def test_incomplete_inventory_stops_before_reading_history(inputs):
    snapshot = inventory(("long", 2))
    snapshot["status"] = "partial"

    report = evidence.read_position_evidence(snapshot, "BTCUSDT")

    assert report["issues"] == ["inventory_incomplete"]
    inputs.cursor.execute.assert_not_called()
    inputs.factory.assert_not_called()
