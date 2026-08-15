# Template guide — Azom reference, fork by hand

**Purpose:** How to copy this **single-tenant Azom reference implementation** for another shop without weakening safety rails. This is not a multi-tenant SaaS control plane and not a one-command instantiate pipeline.  
**Audience:** Developers preparing a new deployment.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Profile** | `config/profile.yaml` — customer name, brand, markets, languages, actor display names, order-status URL |
| **RBAC keys** | `config/rbac.yaml` `roles:` map — CLI `--actor` and permission checks. Must include every profile actor |
| **Single-tenant** | One customer per deployment |
| **Safety rails** | HITL approve, fail-closed allowlists, never-suggest categories, kill-switches — **not** profile-overridable |

## Honest scope

This repository ships as the **Azom** pilot (package 3.0.0). Fork it, then walk the checklist. Install scripts default to the Azom git remote; point them at your fork.

## Checklist (all required)

1. Copy the repo. Keep `AZOM_USE_MOCK=1` until the admin-equivalent signs live.
2. Edit [`config/profile.yaml`](../config/profile.yaml): `customer`, `brand`, `markets`, `languages`, `actors.viewer` / `actors.admin` / `actors.operator`, `order_status_url_template`.
3. Edit [`config/rbac.yaml`](../config/rbac.yaml) so `roles` keys **include** those three actor names. Dashboard Basic Auth usernames come from profile; CLI `--actor` comes from RBAC. Both must match.
4. Edit [`config/sites.yaml`](../config/sites.yaml) domains + customer. LLM spend cap is **`config/limits.yaml` `openrouter_cap`** only (`budget_cap_llm` is a display alias).
5. Edit [`config/mailboxes.yaml`](../config/mailboxes.yaml). Leave extra markets `enabled: false` until credentials exist.
6. Adapt [`SOUL.md`](../SOUL.md) and [`config/prompts.yaml`](../config/prompts.yaml) (voice + classify/draft). Do **not** remove HITL rules.
7. Point Woo/mail/OAuth env at the new shop. Never commit `.env` or tokens.
8. Update systemd `--actor` on retention-purge / shadow-report if the admin name is not `oscar`.
9. Env names stay Azom-shaped (`DASHBOARD_OSCAR_PASSWORD*`) even if the admin actor is renamed — map the person, not the env key.

## What you must not change

- Silent customer mail / wiring FU9 auto-send into poll
- Empty live allowlists becoming allow-all
- Default-on Ads mutate or auto-send
- Suggest-approve on abuse / return / billing
- Inventing order facts

## Runtime leftovers (override via profile/env)

| Location | Default | Override |
|----------|---------|----------|
| Order-status link in drafts | `order_status_url_template` in profile | Set the template |
| Woo base URL | `WOO_BASE_URL` / `WOO_BASE_URL_<DOMAIN>` | Env; else `https://azom.{domain}` |
| LLM Referer | `https://azom.se` in `llm.py` | Acceptable for OpenRouter; change if you care |
| Install default remote | `idealinvestse/customer-agent-template-azom` | Pass your fork to install scripts |

## Verify

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops soak-preflight
python -m ecom_ops --mock cases poll
pytest
# expect: soak_complete false; pytest green
```
