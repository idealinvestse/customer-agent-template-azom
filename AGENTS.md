# AGENTS.md – Azom customer agent (v2.0)

## Budget & roller
- Budget: 100$ OpenRouter (`config/limits.yaml`)
- Jonatan: viewer (+ mail read, SSH read, non-secret settings, **case reply approve/send**)
- Oscar: full_admin + escalation target (critical + code_edit + secrets UI)
- Agent automation: operator (order/product/support/mail send+read/SSH read/case poll)

## Mål
- 3 mån: 50% mindre support-tid + hög engagement
- Onboarding: Telegram-bot + lösenordsskyddad webbdashboard ✅ (v2)

## Runtime target
- **Package version:** 2.0.0
- **OS:** Ubuntu 26.x (primary) / 24.04 LTS
- **Host:** Hetzner Cloud — **CX22 / CPX21** (2 vCPU, 4 GB RAM)
- **Auto-install:** `sudo bash bin/install.sh` eller `bin/install-ubuntu26.sh`
- Docs: `docs/AUTO_INSTALL.md`, `docs/V2_RELEASE.md`, `docs/DEPLOY_UBUNTU24_HETZNER.md`

## V1 (core — kvar i v2)
- order-status, product-desc, support, SSH, mail via `skills/ecom_ops`
- Mail providers: gmail, outlook, exchange_graph, generic_imap, generic_pop3
- CLI: `python -m ecom_ops` · `./bin/ecom-automation.sh`
- Tester: `pytest`

## V2.0 (klart)
- Dashboard onboarding wizard + HTML views (telemetry/escalations/interact)
- Settings UI: Jonatan (non-secret) · Oscar (secrets + resolve escalations)
- Gmail OAuth browser consent → `AZOM_DATA_DIR/oauth/gmail.json`
- Telegram bot: OpenClaw-style (`/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` …) + `/order` `/cases` (`python -m ecom_ops.bot`)
- One-shot Ubuntu install + systemd + prod Docker (`azom-agent:2.0`)
- CLI: `python -m ecom_ops version` · `python -m ecom_ops status`

## Cases (mail → ärende, semi-auto)
- Config: `config/mailboxes.yaml` (N funktionsbrevlådor)
- DB: `AZOM_DATA_DIR/cases.db` (SQLite)
- Dashboard: `/cases` · poll + godkänn draft + skicka
- CLI / poller:
```bash
python -m ecom_ops --mock cases poll
python -m ecom_ops --mock cases list
./bin/cases-poll.sh
```

## Mail CLI
```bash
python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
python -m ecom_ops --mock mail fetch
python -m ecom_ops status
```

## Prod paths (Ubuntu)
- Code: `/opt/azom-agent`
- Data: `/var/lib/azom`
- Logs: `/var/log/azom`
- Env: `/opt/azom-agent/.env` (`AZOM_USE_MOCK=0`)
