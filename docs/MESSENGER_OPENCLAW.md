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
MESSENGER_ALLOWED_PSIDS=...          # fail-closed when set
MESSENGER_ACTOR_MAP=<psid>:jonatan
AZOM_DASHBOARD_PUBLIC_URL=https://ops.example.com
```

Webhook (no Basic auth — HMAC + verify token):

- `GET|POST https://<host>/webhooks/messenger`
- Subscribe: `messages`, `messaging_postbacks`

## Local / mock

Without `MESSENGER_PAGE_ACCESS_TOKEN`, the webhook still processes events and **dry-runs** Send API (logs payload). Unit tests cover signature + parse + approve postback.

## HITL

Same as Telegram: NL “godkänn” never sends; postback `/cases approve` or dashboard only.
