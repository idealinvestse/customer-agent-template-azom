# System overview — AzomOps-Agent v2.0

Single-tenant WooCommerce customer-ops agent: CLI + Cases mail loop + Flask dashboard + OpenClaw-style Telegram bot. Primary runtime: Ubuntu 24/26 on Hetzner CX22/CPX21.

Related identity: [`SOUL.md`](../SOUL.md) · agent notes: [`AGENTS.md`](../AGENTS.md) · skill card: [`skills/ecom-ops/SKILL.md`](../skills/ecom-ops/SKILL.md)

---

## 1. Architecture

```text
                    ┌─────────────────────┐
  Mail inboxes ────►│  cases poll (timer) │──► SQLite cases.db
                    │  classify + draft   │         │
                    └─────────────────────┘         │
                                                    ▼
  WooCommerce API ◄── order_context / order-status / product-desc
                                                    │
  OpenRouter ────────── llm.py (drafts, chat, product-desc optional)
                                                    │
        ┌───────────────┬───────────────────────────┼──────────────┐
        ▼               ▼                           ▼              ▼
   CLI ecom_ops    Dashboard Flask              Telegram bot    Escalations
   (operator)      (Jonatan / Oscar)            (OpenClaw hybrid)  JSONL
```

| Layer | Path | Role |
|-------|------|------|
| Package | `skills/ecom_ops/` | Importable `ecom_ops` (actions, cases, bot, integrations, llm) |
| Skill metadata | `skills/ecom-ops/SKILL.md` | Moss/agent skill card |
| Config (ro in Docker) | `config/*.yaml`, `customer.json` | sites, rbac, mailboxes, limits, cases_ai, integrations |
| Data (rw) | `AZOM_DATA_DIR` | cases.db, oauth, secrets.env, telemetry, escalations, probes |
| Dashboard | `infrastructure/dashboard/` | Flask UI + CSRF + Oscar probes |
| Deploy | `bin/install*.sh`, `infrastructure/systemd/`, Docker | One-shot + services |

---

## 2. Actors & RBAC

| Actor | Role | Typical powers |
|-------|------|----------------|
| **Jonatan** | `viewer` (+ `CASE_REPLY`) | Read mail/SSH, non-secret settings, **approve/send case replies**, cases queue |
| **Oscar** | `full_admin` | Secrets UI, connection probes, resolve escalations, experiment flags (auto-send) |
| **agent** | `operator` | order-status, product-desc, support draft, mail send/read, SSH health, **cases poll** |

Config: `config/rbac.yaml`. Telegram maps chat → actor via `TELEGRAM_ACTOR_MAP` (default unmapped → `jonatan`). Allowlist: `TELEGRAM_ALLOWED_CHAT_IDS`.

---

## 3. Capabilities

| Capability | Module / entry | Notes |
|------------|----------------|-------|
| **order-status** | `actions/order_status` | Woo status update; validate order id/status |
| **product-desc** | `actions/product_desc` | Template default; optional OpenRouter |
| **support** | `actions/support` | Classify + draft; abuse → escalate |
| **mail** | `actions/mail` + `integrations/mail*` | gmail / outlook / exchange_graph / IMAP / POP3 |
| **SSH** | `actions/ssh_ops` | Allowlist only; else Oscar ticket |
| **cases** | `cases/*` | Poll → thread → draft → suggest-approve badge → human send |
| **LLM** | `llm.py` | OpenRouter + cost telemetry + cap |
| **OAuth Gmail** | `oauth/gmail` | Browser consent → `oauth/gmail.json` |
| **Telegram** | `bot/*` | OpenClaw slash + hybrid free-text |
| **smoke / readiness** | `smoke.py`, `ops_status.py` | Opt-in live smoke; `/health` poll age |

---

## 4. Cases 2.0 + Path B (AI)

See also [`docs/CASES.md`](CASES.md).

**Flow:** mailbox poll (5 min) → ingest/thread → hybrid classify + confidence → LLM/template draft (order-enriched) → optional `suggest_approve` → queue → Jonatan approve → SMTP/Graph reply with In-Reply-To.

| Status | Meaning |
|--------|---------|
| `open` | Needs attention |
| `escalated` | Oscar / high-touch |
| `replied` | Human-approved send done |
| `closed` | Closed without reply |

**Path B rails** (`config/cases_ai.yaml`):

- Suggest-approve for allowlisted categories + min confidence + order_id.
- Auto-send: **default off**; kill-switch `AZOM_AUTO_SEND_KILL`; daily cap; never abuse/return/billing.

---

## 5. Telegram (OpenClaw hybrid)

See [`docs/TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) and root [`SOUL.md`](../SOUL.md).

1. Slash commands (`openclaw_commands.py`) — session, tools, cases, order, health, brief.
2. Free text (`chat_agent.py`) — intent → **read-only tool prefetch** → LLM phrasing under system prompt aligned with SOUL.
3. Explicit write UX — approve keyboard, `/cases approve`, escalate confirm. **Never silent send.**

---

## 6. Dashboard routes

| Path | Who | Purpose |
|------|-----|---------|
| `/` | auth | Overview, nav badges, probe cache |
| `/onboarding` | J/O | Checklist, health, Gmail connect |
| `/onboarding/status` | auth | Alpine live JSON |
| `/settings` | Jonatan | Non-secret YAML |
| `/secrets` | Jonatan | Present/missing (no values) |
| `/cases`, `/cases/<id>` | J/O | Queue, draft save, approve, close |
| `/cases/poll` | POST | Manual poll |
| `/interact` | auth | Support draft playground |
| `/oscar`, `/oscar/secrets`, `/oscar/escalations` | Oscar | Admin + resolve |
| `/oscar/secrets/test` | Oscar | Connection probes |
| `/oauth/gmail/*` | auth | Gmail OAuth |
| `/health` | public | Liveness + readiness (poll age) |
| `/data/telemetry`, `/data/escalations` | auth | JSON data views |
| `/logs`, `/telemetry`, `/escalations`, `/manage` | auth | Ops pages |

Auth: Basic (`jonatan` / `oscar` passwords or Werkzeug hashes). CSRF on browser POSTs (`DASHBOARD_SECRET_KEY`).

---

## 7. CLI map

```bash
python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops smoke [--live]          # needs AZOM_LIVE_SMOKE=1 or --live
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock product-desc --product-id 42 --language sv
python -m ecom_ops --mock support --message "Var är order 1001?"
python -m ecom_ops --mock mail send|fetch|reply ...
python -m ecom_ops --mock cases poll|list|show|reply|draft|close ...
python -m ecom_ops.bot                     # Telegram long-poll
```

Global flags: `--mock`, `--actor`, `--site`.

---

## 8. Config & env

| File | Purpose |
|------|---------|
| `config/sites.yaml` | customer, domains |
| `config/rbac.yaml` | roles + escalation targets |
| `config/mailboxes.yaml` | case ingest mailboxes |
| `config/limits.yaml` | OpenRouter cap |
| `config/cases_ai.yaml` | suggest-approve + auto-send rails |
| `config/integrations.yaml` | mail provider presets / flags |
| `config/dashboard.yaml` | dashboard feature flags |
| `config/customer.json` | customer metadata / KPIs |
| `.env` / `.env.example` | secrets + runtime paths |

| Env (highlights) | Purpose |
|------------------|---------|
| `AZOM_USE_MOCK` | Mock all integrations |
| `AZOM_CONFIG_DIR` / `AZOM_DATA_DIR` | Paths |
| `WOO_*`, `MAIL_*`, `GRAPH_*`, `SSH_*` | Integrations |
| `OPENROUTER_API_KEY` | LLM |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ACTOR_MAP` | Bot |
| `DASHBOARD_*` | Auth + bind |
| `AZOM_AUTO_SEND_KILL` | Force auto-send off |
| `AZOM_LIVE_SMOKE`, `AZOM_POLL_STALE_SEC` | Ops |

Prod paths: code `/opt/azom-agent`, data `/var/lib/azom`, logs `/var/log/azom`.

---

## 9. Services (systemd)

| Unit | Purpose |
|------|---------|
| `azom-dashboard.service` | Flask 127.0.0.1:8080 |
| `azom-bot.service` | Telegram long-poll |
| `azom-cases-poll.timer` | Cases poll every 5 min |
| `azom-daily-brief.timer` | Daily KPI brief |

Install: [`docs/AUTO_INSTALL.md`](AUTO_INSTALL.md) · Hetzner: [`docs/DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md) · Docker: [`docs/DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md).

---

## 10. Data artifacts (`AZOM_DATA_DIR`)

| Artifact | Content |
|----------|---------|
| `cases.db` | Cases + messages (schema migrate) |
| `oauth/gmail.json` | Gmail OAuth tokens (0600) |
| `secrets.env` | Oscar-written secrets overlay |
| `runtime.env` | Runtime toggles overlay |
| `escalations.jsonl` | Escalation tickets |
| telemetry / KPI files | Cost + case KPIs |
| `probe_last.json` | Last Oscar probe results |
| cases poll marker | Readiness input for `/health` |

---

## 11. Security model

1. Input validation (`security.py`) on order ids, emails, SSH commands.
2. Secret redaction in telemetry/escalation.
3. SSH allowlist; shell metacharacters rejected.
4. RBAC gates mutations and mail send.
5. Dashboard: Werkzeug password hashes preferred; CSRF; mock default passwords only with `AZOM_USE_MOCK=1`.
6. Telegram chat allowlist strongly recommended in prod.
7. Config volume ro in Docker; secrets only in data dir.

---

## 12. Testing & CI

```bash
pytest
# CI: ruff + pytest with coverage fail_under 65 (pyproject.toml)
bash tests/test_spinup.sh   # optional spinup smoke
```

Key test modules: cases v2, suggest-approve, auto-send rails, chat_agent, openclaw/telegram, dashboard auth, mail, oauth, smoke/readiness, product-desc LLM.

---

## 13. Roadmap snapshot

| Track | Status |
|-------|--------|
| V1 core ops | ✅ |
| V2.0 dashboard + OAuth + bot + install | ✅ |
| Cases 2.0 MVP | ✅ |
| Ops hardening P6–P10 | ✅ |
| Path B suggest-approve + auto-send rails (default off) | ✅ shipped on main |
| Telegram hybrid dialog vNext (tool prefetch, NL suggest) | ✅ |
| Finish plan (regenerate, baseline, measure, calibrate) | 📋 `docs/DEVELOPMENT_PLAN_FINISH.md` |
| Support-time baseline capture | Parallel / required for 50% claim |
| GA4 / engagement | Parked |
| V3 multi-tenant SaaS | Deferred |
| Auto-send live experiment | Oscar-only after finish-plan preconditions |

Decision history: `docs/ideation/`, plans under `docs/superpowers/`. **Next execution:** FU1 regenerate in `DEVELOPMENT_PLAN_FINISH.md`.
