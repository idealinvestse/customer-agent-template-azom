# Template guide — instantiate this agent for a new customer

**Purpose:** How to copy this single-tenant repository for another Agent-as-a-Service customer without weakening safety rails.  
**Audience:** Developers and coding agents preparing a new deployment.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Profile** | `config/profile.yaml` — customer name, markets, languages, actor display names |
| **Single-tenant** | One customer per deployment. This is **not** a multi-tenant SaaS control plane |
| **Safety rails** | HITL approve, fail-closed allowlists, never-suggest categories, kill-switches — **not** profile-overridable |

## What you change

1. Copy the repo. Keep `AZOM_USE_MOCK=1` until Oscar-equivalent signs live.
2. Edit [`config/profile.yaml`](../config/profile.yaml): `customer`, `brand`, `markets`, `languages`, `actors.viewer` / `actors.admin`.
3. Edit [`config/sites.yaml`](../config/sites.yaml), [`config/mailboxes.yaml`](../config/mailboxes.yaml) (leave extra markets `enabled: false` until credentials exist).
4. Adapt [`SOUL.md`](../SOUL.md) voice (language + shop name). Do **not** remove HITL rules.
5. Map dashboard Basic Auth usernames to profile actors (defaults remain `jonatan` / `oscar`).
6. Point Woo/mail/OAuth env at the new shop. Never commit `.env` or tokens.

## What you must not change

- Silent customer mail / wiring FU9 auto-send into poll
- Empty live allowlists becoming allow-all
- Default-on Ads mutate or auto-send
- Suggest-approve on abuse / return / billing
- Inventing order facts

## Verify

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops soak-preflight
python -m ecom_ops --mock cases poll
pytest
# expect: soak_complete false; pytest green
```
