---
name: ecom-ops
description: Azom-specifik WooCommerce ops (order-status update, produktbeskrivning-generering, kundsupport). Total managering SSH/VPS + sidunderhåll. Kritiska/kodredigeringar eskaleras till Oscar.
version: "1.0.0"
---

# ecom-ops (V1 Pilot)

## Prioriterade actions

| Action | Modul | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops order-status --order-id ID --status STATUS --mock` |
| produktbeskrivning | `ecom_ops.actions.product_desc` | `python -m ecom_ops product-desc --product-id ID --language sv --mock` |
| kundsupport | `ecom_ops.actions.support` | `python -m ecom_ops support --message "..." --mock` |
| SSH/VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops ssh --command "uptime" --mock` |

## RBAC

- **Jonatan**: `viewer` / read-only
- **Oscar**: `full_admin`
- **Agent (automation)**: `operator` (order/product/support/SSH read)

## Escalation

Allt **kritiskt**, **kodredigering** och **icke-allowlistad SSH** eskaleras till **Oscar**.

Tickets skrivs till `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Security

- Inputvalidering (order id, status, email, site)
- Secret redaction i telemetry/escalation
- SSH allowlist + blockering av shell-metatecken
- Inga secrets i repo; använd env (`WOO_CONSUMER_KEY`, `WOO_CONSUMER_SECRET`, …)

## Automation

```bash
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
./bin/ecom-automation.sh product-desc --product-id 501 --language sv
./bin/ecom-automation.sh support --message "Var är order 1001?"
./bin/ecom-automation.sh ssh --command "uptime"
./bin/ecom-automation.sh critical "kort sammanfattning"
```
