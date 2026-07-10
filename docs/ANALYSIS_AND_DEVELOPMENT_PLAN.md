# Bred och djup analys + full utvecklingsplan (top 1% nivå)

## Teori

Agent-as-a-Service baserad på datalasse + azom-control-hub: dedikerad, isolerad Moss-agent per kund för operativ driving av WooCommerce e-com.

## Syfte

Minska support-tid 50%, öka engagement, automatisera order/product/support/mail, säker managering ner till SSH, telemetry för usage-fakturering, dashboard för Jonatan.

## Exekvering

1. **V1: Pilot med order-status, product-desc, support, SSH, mail.** ✅ IMPLEMENTED
2. V2: Dashboard + onboarding polish.
3. V3: SaaS skalning.

### V1 acceptance criteria

| Krav | Status | Var |
|------|--------|-----|
| Order-status update (Woo) | ✅ | `ecom_ops.actions.order_status` |
| Product description gen | ✅ | `ecom_ops.actions.product_desc` |
| Support automation | ✅ | `ecom_ops.actions.support` |
| SSH/VPS (safe allowlist) | ✅ | `ecom_ops.actions.ssh_ops` |
| Mail (Gmail/Outlook/Graph/IMAP/POP3/SMTP) | ✅ | `ecom_ops.integrations.mail` + `actions.mail` |
| Escalation till Oscar (critical/code) | ✅ | `ecom_ops.escalation` + `config/rbac.yaml` |
| RBAC Jonatan read-only | ✅ | `ecom_ops.rbac` |
| Tests + CI | ✅ | `tests/`, `.github/workflows/ci.yml` |
| Secret hygiene | ✅ | `ecom_ops.security`, `.env.example` |
| Dashboard + Docker | ✅ | `infrastructure/dashboard/`, `Dockerfile` |

Detaljer: `docs/V1_IMPLEMENTATION.md`.

## Grok-build prompt (V1)

Implementera V1 exakt: order-status, product-desc, support, SSH, mail. Clean code, tests, security, escalation till Oscar vid kritiskt. Mockbara integrationer, CLI `python -m ecom_ops`, bin/ecom-automation.sh, pytest i CI.

## Läs repo för implementation

Start: `skills/ecom_ops/`, `bin/ecom-automation.sh`, `tests/`.
