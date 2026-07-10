---
name: ecom-ops
description: Azom WooCommerce ops (order-status, product desc, support, mail, SSH). V2 adds dashboard onboarding, Gmail OAuth, Telegram conversation state. Critical/code edits escalate to Oscar.
version: "2.0.0"
---

# ecom-ops (V2)

## Prioriterade actions

| Action | Modul | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops order-status --order-id ID --status STATUS --mock` |
| produktbeskrivning | `ecom_ops.actions.product_desc` | `python -m ecom_ops product-desc --product-id ID --language sv --mock` |
| kundsupport | `ecom_ops.actions.support` | `python -m ecom_ops support --message "..." --mock` |
| SSH/VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops ssh --command "uptime" --mock` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops mail send --to a@b.co --subject "..." --body "..." --mock` |
| runtime status | CLI | `python -m ecom_ops status` |

## V2 surfaces

| Surface | Entry |
|---------|--------|
| Dashboard | `./bin/start-dashboard.sh` → `/onboarding`, `/settings`, `/oscar` |
| Gmail OAuth | `/oauth/gmail/start` → tokens in `AZOM_DATA_DIR/oauth/gmail.json` |
| Telegram bot | `python -m ecom_ops.bot` or `./bin/dedicated-bot.sh` |

## Mail providers

- `gmail` – SMTP+IMAP (app password eller OAuth2 XOAUTH2 / browser consent)
- `outlook` – SMTP+IMAP (app password eller OAuth2 XOAUTH2)
- `exchange_graph` – Microsoft Graph REST API (client credentials)
- `generic_imap` / `generic_pop3` – custom hosts via env

## RBAC

- **Jonatan**: `viewer` / read-only (+ mail read, SSH read, dashboard settings non-secret)
- **Oscar**: `full_admin` (secrets UI + escalation resolve)
- **Agent (automation)**: `operator` (order/product/support/mail send+read/SSH read)

## Escalation

Allt **kritiskt**, **kodredigering** och **icke-allowlistad SSH** eskaleras till **Oscar**.

Tickets: `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Automation

```bash
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
./bin/ecom-automation.sh mail fetch
./bin/ecom-automation.sh critical "kort sammanfattning"
sudo bash bin/install.sh   # full VPS bootstrap (Ubuntu 26/24)
```
