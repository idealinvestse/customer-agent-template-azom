---
name: ecom-ops
description: Azom-specifik WooCommerce ops (order-status update, produktbeskrivning-generering, kundsupport, mail). Total managering SSH/VPS + sidunderhåll. Kritiska/kodredigeringar eskaleras till Oscar.
version: "1.1.0"
---

# ecom-ops (V1 Pilot)

## Prioriterade actions

| Action | Modul | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops order-status --order-id ID --status STATUS --mock` |
| produktbeskrivning | `ecom_ops.actions.product_desc` | `python -m ecom_ops product-desc --product-id ID --language sv --mock` |
| kundsupport | `ecom_ops.actions.support` | `python -m ecom_ops support --message "..." --mock` |
| SSH/VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops ssh --command "uptime" --mock` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops mail send --to a@b.co --subject "..." --body "..." --mock` |

## Mail providers

- `gmail` – SMTP+IMAP (app password eller OAuth2 XOAUTH2)
- `outlook` – SMTP+IMAP (app password eller OAuth2 XOAUTH2)
- `exchange_graph` – Microsoft Graph REST API (client credentials)
- `generic_imap` / `generic_pop3` – custom hosts via env

## RBAC

- **Jonatan**: `viewer` / read-only (+ mail read, SSH read)
- **Oscar**: `full_admin`
- **Agent (automation)**: `operator` (order/product/support/mail send+read/SSH read)

## Escalation

Allt **kritiskt**, **kodredigering** och **icke-allowlistad SSH** eskaleras till **Oscar**.

Tickets skrivs till `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Security

- Inputvalidering (order id, status, email, site)
- Secret redaction i telemetry/escalation
- SSH allowlist + blockering av shell-metatecken
- Mail secrets i env (`MAIL_PASSWORD`, OAuth tokens, Graph secret)
- Inga secrets i repo; använd env

## Automation

```bash
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
./bin/ecom-automation.sh product-desc --product-id 501 --language sv
./bin/ecom-automation.sh support --message "Var är order 1001?"
./bin/ecom-automation.sh ssh --command "uptime"
./bin/ecom-automation.sh mail send --to customer@example.com --subject "Test" --body "Hej"
./bin/ecom-automation.sh mail fetch
./bin/ecom-automation.sh critical "kort sammanfattning"
```
