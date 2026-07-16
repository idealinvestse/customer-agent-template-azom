# customer-agent-template-azom

**AzomOps-Agent v2.0** — template för dedikerad kundagent + grund för Agent-as-a-Service.

**Produktionstarget:** Ubuntu 26.x / 24.04 LTS på Hetzner VPS (CX22 / CPX21, 2 vCPU / 4 GB).

**Full systemkarta:** [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) · **Agent-identitet (OpenClaw):** [`SOUL.md`](SOUL.md) · **Kort agent-notes:** [`AGENTS.md`](AGENTS.md)

## Capabilities (v2.0 + Path B)

| Capability | Beskrivning | Eskalering / kontroll |
|------------|-------------|------------------------|
| **order-status** | Uppdatera WooCommerce orderstatus | Access-fel → Oscar |
| **product-desc** | Generera (och valfritt publicera) produktbeskrivning SE/NO/DK | Access-fel → Oscar |
| **support** | Klassificera ärende + draft-svar (LLM + mall-fallback) | Abuse/legal/critical → Oscar |
| **cases** | Mail → ärende, trådning, order-berikad draft, suggest-approve | Skicka kräver human approve |
| **SSH** | Allowlistad health/ops | Osäker/kodredigering → Oscar |
| **mail** | Gmail / Outlook / Exchange Graph / IMAP / POP3 / SMTP | Auth-fel → Oscar |
| **dashboard** | Onboarding, settings, cases-triage, Oscar admin | Secrets only Oscar |
| **Gmail OAuth** | Browser consent → `AZOM_DATA_DIR/oauth/gmail.json` | — |
| **Telegram bot** | OpenClaw slash + hybrid NL chat (tool prefetch) | Draft/send → human path |
| **smoke / readiness** | Opt-in live smoke; `/health` poll-age | — |

**RBAC:** Jonatan = viewer (+ case reply approve/send) · Oscar = full_admin · agent = operator.

**AI rails:** `config/cases_ai.yaml` — suggest-approve on; auto-send **default off** (+ kill-switch `AZOM_AUTO_SEND_KILL`).

## Quick start (dev)

```bash
python -m pip install -r requirements.txt
python -m pip install -e .

# Linux/macOS
export AZOM_USE_MOCK=1
# Windows: $env:AZOM_USE_MOCK=1

python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
python -m ecom_ops --mock cases poll
python -m ecom_ops support --message "Var är order 1001?"
python -m ecom_ops kpis --days 7
python -m ecom_ops classify-eval
bash bin/mock-soak-azom.sh   # soft ops path (mock)
```

**Finish / ops:** [`docs/DEVELOPMENT_PLAN_FINISH.md`](docs/DEVELOPMENT_PLAN_FINISH.md) · live soak [`docs/solutions/2026-07-16-live-soak-checklist.md`](docs/solutions/2026-07-16-live-soak-checklist.md)

## Production: one-shot install

**Rekommenderad VPS: CX22 / CPX21 (2 vCPU, 4 GB RAM).**

```bash
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash
# Credentials: sudo cat /root/azom-install-credentials.txt
```

Docs: [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md) · [`docs/V2_RELEASE.md`](docs/V2_RELEASE.md) · [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md) · [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md)

Docker:

```bash
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

## Dashboard + bot + cases

```bash
./bin/start-dashboard.sh          # 127.0.0.1:8080
./bin/dedicated-bot.sh            # python -m ecom_ops.bot
./bin/cases-poll.sh               # one-shot poll (timer in prod)
```

| Path | Purpose |
|------|---------|
| `/` | Översikt, nav-badges, probe status |
| `/onboarding` | Wizard: secrets checklist, health, Gmail connect |
| `/settings` | Jonatan: non-secret config |
| `/cases` | Ärende-kö, draft, approve/close |
| `/oscar` | Oscar admin (secrets + resolve escalations + probes) |
| `/oauth/gmail/start` | Gmail browser OAuth |
| `/health` | Liveness + cases-poll readiness |

Basic Auth: `jonatan` / `DASHBOARD_PASSWORD` · `oscar` / `DASHBOARD_OSCAR_PASSWORD`  
(mock fallback: passwords `jonatan` / `oscar` when `AZOM_USE_MOCK=1`)

Telegram (OpenClaw): `/help` `/commands` `/status` `/cases` `/order` … + fri text.  
→ [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md) · cases → [`docs/CASES.md`](docs/CASES.md)

## CLI (utvalda)

```bash
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases show --id <uuid>
python -m ecom_ops --mock cases reply --id <uuid>   # approve + send
python -m ecom_ops smoke --live                     # opt-in; se docs
```

## Tests

```bash
pytest
# CI: ruff + coverage ≥ 65%
bash tests/test_spinup.sh
```

## Documentation index

| Doc | Innehåll |
|-----|----------|
| [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) | Arkitektur, ytor, config, säkerhet |
| [`SOUL.md`](SOUL.md) | OpenClaw / agent personality & hard constraints |
| [`AGENTS.md`](AGENTS.md) | Budget, roller, runtime, quick CLI |
| [`docs/CASES.md`](docs/CASES.md) | Cases 2.0 + Path B rails |
| [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md) | Bot-kommandon & hybrid chat |
| [`docs/V2_RELEASE.md`](docs/V2_RELEASE.md) | Release notes 2.0 |
| [`docs/V2_OAUTH_GMAIL.md`](docs/V2_OAUTH_GMAIL.md) | Gmail OAuth |
| [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md) | One-shot Ubuntu install |
| [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md) | Hetzner sizing & deploy |
| [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md) | Config ro vs data rw |
| [`docs/V1_IMPLEMENTATION.md`](docs/V1_IMPLEMENTATION.md) | V1 package/security notes |
| [`docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md`](docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md) | Acceptance + plan |
| [`docs/GROK_BUILD_PROMPT.md`](docs/GROK_BUILD_PROMPT.md) | Agent fetch+build prompt |
| [`docs/ideation/`](docs/ideation/) | Beslut & backlog |
| [`docs/superpowers/`](docs/superpowers/) | Specs & implementation plans |
| [`docs/solutions/`](docs/solutions/) | Prod-path write-ups |
| [`skills/ecom-ops/SKILL.md`](skills/ecom-ops/SKILL.md) | Skill card |

## Roadmap

1. **V1** – order-status, product-desc, support, SSH, mail ✅  
2. **V2.0** – Dashboard onboarding + Gmail OAuth + Telegram + auto-install ✅  
3. **Cases 2.0 + Path B** – suggest-approve, richer drafts, auto-send rails (off) ✅ on `main`  
4. **Finish current goals** – regenerate draft, baseline/KPI ops, classify calibrate; optional auto-send trial → [`docs/DEVELOPMENT_PLAN_FINISH.md`](docs/DEVELOPMENT_PLAN_FINISH.md)  
5. **V3** – SaaS multi-tenant skalning (deferred)

## Grok Build

Optimal fetch+build prompt for coding agents: [`docs/GROK_BUILD_PROMPT.md`](docs/GROK_BUILD_PROMPT.md)
