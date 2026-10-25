"""Recording-only plan tools; reuse the MCP server transport and auth wrapper."""
from typing import Any


def register_trade_plan_tools(server):
    @server._tool
    def list_trade_plans(page: int = 1, page_size: int = 20, q: str = "", lifecycle: str = "", direction: str = "", primary_strategy: str = "", retrospective: str = "", source_kind: str = "", source_id: int = 0) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        params = {"page": page, "page_size": page_size, "q": q, "lifecycle": lifecycle, "direction": direction, "primary_strategy": primary_strategy, "retrospective": retrospective}
        if source_kind:
            params["source_kind"] = source_kind
        if source_id:
            params["source_id"] = source_id
        return server._get("/api/agent/v1/trade-plans", params=params)

    @server._tool
    def get_trade_plan(plan_id: str) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        return server._get(f"/api/agent/v1/trade-plans/{plan_id}")

    @server._tool
    def list_trade_plan_versions(plan_id: str) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        return server._get(f"/api/agent/v1/trade-plans/{plan_id}/versions")

    @server._tool
    def get_trade_plan_version(plan_id: str, version_id: str) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        return server._get(f"/api/agent/v1/trade-plans/{plan_id}/versions/{version_id}")

    @server._tool
    def list_trade_plan_events(plan_id: str) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        return server._get(f"/api/agent/v1/trade-plans/{plan_id}/events")

    @server._tool
    def get_trade_plan_request(request_id: str) -> Any:
        """Read recording-only trade plan data; never starts or changes trading."""
        return server._get(f"/api/agent/v1/trade-plans/requests/{request_id}")

    @server._tool
    def create_trade_plan(snapshot: dict, idempotency_key: str) -> Any:
        """Save research records only. No order, strategy start, takeover, or stop changes."""
        payload = {"snapshot": snapshot}
        payload["request_id"] = idempotency_key
        return server._post("/api/agent/v1/trade-plans", json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def revise_trade_plan(plan_id: str, snapshot: dict, change_reason: str, expected_revision: int, idempotency_key: str) -> Any:
        """Save research records only. No order, strategy start, takeover, or stop changes."""
        payload = {"snapshot": snapshot, "change_reason": change_reason, "expected_revision": expected_revision}
        payload["request_id"] = idempotency_key
        return server._post(f"/api/agent/v1/trade-plans/{plan_id}/versions", json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def set_trade_plan_state(plan_id: str, lifecycle: str, reason: str, expected_revision: int, idempotency_key: str) -> Any:
        """Save research records only. No order, strategy start, takeover, or stop changes."""
        payload = {"lifecycle": lifecycle, "reason": reason, "expected_revision": expected_revision}
        payload["request_id"] = idempotency_key
        return server._post(f"/api/agent/v1/trade-plans/{plan_id}/state", json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def copy_trade_plan(plan_id: str, expected_revision: int, idempotency_key: str) -> Any:
        """Save research records only. No order, strategy start, takeover, or stop changes."""
        payload = {"expected_revision": expected_revision}
        payload["request_id"] = idempotency_key
        return server._post(f"/api/agent/v1/trade-plans/{plan_id}/copy", json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def record_trade_plan_decision(plan_id: str, status: str, reason: str, observed_at: str, expected_revision: int, idempotency_key: str, assessment_author: str = "human") -> Any:
        """Save research records only. No order, strategy start, takeover, or stop changes."""
        payload = {"status": status, "reason": reason, "observed_at": observed_at, "expected_revision": expected_revision, "assessment_author": assessment_author}
        payload["request_id"] = idempotency_key
        return server._post(f"/api/agent/v1/trade-plans/{plan_id}/events", json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def list_trade_plan_accounts() -> Any:
        """List secret-free owned accounts, including archived account identities; no exchange calls."""
        return server._get('/api/agent/v1/trade-plans/accounts')

    @server._tool
    def list_trade_plan_sources(plan_id: str, credential_id: int, source_kind: str = 'history', version_id: str = '', page: int = 1, page_size: int = 20, source_id: int = 0) -> Any:
        """Preview existing owned execution facts. Order/strategy references are not proof of fills."""
        params = {'credential_id': credential_id, 'source_kind': source_kind, 'page': page, 'page_size': page_size}
        if source_id:
            params['source_id'] = source_id
        if version_id:
            params['version_id'] = version_id
        return server._get(f'/api/agent/v1/trade-plans/{plan_id}/sources', params=params)

    @server._tool
    def create_trade_plan_attempt(plan_id: str, version_id: str, credential_id: int, execution_mode: str, expected_revision: int, idempotency_key: str, note: str = '') -> Any:
        """Freeze one adopted version and reference risk; never executes a trade. Late evidence remains unverifiable."""
        return server._post(f'/api/agent/v1/trade-plans/{plan_id}/attempts', json={'request_id': idempotency_key, 'expected_revision': expected_revision, 'version_id': version_id, 'credential_id': credential_id, 'execution_mode': execution_mode, 'note': note}, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def link_trade_plan_record(plan_id: str, attempt_id: str, source_kind: str, source_id: int, attribution: str, reason: str, expected_revision: int, idempotency_key: str) -> Any:
        """Link a server-read fact after preview. Use pending for ambiguous ownership. No order or takeover."""
        return server._post(f'/api/agent/v1/trade-plans/{plan_id}/links', json={'request_id': idempotency_key, 'expected_revision': expected_revision, 'attempt_id': attempt_id, 'source_kind': source_kind, 'source_id': source_id, 'attribution': attribution, 'reason': reason}, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def revoke_trade_plan_link(plan_id: str, attempt_id: str, link_id: str, reason: str, expected_revision: int, idempotency_key: str) -> Any:
        """Correct a mistaken link, retaining its snapshot and audit history. Does not change execution."""
        return server._post(f'/api/agent/v1/trade-plans/{plan_id}/links/revoke', json={'request_id': idempotency_key, 'expected_revision': expected_revision, 'attempt_id': attempt_id, 'link_id': link_id, 'reason': reason}, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def refresh_trade_plan_link(plan_id: str, attempt_id: str, link_id: str, reason: str, expected_revision: int, idempotency_key: str, attribution: str = '') -> Any:
        """Append a fresh server fact snapshot. Missing sources retain prior evidence and become unavailable."""
        payload = {'request_id': idempotency_key, 'expected_revision': expected_revision, 'attempt_id': attempt_id, 'link_id': link_id, 'reason': reason}
        if attribution:
            payload['attribution'] = attribution
        return server._post(f'/api/agent/v1/trade-plans/{plan_id}/links/refresh', json=payload, headers=server._idempotency_headers(idempotency_key))

    @server._tool
    def save_trade_plan_review(plan_id: str, attempt_id: str, assessment: dict, change_reason: str, expected_revision: int, idempotency_key: str) -> Any:
        """Append market_judgment/strategy_rules/execution evaluations plus improvement; values require reasons. Facts and returns are server-computed; missing evidence stays not evaluable."""
        return server._post(f'/api/agent/v1/trade-plans/{plan_id}/reviews', json={'request_id': idempotency_key, 'expected_revision': expected_revision, 'attempt_id': attempt_id, 'assessment': assessment, 'change_reason': change_reason}, headers=server._idempotency_headers(idempotency_key))

    return {'list_trade_plans': list_trade_plans, 'get_trade_plan': get_trade_plan, 'list_trade_plan_versions': list_trade_plan_versions, 'get_trade_plan_version': get_trade_plan_version, 'list_trade_plan_events': list_trade_plan_events, 'get_trade_plan_request': get_trade_plan_request, 'create_trade_plan': create_trade_plan, 'revise_trade_plan': revise_trade_plan, 'set_trade_plan_state': set_trade_plan_state, 'copy_trade_plan': copy_trade_plan, 'record_trade_plan_decision': record_trade_plan_decision, 'list_trade_plan_accounts': list_trade_plan_accounts, 'list_trade_plan_sources': list_trade_plan_sources, 'create_trade_plan_attempt': create_trade_plan_attempt, 'link_trade_plan_record': link_trade_plan_record, 'revoke_trade_plan_link': revoke_trade_plan_link, 'refresh_trade_plan_link': refresh_trade_plan_link, 'save_trade_plan_review': save_trade_plan_review}
