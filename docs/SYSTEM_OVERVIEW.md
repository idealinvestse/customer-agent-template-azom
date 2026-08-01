# System overview — AzomOps-Agent

**Purpose:** Full architecture map of the single-tenant Azom customer-ops agent: CLI, cases mail loop, Flask dashboard, Messenger/Telegram bots, Woo/WP integrations.  
**Audience:** Developers and coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Single-tenant** | One customer (Azom). No multi-tenant SaaS control plane (V3 deferred). |
| **AZOM_DATA_DIR** | Writable data root: DB, OAuth, secrets overlay, telemetry, poll markers. |
| **AZOM_CONFIG_DIR** | Config root (`config/*.yaml`); read-only in Docker. |
| **HITL** | Human-in-the-loop — customer mail send requires explicit approve. |
| **Fail-closed** | In live mode, empty Messenger/Telegram allowlist or actor map denies access. |

## 1. Architecture

```text
                    ┌─────────────────────┐
  Mail inboxes ────►│  cases poll (timer) │──► SQLite cases.db
                    │  classify + draft   │         │
                    └─────────────────────┘         │
                                                    ▼
  WooCommerce API ◄── order_context / order-status / product-desc
  WordPress API  ◄── wordpress client (posts/pages/media/users/settings)
  Woo Webhooks   ◄── /webhooks/woo (HMAC-SHA256) ── Dashboard route
                                                    │
  OpenRouter ────────── llm.py (drafts, chat, product-desc optional)
                                                    │
        ┌───────────────┬───────────────────────────┼──────────────┐
        ▼               ▼                           ▼              ▼
   CLI ecom_ops    Dashboard Flask         Messenger + Telegram   Escalations
   (operator)      (Jonatan / Oscar)       (shared BotHandler)     JSONL
```

| Layer | Path | Role |
|-------|------|------|
| Package | `skills/ecom_ops/` | Importable `ecom_ops` |
| Skill metadata | `skills/ecom-ops/SKILL.md` | Moss/agent skill card |
| Config (ro in Docker) | `config/*.yaml`, `customer.json` | sites, rbac, mailboxes, limits, cases_ai |
| Data (rw) | `AZOM_DATA_DIR` | cases.db, oauth, secrets.env, telemetry, probes |
| Dashboard | `infrastructure/dashboard/` | Flask UI + CSRF + webhooks + Oscar probes |
| Deploy | `bin/install*.sh`, `infrastructure/systemd/`, Docker | One-shot + services |

**Surface roles:**

| Surface | Role |
|---------|------|
| **Messenger** | Daily-driver ops chat for Jonatan |
| **Telegram** | Backup chat — same `BotHandler` brain |
| **Dashboard** | System of record / full power (edit, poll, bulk close, settings, secrets) |
| **CLI** | Automation and operator tooling |
| **Timers** | Cases poll every 5 minutes |

## 2. Actors and RBAC

| Actor | Role | Typical powers |
|-------|------|----------------|
| **Jonatan** | `viewer` (+ `CASE_REPLY`) | Read mail/SSH, non-secret settings, **approve/send case replies**, cases queue |
| **Oscar** | `full_admin` | Secrets UI, connection probes, resolve escalations, experiment flags (auto-send) |
| **agent** | `operator` | order-status, product-desc, support draft, mail send/read, SSH health, **cases poll** |

Config: `config/rbac.yaml`.

- Telegram: `TELEGRAM_ACTOR_MAP` maps chat → actor. Non-empty map ⇒ unmapped denied. Allowlist: `TELEGRAM_ALLOWED_CHAT_IDS`.
- Messenger: `MESSENGER_ACTOR_MAP` / `MESSENGER_ALLOWED_PSIDS` — **fail-closed when empty in live mode**.

## 3. Capabilities

| Capability | Module / entry | Notes |
|------------|----------------|-------|
| **order-status** | `actions/order_status` | Woo status update; validate order id/status |
| **product-desc** | `actions/product_desc` | Template default; optional OpenRouter |
| **support** | `actions/support` | Classify + draft; abuse → escalate |
| **mail** | `actions/mail` + `integrations/mail*` | See [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| **SSH** | `actions/ssh_ops` | Allowlist only; else Oscar ticket |
| **cases** | `cases/*` | Poll → draft → suggest-approve → human send — [`CASES.md`](CASES.md) |
| **LLM** | `llm.py` | OpenRouter + cost telemetry + cap |
| **OAuth Gmail** | `oauth/gmail` | Browser consent → `oauth/gmail.json` |
| **Telegram / Messenger** | `bot/*` | OpenClaw slash + hybrid free-text |
| **Woo / WP** | `integrations/woocommerce.py`, `wordpress.py` | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| **smoke / readiness** | `smoke.py`, `ops_status.py` | Opt-in live smoke; `/health` poll age |

## 4. Cases 2.0 + Path B (AI)

See [`CASES.md`](CASES.md) for the full operator guide (Swedish).

**Flow:** mailbox poll (5 min) → ingest/thread → hybrid classify + confidence → LLM/template draft (order-enriched) → optional `suggest_approve` → queue → Jonatan approve → SMTP/Graph reply with In-Reply-To.

| Status | Meaning |
|--------|---------|
| `open` | Needs attention |
| `escalated` | Oscar / high-touch |
| `sending` | Transient claim during approve-and-send |
| `replied` | Human-approved send done |
| `closed` | Closed without reply |

**Path B rails** (`config/cases_ai.yaml`):

- Suggest-approve for allowlisted categories + min confidence + order_id.
- Auto-send: **default off**; **not wired** into poll; kill-switch `AZOM_AUTO_SEND_KILL`.

## 5. Messenger and Telegram

See [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) and [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md). Identity: [`SOUL.md`](../SOUL.md).

1. Slash / postback commands — session, tools, cases, order, health, brief.
2. Free text — intent → **read-only tool prefetch** → LLM phrasing under SOUL-aligned prompt.
3. Explicit write UX — approve keyboard/postback, `/cases approve`, dashboard, CLI. **Never silent send.**

Messenger webhook: `GET|POST /webhooks/messenger` (HMAC + verify token; not Basic Auth).

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
| `/oauth/gmail/start`, `/status` | auth | Gmail OAuth start / status |
| `/oauth/gmail/callback` | public | Google redirect (OAuth `state` validated) |
| `/webhooks/messenger` | Meta | Messenger webhook |
| `/webhooks/woo` | Woo | HMAC-verified Woo webhooks |
| `/health` | public | Liveness + readiness (poll age) |
| `/ready` | public | 503 when cases poll stale |
| `/oscar/gdpr/export`, `/oscar/gdpr/delete` | Oscar | GDPR export / delete |
| `/cases/bulk-close` | POST auth | Bulk close (not bulk approve) |
| `/data/telemetry`, `/data/escalations` | auth | JSON data views |
| `/logs`, `/telemetry`, `/escalations`, `/manage` | auth | Ops pages |

Auth: Basic Auth usernames are hardcoded as `jonatan` / `oscar` (password or Werkzeug hash via `DASHBOARD_PASSWORD*` / `DASHBOARD_OSCAR_PASSWORD*`). `DASHBOARD_USER` in `.env` is documentation-only — code does not read it. CSRF on browser POSTs (`DASHBOARD_SECRET_KEY`).

Operator procedures: [`PILOT_OPS.md`](PILOT_OPS.md) (Swedish).

## 7. CLI map

Full reference: [`CLI_REFERENCE.md`](CLI_REFERENCE.md).

```bash
python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops smoke [--live]
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock product-desc --product-id 42 --language sv
python -m ecom_ops --mock support --message "Var är order 1001?"
python -m ecom_ops --mock mail send|fetch|reply ...
python -m ecom_ops --mock cases poll|list|show|reply|draft|regenerate|close|retention-purge ...
python -m ecom_ops kpis|classify-eval|draft-eval|drift-check|trends
python -m ecom_ops.bot
```

Global flags: `--mock`, `--actor`, `--site`.  
Oscar connection probes are dashboard-only (`/oscar/secrets/test`), not CLI.

## 8. Config and env

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
| `WOO_*`, `WP_*`, `MAIL_*`, `GRAPH_*`, `SSH_*` | Integrations |
| `OPENROUTER_API_KEY` | LLM |
| `TELEGRAM_*` | Bot allowlist + actor map |
| `MESSENGER_*` | Page token, HMAC secret, PSID allowlist/map |
| `AZOM_DASHBOARD_PUBLIC_URL` | Deep links from Messenger |
| `DASHBOARD_*` | Auth + bind |
| `AZOM_AUTO_SEND_KILL` | Force auto-send off |
| `AZOM_LIVE_SMOKE`, `AZOM_POLL_STALE_SEC` | Ops |
| `WOO_WEBHOOK_SECRET` | Inbound Woo webhook HMAC |

Prod paths (systemd): code `/opt/azom-agent`, data `/var/lib/azom`, logs `/var/log/azom`.  
Docker data path: `/app/.azom-data` (see [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md)).

## 9. Services (systemd)

| Unit | Purpose |
|------|---------|
| `azom-dashboard.service` | Flask 127.0.0.1:8080 (includes Messenger + Woo webhooks) |
| `azom-bot.service` | Telegram long-poll only (no Messenger unit) |
| `azom-cases-poll.timer` | Cases poll every 5 min |
| `azom-daily-brief.timer` | Daily KPI brief |
| `azom-backup.timer` | Data backup |
| `azom-retention-purge.timer` | GDPR retention purge |

Install: [`AUTO_INSTALL.md`](AUTO_INSTALL.md) · Hetzner: [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md) · Docker: [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md).

## 10. Data artifacts (`AZOM_DATA_DIR`)

| Artifact | Content |
|----------|---------|
| `cases.db` | Cases + messages (schema migrate) |
| `oauth/gmail.json` | Gmail OAuth tokens (mode 0600) |
| `secrets.env` | Oscar-written secrets overlay |
| `runtime.env` | Runtime toggles overlay |
| `escalations.jsonl` | Escalation tickets |
| telemetry / KPI files | Cost + case KPIs (`python -m ecom_ops kpis`) |
| `last_case_poll.json` | Poll readiness (`partial` / errors / age → `/health`) |
| `probe_last.json` | Last Oscar probe results |

## 11. Security model

1. Input validation (`security.py`) on order ids, emails, SSH commands.
2. Secret redaction in telemetry/escalation (includes WP + webhook secrets).
3. SSH allowlist; shell metacharacters rejected.
4. RBAC gates mutations and mail send.
5. Dashboard: Werkzeug password hashes preferred; CSRF; mock default passwords only with `AZOM_USE_MOCK=1`.
6. Telegram/Messenger allowlists + actor maps **fail-closed in live** when empty (or when map is set and chat/PSID is unmapped).
7. Config volume read-only in Docker; secrets only in data dir.
8. Messenger webhook: Meta verify + HMAC (not dashboard Basic Auth).

Compliance retention and DPIA notes: [`COMPLIANCE.md`](COMPLIANCE.md).

## 12. Testing and CI

```bash
pytest
# CI: ruff + pytest with coverage fail_under 65 (pyproject.toml)
bash tests/test_spinup.sh
```

Developer workflow: [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md).

## 13. Status snapshot

Authoritative detail: [`CURRENT_STATE.md`](CURRENT_STATE.md).

| Track | Status |
|-------|--------|
| V1 core ops | Shipped |
| V2.0 dashboard + OAuth + bots + install | Shipped |
| Cases 2.0 + Path B rails (auto-send off, not wired) | Shipped |
| V2.1 Woo/WP capacity | Shipped |
| V2.2 mail probe / env matrix / bulk close | Shipped |
| V2.3 robustness harden | Shipped (code) |
| Oscar A1 live soak | **Ops next — human gate** |
| FU9 auto-send wire | **Not wired** — see [`CASES.md`](CASES.md) |
| V3 multi-tenant / FAQ / GA4 | Deferred / parked |

## Related living docs

| Topic | Doc |
|-------|-----|
| Pilot ops (SV) | [`PILOT_OPS.md`](PILOT_OPS.md) |
| Cases (SV) | [`CASES.md`](CASES.md) |
| Mail (SV) | [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| Woo/WP (EN) | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| CLI (EN) | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
