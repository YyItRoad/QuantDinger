"""Whole-position adoption through real Gateway auth and shared management."""
from contextlib import contextmanager
from unittest.mock import Mock
import asyncio
import importlib
import json
import sys

import pytest
from flask import Flask
from app.routes.agent_v1 import register
from app.routes.agent_v1 import position_management as routes
from app.services.live_trading import position_management as management
from app.utils import agent_auth

BASE = "/api/agent/v1/trading/accounts/2"
HEADERS = {"Authorization": "Bearer qd_agent_adoption_test", "Idempotency-Key": "adopt-1"}
BODY = dict(symbol="BTC/USDT", side="long", market_type="swap", source_id=9,
            name="Manage BTC", initial_capital=100, confirm_adoption=True,
            params={"fixed_stop_price": 90}, entry_reason="Existing whole holding")


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    register(app)
    return app.test_client()


@pytest.fixture
def boundary(monkeypatch):
    token = dict(id=71, user_id=7, scopes="R,W", status="active", paper_only=True,
                 markets="*", instruments="*", rate_limit_per_min=60)
    audit = Mock()
    for name, value in {
        "_ensure_schema": lambda: None, "_lookup_token": lambda _: token,
        "_audit": audit, "_touch_token_last_used": lambda *a: None,
        "_check_rate_limit": lambda *a: dict(allowed=True, limit=60, remaining=59, reset=123),
    }.items():
        monkeypatch.setattr(agent_auth, name, value)
    completed = {}

    def reserve(token_id, key):
        identity = (token_id, key)
        fingerprint = agent_auth._request_fingerprint()
        if identity in completed:
            old = completed[identity]
            return ("completed", old) if old["fingerprint"] == fingerprint else ("mismatch", None)
        return "reserved", None

    def complete(token_id, key, response):
        completed[token_id, key] = dict(fingerprint=agent_auth._request_fingerprint(),
            response_body=response.get_json(), response_status=response.status_code)

    monkeypatch.setattr(agent_auth, "_reserve_idempotency", reserve)
    monkeypatch.setattr(agent_auth, "_complete_idempotency", complete)
    credential = Mock(side_effect=lambda cid: {"id": 2} if cid == 2 else None)
    monkeypatch.setattr(routes, "_credential", credential)
    listing = Mock(return_value=[])
    monkeypatch.setattr(management, "list_managed_positions_for_account", listing)
    snapshot = Mock(return_value={"swap_positions": [dict(symbol="BTC/USDT", side="long",
        size=7, entry_price=99, mark_price=100, leverage=3, market_type="swap", inst_id="BTCUSDT")]})
    monkeypatch.setattr(management, "fetch_target_position_snapshot", snapshot)

    @contextmanager
    def lock(**kwargs):
        yield

    monkeypatch.setattr(management, "_management_leg_lock", lock)
    service = Mock()
    service.create_strategy.return_value = 44
    service.get_strategy.return_value = {"strategy_name": "Manage BTC", "timeframe": "4h"}
    service.update_strategy_status.return_value = True
    monkeypatch.setattr(management, "get_strategy_service", lambda: service)
    upsert, enqueue = Mock(), Mock()
    monkeypatch.setattr(management, "upsert_position", upsert)
    monkeypatch.setattr(management, "StrategyCommandRepository", lambda: Mock(enqueue=enqueue))
    monkeypatch.setattr(management, "append_strategy_log", Mock())
    return dict(token=token, audit=audit, service=service, upsert=upsert, enqueue=enqueue,
                listing=listing, snapshot=snapshot, credential=credential)


def adopt(client, body=None, headers=None, base=BASE):
    return client.post(base + "/managed-strategies", json=BODY if body is None else body,
                       headers=HEADERS if headers is None else headers)


def test_w_adopts_real_whole_leg_without_t_or_quick_order_flags(client, boundary, monkeypatch):
    boundary["token"]["scopes"] = "W"
    monkeypatch.delenv("AGENT_LIVE_TRADING_ENABLED", raising=False)
    body = {**BODY, "timeframe": "4h", "direction_mode": "long_only", "inst_id": "BTCUSDT",
            "account_risk": {"max_position_pct": 0.5}, "notification_channels": ["webhook"],
            "notification_targets": {"webhook": "saved-target"}}
    response = adopt(client, body)
    assert response.status_code == 201 and response.json["code"] == 0
    assert response.json["data"]["status"] == "starting"
    assert response.json["data"]["size"] == "7"
    payload = boundary["service"].create_strategy.call_args.args[0]
    assert payload["user_id"] == 7 and payload["credentialId"] == 2
    assert payload["executionMode"] == "live" and payload["leverage"] == 3
    assert payload["sourceId"] == 9 and payload["directionMode"] == "long_only"
    assert payload["positionManagement"]["timeframe"] == "4h"
    assert payload["positionManagement"]["entry_reason"] == BODY["entry_reason"]
    assert payload["params"]["fixed_stop_price"] == 90
    assert payload["accountRisk"] == body["account_risk"]
    assert payload["notificationChannels"] == ["webhook"]
    assert payload["notificationTargets"] == body["notification_targets"]
    assert boundary["upsert"].call_args.kwargs["size"] == 7
    assert boundary["enqueue"].call_args.kwargs["command_type"] == "start"
    boundary["audit"].assert_called_once()


@pytest.mark.parametrize("scope", ["R", "T", "N", "B"])
def test_w_required_for_adoption(client, boundary, scope):
    boundary["token"]["scopes"] = scope
    assert adopt(client).status_code == 403
    boundary["snapshot"].assert_not_called()


def test_confirmation_auth_and_idempotency_required(client, boundary):
    assert adopt(client, headers={}).status_code == 401
    assert adopt(client, headers={"Authorization": HEADERS["Authorization"]}).status_code == 400
    assert adopt(client, {**BODY, "confirm_adoption": False}).status_code == 400
    boundary["snapshot"].assert_not_called()


def test_replay_returns_same_strategy_and_changed_request_conflicts(client, boundary):
    first = adopt(client)
    second = adopt(client)
    assert first.json == second.json and second.headers["Idempotent-Replayed"] == "true"
    assert adopt(client, {**BODY, "name": "different"}).status_code == 409
    boundary["service"].create_strategy.assert_called_once()
    boundary["enqueue"].assert_called_once()


def test_account_ownership_and_safe_read_projection(client, boundary):
    assert adopt(client, base=BASE.replace("/2", "/99")).status_code == 404
    boundary["snapshot"].assert_not_called()
    boundary["listing"].return_value = [{"symbol": "BTC/USDT", "strategy_id": 44}]
    r = client.get(BASE + "/managed-positions", headers=HEADERS)
    assert r.status_code == 200 and r.json["data"]["items"][0]["strategy_id"] == 44
    boundary["listing"].assert_called_once_with(user_id=7, credential_id=2)
    assert client.get(BASE.replace("/2", "/99") + "/managed-positions", headers=HEADERS).status_code == 404
    boundary["token"]["scopes"] = "W"
    assert client.get(BASE + "/managed-positions", headers=HEADERS).status_code == 403


@pytest.mark.parametrize("patch", [
    {"user_id": 99}, {"credential_id": 99}, {"qty": 1}, {"execution_mode": "signal"},
    {"source_id": True}, {"source_id": 0}, {"initial_capital": -1}, {"initial_capital": True},
    {"initial_capital": float("inf")}, {"params": {"fixed_stop_price": float("nan")}},
    {"params": []}, {"side": "buy"}, {"market_type": "future"}, {"name": " "},
    {"entry_reason": "x" * 501}, {"notification_channels": "webhook"},
    {"account_risk": []}, {"confirm_adoption": "true"},
])
def test_invalid_transport_fields_fail_before_exchange(client, boundary, patch):
    assert adopt(client, {**BODY, **patch}).status_code == 400
    boundary["snapshot"].assert_not_called()


def test_allowlists_apply_to_actual_instrument_not_only_client_symbol(client, boundary):
    boundary["token"]["instruments"] = "BTC/USDT"
    boundary["snapshot"].return_value["swap_positions"][0]["symbol"] = "ETH/USDT"
    assert adopt(client, {**BODY, "inst_id": "BTCUSDT"}).status_code == 403
    boundary["service"].create_strategy.assert_not_called()


@pytest.mark.parametrize("leg_patch, inst_id", [
    ({"symbol": "ETH/USDT", "inst_id": "ETHUSDT"}, "ETHUSDT"),
    ({}, "NONEXISTENT"),
    ({"market_type": "spot"}, "BTCUSDT"),
])
def test_conflicting_target_rejected_even_with_wildcard_permissions(client, boundary, leg_patch, inst_id):
    boundary["snapshot"].return_value["swap_positions"][0].update(leg_patch)
    response = adopt(client, {**BODY, "inst_id": inst_id})
    assert response.status_code == 400 and "does not match" in response.json["message"]
    boundary["service"].create_strategy.assert_not_called()
    boundary["upsert"].assert_not_called()
    boundary["enqueue"].assert_not_called()


def test_canonical_symbol_and_instrument_can_refer_to_same_whole_leg(client, boundary):
    response = adopt(client, {**BODY, "symbol": "btcusdt", "inst_id": " BTCUSDT "})
    assert response.status_code == 201 and response.json["data"]["symbol"] == "BTC/USDT"


def test_pre_creation_outage_replays_error_but_allows_safe_new_key(client, boundary):
    boundary["snapshot"].side_effect = RuntimeError("temporary exchange outage")
    first = adopt(client)
    assert first.status_code == 500 and first.json["retriable"] is True
    assert first.json["details"] == {"creation_attempted": False, "retry_safe": True,
        "retry_action": "refresh_positions_then_retry_with_new_key"}
    boundary["snapshot"].side_effect = None
    replay = adopt(client)
    assert replay.status_code == 500 and replay.json == first.json
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert adopt(client, {**BODY, "name": "changed"}).status_code == 409
    assert boundary["snapshot"].call_count == 1
    boundary["service"].create_strategy.assert_not_called()
    recovered = adopt(client, headers={**HEADERS, "Idempotency-Key": "safe-retry"})
    assert recovered.status_code == 201 and recovered.json["data"]["status"] == "starting"
    boundary["service"].create_strategy.assert_called_once()
    boundary["enqueue"].assert_called_once()


def test_lease_busy_before_creation_can_retry_without_duplicate(client, boundary, monkeypatch):
    available = management._management_leg_lock

    @contextmanager
    def busy(**kwargs):
        raise management.PositionManagementError("busy", status_code=409)
        yield

    monkeypatch.setattr(management, "_management_leg_lock", busy)
    first = adopt(client)
    assert first.status_code == 409 and first.json["details"]["retry_safe"] is True
    boundary["service"].create_strategy.assert_not_called()
    monkeypatch.setattr(management, "_management_leg_lock", available)
    assert adopt(client, headers={**HEADERS, "Idempotency-Key": "lease-retry"}).status_code == 201
    boundary["service"].create_strategy.assert_called_once()


def test_unknown_failure_after_create_attempt_never_advertises_safe_retry(client, boundary):
    def possibly_committed(payload):
        boundary["listing"].return_value = [dict(symbol="BTC/USDT", side="long", market_type="swap")]
        raise RuntimeError("connection lost after strategy commit")

    boundary["service"].create_strategy.side_effect = possibly_committed
    first = adopt(client)
    assert first.status_code == 500 and first.json["retriable"] is False
    assert first.json["details"] == {"creation_attempted": True, "retry_safe": False,
                                      "retry_action": "inspect_existing_strategy"}
    assert adopt(client).json == first.json
    # Even an accidental fresh request cannot duplicate an assigned leg.
    assert adopt(client, headers={**HEADERS, "Idempotency-Key": "unsafe-retry"}).status_code == 409
    boundary["service"].create_strategy.assert_called_once()
    boundary["upsert"].assert_not_called()
    boundary["enqueue"].assert_not_called()


def test_start_failure_replay_does_not_create_or_restart_another_instance(client, boundary):
    boundary["service"].update_strategy_status.return_value = False
    first = adopt(client)
    assert first.status_code == 201 and first.json["data"]["status"] == "stopped"
    assert first.json["data"]["warning"]
    boundary["service"].update_strategy_status.return_value = True
    assert adopt(client).json == first.json
    boundary["listing"].return_value = [dict(symbol="BTC/USDT", side="long", market_type="swap")]
    assert adopt(client, headers={**HEADERS, "Idempotency-Key": "start-retry"}).status_code == 409
    boundary["service"].create_strategy.assert_called_once()
    boundary["service"].update_strategy_status.assert_called_once()
    boundary["enqueue"].assert_not_called()


def test_other_write_endpoint_keeps_original_failure_replay(client, boundary, monkeypatch):
    from app.routes.agent_v1 import strategies
    create = Mock(side_effect=RuntimeError("temporary workspace failure"))
    monkeypatch.setattr(strategies._strategy_service, "create_strategy", create)
    headers = {**HEADERS, "Idempotency-Key": "other-write"}
    body = {"name": "Stopped configuration"}
    first = client.post("/api/agent/v1/strategies", json=body, headers=headers)
    assert first.status_code == 500
    create.side_effect = None
    create.return_value = 44
    replay = client.post("/api/agent/v1/strategies", json=body, headers=headers)
    assert replay.status_code == 500 and replay.json == first.json
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert "retry_safe" not in replay.text
    create.assert_called_once()


def test_allowlists_filter_reads_and_reject_adoption(client, boundary):
    boundary["token"]["instruments"] = "ETH/USDT"
    boundary["listing"].return_value = [{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}]
    assert client.get(BASE + "/managed-positions", headers=HEADERS).json["data"]["items"] == [{"symbol": "ETH/USDT"}]
    assert adopt(client).status_code == 403
    boundary["snapshot"].assert_not_called()


def test_spot_whole_position_supported(client, boundary):
    leg = boundary["snapshot"].return_value.pop("swap_positions")[0]
    leg.update(market_type="spot", leverage=1)
    boundary["snapshot"].return_value["spot_positions"] = [leg]
    response = adopt(client, {**BODY, "market_type": "spot"})
    assert response.status_code == 201 and response.json["data"]["size"] == "7"
    assert boundary["upsert"].call_args.kwargs["market_type"] == "spot"


def test_remaining_entry_orders_do_not_change_existing_takeover_flow(client, boundary):
    boundary["snapshot"].return_value["open_orders"] = [{"symbol": "BTC/USDT", "remaining": 3}]
    assert adopt(client).status_code == 201
    assert boundary["upsert"].call_args.kwargs["size"] == 7


def test_source_validation_remains_in_shared_strategy_service(client, boundary):
    boundary["service"].create_strategy.side_effect = ValueError("strategyV2.sourceNotFound")
    r = adopt(client)
    assert r.status_code == 400 and r.json["message"] == "strategyV2.sourceNotFound"
    boundary["upsert"].assert_not_called()
    boundary["enqueue"].assert_not_called()


def test_existing_assignment_rejected_and_start_failure_preserved(client, boundary):
    boundary["listing"].return_value = [dict(symbol="BTC/USDT", side="long", market_type="swap")]
    assert adopt(client).status_code == 409
    boundary["service"].create_strategy.assert_not_called()
    boundary["listing"].return_value = []
    boundary["service"].update_strategy_status.return_value = False
    r = adopt(client, headers={**HEADERS, "Idempotency-Key": "adopt-2"})
    assert r.status_code == 201 and r.json["data"]["strategy_id"] == 44
    assert r.json["data"]["status"] == "stopped" and r.json["data"]["warning"]
    boundary["enqueue"].assert_not_called()


def test_no_position_or_invalid_stop_does_not_create(client, boundary):
    assert adopt(client, {**BODY, "params": {"fixed_stop_price": 110}}).status_code == 400
    boundary["snapshot"].return_value = {"swap_positions": []}
    assert adopt(client, headers={**HEADERS, "Idempotency-Key": "adopt-2"}).status_code == 409
    boundary["service"].create_strategy.assert_not_called()


def test_internal_errors_do_not_leak(client, boundary):
    boundary["credential"].side_effect = RuntimeError("private-database-password")
    for method, path in (("get", "/managed-positions"), ("post", "/managed-strategies")):
        r = getattr(client, method)(BASE + path, headers=HEADERS, **({"json": BODY} if method == "post" else {}))
        assert r.status_code == 500 and "private-database-password" not in r.text


def test_mcp_to_gateway_keeps_whole_workflow_and_http_errors(client, boundary, monkeypatch):
    pytest.importorskip("mcp")
    import httpx
    monkeypatch.setenv("QUANTDINGER_BASE_URL", "http://fixture.invalid")
    monkeypatch.setenv("QUANTDINGER_AGENT_TOKEN", "qd_agent_adoption_test")
    monkeypatch.setenv("QUANTDINGER_MCP_AUTH_MODE", "static")
    sys.modules.pop("quantdinger_mcp.server", None)
    server = importlib.import_module("quantdinger_mcp.server")

    def respond(req):
        result = client.open(str(req.url).split("fixture.invalid", 1)[1], method=req.method,
                             headers=dict(req.headers), data=req.content)
        return httpx.Response(result.status_code, json=result.json)

    with httpx.Client(base_url="http://fixture.invalid", headers=HEADERS,
                      transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(server, "_client", transport)
        args = {**BODY, "credential_id": 2, "idempotency_key": "mcp-adopt"}
        boundary["snapshot"].side_effect = RuntimeError("temporary snapshot failure")
        failed = asyncio.run(server.mcp.call_tool("adopt_position_management", args))
        assert failed.isError
        failure_body = json.loads(failed.content[0].text)["body"]
        assert failure_body["details"]["retry_safe"] is True
        assert failure_body["details"]["retry_action"] == "refresh_positions_then_retry_with_new_key"
        boundary["snapshot"].side_effect = None
        replay = asyncio.run(server.mcp.call_tool("adopt_position_management", args))
        assert replay.isError and boundary["snapshot"].call_count == 1
        args["idempotency_key"] = "mcp-safe-retry"
        result = asyncio.run(server.mcp.call_tool("adopt_position_management", args))
        assert json.loads(result[0].text)["size"] == "7"
        boundary["token"]["scopes"] = "R"
        result = asyncio.run(server.mcp.call_tool("list_managed_positions", {"credential_id": 2}))
        assert json.loads(result[0].text) == {"items": []}
        args["idempotency_key"] = "mcp-denied"
        result = asyncio.run(server.mcp.call_tool("adopt_position_management", args))
        assert result.isError and "W" in result.content[0].text
    boundary["service"].create_strategy.assert_called_once()
