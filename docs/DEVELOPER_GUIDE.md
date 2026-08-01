# Developer guide

**Purpose:** How to set up, run, test, and change this repository without inventing capabilities or weakening safety.  
**Audience:** Developers and coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Mock mode** | `AZOM_USE_MOCK=1` — no real Woo/mail/Telegram/OpenRouter network; safe local default. |
| **Live mode** | `AZOM_USE_MOCK=0` — real credentials required; production posture. |
| **Package root** | Importable Python package at `skills/ecom_ops/` (module name `ecom_ops`). |
| **Skill card** | Metadata for agent hosts at `skills/ecom-ops/SKILL.md` (hyphen in folder name). |

## Repository layout

| Path | Role |
|------|------|
| `skills/ecom_ops/` | Importable `ecom_ops` package (actions, cases, bot, integrations, llm, cli) |
| `skills/ecom-ops/SKILL.md` | Skill card for Moss / agent hosts |
| `infrastructure/dashboard/` | Flask dashboard + webhooks + Oscar probes |
| `infrastructure/systemd/` | Unit files for Ubuntu install |
| `infrastructure/docker-compose.prod.yml` | Production Docker compose |
| `config/` | YAML/JSON config (read-only in Docker) |
| `bin/` | Install, start, poll, soak helper scripts |
| `tests/` | pytest suite + fixtures |
| `docs/` | Living documentation (this tree) |
| `.env.example` | Env var contract — copy to `.env` for local work |

## Prerequisites

1. Python **3.11+**
2. Git
3. On Windows: PowerShell is fine for most commands; use Git Bash or WSL for `bash bin/*.sh` scripts when needed.
4. Do **not** put real production secrets in the repo or in commits.

## Local setup (mock-first)

```bash
# cwd: repository root
python -m pip install -r requirements.txt
python -m pip install -e .

# Linux/macOS
export AZOM_USE_MOCK=1
export AZOM_CONFIG_DIR=./config
export AZOM_DATA_DIR=./.azom-data

# Windows PowerShell
# $env:AZOM_USE_MOCK="1"
# $env:AZOM_CONFIG_DIR="./config"
# $env:AZOM_DATA_DIR="./.azom-data"

python -m ecom_ops version
# expect: package version string / JSON including 2.0.0

python -m ecom_ops status
# expect: mock flags and config paths; no network required
```

Optional: copy `.env.example` to `.env` and keep `AZOM_USE_MOCK=1`.

### Smoke commands (mock)

```bash
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
python -m ecom_ops --mock cases poll
python -m ecom_ops support --message "Var är order 1001?"
python -m ecom_ops classify-eval
python -m ecom_ops kpis --days 7
bash bin/mock-soak-azom.sh
# expect: scripts and CLI exit 0; cases/mail use in-memory or local mock stores
```

Dashboard (local):

```bash
./bin/start-dashboard.sh
# expect: Flask on 127.0.0.1:8080
# mock Basic Auth passwords: jonatan / oscar  (ONLY when AZOM_USE_MOCK=1)
```

Bot (needs token only for real Telegram; mock paths covered by tests):

```bash
python -m ecom_ops.bot
```

## Tests and CI

```bash
pytest
# CI also runs Ruff and enforces coverage fail_under 65 (see pyproject.toml)

bash tests/test_spinup.sh
# optional spinup smoke
```

### Do / Do not for agents changing code

**Do:**

- Read [`CURRENT_STATE.md`](CURRENT_STATE.md) and the living doc for the surface you touch before coding.
- Prefer mock-mode tests; add fixtures under `tests/fixtures/` when classifying or drafting.
- Keep human-approve invariants: no silent customer mail; no auto-send wire without Oscar written enable + FU9 gates.
- Fix real build/test failures; keep coverage ≥ 65%.

**Do not:**

- Weaken or delete tests to make CI green.
- Commit `.env`, OAuth tokens, or `secrets.env`.
- Enable NO/DK mailboxes or `auto_send_enabled` without Oscar authorization.
- Invent Woo/WP/dashboard capabilities that are not in code.
- Expand into parked scope (V3, FAQ/KB, GA4, default-on auto-send).

## Where to change what

| Goal | Start here |
|------|------------|
| CLI commands | `skills/ecom_ops/cli.py` — also update [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
| Cases poll / approve | `skills/ecom_ops/cases/` |
| Suggest / auto-send rails | `skills/ecom_ops/cases/suggest.py`, `auto_send.py`, `config/cases_ai.yaml` |
| Telegram / Messenger brain | `skills/ecom_ops/bot/` |
| Woo / WP clients | `skills/ecom_ops/integrations/woocommerce.py`, `wordpress.py`, `webhooks.py` |
| Mail providers | `skills/ecom_ops/integrations/mail*` |
| Dashboard routes / probes | `infrastructure/dashboard/` (probes are Oscar UI, not CLI) |
| RBAC | `config/rbac.yaml` + security checks in code |
| Agent voice | `SOUL.md` + `skills/ecom_ops/bot/chat_agent.py` system prompt |

## Production target (context only)

- Ubuntu 26 primary / 24.04 supported on Hetzner CX22/CPX21.
- Dashboard binds **127.0.0.1:8080** behind a TLS reverse proxy.
- Install docs: [`AUTO_INSTALL.md`](AUTO_INSTALL.md), [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md), [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md).
- Ops day-to-day: [`PILOT_OPS.md`](PILOT_OPS.md) (Swedish).

## Escalation while developing

Escalate to **Oscar** (ticket / human) when you hit: critical security issues, required code-edit on prod host, unsafe SSH, secrets/OAuth/probe failures that need credential changes, or any request to wire auto-send.

## Build-agent checklist (absorbed from former Grok build prompt)

1. Fetch and read living docs + code; do not invent features.
2. Reproduce with mock CLI/tests first.
3. Implement the smallest correct change.
4. Run `pytest` (and Ruff if you touch style-sensitive files).
5. Update living docs if behavior or CLI changed.
6. Never commit secrets.

## Related

- CLI: [`CLI_REFERENCE.md`](CLI_REFERENCE.md)
- Architecture: [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md)
- Woo/WP: [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md)
- Cases safety: [`CASES.md`](CASES.md)
