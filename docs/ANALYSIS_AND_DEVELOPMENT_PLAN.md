# Bred och djup analys + full utvecklingsplan (top 1% nivå)

## Teori

Agent-as-a-Service baserad på datalasse + azom-control-hub: dedikerad, isolerad Moss-agent per kund för operativ driving av WooCommerce e-com.

## Syfte

Minska support-tid 50%, öka engagement, automatisera order/product/support/mail, säker managering ner till SSH, telemetry för usage-fakturering, dashboard för Jonatan.

## Exekvering

1. **V1: Pilot med order-status, product-desc, support, SSH, mail.** ✅ IMPLEMENTED
2. **V2.0: Dashboard onboarding + Gmail OAuth + Telegram OpenClaw hybrid + auto-install.** ✅ RELEASED (`2.0.0`)
3. **Cases 2.0 + Path B (suggest-approve, draft quality, auto-send rails default-off).** ✅ on `main`
4. **Finish current goals (post Path B):** regenerate, baseline, operate/measure, calibrate — se `docs/DEVELOPMENT_PLAN_FINISH.md`
5. V3: SaaS skalning (deferred)

**Systemkarta:** `docs/SYSTEM_OVERVIEW.md` · **SOUL:** `SOUL.md` · **Beslut:** `docs/ideation/2026-07-11-azom-project-overview-next-steps-scope.md` · **Finish plan:** `docs/DEVELOPMENT_PLAN_FINISH.md`

### V2 acceptance criteria

| Krav | Status | Var |
|------|--------|-----|
| Dashboard onboarding wizard | ✅ | `/onboarding`, `status.health_probe` |
| Settings (Jonatan non-secret) | ✅ | `/settings`, `settings_store` |
| Oscar admin secrets + resolve | ✅ | `/oscar/*` |
| Gmail OAuth browser consent | ✅ | `ecom_ops.oauth.gmail` |
| Telegram conversation state + OpenClaw | ✅ | `ecom_ops.bot`, `docs/TELEGRAM_OPENCLAW.md` |
| One-shot Ubuntu install | ✅ | `bin/install-ubuntu26.sh` |
| Version CLI | ✅ | `python -m ecom_ops version` |

Release notes: `docs/V2_RELEASE.md`.

### Cases 2.0 + Path B acceptance

| Krav | Status | Var |
|------|--------|-----|
| Mail → case poll + threading + mark_read | ✅ | `ecom_ops.cases`, timer |
| Order-enriched drafts + human approve/send | ✅ | dashboard `/cases`, Telegram, CLI |
| Suggest-approve eligibility + UX | ✅ | `config/cases_ai.yaml`, Telegram/dashboard |
| Auto-send rails default off + kill-switch | ✅ | `ecom_ops.cases.auto_send` |
| Hybrid dialog tool prefetch | ✅ | `chat_agent.py` (commit hybrid vNext) |
| Smoke/readiness + CI ruff/cov≥65% | ✅ | `ops_status`, `smoke`, pyproject |

Docs: `docs/CASES.md`. Plan: `docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`.

### V1 acceptance criteria

| Krav | Status | Var |
|------|--------|-----|
| Order-status update (Woo) | ✅ | `ecom_ops.actions.order_status` |
| Product description gen | ✅ | `ecom_ops.actions.product_desc` |
| Support automation | ✅ | `ecom_ops.actions.support` |
| SSH/VPS (safe allowlist) | ✅ | `ecom_ops.actions.ssh_ops` |
| Mail (Gmail/Outlook/Graph/IMAP/POP3/SMTP) | ✅ | `ecom_ops.integrations.mail` + `actions.mail` |
| Escalation till Oscar (critical/code) | ✅ | `ecom_ops.escalation` + `config/rbac.yaml` |
| RBAC Jonatan read-only (+ CASE_REPLY later) | ✅ | `ecom_ops.rbac` |
| Tests + CI | ✅ | `tests/`, `.github/workflows/ci.yml` |
| Secret hygiene | ✅ | `ecom_ops.security`, `.env.example` |
| Dashboard + Docker | ✅ | `infrastructure/dashboard/`, `Dockerfile` |

Detaljer: `docs/V1_IMPLEMENTATION.md`.

## Grok-build prompt (V2.0)

**Full optimal prompt:** `docs/GROK_BUILD_PROMPT.md` (copy-paste block for Grok Build: fetch, venv, pytest, mock smoke, acceptance).

Kort: Clone main, venv + `pip install -e .`, `AZOM_USE_MOCK=1`, `python -m ecom_ops version` (=2.0.0), `pytest`, mock smoke (order/product/support/ssh/mail/cases). Fix only build breaks.

## Läs repo för implementation

Start: `SOUL.md`, `docs/SYSTEM_OVERVIEW.md`, `skills/ecom_ops/`, `bin/ecom-automation.sh`, `tests/`.
