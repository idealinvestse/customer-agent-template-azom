# AGENTS.md – Azom customer agent

## Budget & roller
- Budget: 100$ OpenRouter (`config/limits.yaml`)
- Jonatan: read-only / viewer (+ mail read, SSH read)
- Oscar: full_admin + escalation target (critical + code_edit)
- Agent automation: operator (order/product/support/mail send+read/SSH read)

## Mål
- 3 mån: 50% mindre support-tid + hög engagement
- Onboarding (V2): dedikerad Telegram-bot + lösenordsskyddad webbdashboard

## Runtime target
- **OS:** Ubuntu 26.x (primary) / 24.04 LTS
- **Host:** Hetzner Cloud
- **Rekommenderad storlek:** **CX22 / CPX21** (2 vCPU, **4 GB RAM**, 40 GB NVMe)
- **Auto-install (färdig miljö):** `sudo bash bin/install.sh` eller `bin/install-ubuntu26.sh`
- Docs: `docs/AUTO_INSTALL.md`, `docs/DEPLOY_UBUNTU24_HETZNER.md`

## V1 (klart)
- order-status, product-desc, support, SSH, mail via `skills/ecom_ops`
- Mail providers: gmail, outlook, exchange_graph, generic_imap, generic_pop3
- Auth: app password **eller** OAuth2 (XOAUTH2 / Graph client credentials)
- Kör: `python -m ecom_ops --help` eller `./bin/ecom-automation.sh`
- Dashboard: systemd `azom-dashboard` eller `./bin/start-dashboard.sh` (127.0.0.1:8080)
- Bot: systemd `azom-bot` eller `./bin/dedicated-bot.sh`
- Docker prod: `docker compose -f infrastructure/docker-compose.prod.yml up -d`
- Tester: `pytest`

## V2 (klart)
- Dashboard onboarding wizard + polish (Basic Auth behålls)
- Gmail OAuth browser consent → `AZOM_DATA_DIR/oauth/gmail.json`
- Telegram bot conversation state (`python -m ecom_ops.bot`)
- Settings UI: Jonatan editerar icke-hemliga; Oscar editerar secrets (`/oscar`)
- Docs: `docs/V2_OAUTH_GMAIL.md`, `docs/superpowers/specs/2026-07-11-dashboard-settings-design.md`

## Mail CLI
```bash
python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
python -m ecom_ops --mock mail fetch
python -m ecom_ops --mock mail reply --to a@b.co --subject "Re: ..." --body "..."
```

## Prod paths (Ubuntu)
- Code: `/opt/azom-agent`
- Data: `/var/lib/azom`
- Logs: `/var/log/azom`
- Env: `/opt/azom-agent/.env` (`AZOM_USE_MOCK=0`)
