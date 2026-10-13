# Codex task: Add general-purpose notification MCP tool

## Objective

Extend the existing QuantDinger MCP with a restricted, general-purpose `send_notification` tool. ChatGPT's scheduled analysis reads market data through QuantDinger MCP and may call this tool to forward an important alert through QuantDinger's **existing configured Webhook/notification channel**. No n8n, Bark integration, automated trading, or new AI service.

## Inspect before coding

1. Locate the **actual MCP server implementation** for `https://trade.yangyang.fun/mcp` in this repository. If this repo lacks it, identify the authoritative repository/path and explain the blocker; do not pretend the work is complete.
2. Identify existing notification sender(s), configured Webhook delivery, provider settings, and auth/OAuth permissions. Reuse these rather than writing a redundant notification stack.
3. The deployed server had manual container changes for OAuth hybrid token validation (`/app/src/quantdinger_mcp/oauth_auth.py` was discussed). Confirm repository parity before altering authorization; don't overwrite production authentication behavior.

## Proposed API

MCP tool: `send_notification`
- `title`: required nonempty string
- `message`: required nonempty string
- `level`: optional enum `info | warning | critical`, default `info`
- `idempotency_key`: optional string
- Never accept arbitrary Webhook URL, destination, token, or headers from the caller.
- Server-side destination/channel selection only.
- Structured outcome `delivered | duplicate | failed` with safe diagnostic text.

## Security and reliability

- Apply **least privilege**: a restricted permission such as `quantdinger.notify`, separate from read (`R`) and from write/trade. Research the existing `N` scope and avoid accidentally exposing alert creation/deletion to users who only need sending.
- Validate title/message sizes and level; rate-limit per authorized user; idempotency by user and key with TTL.
- Do not log bearer tokens, Webhook secrets, or full sensitive messages.
- Handle missing notification configuration, timeouts, upstream errors safely.
- Keep existing read-only market tools working.

## Tests and docs

Unit/integration coverage for successful delivery, duplicate key, malformed input, unauthorized requests (403), rate limits, absent configuration, and Webhook failure; mock real network push. Document expected authorization configuration, local testing steps, and example call. No real notifications sent without user's permission.

## Delivery

- Commit on this isolated task branch or a separate implementation branch and **open a PR** against `main`.
- PR must include changed files, tests run/results, migration/config requirements and any blockers.
- **No merge, no production deployment, no live trades, and no changes to existing schedules.**
- If the MCP server is outside this repository, stop with precise findings and recommended next steps rather than implementing an unrelated tool.

## Acceptance / future integration

1. Interactive ChatGPT can discover and (with restricted credentials) call `send_notification`.
2. Later, test **whether ChatGPT scheduled tasks can call connected MCP tools unattended**; do not assume this works.
3. Only then update the existing hourly NEAR/USDT short setup monitor to call `send_notification` on distinct `ENTRY_READY` or `INVALIDATED` events, retaining duplicate suppression.

## Trading monitoring context (do not implement here)

NEAR/USDT false-upside-breakout reversal, monitor 1D/4H/1H, wait for 4H rebound failure + 1H bearish structure and risk/reward assessment. All trading decisions remain human-approved.
