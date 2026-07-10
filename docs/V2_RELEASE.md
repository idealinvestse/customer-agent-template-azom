# Azom ecom-ops **v2.0.0**

Release notes for the V2 product line (dashboard onboarding, Gmail OAuth, Telegram conversation state, auto-install).

## What's in 2.0

| Area | Capability |
|------|------------|
| **Core ops (from V1)** | order-status, product-desc, support, SSH allowlist, mail (Gmail/Outlook/Graph/IMAP/POP3/SMTP) |
| **RBAC** | Jonatan viewer, Oscar full_admin, agent operator + escalation tickets |
| **Dashboard** | Onboarding wizard, settings (Jonatan), secrets + escalations resolve (Oscar), interact draft |
| **Gmail OAuth** | Browser consent flow → `AZOM_DATA_DIR/oauth/gmail.json` |
| **Telegram bot** | Stateful conversations: support draft, order lookup (`python -m ecom_ops.bot`) |
| **Deploy** | One-shot `bin/install-ubuntu26.sh` / `bin/install.sh` for Ubuntu 26/24 on Hetzner |
| **Docker** | `Dockerfile` + `docker-compose.prod.yml` image tag `azom-agent:2.0` |

## Entry points

```bash
python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
python -m ecom_ops.bot                    # Telegram long-poll
./bin/start-dashboard.sh                  # Flask dashboard
sudo bash bin/install.sh                  # full VPS install
```

## Dashboard routes

| Path | Who | Purpose |
|------|-----|---------|
| `/` | authenticated | Overview + telemetry |
| `/onboarding` | Jonatan/Oscar | Checklist + health probe + Gmail connect |
| `/settings` | Jonatan | Non-secret YAML (sites, limits, integrations flags) |
| `/secrets` | Jonatan | Present/missing secrets (no values) |
| `/oscar`, `/oscar/secrets`, `/oscar/escalations` | Oscar | Admin secrets + resolve tickets |
| `/oauth/gmail/start` | authenticated | Start Gmail OAuth (mock tokens when `AZOM_USE_MOCK=1`) |
| `/health` | public | Liveness for systemd/docker |

## Upgrade from 1.x

1. Pull `main` or re-run `sudo bash bin/install-ubuntu26.sh` (idempotent).
2. Ensure `.env` has `DASHBOARD_OSCAR_PASSWORD` if using Oscar UI.
3. Set `MAIL_OAUTH_CLIENT_ID` / `MAIL_OAUTH_CLIENT_SECRET` for Gmail consent.
4. Restart: `systemctl restart azom-dashboard azom-bot`

## Version alignment

| Artifact | Version |
|----------|---------|
| `pyproject.toml` / package | **2.0.0** |
| `ecom_ops.__version__` | **2.0.0** |
| Docker image tag | `azom-agent:2.0` |
| Skill metadata | 2.0.0 |

## Out of scope (V3)

- Multi-tenant SaaS control plane
- Outlook interactive OAuth browser flow
- Full LLM OpenRouter product-desc path as default
