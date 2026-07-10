# customer-agent-template-azom

Template för dedikerad **AzomOps-Agent** + grund för Agent-as-a-Service.

**Produktionstarget:** Ubuntu 24.04 LTS på Hetzner VPS.

## V1 Pilot (implementerad)

| Capability | Beskrivning | Eskalering |
|------------|-------------|------------|
| **order-status** | Uppdatera WooCommerce orderstatus | Access-fel → Oscar |
| **product-desc** | Generera (och valfritt publicera) produktbeskrivning SE/NO/DK | Access-fel → Oscar |
| **support** | Klassificera ärende + draft-svar | Abuse/legal/critical → Oscar |
| **SSH** | Allowlistad health/ops | Osäker/kodredigering → Oscar |
| **mail** | Gmail / Outlook / Exchange Graph / IMAP / POP3 / SMTP | Auth-fel → Oscar |

**RBAC:** Jonatan = viewer (read-only + mail read), Oscar = full_admin, agent = operator.

## Quick start (dev)

```bash
python -m pip install -r requirements.txt
python -m pip install -e .

# Mock-läge (ingen extern trafik)
export AZOM_USE_MOCK=1   # Windows PowerShell: $env:AZOM_USE_MOCK=1

python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock product-desc --product-id 501 --language sv
python -m ecom_ops support --message "Var är order 1001?"
python -m ecom_ops --mock ssh --command uptime
python -m ecom_ops --mock mail send --to customer@example.com --subject "Test" --body "Hej"
python -m ecom_ops --mock mail fetch
```

## Production: Ubuntu 24 / Hetzner

**Rekommenderad VPS: CX22 eller CPX21 (2 vCPU, 4 GB RAM, ~€4–7/mån).**

```bash
# På servern (root)
git clone <repo> /opt/azom-agent && cd /opt/azom-agent
sudo bash bin/bootstrap-ubuntu24.sh
sudo nano /opt/azom-agent/.env   # secrets + AZOM_USE_MOCK=0
sudo systemctl enable --now azom-dashboard azom-bot azom-daily-brief.timer
```

Full guide: [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md)

Docker prod:

```bash
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

## Mail connector

| Provider | Protokoll | Auth |
|----------|-----------|------|
| **gmail** | SMTP + IMAP | App password eller OAuth2 (XOAUTH2) |
| **outlook** | SMTP + IMAP | App password eller OAuth2 (XOAUTH2) |
| **exchange_graph** | Microsoft Graph API | OAuth2 client credentials |
| **generic_imap** | SMTP + IMAP | Password eller OAuth2 |
| **generic_pop3** | POP3 (+ SMTP separat för send) | Password |

## Dashboard + bot

```bash
./bin/start-dashboard.sh          # 127.0.0.1:8080 Basic Auth (Jonatan)
./bin/dedicated-bot.sh            # Telegram (TELEGRAM_BOT_TOKEN, conversation state)
```

Dashboard V2+:
- `/onboarding` — wizard (secrets checklist, health probe, mock/live)
- `/settings` — Jonatan redigerar icke-hemliga YAML/inställningar
- `/secrets` — present/missing + begär Oscar-uppdatering
- `/data/telemetry`, `/data/escalations`, `/interact` — tydliga HTML-vyer
- `/oscar`, `/oscar/secrets`, `/oscar/escalations` — Oscar full_admin (Basic Auth user `oscar`)
- `/oauth/gmail/start` — Gmail browser consent (mock stores tokens when `AZOM_USE_MOCK=1`)

Login: Basic Auth — `jonatan` / `DASHBOARD_PASSWORD` (mock: `jonatan`) eller `oscar` / `DASHBOARD_OSCAR_PASSWORD` (mock: `oscar`).

## Tests

```bash
pytest
bash tests/test_spinup.sh
```

## Config

- `config/sites.yaml` – kund + domäner + LLM-budget
- `config/rbac.yaml` – roller + escalation (Oscar)
- `config/limits.yaml` – OpenRouter cap, Jonatan read-only
- `config/integrations.yaml` – integrationsflaggor inkl. mail providers
- `.env.example` – secrets (kopiera till `.env`, committas ej)
- `infrastructure/systemd/` – systemd units för Ubuntu 24

## Roadmap

1. **V1** – Pilot: order-status, product-desc, support, SSH, mail ✅
2. **V2** – Dashboard onboarding + Gmail OAuth + Telegram state ✅
3. **V3** – SaaS-skalning

Se `docs/ANALYSIS_AND_DEVELOPMENT_PLAN.md`, `docs/V1_IMPLEMENTATION.md`, `docs/V2_OAUTH_GMAIL.md`.
