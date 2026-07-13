# V1 Implementation Notes

## Scope (from plan)

> V1: Pilot med order-status, product-desc, support, SSH, mail.

## Package layout

```
skills/ecom_ops/           # Python package (import ecom_ops)
  actions/                 # order_status, product_desc, support, ssh_ops, mail
  cases/                   # Cases 2.0 + Path B suggest/auto_send rails (V2+)
  bot/                     # Telegram OpenClaw hybrid (V2)
  oauth/                   # Gmail OAuth (V2)
  integrations/            # WooCommerce + SSH + mail clients (+ mocks)
  security.py, rbac.py, escalation.py, telemetry.py, config.py, cli.py, llm.py
skills/ecom-ops/           # Moss skill metadata + shim
  SKILL.md
  integrations.py
tests/                     # pytest suite
bin/ecom-automation.sh     # operator entrypoint
infrastructure/dashboard/  # Flask dashboard (Jonatan + Oscar)
Dockerfile                 # container image
SOUL.md / docs/SYSTEM_OVERVIEW.md  # identity + full system map (V2 docs)
```

## Security model

1. Validate all external inputs (order id, status enum, email, site).
2. Never log secrets (redaction in telemetry + escalation).
3. SSH allowlist only for auto-exec; everything else → Oscar ticket.
4. Shell metacharacters (`;`, `|`, `&&`, …) rejected.
5. RBAC: Jonatan cannot mutate orders/products; case reply approve/send is an explicit CASE_REPLY exception. Bulk mail send still operator/Oscar.
6. Mail credentials only via env; support app password **and** OAuth2.

## Mail connector

| Layer | Module |
|-------|--------|
| Integration | `ecom_ops.integrations.mail` |
| Action | `ecom_ops.actions.mail` |
| CLI | `python -m ecom_ops mail {send,fetch,reply}` |
| Mock | `InMemoryMailTransport` / `AZOM_USE_MOCK=1` |

Providers: `gmail`, `outlook`, `exchange_graph`, `generic_imap`, `generic_pop3`.

Auth modes:

- **App password / password** – SMTP login + IMAP/POP3 login
- **OAuth2 XOAUTH2** – Gmail/Outlook SMTP+IMAP with access/refresh token
- **Graph client credentials** – `GRAPH_TENANT_ID` + `GRAPH_CLIENT_ID` + `GRAPH_CLIENT_SECRET`

## Escalation

`EscalationService` writes JSONL tickets assigned to **oscar** for:

- `critical`
- `code_edit`
- `ssh_unsafe`
- access denials on sensitive paths

## Telemetry

Local JSONL usage events for billing metering (`cost_usd`, tokens/api_calls/emails).

## Dashboard / bot

- Flask dashboard: Basic Auth, read-only routes (`/`, `/logs`, `/telemetry`, `/escalations`, `/health`)
- Telegram bot: `/help`, `/health`, `/brief` (requires `TELEGRAM_BOT_TOKEN`)

## Production target (Ubuntu 24 / Hetzner)

| Item | Value |
|------|--------|
| OS | Ubuntu 24.04 LTS |
| Recommended VPS | **CX22 / CPX21** — 2 vCPU, **4 GB RAM** |
| Bootstrap | `bin/bootstrap-ubuntu24.sh` |
| systemd | `infrastructure/systemd/azom-*.service` |
| Docker prod | `infrastructure/docker-compose.prod.yml` |
| Guide | `docs/DEPLOY_UBUNTU24_HETZNER.md` |

Dashboard binds `127.0.0.1:8080` by default on bare metal; use reverse proxy for TLS.

## Out of scope (V2+)

- Full multi-tenant SaaS control plane
- Interactive OAuth browser consent flow (tokens supplied via env)
- Advanced Telegram conversation state
