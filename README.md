# customer-agent-template-azom

**AzomOps-Agent v2.0** — template för dedikerad kundagent + grund för Agent-as-a-Service.

**Produktionstarget:** Ubuntu 26.x / 24.04 LTS på Hetzner VPS.

## Capabilities (v2.0)

| Capability | Beskrivning | Eskalering |
|------------|-------------|------------|
| **order-status** | Uppdatera WooCommerce orderstatus | Access-fel → Oscar |
| **product-desc** | Generera (och valfritt publicera) produktbeskrivning SE/NO/DK | Access-fel → Oscar |
| **support** | Klassificera ärende + draft-svar | Abuse/legal/critical → Oscar |
| **SSH** | Allowlistad health/ops | Osäker/kodredigering → Oscar |
| **mail** | Gmail / Outlook / Exchange Graph / IMAP / POP3 / SMTP | Auth-fel → Oscar |
| **dashboard** | Onboarding, settings, Oscar admin | Secrets only Oscar |
| **Gmail OAuth** | Browser consent → lagrad refresh token | — |
| **Telegram bot** | Stateful support draft + order lookup | Draft → Oscar |

**RBAC:** Jonatan = viewer, Oscar = full_admin, agent = operator.

## Quick start (dev)

```bash
python -m pip install -r requirements.txt
python -m pip install -e .

export AZOM_USE_MOCK=1   # Windows: $env:AZOM_USE_MOCK=1

python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
python -m ecom_ops support --message "Var är order 1001?"
```

## Production: one-shot install

**Rekommenderad VPS: CX22 / CPX21 (2 vCPU, 4 GB RAM).**

```bash
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash
# Credentials: sudo cat /root/azom-install-credentials.txt
```

Docs: [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md) · [`docs/V2_RELEASE.md`](docs/V2_RELEASE.md)

Docker:

```bash
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

## Dashboard + bot

```bash
./bin/start-dashboard.sh          # 127.0.0.1:8080
./bin/dedicated-bot.sh            # python -m ecom_ops.bot
```

| Path | Purpose |
|------|---------|
| `/onboarding` | Wizard: secrets checklist, health, Gmail connect |
| `/settings` | Jonatan: non-secret config |
| `/oscar` | Oscar admin (secrets + resolve escalations) |
| `/oauth/gmail/start` | Gmail browser OAuth |

Basic Auth: `jonatan` / `DASHBOARD_PASSWORD` · `oscar` / `DASHBOARD_OSCAR_PASSWORD`  
(mock fallback: passwords `jonatan` / `oscar` when `AZOM_USE_MOCK=1`)

## Tests

```bash
pytest
bash tests/test_spinup.sh
```

## Roadmap

1. **V1** – order-status, product-desc, support, SSH, mail ✅
2. **V2.0** – Dashboard onboarding + Gmail OAuth + Telegram state + auto-install ✅
3. **V3** – SaaS multi-tenant skalning

See `docs/V2_RELEASE.md`, `docs/V2_OAUTH_GMAIL.md`, `docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md`.
