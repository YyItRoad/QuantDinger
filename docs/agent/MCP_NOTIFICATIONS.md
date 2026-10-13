# Send-only MCP notifications

`send_notification` forwards text through the user's saved, enabled Webhook
notification channel. It reuses `SignalNotifier` provider adaptation, signing,
timeouts and retry behavior. No new provider, AI service, schedule or trading
operation is introduced. Other profile channels are not selected by this tool.

## Authorization and configuration

- Save a Webhook URL in the user's existing notification settings and include
  `webhook` in `default_channels`. Saved bearer/signing secrets are reused.
- Issue an Agent Gateway token with `NS` (notification send only). Add `R` when
  market reads are needed. `N` remains alert lifecycle management and does NOT
  imply `NS`; `NS` does not grant alert creation, deletion, write or trading.
- For hybrid OAuth, grant `quantdinger.notify` in addition to the existing
  HTTP transport requirement `quantdinger.read`. The dedicated
  `QUANTDINGER_MCP_OAUTH_AGENT_TOKEN` must have `R,NS`. Keep it distinct from
  the primary Agent token and inbound static token. A static MCP credential
  can invoke the tool only if its backing Agent token has `NS`.
- Apply `python -m app.commands.migrate` with `QD_PROCESS_ROLE=migration` in
  `backend_api_python` before starting the updated backend. This registers
  `20261013_agent_notifications.sql`; no existing credentials gain permissions.

```json
{
  "title": "Market observation",
  "message": "A previously defined condition was observed. Review the evidence.",
  "level": "warning",
  "idempotency_key": "observation-2026-10-13-001"
}
```

Call that payload with MCP `send_notification`, or POST it to
`/api/agent/v1/notifications/send` using an NS Agent token. This is an example,
not an instruction to send a live notification. Title is 1–200 characters,
message 1–8000, level info/warning/critical, key 1–120 if supplied. Blank strings
and extra destination/token/header fields are rejected by the Gateway.

Responses contain `status` (`delivered`, `duplicate`, `failed`) and a safe
`diagnostic`. HTTP denials include 403 for scope, 429 for rate limits, 409 for
missing configuration or key conflict, and 502/503 for delivery/storage errors.
MCP surfaces HTTP failures as protocol tool errors. Provider response text,
Webhook URLs, credentials and message bodies are not included in diagnostics
or Agent audit summaries.

## Reliability

The database serializes reservations per user across processes and tokens.
There are at most 10 new delivery attempts per rolling minute per user, in
addition to existing Agent token/tenant limits. Reusing a key with a different
payload returns 409. Identical keys reserve one send for 24 hours. Records store
hashes and state, not message bodies. Expired records for a user are removed on
the next attempt. Omit the key only when every call should be a new notification.

`duplicate` means already reserved, including pending, failed and ambiguous
attempts; it does not assert prior delivery. Timeouts and process failures do
not release reservations. Do not retry uncertain delivery with a new key.
The shared sender may retry HTTP 429/5xx once; Webhook systems cannot guarantee
exactly-once delivery. `delivered` means accepted by the Webhook, not read by a
human. API timeout is 10 seconds per attempt.

## Local validation and deployment parity

From the repository root, install the backend development requirements and
`pip install -e './mcp_server[dev]'`. Run:

```bash
python -m pytest mcp_server/tests -q
cd backend_api_python
python -m pytest tests/test_agent_notifications.py -q
# Set TEST_NOTIFICATION_DATABASE_URL to a disposable local PostgreSQL database.
python -m pytest tests/test_agent_notifications_postgres.py -q
```

The PostgreSQL tests create and drop an isolated schema. All Webhook pushes in
tests are mocked. No production endpoint or schedule needs to be changed.

The source is `mcp_server/src/quantdinger_mcp/server.py`; the Dockerfile copies
`src/` to `/app/src/`. `docker-compose.mcp.ghcr.yml` configures the personal
`trade.yangyang.fun` deployment. The starting OAuth source SHA-256 was
`2c53f1d0cd697b4abb18693ac7390de63dc21c0b7667685def5bb53356841f33`.
Live container parity has NOT been independently verified. The user requested
implementation against repository code first, with local verification later.
Before any deployment, compare the live OAuth source with the task branch and
review this PR's narrow scope/logging changes. JWT issuer/audience/signature,
subject validation, static/OAuth token routing and HTTP read-scope requirement
are preserved; debug prints of tokens and claims are removed.

Interactive ChatGPT discovery/authorized invocation must be tested by the
operator. Unattended scheduled-task MCP access is unverified. Existing hourly
NEAR/USDT monitoring schedules remain unchanged.
