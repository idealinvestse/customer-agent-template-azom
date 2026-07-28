# Messenger OpenClaw hybrid (daily driver)

Jonatan’s primary ops chat. Telegram remains the backup transport with the same `BotHandler` brain.

Related: [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) · [`SOUL.md`](../SOUL.md) · design [`superpowers/specs/2026-07-28-meta-messenger-ops-design.md`](superpowers/specs/2026-07-28-meta-messenger-ops-design.md)

## Layering

| Surface | Role |
|---------|------|
| **Messenger** | Daily driver — list/show/approve/next, order lookup, recovery |
| **Dashboard** | Full power — edit draft, order panel, poll, bulk, settings |
| **Telegram** | Backup chat |

Handoff: when Messenger is not enough, replies include **Öppna i dashboard** (`AZOM_DASHBOARD_PUBLIC_URL` + `/cases/{id}?from=messenger`).

## Environment

```bash
MESSENGER_PAGE_ACCESS_TOKEN=...
MESSENGER_APP_SECRET=...
MESSENGER_VERIFY_TOKEN=...
# Required when AZOM_USE_MOCK=0 (empty = fail-closed)
MESSENGER_ALLOWED_PSIDS=psid1,psid2
MESSENGER_ACTOR_MAP=<psid>:jonatan
# Optional explicit flag (also implied when AZOM_USE_MOCK=0):
# MESSENGER_FAIL_CLOSED=1
AZOM_DASHBOARD_PUBLIC_URL=https://ops.example.com
```

Webhook (no Basic auth — HMAC + verify token):

- `GET|POST https://<host>/webhooks/messenger`
- Subscribe: `messages`, `messaging_postbacks`
- Events are deduped by Messenger `mid`; the webhook always returns HTTP 200 after per-event handling so Meta does not retry entire batches.

## Local / mock

With `AZOM_USE_MOCK=1`, empty allowlist/actor-map still works (dev default → jonatan).

Without `MESSENGER_PAGE_ACCESS_TOKEN`:

- **Outbound Graph Send API** is dry-run (logs payload; operator sees a warning footer).
- **In prod (`AZOM_USE_MOCK=0`)**, mutating actions are **blocked** (approve / close / order write / product write). Read-only commands (`/cases list|show`, `/order`) still run.
- In mock mode, approve postbacks still execute (mail via mock) so unit tests can cover HITL.

## HITL

Same as Telegram: NL “godkänn” never sends; postback `/cases approve` or dashboard only.
