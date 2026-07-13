---
name: ecom-ops
description: >
  Azom WooCommerce ops (order-status, product desc, support, mail, SSH, cases).
  V2: dashboard onboarding, Gmail OAuth, OpenClaw Telegram hybrid, Cases 2.0 +
  Path B suggest-approve rails. Critical/code edits escalate to Oscar. Never
  silent customer mail — human approve for case reply.
version: "2.0.0"
---

# ecom-ops (V2 + Path B)

**Identity / hard rules:** repository root `SOUL.md`  
**System map:** `docs/SYSTEM_OVERVIEW.md`  
**Telegram:** `docs/TELEGRAM_OPENCLAW.md` · **Cases:** `docs/CASES.md`

## Prioriterade actions

| Action | Modul | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops order-status --order-id ID --status STATUS --mock` |
| produktbeskrivning | `ecom_ops.actions.product_desc` | `python -m ecom_ops product-desc --product-id ID --language sv --mock` |
| kundsupport | `ecom_ops.actions.support` | `python -m ecom_ops support --message "..." --mock` |
| cases poll / approve | `ecom_ops.cases.service` | `python -m ecom_ops cases poll\|list\|show\|reply\|draft\|close --mock` |
| SSH/VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops ssh --command "uptime" --mock` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops mail send\|fetch\|reply --mock` |
| runtime status | CLI | `python -m ecom_ops status` · `python -m ecom_ops smoke` |

## V2 surfaces

| Surface | Entry |
|---------|--------|
| Dashboard | `./bin/start-dashboard.sh` → `/onboarding`, `/settings`, `/cases`, `/oscar` |
| Gmail OAuth | `/oauth/gmail/start` → tokens in `AZOM_DATA_DIR/oauth/gmail.json` |
| Telegram bot | `python -m ecom_ops.bot` or `./bin/dedicated-bot.sh` |
| Cases timer | `./bin/cases-poll.sh` / `azom-cases-poll.timer` |

## Telegram (OpenClaw hybrid)

Slash: `/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` `/cases` `/order` `/health` `/brief` …  
Free text: read-only tool prefetch + LLM phrasing (Swedish). **Send only** via `/cases approve` or approve button — never from free-text alone.

## Mail providers

- `gmail` – SMTP+IMAP (app password eller OAuth2 XOAUTH2 / browser consent)
- `outlook` – SMTP+IMAP (app password eller OAuth2 XOAUTH2)
- `exchange_graph` – Microsoft Graph REST API (client credentials)
- `generic_imap` / `generic_pop3` – custom hosts via env

## Cases AI rails

`config/cases_ai.yaml`: suggest-approve for `order_status`/`shipping` (default); auto-send **off** unless Oscar enables + kill-switch clears (`AZOM_AUTO_SEND_KILL`).

## RBAC

- **Jonatan**: `viewer` / read-only (+ mail read, SSH read, dashboard settings non-secret, **CASE_REPLY**)
- **Oscar**: `full_admin` (secrets UI + escalation resolve + probes)
- **Agent (automation)**: `operator` (order/product/support/mail send+read/SSH read/case poll)

## Escalation

Allt **kritiskt**, **kodredigering** och **icke-allowlistad SSH** eskaleras till **Oscar**.

Tickets: `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Automation

```bash
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
./bin/ecom-automation.sh mail fetch
./bin/ecom-automation.sh critical "kort sammanfattning"
./bin/cases-poll.sh
sudo bash bin/install.sh   # full VPS bootstrap (Ubuntu 26/24)
```
