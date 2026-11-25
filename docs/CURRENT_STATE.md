# Current state — AzomOps-Agent

**Purpose:** Single source of truth for package version, what is shipped, what is blocked on humans, and what is out of scope.  
**Audience:** Coding agents and developers. Operators may skim the “Ops next” section.  
**Read this first:** [`DOC_STYLE.md`](DOC_STYLE.md), then [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Package version** | Python package version in `pyproject.toml` — currently **3.0.0**. |
| **Path B** | Cases AI quality: suggest-approve badge + auto-send **rails** (default off, not wired to poll send). |
| **Live soak (A1 / FU6)** | Human-run checklist on a real host before any auto-send discussion. |
| **FU9** | Auto-send wire into poll. **Not done.** Rails and kill-switch exist only. |
| **Fail-closed** | In live mode (`AZOM_USE_MOCK=0`), empty Messenger/Telegram allowlist **or** actor map denies access; unmapped ids denied when map is set. |

## Source of truth order

1. This file (`docs/CURRENT_STATE.md`)
2. `AGENTS.md`
3. Code and tests
4. Chat memory (ignore if conflicting)

## Package and runtime

| Item | Value |
|------|--------|
| Package | **3.0.0** (`pyproject.toml` and `ecom_ops.__version__` — keep in sync) |
| Docker image tag | `azom-agent:3.0` (`infrastructure/docker-compose.prod.yml`; `2.0` tag retained for rollback) |
| Primary OS | Ubuntu 26.x (24.04 LTS supported) |
| Host sizing | Hetzner CX22 / CPX21 — 2 vCPU, 4 GB RAM |
| Prod code (systemd) | `/opt/azom-agent` |
| Prod data (systemd) | `/var/lib/azom` (`AZOM_DATA_DIR`) |
| Prod logs (systemd) | `/var/log/azom` |
| Prod env (systemd) | `/opt/azom-agent/.env` with `AZOM_USE_MOCK=0` |
| Docker data | `/app/.azom-data` (compose volume; not the systemd path) |

## Shipped (code on main)

Treat these as done in the repository. Do not re-implement from scratch.

| Track | What shipped |
|-------|----------------|
| **V1 core** | order-status, product-desc, support classify/draft, SSH health allowlist (live off by default; pinned host and dedicated non-root reader required), mail providers (gmail, outlook, exchange_graph, generic_imap, generic_pop3) |
| **V2.0** | Dashboard onboarding/settings/cases/Oscar, Gmail OAuth browser consent, Telegram OpenClaw hybrid bot, Meta Messenger webhook + shared bot brain, one-shot Ubuntu install + systemd + Docker `azom-agent:2.0`, CLI `version` / `status` / `smoke` |
| **Cases 2.0 + Path B** | Poll → thread → classify → order-enriched draft → suggest-approve → human approve → threaded send; auto-send rails default off + `AZOM_AUTO_SEND_KILL` |
| **Sprint A/B/C + SB5** | Approve-flow, ★ measure, order extract/email lookup, richer context, classify fixtures, poll partial rails, Telegram/Messenger actor fail-closed, soft draft without order_id, regenerate draft (never sends) |
| **V2.1 Woo/WP** | Shipment trackings API, multi-site `domain=`, WordPress client, transport retry/session, webhooks HMAC, pagination iterators, extra Woo endpoints, system status, WP probes + secret redaction — see [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| **V2.2 mail ops** | Live `probe_mail`, mail env matrix / per-mailbox `env_prefix`, bulk close (not bulk approve) |
| **V2.3 robustness** | Thread reopen, OAuth expiry harden, probe fail-closed; code DoD green |
| **Path B2** | Richer return/billing drafts + priority/UI escalate hints; still never suggest-approve those categories |
| **Shadow Live Ledger** | Null-send profile (`AZOM_NULL_SEND` / `--null-send`): refuse customer mail; poll records FU9 shadow decisions; dashboard badge + `cases shadow-report`. Soft-soak via `bin/mock-soak-azom.sh`. **Not** FU9 wire; **not** A1 soak complete |
| **Marketing Google (Ads+GA4)** | Mock-first ledger + HITL rails. Live **read** clients (GA4 runReport + Ads GAQL) via REST + OAuth; optional `[marketing-live]` extra. Mutate / MP / Merchant writes still refused. Fail-closed allowlists unchanged. See [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md). |
| **Pilot maturity toolkit** | Read-only `soak-preflight`, Oscar `cases calibration-export` / `calibration-report` (redacted), `kpis --baseline` deltas, shadow suggest-vs-outcome cross-tab. **Does not** mark A1 soak, baseline, or live calibration complete. |
| **Ops hardening** | SQLite WAL + busy_timeout, budget pacing fields, structured poll/approve logs, `/health` extras (`null_send`, `budget_ratio`, `last_poll_errors`). Shared allowlisted runtime overlays (CLI/poll/bot/dashboard). `/metrics` token-only. Waitress in prod. Case send/close state machine + replied retention. Backup script checkpoints WAL before copy. |
| **V3.0.0** | Pilot toolkit + ops hardening + NO/DK enable-ready (still disabled) + live marketing **reads** + dashboard health/webhook modules + cases approve/ingest/draft facades + `config/profile.yaml` + [`TEMPLATE_GUIDE.md`](TEMPLATE_GUIDE.md). **Not** multi-tenant SaaS. **Not** A1 soak complete. **Not** FU9 wire. |
| **Model router** | `skills/ecom_ops/model_router/` + `llm.py` facade + `config/model_profiles.yaml`. Order: ChatGPT OAuth (Azom-owned refresh; `is_configured()` requires session when SIWC env empty), opt-in xAI Grok Build (delegated), opt-in xAI API key (`estimated_external` cost, not counted in OpenRouter cap), OpenRouter fallback (per-model pricing + `usage.cost`). 7 task-scoped profiles (`cases_classify`, `cases_draft`, `product_copy`, `chat_fast_tools`, `chat_general`, `wp_tool_router`, `wp_code_deep`) with mtime-reloaded `model_profiles.override.yaml`, `RouteCapabilities`, cross-process circuit breaker (`model_router_health.json`), and honest failure telemetry (exact numeric `prompt_tokens`/`completion_tokens`/`total_tokens` preserved). Oscar `/oscar` mounts/tests slots with **no fallback**, resets breaker, edits versioned `chat`, `classify`, `draft`, `product_desc`, and `wp_coder` prompts at `/oscar/behavior`; chat appends `WP_TOOL_GUIDE` / `WP_DRIFT_TOOL_GUIDE` conditionally and keeps static-prefix prompt caching. Usage filters live at `/data/telemetry`. Action history lives at `/data/audit`. Direct app-owned xAI OAuth is **not** implemented. ChatGPT `id_token` signature is **not** verified. Live SIWC connect is an Oscar step, not done. See [`MODEL_ROUTER.md`](MODEL_ROUTER.md). |
| **WordPress site ops (Phase 1–2 + bot previews)** | Live registry `config/wp_sites.yaml` targets `azom.no` production (with optional `woo_version` on `Site`); mock mode uses `config/wp_sites.mock.yaml`. Site-specific `WP_SECRET_*` refs hold names only (`user:app_password` binds per-site REST reads when live reads are enabled). Health checks and backup manifests remain mock/local; a verified manifest is not off-site, encrypted, or green. The Telegram bot has bounded site-bound drift reads (3 calls, 30 seconds, 4 KB per result / 10 KB total). A separate file/workspace turn (16 calls, 10 reads, 5 searches, 60 seconds, 16 KiB / 48 KiB, with drift read-tool interop and `_finish_on_budget()` summary) covers `ALL_FILE_TOOLS`, line-range reads (`start_line`/`end_line`) in `wp_read_theme_plugin_file`, literal/regex/`context_lines`/`plan_id` search in `wp_search_component`, `_` in component slugs, and a read-only third-party plugin policy (extend via `azom-site-<site_id>`), plus `wp_propose_file_diff` / `wp_propose_new_file` storing hashes in state and diffs outside it. When a real sandbox run creates PNGs, `wp_workspace_run_checks` attaches allowlisted screenshots (`BotReply.attachments` + `safe_workspace_attachments()` → Telegram `_send_photo` / `sendPhoto`) without passing absolute paths to LLM messages. Staging apply writes only a local mock tree after a plan-id button (`wpfile:stage:fe-...` or `wpreview:stage:wr-...`). The production review POST returns `BackupNotVerified` and does not write. Live file access is SFTP-only, key-only, host-pinned, capped (64 KiB / 256 entries, with `open_batch()` session reuse), and read-only; it requires a dedicated restricted `*-wp-reader` account and remains disabled until `AZOM_WP_FILES_SSH=1` is explicitly enabled. WordPress SFTP never opens a remote shell or writes live files. `restore()` remains `RestoreNotInMvp`; update/restore plans are immutable, preview-only records with no execution path. A page source review (`granska startsidans källkod`, or a named page via `target_page` from `"granska sidan <slug|id> källkod"`) is a separate site-bound turn with four steps: front page/target page + template hierarchy (with block-theme `WordPressClient.list_templates()` for `site_editor`), editor raw content, theme/plugin source, and browser preview. It does not call order tools or attach an order digest. Database content, source files, and rendered DOM stay separate. `php_lint` runs fixed `php -l` when PHP exists; `run_catalog` leaves unavailable checks (`wpcs_php`, `plugin_check`, etc.) `unavailable`, and browser/sandbox stay `not_run/unavailable` until their runtimes actually run. `AZOM_WP_SANDBOX=0` stays the default. Preview and local staging do not open on a stub result. Checksum, updates, scanners, live health checks, production deploy, and production restore are **not** implemented. Not managed hosting. Not Phase 4. See [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md). |
| **WordPress theme/plugin toolbox (Etapp 4T)** | `ALL_FILE_TOOLS` in `skills/ecom_ops/wp_ops/agent_tools.py` adds 15 `EXTRA_FILE_TOOLS` (`wp_list_workspaces`, `wp_workspace_list_files`, `wp_workspace_read_file`, `wp_workspace_diff`, `wp_workspace_apply_changes`, `wp_workspace_bulk_replace`, `wp_workspace_scaffold`, `wp_workspace_theme_json`, `wp_workspace_copy_woo_template`, `wp_workspace_static_checks`, `wp_code_index`, `wp_template_hierarchy`, `wc_template_overrides_audit`, `wp_component_intel`, `wp_playbook`) plus enhanced `wp_search_component` (`mode="literal"\|"regex"`, `context_lines`, `plan_id`) inside the 16-call / 10-read / 5-search / 60s loop. Ships pure-Python `static_checks.py`, `code_index.py`, `theme_json.py`, `woo_templates.py`, 10 `playbooks.py` topics + `skills/wordpress-theme-dev/SKILL.md` & `skills/wordpress-plugin-dev/SKILL.md`, and fail-closed `AZOM_WPORG_INTEL=1` WordPress.org intel (off by default, mock offline when `AZOM_USE_MOCK=1`, HTTP isolated in `skills/ecom_ops/integrations/wp_org_client.py`). Adds Telegram `/wp plans [site_id]` and `/wp plan <plan_id>`, multi-file workspace inline staging (`wpreview:stage:wr-...` / `wpreview:no:wr-...`, Oscar `WP_OPS_ADMIN` + `CODE_EDIT` + snapshot hash binding), `WP_CODE_INTENT_RE` bypass of `site_content`/catalog prefetch, `CHAT_RUNTIME_POLICY` + `WP_TOOL_GUIDE` prompt compression, and audit bug fixes #1–#10 (`LocalTree` mock workspace open, new-file staging creation, `wp_workspace_run_checks` summary/findings preservation, `list_entries` tree walk, secret-scrubbed search excerpts, budget-stop summary + 16k history clip, `site_content` fallthrough, rejected plan dir cleanup, and sandbox/prompt alignment). **Oscar-owned / remains gated:** live staging site registration & `woo_version`, explicit `AZOM_WP_SANDBOX=1` / `AZOM_WP_FILES_SSH=1` (with restricted `*-wp-reader` account), optional `AZOM_WPORG_INTEL=1`, and live pilot soak ([`PILOT_OPS.md`](PILOT_OPS.md), [`runbooks/wp-ops-read-pilot.md`](runbooks/wp-ops-read-pilot.md)); production deploy (`BackupNotVerified`) and restore (`RestoreNotInMvp`) stay closed. |
| **Telegram & Messenger jobs** | Durable Telegram inbox and per-chat background jobs in `AZOM_DATA_DIR/telegram_jobs.sqlite3`; bounded worker pool (default 2, max 4), structured `ProgressEvent` + `LiveStatusBoard` (1.5s LLM/WP status delay, first-delta flush, honest `model-draft` failure status), per-chat 429 rate-limiter, HTTP 400 not-modified handling, `reply_to_message_id` threading, and model-router/profile phase updates. Messenger webhook (`POST /webhooks/messenger`) uses durable SQLite queue `AZOM_DATA_DIR/messenger_jobs.sqlite3` (WAL, `mid` dedup, restart reconciliation, `typing_on`, 1950-char chunking) so slow LLM turns return HTTP 200 immediately (drains inline during synchronous Flask tests). `AZOM_TELEGRAM_MODEL_ROUTE` is scoped to Telegram only. Production deployment and operator acceptance remain. WordPress SSH is still disabled. Homepage source review is job kind `page_review` in the repository and does not call order tools. See [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) and [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md).|
| **Pre-change clarification (`change_clarifier`)** | Ambiguous change requests, visual/annotated screenshot descriptions (`"ringat in på bilden"`), layout/theme requests, and multi-path edits trigger a pre-change verification step (`skills/ecom_ops/bot/change_clarifier.py`, `skills/change-verification/SKILL.md`, `prompts.yaml` `chat` v1.2 / `wp_coder` v1.1). `_SafeTextStream` withholds `<clarify>` blocks from streaming drafts and strips raw tags; `BotHandler` manages up to `MAX_CLARIFY_ROUNDS` (3) rounds with single-use `cl-[0-9a-f]{12}` proposals, offline fallback, Messenger 3-button priority (`clarify:go:`/`clarify:pick:` ahead of `clarify:other:`/`clarify:cancel:`), and `1`–`4`/text fallback. All `clarify:*` callbacks stay ≤ 64 UTF-8 bytes, remain non-mutating (`kind="callback"`), and never bypass final HITL write/staging buttons (`proposal:yes:pr-...`, `action:yes`, `wpfile:stage:fe-...`, `wpreview:stage:wr-...`, `/cases approve`) or RBAC. |

**WordPress review execution status:** `php_lint` runs fixed `php -l` when PHP is installed. A timeout is status `timeout`, and an earlier syntax error stays `failed`. Other executable checks, a WordPress container, and browser rendering remain unavailable unless opted in. No `passed` result is synthesized. A run stores `check_run` (`cr-` plus 12 hex digits) with site, plan, base/work hashes, the server check policy, and profile `wp-review-catalog` version `1`. It is `published` only when that snapshot still matches and every required job passed. `woo_static` is `static_precheck` with `runtime_proof` false. Old `approved_for_staging` or unbound `tests_passed` metadata does not open preview or local staging. User preview stays `PreviewBlocked` while the sandbox is not running. A database template name stays unverified until that file is read. `selection` is `candidate` only when a hierarchy file was read and that assignment was not verified by the read. `theme.json` stays in the source inventory and is not a selected template. A file over the read limit is `clipped`. The 32-file text cap is `capped`. Either one stops the hierarchy. Later candidates are not read or recorded, and the file is not read from the parent. `front-page.php` stays selected when a later page template was also read. A safe assigned `.php` path is the next classic candidate after `front-page.php`. A missing child file can. An unknown active theme returns `selection` `unavailable` and an empty candidate list. For block themes, `step_home()` reads Site Editor templates via `WordPressClient.list_templates()` (`site_editor: "read"` when non-empty; otherwise `"unavailable"`). The review page POST `/checks` calls `ReviewPlans.run_checks()` (delegating to the sandbox when `AZOM_WP_SANDBOX=1`, otherwise `run_catalog` and leaving `published` false while a required job is unavailable). `ReviewPlans.artifact_file()` and `GET /wp/<site_id>/reviews/<plan_id>/artifact/<name>` (`WP_OPS_ADMIN` + `CODE_EDIT`, `image/png`) serve validated sandbox PNGs to `wp_review.html`, where `data-rendered-preview` stays `"false"` unless real PNGs and an executed passing `browser_smoke` row exist. A hand-edited profile name does not satisfy `wp-review-catalog` version `1`. A workspace root without `base` or `work` becomes `not_resumable`. The REST call uses the remaining review budget as its timeout. The mock backup field `offsite` stays false. `bin/backup-azom-data.sh` rsyncs `AZOM_BACKUP_DIR` only when `--dest` or `AZOM_BACKUP_DEST` is set; that copy can include `wp_ops_state.json` and is not a site restore. A future read pilot is an Oscar checklist in [`runbooks/wp-ops-read-pilot.md`](runbooks/wp-ops-read-pilot.md) and is not enabled. The page-review resolver supports default homepage settings or a named `target_page` (`extract_page_review_target()`); source reads inventory theme templates/assets and up to four active plugin entry points. `wp_review_plans.json` is retained in the normal backup while `wp_workspaces/` (source, diffs, and artifacts) is excluded; a plan restored without its workspace becomes `not_resumable`. Telegram review links require an HTTPS dashboard base and are authenticated review links, not approvals. `bin/fetch-wp-runtime.py` downloads pinned WordPress 7.1.5 and, on Windows x86 or x64, pinned PHP 8.3.37 outside the repo (`skills/ecom_ops/integrations/wp_runtime_fetch.py`). Linux and macOS exit `php_binary_unpinned` before any download. The manifest is written only after the published checksum, `version.php`, and `php -v` match. That download does not start a server, does not put `php` on `PATH`, and leaves `AZOM_WP_SANDBOX` off. `run_php_lint` reads `runtime.json` only when an explicit `runtime_dir` is passed; otherwise it uses `PATH` only, and a missing binary stays `unavailable` / `php_binary_missing`. `skills/ecom_ops/integrations/wp_local_runtime.py` (`LocalWordPressSandbox` + `resolve_local_runtime()` non-Windows fallback when `wordpress/` + system PHP 8.2–8.4 with `pdo_sqlite` exist) and `wp_browser_smoke.py` (`run_vertical_theme_render()` and `run_vertical_plugin_smoke()` using serialized `active_plugins` + synthetic host theme) provide an opt-in, loopback-only (`127.0.0.1`) local WordPress + SQLite + headless Chromium/Edge vertical render when `AZOM_WP_SANDBOX=1` (E2E tests via `AZOM_WP_RUNTIME_E2E=1`, skipped by default). `SandboxGate` reports `running` only after a real loopback process is verified alive with no published port, no Docker socket, no broad host mount, and no host secrets; `AZOM_WP_RUNTIME_DIR` is protected from dashboard env overlays. `wpcs_php` and `plugin_check` remain `unavailable`, default `run_catalog` remains unchanged/unpublished, `AZOM_WP_SANDBOX=0` remains default, and this is not a customer-site review and does not contact `azom.no`. See [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) and [`COMPLIANCE.md`](COMPLIANCE.md).

## Ops next (human-owned — agents must not mark done)

| Gate | Owner | Status |
|------|-------|--------|
| **A1 live soak** | Oscar (host) + Jonatan (approve sample) | **Blocked** — not executed on prod. Checklist lives in [`PILOT_OPS.md`](PILOT_OPS.md). |
| Baseline KPI / support hours | Oscar + Jonatan | Open — needed for “50% less support time” story |
| Live classify calibration (20–50 redacted samples) | Oscar + Jonatan | Open — do not lower suggest thresholds without this |
| Weekly ops cadences | Oscar / Jonatan | Open after soak |

**Agent rule:** Never mark live soak complete. Never open a PR that wires auto-send into poll until every FU9 gate in [`CASES.md`](CASES.md) is green **and** Oscar gives written enable.

The local OpenClaw client upgrade is a separate Windows snapshot. Its docs are pushed on `docs/openclaw-local-upgrade` (`e66c56f`) and are not merged to `main`. That work does not deploy Azom and does not close the four gates above. Detail: [`openclaw-upgrade-report.md`](openclaw-upgrade-report.md).

## FU9 auto-send (summary)

- **Status:** Not wired. `should_auto_send` / day counter exist; poll does **not** send.
- **Default:** `config/cases_ai.yaml` → `auto_send_enabled: false`.
- **Kill-switch:** `AZOM_AUTO_SEND_KILL=1` always denies.
- Full gates and rollback: [`CASES.md`](CASES.md) section “Auto-send (FU9)”.

## Mailbox enablement

- SE support mailbox is the pilot path (human approve only).
- **NO/DK** mailboxes in `config/mailboxes.yaml` stay `enabled: false` until Oscar supplies credentials **and** authorizes enablement.
- Do not enable NO/DK in a coding task without that written authorization.

## Explicit non-goals (parked)

Do not start these unless product ownership changes:

- V3 multi-tenant SaaS
- FAQ / knowledge base
- Meta / TikTok ads, GA4 BigQuery warehouse, default-on Ads mutate
- IMAP IDLE (timer poll only)
- Default-on auto-send
- Outlook browser OAuth UI (env + probe only today)
- Broad scope expansion beyond Azom single-customer deepen
- Managed WordPress hosting, autonomous production updates or restores, malware cleanup, and a WordPress availability SLA. Phase 2 is a local mock manifest and staging drill only ([`WORDPRESS_OPS.md`](WORDPRESS_OPS.md)). Named theme and plugin diff plans exist; production file deploy and file restore are not done.

## OpenRouter budget

- Cap: **USD 100** via `config/limits.yaml`, counted from estimated **OpenRouter** cost only.
- ChatGPT OAuth and xAI routes record `cost_usd=0` and can still run when that cap is exhausted.
- When no non-OpenRouter route is configured and the cap is exhausted: skip LLM phrasing/drafts; still serve order, cases, status, product catalog, and page reads via tools. Product and page writes stay on the confirm button.

## Roles (quick)

| Actor | Role | Typical powers |
|-------|------|----------------|
| **Jonatan** | `viewer` | Read mail/SSH, non-secret settings, **CASE_REPLY**, **MARKETING_READ** + **MARKETING_SUGGEST**, **WP_OPS_READ** |
| **Oscar** | `full_admin` | Secrets UI, probes, resolve escalations, experiment flags, **MARKETING_MUTATE**, `shadow-report` / `retention-purge`, **WP_OPS_ADMIN**. **WP_OPS_RESTORE** exists and still refuses. |
| **agent** | `operator` | order/product/support, **MAIL_SEND**+**MAIL_READ**, **CASE_REPLY**, SSH health, cases poll, **MARKETING_READ**, **WP_OPS_OPERATE** (staging health checks only) |

## Absorption note (historical docs removed)

Facts from former `docs/superpowers/`, `docs/solutions/`, `docs/ideation/`, finish/release plans were absorbed into living docs (`CURRENT_STATE`, `CASES`, `PILOT_OPS`, `WOO_WORDPRESS`, `MAIL_PROVIDERS`, `SYSTEM_OVERVIEW`, `DEVELOPER_GUIDE`). Those historical paths no longer exist. Do not recreate them; update living docs instead.

## Changelog / rollback (2.0 → 3.0)

- **Upgrade:** `git pull` + `pip install -e .` (optional extra `[marketing-live]`). Additive SQLite ALTERs only. `config/profile.yaml` is optional — missing file keeps Azom defaults.
- **Rollback:** keep Docker tag `azom-agent:2.0`; revert the 3.0 commit(s). WAL can be disabled by reverting the store pragma. No new default-on send or mutate flags.
- **Do not** treat this bump as soak-complete, FU9 wire, or NO/DK enablement.

## Related living docs

| Need | Doc |
|------|-----|
| Architecture map | [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) |
| Code vs ops maturity scores | [`MATURITY.md`](MATURITY.md) |
| Dev setup / tests | [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) |
| CLI details | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
| Cases + FU9 | [`CASES.md`](CASES.md) |
| Daily pilot ops + soak | [`PILOT_OPS.md`](PILOT_OPS.md) |
| Mail setup | [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| Gmail OAuth | [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md) |
| Woo/WP API surface | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| WordPress site operations | [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) · operator [`runbooks/wp-ops-backup.md`](runbooks/wp-ops-backup.md) · read pilot [`runbooks/wp-ops-read-pilot.md`](runbooks/wp-ops-read-pilot.md) |
| Model router | [`MODEL_ROUTER.md`](MODEL_ROUTER.md) · operator [`runbooks/model-oauth.md`](runbooks/model-oauth.md) |
| OpenClaw client upgrade | [`runbooks/openclaw-upgrade.md`](runbooks/openclaw-upgrade.md) · snapshot [`openclaw-upgrade-report.md`](openclaw-upgrade-report.md) |
| Gateway / secret incidents | [`runbooks/gateway-recovery.md`](runbooks/gateway-recovery.md) · [`runbooks/secret-leak-response.md`](runbooks/secret-leak-response.md) |
| Google Ads + GA4 | [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md) |
| Install / deploy | [`AUTO_INSTALL.md`](AUTO_INSTALL.md), [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md) |
| Docker overlays | [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md) |
| Template instantiate | [`TEMPLATE_GUIDE.md`](TEMPLATE_GUIDE.md) |
| Index | [`README.md`](README.md) |
