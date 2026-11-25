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
| **Null-send** | Profile (`AZOM_NULL_SEND` / `--null-send`) that refuses customer mail and records FU9 shadow decisions. |
| **Shadow Live Ledger** | Poll-time FU9 would-have trail under null-send; dashboard badge + Oscar `cases shadow-report`. |

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
| Dashboard | `infrastructure/dashboard/` | Thin `app.py` factory + `auth.py` / `core_routes.py` / `cases_routes.py` / `oscar_routes.py` / `marketing_routes.py` / `oauth_routes.py` / `webhooks.py` / `health_routes.py` |
| Deploy | `bin/install*.sh`, `infrastructure/systemd/`, Docker | One-shot + services |

**Surface roles:**

| Surface | Role |
|---------|------|
| **Messenger** | Daily-driver ops chat for Jonatan |
| **Telegram** | Backup chat — same `BotHandler` brain |
| **Dashboard** | System of record / full power (edit, poll, bulk close, settings, secrets, marketing) |
| **CLI** | Automation and operator tooling |
| **Timers** | Cases poll every 5 minutes |

## 2. Actors and RBAC

Auth source of truth: `config/rbac.yaml` + `skills/ecom_ops/rbac.py`.  
`config/limits.yaml` → `jonatan_role` is **display-only** in the dashboard settings UI (not used for permission checks).

| Actor | Role | Typical powers |
|-------|------|----------------|
| **Jonatan** | `viewer` | Read + mail/SSH read + **CASE_REPLY** + **MARKETING_READ** + **MARKETING_SUGGEST** (negatives approve) |
| **Oscar** | `full_admin` | All permissions: secrets, probes, escalations, experiment flags, **MARKETING_MUTATE**, `cases shadow-report`, `retention-purge` |
| **agent** | `operator` | order/product/support, **MAIL_SEND** + **MAIL_READ**, **CASE_REPLY**, SSH read, cases poll, **MARKETING_READ** only |

- Telegram: `TELEGRAM_ACTOR_MAP` maps chat → actor. Non-empty map ⇒ unmapped denied. Allowlist: `TELEGRAM_ALLOWED_CHAT_IDS`.
- Messenger: `MESSENGER_ACTOR_MAP` / `MESSENGER_ALLOWED_PSIDS` — **fail-closed when empty in live mode**.
- Oscar-only CLI: `cases shadow-report` and `cases retention-purge` require `Permission.ADMIN`.

## 3. Capabilities

| Capability | Module / entry | Notes |
|------------|----------------|-------|
| **order-status** | `actions/order_status` | Woo status update; validate order id/status |
| **product-desc** | `actions/product_desc` | Template default; optional model router |
| **support** | `actions/support` | Classify + draft; abuse → escalate |
| **mail** | `actions/mail` + `integrations/mail*` | See [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| **SSH** | `actions/ssh_ops` | Exact read-only health allowlist; live disabled by default and requires a dedicated non-root `*-ssh-reader`, explicit key, pinned `known_hosts`, and `AZOM_SSH_READ_ENABLED=1`. Other commands are blocked/escalated. |
| **cases** | `cases/*` | Poll → draft → suggest-approve → human send — [`CASES.md`](CASES.md) |
| **Shadow Live Ledger** | `cases/shadow_report.py`, `runtime_profile.py` | Null-send refuse + FU9 shadow trail — not FU9 wire |
| **marketing** | `actions/marketing`, `marketing/*` | Google Ads + GA4 mock-first HITL — [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md) |
| **LLM** | `llm.py` + `model_router/` | Route order, ChatGPT OAuth, xAI, OpenRouter fallback — [`MODEL_ROUTER.md`](MODEL_ROUTER.md) |
| **OAuth Gmail** | `oauth/gmail` | Browser consent → `oauth/gmail.json` |
| **OAuth Google marketing** | `oauth/google_marketing` | Oscar-only start → `oauth/google_marketing.json` |
| **Telegram / Messenger** | `bot/*` | OpenClaw slash + hybrid free-text + `/marketing` read; Telegram also surfaces progress and streamed answer drafts |
| **Woo / WP** | `integrations/woocommerce.py`, `wordpress.py` | Content and store API — [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| **WordPress site ops** | `wp_ops/` | Mock health/backups, a separate theme/plugin file turn (windowed read, tree, literal search, named diff), a separate homepage source review (four site-bound reads; fixed `php -l` runs when PHP exists, while browser/runtime remain unavailable), local-tree staging only after a plan-id button, production deploy still `BackupNotVerified`, key-only read-only SFTP with no remote shell, and immutable non-executable update/restore previews — [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) |
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
- Path B2: richer return/billing drafts; still never ★ / never auto-send.
- Shadow Live Ledger: under `AZOM_NULL_SEND=1` / `--null-send`, poll records would-have FU9 decisions; customer mail refused. Default **off** (not set in systemd).

## 5. Messenger and Telegram

See [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) and [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md). Identity: [`SOUL.md`](../SOUL.md).

1. Slash / postback commands — session, tools, cases, order, health, brief, marketing (read-only).
2. Free text — intent → read tools and confirm-only write proposals → SOUL-aligned LLM response. WordPress drift uses a named-tool loop bound to the selected site (3 calls / 30 seconds). Theme and plugin files use a separate turn (4 reads, 1 search, 8 calls, 45 seconds) that can propose a diff and cannot write it. “Granska startsidans källkod” is a third turn: four site-bound homepage steps, no order tools, and no order digest. A missing PHP binary leaves `php_lint` unavailable; browser and sandbox remain unavailable until their runtimes run.
3. Explicit write UX — confirm button/postback, `/cases approve`, dashboard, CLI. **Never silent send.** WordPress update/restore plans are previews only and cannot execute.

**Telegram background jobs:** A durable inbox acknowledges updates before moving the poll cursor. A bounded worker pool runs up to 3 concurrent read tasks (`task_id`) per chat while serializing approved write actions per chat and via SQLite `bot_resource_locks`. `/status`, `/jobs`, `/steer`, `/task`, `/stop`, `/cancel`, and Oscar-only `/reconcile` stay responsive while work runs. Hybrid routing (`steer_router.py`) steers a specific task on Telegram reply (or `/steer`) and classifies unthreaded follow-ups into steer vs. new parallel task; write proposals use durable single-use `bot_proposals` (`proposal:yes:pr-<id>`) and atomic `append_turn_atomic` session merges. Telegram conversation state migrates from JSON into the job database. Messenger remains on its existing synchronous webhook and store.

**Telegram progress UX:** Text turns, slash commands, and approved actions emit structured `ProgressEvent` updates rendered in-place by `LiveStatusBoard` (step progress, elapsed time, provenance, sanitized outcome, and periodic 8s heartbeat with model-router phase labels), threaded via `reply_to_message_id` and governed by `PerChatRateLimiter` (handling HTTP 400 not-modified and HTTP 429 `retry_after`). Model text streams into an editable answer draft (with chunk 2+ buffered during parallel runs until `finish()`). Messenger does not pass these callbacks and remains final-response only. This is Azom's Bot API implementation of the progress/edit pattern; it is separate from OpenClaw's native stream mode and never exposes private reasoning, PII, or tool arguments.

Messenger webhook: `GET|POST /webhooks/messenger` (HMAC + verify token; not Basic Auth).

## 6. Dashboard routes

| Path | Who | Purpose |
|------|-----|---------|
| `/` | auth | Overview, nav badges, probe cache |
| `/onboarding` | J/O | Checklist, health, Gmail connect |
| `/onboarding/status` | auth | Alpine live JSON |
| `/settings` | Jonatan | Non-secret YAML |
| `/secrets` | Jonatan | Present/missing (no values) |
| `/cases`, `/cases/<id>` | J/O | Queue, draft save, approve, close (+ shadow badges) |
| `/cases/poll` | POST | Manual poll |
| `/marketing` | J/O | Ads+GA4 digest + suggest HITL |
| `/wp`, `/wp/<id>` | `WP_OPS_READ` | WordPress site status. Unknown and mock-verified backups are not green |
| `GET /wp/<id>/ssh` | `WP_OPS_READ` | Site root and SSH login. The private key is not shown. A viewer does not see the identity path |
| `POST /wp/<id>/ssh` | `WP_OPS_ADMIN` | Save root, host, user, port, key, and a public host key. Does not open SSH |
| `POST /wp/<id>/check` | operate / admin | Mock read-only check |
| `POST /wp/<id>/backup` | operate / admin | Local mock backup manifest. No production restore route |
| `POST /wp/<id>/restore-drill` | `WP_OPS_OPERATE` | Staging drill of a verified manifest |
| `GET /wp/<id>/files/plan/<plan_id>` | `WP_OPS_ADMIN` and `CODE_EDIT` | Review a stored theme/plugin diff. No write |
| `POST /wp/<id>/files/deploy` | `WP_OPS_ADMIN` and `CODE_EDIT` | CSRF POST. Returns `BackupNotVerified` and does not write |
| `/marketing/suggests/*` | POST J/O | Build / deny / approve suggests |
| `/interact` | auth | Support draft playground |
| `/oscar`, `/oscar/secrets`, `/oscar/escalations` | Oscar | Admin + resolve |
| `/oscar/models/settings` | Oscar | Save model ids and Grok Build flag |
| `/oscar/behavior` | Oscar | Edit chat, classify, draft, and product-copy prompts; chat runtime policy remains fixed |
| `/oscar/telegram` | Oscar | Telegram process status. Allowlist stays in the process env |
| `/oscar/secrets/test` | Oscar | Connection probes (incl. ga4 / google_ads / merchant) |
| `/oauth/gmail/start`, `/status` | auth | Gmail OAuth start / status |
| `/oauth/gmail/callback` | public | Google redirect (OAuth `state` validated) |
| `/oauth/google/start`, `/status` | Oscar | Marketing Google OAuth start / status |
| `/oauth/google/callback` | public | Marketing Google redirect |
| `/webhooks/messenger` | Meta | Messenger webhook |
| `/webhooks/woo` | Woo | HMAC-verified Woo webhooks |
| `/health` | public | Liveness + readiness (poll age) |
| `/ready` | public | 503 when cases poll stale |
| `/oscar/gdpr/export`, `/oscar/gdpr/delete` | Oscar | GDPR export / delete |
| `/cases/bulk-close` | POST auth | Bulk close (not bulk approve) |
| `/data/telemetry`, `/data/audit`, `/data/escalations` | auth | Usage rows, audit history, escalations |
| `/logs`, `/telemetry`, `/escalations`, `/manage` | auth | Ops pages |

Auth: Basic Auth usernames come from `config/profile.yaml` actors (Azom defaults `jonatan` / `oscar`). Passwords or Werkzeug hashes via `DASHBOARD_PASSWORD*` / `DASHBOARD_OSCAR_PASSWORD*`. `DASHBOARD_USER` in `.env` is documentation-only — code does not read it. CSRF on browser POSTs (`DASHBOARD_SECRET_KEY`). `/metrics` is **token-only** (`METRICS_SCRAPE_TOKEN` Bearer). `/live` is liveness; `/ready` is 503 when cases poll is stale.

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
python -m ecom_ops --mock [--null-send] cases poll|list|show|reply|draft|regenerate|close ...
python -m ecom_ops --actor oscar cases shadow-report [--days 7]
python -m ecom_ops --actor oscar cases retention-purge [--dry-run]
python -m ecom_ops --mock marketing digest|health|suggests|…
python -m ecom_ops --mock wp sites|status|show|check|backup|backups|restore-drill
python -m ecom_ops kpis|classify-eval|draft-eval|drift-check|trends|soak-preflight
python -m ecom_ops.bot
```

Global flags: `--mock`, `--null-send`, `--actor`, `--site`.  
`shadow-report` / `retention-purge` require Oscar (`ADMIN`).  
Oscar connection probes are dashboard-only (`/oscar/secrets/test`), not CLI.

## 8. Config and env

| File | Purpose |
|------|---------|
| `config/sites.yaml` | customer, domains |
| `config/rbac.yaml` | roles + escalation targets |
| `config/mailboxes.yaml` | case ingest mailboxes |
| `config/limits.yaml` | OpenRouter cap |
| `config/cases_ai.yaml` | suggest-approve + auto-send rails |
| `config/marketing.yaml` | Ads/GA4 allowlists, mutate defaults, kill env names |
| `config/wp_sites.yaml` | Live Azom production target; mock mode uses `config/wp_sites.mock.yaml`. Secret refs only |
| `config/integrations.yaml` | mail presets; Google flags are **non-gating reserved** |
| `config/dashboard.yaml` | reserved UI flags — **not loaded** by Python |
| `config/customer.json` | customer metadata / KPIs |
| `.env` / `.env.example` | secrets + runtime paths |

| Env (highlights) | Purpose |
|------------------|---------|
| `AZOM_USE_MOCK` | Mock all integrations |
| `AZOM_CONFIG_DIR` / `AZOM_DATA_DIR` | Paths |
| `WOO_*`, `WP_*`, `MAIL_*`, `GRAPH_*`, `SSH_*` | Integrations. `WP_USERNAME` / `WP_APP_PASSWORD` are the content client, not per-site ops |
| `WP_SECRET_*` | One application-password variable per registered WordPress site |
| `AZOM_MODEL_ROUTE_ORDER` | Model route order (default chatgpt, Grok Build, xAI key, OpenRouter) |
| `OPENAI_SIWC_*` / `OPENAI_MODEL` | ChatGPT OAuth (Azom-owned refresh). Endpoints are env, not hardcoded |
| `AZOM_XAI_GROK_BUILD` / `XAI_GROK_BIN` / `XAI_MODEL` | Delegated Grok Build route (opt-in) |
| `AZOM_TELEGRAM_MODEL_ROUTE` | Optional Telegram-only route id. No fall-through |
| `XAI_API_KEY` | xAI API-key fallback |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | OpenRouter fallback. Dollar cap applies here only |
| `TELEGRAM_*` | Bot allowlist + actor map |
| `MESSENGER_*` | Page token, HMAC secret, PSID allowlist/map |
| `AZOM_DASHBOARD_PUBLIC_URL` | Deep links from Messenger |
| `DASHBOARD_*` | Auth + bind |
| `AZOM_AUTO_SEND_KILL` | Force auto-send off |
| `AZOM_NULL_SEND` | Null-send / shadow profile (default off) |
| `AZOM_GA4_PROPERTY_IDS` / `AZOM_GADS_CUSTOMER_IDS` | Marketing fail-closed allowlists |
| `AZOM_ADS_MUTATE_KILL` / `AZOM_GA_MUTATE_KILL` / `AZOM_MP_KILL` | Marketing mutate kills (`GA` reserved until admin mutate exists) |
| `AZOM_LIVE_SMOKE`, `AZOM_POLL_STALE_SEC` | Ops |
| `METRICS_SCRAPE_TOKEN` | Required Bearer for `/metrics` (empty = 403) |
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
| `cases.db` | Cases + messages + shadow decision columns (SQLite WAL + busy_timeout) |
| `oauth/gmail.json` | Gmail OAuth tokens (mode 0600) |
| `oauth/google_marketing.json` | Google Ads/GA4 OAuth tokens (Oscar) |
| `secrets.env` | Oscar-written secrets overlay |
| `runtime.env` | Runtime toggles overlay |
| `escalations.jsonl` | Escalation tickets |
| `marketing_suggests.jsonl` | Marketing suggest ledger |
| telemetry / KPI files | Cost + case KPIs (`python -m ecom_ops kpis`) |
| `last_case_poll.json` | Poll readiness (`partial` / errors / age → `/health`) |
| `probe_last.json` | Last Oscar probe results |

## 11. Security model

1. Input validation (`security.py`) on order ids, emails, SSH commands.
2. Secret redaction in telemetry/escalation (includes WP + webhook secrets).
3. SSH allowlist; shell metacharacters rejected. Live additionally requires an explicit host pin, a per-process enable, a dedicated non-root account, and an explicit key; system SSH config and agent identities are disabled.
4. RBAC gates mutations and mail send.
5. Dashboard: Werkzeug password hashes preferred; CSRF; mock default passwords only with `AZOM_USE_MOCK=1`.
6. Telegram/Messenger allowlists + actor maps **fail-closed in live** when empty (or when map is set and chat/PSID is unmapped).
7. Config volume read-only in Docker; secrets only in data dir.
8. Messenger webhook: Meta verify + HMAC (not dashboard Basic Auth).

Compliance retention and DPIA notes: [`COMPLIANCE.md`](COMPLIANCE.md).

## 12. Testing and CI

```bash
pytest
# CI: ruff + pytest with coverage fail_under 70 (pyproject.toml)
bash tests/test_spinup.sh
```

Developer workflow: [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md).

## 13. Status snapshot

Authoritative detail: [`CURRENT_STATE.md`](CURRENT_STATE.md). Per-surface code and ops scores: [`MATURITY.md`](MATURITY.md).

| Track | Status |
|-------|--------|
| V1 core ops | Shipped |
| V2.0 dashboard + OAuth + bots + install | Shipped |
| Cases 2.0 + Path B rails (auto-send off, not wired) | Shipped |
| V2.1 Woo/WP capacity | Shipped |
| V2.2 mail probe / env matrix / bulk close | Shipped |
| V2.3 robustness harden | Shipped (code) |
| Path B2 return/billing drafts | Shipped (never ★) |
| Shadow Live Ledger (null-send) | Shipped — soft-soak; not A1; not FU9 wire |
| Marketing Google (Ads+GA4) | Mock-first rails shipped; live **reads** (GA4/Ads REST) — mutate/MP still refused — [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md) |
| Oscar A1 live soak | **Ops next — human gate** |
| FU9 auto-send wire | **Not wired** — see [`CASES.md`](CASES.md) |
| V3.0.0 production maturity + template profile | Shipped (code) — soak/NO-DK/mutate still human-gated |
| Multi-tenant SaaS / FAQ / Meta ads | Deferred / parked |

## Related living docs

| Topic | Doc |
|-------|-----|
| Maturity scores (EN) | [`MATURITY.md`](MATURITY.md) |
| Pilot ops (SV) | [`PILOT_OPS.md`](PILOT_OPS.md) |
| Cases (SV) | [`CASES.md`](CASES.md) |
| Mail (SV) | [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| Marketing Google (EN) | [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md) |
| Woo/WP (EN) | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| CLI (EN) | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
