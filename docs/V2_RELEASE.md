# Azom ecom-ops **v2.0.0**

Release notes for the V2 product line (dashboard onboarding, Gmail OAuth, Telegram OpenClaw hybrid, Cases 2.0, Path B rails, auto-install).

**Full map:** [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) · **Identity:** [`../SOUL.md`](../SOUL.md)

## What's in 2.0

| Area | Capability |
|------|------------|
| **Core ops (from V1)** | order-status, product-desc, support, SSH allowlist, mail (Gmail/Outlook/Graph/IMAP/POP3/SMTP) |
| **RBAC** | Jonatan viewer (+ CASE_REPLY), Oscar full_admin, agent operator + escalation tickets |
| **Dashboard** | Onboarding wizard, settings (Jonatan), secrets + escalations resolve (Oscar), interact draft, cases triage, ops polish (nav badges, probes) |
| **Gmail OAuth** | Browser consent flow → `AZOM_DATA_DIR/oauth/gmail.json` |
| **Telegram bot** | OpenClaw slash catalog + hybrid free-text (tool prefetch, suggest-approve NL, never silent send) |
| **Cases 2.0** | Mail→SQLite cases, 5 min poll, threading, mark_read, order-enriched drafts, approve/send |
| **Path B AI** | classify confidence, suggest-approve, richer order context in drafts; auto-send rails **default off** |
| **Ops hardening** | CSRF + salted passwords, smoke/readiness, schema migrate, mail provider split, CI ruff + cov≥65%, product-desc LLM path |
| **Deploy** | One-shot `bin/install-ubuntu26.sh` / `bin/install.sh` for Ubuntu 26/24 on Hetzner |
| **Docker** | `Dockerfile` + `docker-compose.prod.yml` image tag `azom-agent:2.0` |

## Entry points

```bash
python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops smoke [--live]
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
python -m ecom_ops --mock cases poll
python -m ecom_ops.bot                    # Telegram long-poll
./bin/start-dashboard.sh                  # Flask dashboard
./bin/cases-poll.sh
sudo bash bin/install.sh                  # full VPS install
```

## Dashboard routes

| Path | Who | Purpose |
|------|-----|---------|
| `/` | authenticated | Overview + telemetry + badges |
| `/onboarding` | Jonatan/Oscar | Checklist + health probe + Gmail connect |
| `/settings` | Jonatan | Non-secret YAML (sites, limits, integrations flags) |
| `/secrets` | Jonatan | Present/missing secrets (no values) |
| `/cases`, `/cases/<id>` | Jonatan/Oscar | Queue, draft, approve, close |
| `/oscar`, `/oscar/secrets`, `/oscar/escalations` | Oscar | Admin secrets + resolve tickets |
| `/oscar/secrets/test` | Oscar | Connection probes (Woo/mail/Telegram/OpenRouter/SSH/OAuth) |
| `/oauth/gmail/start` | authenticated | Start Gmail OAuth (mock tokens when `AZOM_USE_MOCK=1`) |
| `/health` | public | Liveness + readiness (last cases-poll age) |
| `/interact` | authenticated | Support draft playground |

## Telegram (OpenClaw)

See [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md).

Highlights: `/status` `/whoami` `/new` `/reset` `/tools` `/tasks` `/usage` `/model` `/cases` `/order` `/health` `/brief`; free-text uses read-only tools; approve via `/cases approve` or button only.

## Cases + Path B

See [`CASES.md`](CASES.md). Config: `config/cases_ai.yaml`, `config/mailboxes.yaml`.

## Upgrade from 1.x

1. Pull `main` or re-run `sudo bash bin/install-ubuntu26.sh` (idempotent).
2. Ensure `.env` has `DASHBOARD_OSCAR_PASSWORD` and `DASHBOARD_SECRET_KEY` if using browser POSTs.
3. Set `MAIL_OAUTH_CLIENT_ID` / `MAIL_OAUTH_CLIENT_SECRET` for Gmail consent.
4. Set `TELEGRAM_ALLOWED_CHAT_IDS` / `TELEGRAM_ACTOR_MAP` for production bot safety.
5. Restart: `systemctl restart azom-dashboard azom-bot azom-cases-poll.timer`

## Version alignment

| Artifact | Version |
|----------|---------|
| `pyproject.toml` / package | **2.0.0** |
| `ecom_ops.__version__` | **2.0.0** |
| Docker image tag | `azom-agent:2.0` |
| Skill metadata | 2.0.0 |

## Out of scope (V3 / later)

- Multi-tenant SaaS control plane
- Outlook interactive OAuth browser flow
- FAQ/KB, IMAP IDLE
- Default-on auto-send (rails only until Oscar experiment)
- GA4 / engagement product
