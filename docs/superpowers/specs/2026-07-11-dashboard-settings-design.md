# Design: Dashboard settings & clear data presentation

**Date:** 2026-07-11  
**Status:** approved (Approach 1 + Oscar login views)  
**Decisions:** Jonatan edits non-secret settings; secrets require Oscar escalation or Oscar login; Basic Auth kept; Oscar gets dedicated admin boxes/views.

## Problem

The dashboard is mostly read-only JSON/HTML. Jonatan cannot adjust sites/limits/integrations from the UI, secrets are only present/missing without a clear Oscar path, and operational data (telemetry, escalations, interact) is under-presented.

## Goals

1. All **non-secret** keys/settings editable in the web dashboard.
2. All **secrets** visible as present/missing only; request updates via Oscar escalation.
3. Relevant data clearly presented as HTML pages (not only JSON APIs).

## Non-goals

- Session/cookie auth (Basic Auth stays).
- Jonatan writing secret values into `.env` from the UI.
- Multi-tenant SaaS settings.

## Actors

| Actor | Auth | Can |
|-------|------|-----|
| Jonatan | Basic Auth user `jonatan` + `DASHBOARD_PASSWORD` | View all; edit YAML non-secrets; request secret updates |
| Oscar | Basic Auth user `oscar` + `DASHBOARD_OSCAR_PASSWORD` | All of Jonatan + edit secrets, Oscar admin boxes, resolve escalations |

Username in Basic Auth selects role. Wrong password → 401.

## Settings taxonomy

### Editable by Jonatan (write to `config/*.yaml` or overlay)

| Setting | Source | UI |
|---------|--------|-----|
| customer | `sites.yaml` | text |
| domains | `sites.yaml` | comma-separated |
| budget_cap_llm | `sites.yaml` | number |
| openrouter_cap | `limits.yaml` | number |
| jonatan_role | `limits.yaml` | display (read-only; RBAC owned) |
| email.default_provider / MAIL_PROVIDER display | `integrations.yaml` + note | select |
| email.enabled, smtp/imap/pop3 flags | `integrations.yaml` | toggles |
| mailcow, order_api, selenium, woocommerce_api, wordpress_api, smart_handling, full_agent_tools | `integrations.yaml` | toggles |
| AZOM_USE_MOCK | `.env` or `AZOM_DATA_DIR/runtime.env` overlay | toggle (mock/live) |

**Mock/live:** Prefer writing `AZOM_DATA_DIR/runtime.env` overlay that dashboard and loaders read, so repo `.env` is not always writable; document that production may symlink/copy.

### Secrets (never show values; Oscar path)

WooCommerce keys, mail password/OAuth client secret/tokens, SSH password/key, OpenRouter, Telegram token, Graph secrets, dashboard password.

UI: checklist + form “Begär uppdatering” → `EscalationService.escalate_critical` with key **names** and optional note (no secret values in ticket body from UI).

## Pages / IA

| Route | Purpose |
|-------|---------|
| `/` | Overview: cost vs caps, runtime, Gmail status, recent telemetry + escalations |
| `/onboarding` | Wizard (existing) |
| `/settings` | Edit non-secret settings; save → YAML |
| `/secrets` | Present/missing + request Oscar |
| `/data/telemetry` | HTML table of telemetry |
| `/data/escalations` | HTML table of escalations |
| `/interact` | GET form + POST draft (HTML), keep JSON API |
| `/oauth/gmail/*` | Existing |
| `/oscar` | Oscar home: admin boxes (secrets editor, open escalations, runtime controls) — Oscar only |
| `/oscar/secrets` | Oscar: set secret values (write `AZOM_DATA_DIR/secrets.env`) |
| `/oscar/escalations` | Oscar: list + mark resolved |

Nav (Jonatan): Översikt · Onboarding · Inställningar · Secrets · Telemetry · Eskaleringar · Interagera · Gmail  
Nav (Oscar): + **Oscar** · Oscar secrets · Oscar eskaleringar

### Oscar boxes (on `/oscar`)

1. **Öppna eskaleringar** — count + link to resolve  
2. **Secrets** — how many set / missing + link to editor  
3. **Runtime** — mock/live toggle (same as settings)  
4. **Gmail OAuth** — connected status + connect/disconnect  
5. **Snabblänkar** — settings, telemetry, interact

## Backend

- `infrastructure/dashboard/settings_store.py` — load/save YAML safely (validate types, backup `.bak` before write).
- `POST /settings` — form or JSON; only allowlisted keys; clear RBAC config cache after write.
- `POST /secrets/request` — escalate key names to Oscar.
- Runtime overlay for `AZOM_USE_MOCK` under `AZOM_DATA_DIR/runtime.env`.
- Tests: `tests/test_dashboard_settings.py` (save sites/limits, reject secret keys, escalate request).

## Presentation

- Shared `base.html` styles; settings forms with labels, help text, success/error flash.
- Mask nothing on non-secrets; never render secret values.
- Cost vs cap progress indicator on home.

## Verification

- Jonatan can change domains and openrouter_cap; files update; reload shows new values.
- POST of `WOO_CONSUMER_SECRET` rejected.
- Secret request creates escalation ticket with key name only.
- Telemetry/escalations HTML pages render.
- `pytest` green.
