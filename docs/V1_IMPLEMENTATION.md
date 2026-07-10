# V1 Implementation Notes

## Scope (from plan)

> V1: Pilot med order-status, product-desc, support, SSH.

## Package layout

```
skills/ecom_ops/           # Python package (import ecom_ops)
  actions/                 # order_status, product_desc, support, ssh_ops
  integrations/            # WooCommerce + SSH clients (+ mocks)
  security.py, rbac.py, escalation.py, telemetry.py, config.py, cli.py
skills/ecom-ops/           # Moss skill metadata + shim
  SKILL.md
  integrations.py
tests/                     # pytest suite
bin/ecom-automation.sh     # operator entrypoint
```

## Security model

1. Validate all external inputs (order id, status enum, email, site).
2. Never log secrets (redaction in telemetry + escalation).
3. SSH allowlist only for auto-exec; everything else → Oscar ticket.
4. Shell metacharacters (`;`, `|`, `&&`, …) rejected.
5. RBAC: Jonatan cannot mutate orders/products/support replies.

## Escalation

`EscalationService` writes JSONL tickets assigned to **oscar** for:

- `critical`
- `code_edit`
- `ssh_unsafe`
- access denials on sensitive paths

## Telemetry

Local JSONL usage events for billing metering (`cost_usd`, tokens/api_calls).

## Out of scope (V2+)

- Full Flask dashboard auth UX
- Telegram bot polish
- Multi-tenant SaaS control plane
