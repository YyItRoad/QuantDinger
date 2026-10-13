# MCP notifications

`send_notification` sends through the user's saved, enabled Webhook. It reuses
existing provider adaptation, signing, timeout and retry behavior. No new
channel, notification table or schedule is introduced. Vendor text includes
`[LEVEL] title` and the message; generic Webhooks retain the structured fields.

## Configuration and invocation

Use existing Agent scope `N` (`R,N` for market reads as well). N also permits
existing signal-alert lifecycle operations. Hybrid OAuth uses
`quantdinger.write`, like other notification write tools, while retaining the
HTTP `quantdinger.read` requirement. The dedicated OAuth Agent token needs
`R,N`; existing credential separation remains required.

Save and enable `webhook` in the user's notification settings. No dedicated
notification migration is required. Existing Agent tables remain managed by
the normal bootstrap mechanism.

```json
{"title":"Market observation","message":"Review the observed condition.","level":"warning","idempotency_key":"observation-2026-10-13-001"}
```

For MCP, `idempotency_key` is required (1–120 characters). It is forwarded as
HTTP `Idempotency-Key`. Direct HTTP callers POST to
`/api/agent/v1/notifications/send` with that header and a JSON body containing
only title (1–200), message (1–8000), and optional level
(info/warning/critical). Blank text and caller-selected destinations or
credentials are rejected.

## Results and reliability

`delivered` means Webhook acceptance, not a human read receipt. Failed sends
return `failed`. Existing Agent idempotency replays the original response,
including failures, per token/method/path/key; there is no separate duplicate
status. Changed bodies or in-progress requests return 409. Rate limits reuse
existing Agent configuration. There is no separate per-user ten-send limit or
cross-token 24-hour deduplication.

Timeouts and 429/5xx retries use the original sender. No additional retry queue
is added. Reuse the original key after uncertain delivery. The existing Agent
mechanism may reclaim stale in-progress reservations, so exactly-once external
delivery is not guaranteed. Notification bodies and secrets are omitted from
Agent audit summaries; diagnostics remain safe.

## Verification and deployment

Feature code lives in `app/services/agent_notifications.py`, the standalone
`app/routes/agent_v1/notification_send.py` route, and
`mcp_server/src/quantdinger_mcp/notifications.py`. Shared entrypoints only
register the feature. Vendor text reuses `display.plain` instead of adding a
custom event branch to the shared formatter. Necessary audit/log redaction
is retained; authentication, limits, idempotency and delivery are not copied.

Run `python -m pytest tests/test_agent_notifications.py -q` from the backend,
and `python -m pytest mcp_server/tests -q` from the repository root. Pushes are
mocked. Live OAuth source parity, interactive ChatGPT access and unattended
scheduled-task invocation still require operator validation. Existing
NEAR/USDT monitoring is unchanged; no production deployment or real send is
part of this change.
