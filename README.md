# customer-agent-template-azom

**AzomOps-Agent** — dedikerad kundagent för Azom (WooCommerce SE/NO/DK) + grund för Agent-as-a-Service.

**Package:** 2.0.0 · **Produktionstarget:** Ubuntu 26.x / 24.04 LTS på Hetzner VPS (CX22 / CPX21, 2 vCPU / 4 GB).

| För dig som… | Börja här |
|--------------|-----------|
| Operatör / pilot (svenska) | [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) · [`docs/CASES.md`](docs/CASES.md) |
| Coding agent (English) | [`AGENTS.md`](AGENTS.md) · [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) · [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) |
| Systemkarta | [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) |
| Agent-röst / HITL | [`SOUL.md`](SOUL.md) |

Dokumentationsstil: [`docs/DOC_STYLE.md`](docs/DOC_STYLE.md) · Index: [`docs/README.md`](docs/README.md)

## Capabilities

| Capability | Beskrivning | Eskalering / kontroll |
|------------|-------------|------------------------|
| **order-status** | Uppdatera WooCommerce orderstatus | Access-fel → Oscar |
| **product-desc** | Generera (och valfritt publicera) produktbeskrivning SE/NO/DK | Access-fel → Oscar |
| **support** | Klassificera ärende + draft-svar (LLM + mall-fallback) | Abuse/legal/critical → Oscar |
| **cases** | Mail → ärende, trådning, order-berikad draft, suggest-approve | Skicka kräver human approve |
| **Shadow Live Ledger** | Null-send profil + FU9 skuggspår (`cases shadow-report`) | Default av; Oscar ADMIN för rapport |
| **marketing** | Google Ads + GA4 digest / suggest / HITL mutate | Mock-first; live API stubbade |
| **SSH** | Allowlistad health/ops | Osäker/kodredigering → Oscar |
| **mail** | Gmail / Outlook / Exchange Graph / IMAP / POP3 / SMTP | Auth-fel → Oscar |
| **dashboard** | Onboarding, settings, cases-triage, marketing, Oscar admin | Secrets only Oscar |
| **Gmail OAuth** | Browser consent → `AZOM_DATA_DIR/oauth/gmail.json` | — |
| **Messenger** | Daily-driver ops chat (webhook) | Fail-closed PSID allowlist i prod |
| **Telegram bot** | Backup OpenClaw slash + hybrid NL | Draft/send → human path |
| **Woo/WP** | Orders, trackings, WP REST, webhooks | Se [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md) |
| **smoke / readiness** | Opt-in live smoke; `/health` poll-age | — |

**RBAC:** Jonatan = viewer (+ CASE_REPLY, MARKETING_READ/SUGGEST) · Oscar = full_admin · agent = operator (MAIL_SEND + CASE_REPLY + MARKETING_READ).

**AI rails:** `config/cases_ai.yaml` — suggest-approve on; auto-send **default off**, **not wired** into poll (+ kill-switch `AZOM_AUTO_SEND_KILL`). Null-send: `AZOM_NULL_SEND` (default off).

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
bash bin/mock-soak-azom.sh   # soft ops path (mock) — ersätter INTE live soak
```

Mer: [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) · CLI: [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md)  
Live soak (människa): [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) — agents får inte markera klar.

## Production: one-shot install

**Rekommenderad VPS: CX22 / CPX21 (2 vCPU, 4 GB RAM).**

```bash
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash
# Credentials: sudo cat /root/azom-install-credentials.txt
```

Docs: [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md) · [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md) · [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md)

Docker (data dir `/app/.azom-data`, image `azom-agent:2.0`):

```bash
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

Se [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md).

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
| `/cases` | Ärende-kö, draft, approve/close (+ skugg-badges) |
| `/marketing` | Ads+GA4 digest + suggest HITL |
| `/oscar` | Oscar admin (secrets + resolve escalations + probes) |
| `/webhooks/messenger` | Meta Messenger |
| `/webhooks/woo` | Woo HMAC webhooks |
| `/oauth/gmail/start` | Gmail browser OAuth |
| `/oauth/google/start` | Marketing Google OAuth (Oscar) |
| `/health` | Liveness + cases-poll readiness |

Basic Auth: `jonatan` / `DASHBOARD_PASSWORD` · `oscar` / `DASHBOARD_OSCAR_PASSWORD`  
(mock fallback: passwords `jonatan` / `oscar` when `AZOM_USE_MOCK=1`)

Messenger (daily driver) → [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md)  
Telegram (backup) → [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md)  
Cases → [`docs/CASES.md`](docs/CASES.md)

## CLI (utvalda)

```bash
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases show --id <uuid>
python -m ecom_ops --mock cases reply --id <uuid>   # approve + send
python -m ecom_ops --actor oscar cases shadow-report --days 7
python -m ecom_ops --mock marketing digest --days 7
python -m ecom_ops smoke --live                     # opt-in; se docs
```

## Tests

```bash
pytest
# CI: ruff + coverage ≥ 65%
bash tests/test_spinup.sh
```

## Documentation index

Full table + coverage matrix: [`docs/README.md`](docs/README.md)

| Doc | Innehåll |
|-----|----------|
| [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) | Version, shipped, blockers (EN) |
| [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) | Arkitektur (EN) |
| [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) | Setup, mock, tests (EN) |
| [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) | CLI (EN) |
| [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) | Drift + live soak (SV) |
| [`docs/CASES.md`](docs/CASES.md) | Cases + FU9 (SV) |
| [`docs/MAIL_PROVIDERS.md`](docs/MAIL_PROVIDERS.md) | Mail setup (SV) |
| [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md) | Messenger (SV) |
| [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md) | Telegram (SV) |
| [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md) | Woo/WP (EN) |
| [`docs/runbooks/`](docs/runbooks/) | Incident-runbooks (SV) |
| [`SOUL.md`](SOUL.md) | Agent personality & hard constraints |
| [`AGENTS.md`](AGENTS.md) | Agent operating notes (EN) |
| [`skills/ecom-ops/SKILL.md`](skills/ecom-ops/SKILL.md) | Skill card (EN) |

## Roadmap (kort)

1. **V1–V2.3 code** — shipped on `main` (see [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md))  
2. **Ops next** — Oscar A1 live soak + baseline (human)  
3. **FU9 auto-send** — rails only until Oscar written enable  
4. **V3** — SaaS multi-tenant (deferred)
