---
name: ecom-ops
description: >
  AzomOps-Agent for one WooCommerce shop: order status, product copy, support
  drafts, human-approved cases, mail, dashboard, Messenger and Telegram,
  Google Ads and GA4 reads, and bounded WordPress review. Customer mail,
  campaign changes, and production file writes wait for an explicit approve.
  Critical edits and secrets escalate to Oscar.
version: "3.0.0"
---

# ecom-ops (3.0 + Path B)

**Purpose:** Skill card for agent hosts (Moss / OpenClaw-style) describing what this package can do.  
**Audience:** Coding agents and skill hosts.  
**Read this first:** repository root `AGENTS.md`, `docs/CURRENT_STATE.md`, `SOUL.md`.

**What this skill is:** The operating card for one shop. It drafts, looks up orders, and proposes changes. Customer mail goes out only on an explicit approve path. Ads mutations and production theme writes stay refused. The human-facing tour is `docs/FUNKTIONER.md`.

**Identity / hard rules:** `SOUL.md`  
**System map:** `docs/SYSTEM_OVERVIEW.md`  
**Messenger (daily driver):** `docs/MESSENGER_OPENCLAW.md`  
**Telegram (backup):** `docs/TELEGRAM_OPENCLAW.md`  
**Cases:** `docs/CASES.md`  
**CLI:** `docs/CLI_REFERENCE.md`  
**Dev setup:** `docs/DEVELOPER_GUIDE.md`  
**Model router:** `docs/MODEL_ROUTER.md`  
**Safe ops:** `skills/azom-safe-ops/SKILL.md`  
**Router health:** `skills/model-router-health/SKILL.md`  
**OAuth incident:** `skills/oauth-incident/SKILL.md`  
**OpenClaw client upgrade:** `skills/openclaw-upgrade/SKILL.md`  
**Change verification:** `skills/change-verification/SKILL.md`  
**Web search & deep research:** `skills/web-research/SKILL.md` (`docs/WEB_SEARCH_RESEARCH.md`)  
**SEO Studio:** `skills/wordpress-seo/SKILL.md` (`docs/SEO_STUDIO.md`)  
**WordPress review & dev:** `docs/WP_SKILL_GUARDRAILS.md`, `skills/wordpress-review/SKILL.md`, `skills/wordpress-theme-dev/SKILL.md`, `skills/wordpress-plugin-dev/SKILL.md`, `skills/wordpress-plugin-ops/SKILL.md`, `skills/wordpress-seo/SKILL.md`, `skills/wordpress-ui-iteration/SKILL.md`, `skills/wordpress-page-builder/SKILL.md`

## Prioritized actions

| Action | Module | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops --mock order-status --order-id ID --status STATUS` |
| product description | `ecom_ops.actions.product_desc` | `python -m ecom_ops --mock product-desc --product-id ID --language sv [--research-brief res-... \| --research]` |
| customer support draft | `ecom_ops.actions.support` | `python -m ecom_ops --mock support --message "..."` |
| cases poll / approve | `ecom_ops.cases.service` | `python -m ecom_ops --mock cases poll\|list\|show\|reply\|draft\|regenerate\|close\|research` |
| shadow report (Oscar) | `ecom_ops.cases.shadow_report` | `python -m ecom_ops --actor oscar cases shadow-report` |
| retention purge (Oscar) | `ecom_ops.cases.retention` | `python -m ecom_ops --actor oscar cases retention-purge --dry-run` |
| null-send profile | `ecom_ops.runtime_profile` | `--null-send` or `AZOM_NULL_SEND=1` |
| SSH / VPS | `ecom_ops.actions.ssh_ops` | Mock: `python -m ecom_ops --mock ssh --command "uptime"`; live requires `AZOM_SSH_READ_ENABLED=1`, pinned known_hosts, explicit key, and a dedicated non-root `*-ssh-reader` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops --mock mail send\|fetch\|reply` |
| marketing Ads+GA4 | `ecom_ops.actions.marketing` | `python -m ecom_ops --mock marketing digest\|health\|consistency\|suggests\|research\|…` |
| SEO Studio | `ecom_ops.seo.service` | `python -m ecom_ops --mock seo audit\|fix-propose\|fix-apply\|batch-propose\|batch-apply\|gsc\|parity\|deep-crawl\|diff\|digest\|strategy\|campaign\|vendors\|autopilot\|link-pack\|link-pack-status\|link-verify` |
| web search / research | `ecom_ops.research` | `python -m ecom_ops --mock search --query "..."` · `fetch-url --url "https://..."` · `research start\|list\|show\|status\|steer\|cancel` |
| WordPress site ops | `ecom_ops.wp_ops` | `python -m ecom_ops --mock wp sites\|status\|check\|file-propose` |
| runtime status | CLI | `python -m ecom_ops status` · `python -m ecom_ops smoke` · `python -m ecom_ops ops status [--json]` · `python -m ecom_ops --actor oscar ops gc [--dry-run] [--grace-hours N]` |

## Surfaces

| Surface | Entry |
|---------|--------|
| Dashboard | `./bin/start-dashboard.sh` → `/onboarding`, `/settings`, `/cases`, `/marketing`, `/seo`, `/research`, `/wp`, `/oscar`, `/ops/runtime` (Oscar) |
| Model router | `ecom_ops.llm` facade over `ecom_ops.model_router`. Oscar: `/oscar` test, `/oauth/openai/start` |
| Gmail OAuth | `/oauth/gmail/start` → tokens in `AZOM_DATA_DIR/oauth/gmail.json` |
| Google marketing OAuth | `/oauth/google/start` → `AZOM_DATA_DIR/oauth/google_marketing.json` |
| Marketing dashboard | `/marketing` (Jonatan read + suggest HITL) |
| Messenger webhook | Dashboard `GET\|POST /webhooks/messenger` |
| Telegram bot | `python -m ecom_ops.bot` or `./bin/dedicated-bot.sh` |
| Cases timer | `./bin/cases-poll.sh` / `azom-cases-poll.timer` |
| Pilot toolkit | `python -m ecom_ops soak-preflight` · `cases calibration-report` |
| Woo webhook | `POST /webhooks/woo` (HMAC) |

## Messenger / Telegram (OpenClaw hybrid)

Slash: `/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` `/cases` `/order` `/health` `/brief` `/marketing` `/seo` (`scan|product|page|media|batch|gsc|parity|diff|digest|verify|strategy|campaign|links|vendors|autopilot|plans|plan`) `/search` (`/web`) `/research` (`list|status|show|steer|cancel`) `/ops` (Oscar-only) `/wp sites` `/wp use <site-id>` `/wp plugins [site-id]` `/wp mu-plugins [site-id]` `/wp plans [site-id]` `/wp plan <id>` `/wp changes [site-id]` `/wp rollback <change-id>` `/wp bridge [site-id]` `/wp clear`.

Free text uses tool-backed reads and proposals. It can read WooCommerce orders/products and WordPress pages, posts, site title/tagline, themes, and plugin status, and run bounded public HTTPS `web_search` / `web_fetch_extract` or async `web_research_start` (`res-<12hex>`, see `skills/web-research/SKILL.md`). Ambiguous change requests, visual or annotated screenshot descriptions (such as `"ringat in på bilden"`), layout requests, multi-path edits, or broad research asks trigger the verification contract (`skills/change-verification/SKILL.md`, `<clarify>{...}</clarify>` with `dialog_kind="change"` or `dialog_kind="research"`) with up to 3 rounds of clickable clarification buttons (`clarify:pick`, `clarify:go`, `clarify:other`, `clarify:cancel`) before opening workspaces, starting deep research, or proposing edits. Clarification never writes files or replaces final confirmation buttons (`proposal:yes:pr-...`, `action:yes`, `wpfile:stage:fe-...`, `wpreview:stage:wr-...`, `wpreview:push_draft:wr-...`). Order, product, page, post, and site-text writes wait for their confirmation button (with legacy `order:set:*`, `cases:approve:*`, and `product:desc:*` durably bound in `bot_proposals` by actor, domain/market, target, and expiry); plugin on/off also requires Oscar. Messenger uses Quick Replies (up to 13 chips) for `<clarify>` and `>3`-option menus, shares SQLite `messenger_proposals.sqlite3`, supports anti-SSRF inbound image intake (`messenger_inbox_media/`), and sends outbound workspace PNGs (`compare-desktop.png`, `preview-*`, `focus-*`). A free-text "yes" never authorizes a write. Customer mail only goes through an explicit approve path.

WordPress operations use the named, site-bound tools `wp_list_sites`, `wp_site_status`, `wp_backup_status`, `wp_security_findings`, `wp_prepare_update_plan`, `wp_prepare_restore_plan`, and `wp_list_open_operations`. The server checks actor and site permissions and limits a turn to 3 calls, 30 seconds, 4 KiB per result, and 10 KiB total. Site context is bound to actor, channel, and conversation for 12 hours; `/wp sites`, `/wp use <site-id>`, and `/wp clear` select or clear it. Update/restore plans are locked previews with no approval or execution path. Status, backup, and security results may be mock or unknown; live health checks, security scanning, version discovery, update execution, and production restore are not implemented.

`AZOM_TELEGRAM_MODEL_ROUTE` pins one route for Telegram free text and does not fall through on failure. Customer mail requires `/cases approve` or the approve button/postback; never from free text. Ads mutations remain blocked outside the dashboard/CLI approval rails.


Telegram runs durable per-chat jobs from a SQLite inbox with a bounded worker pool. Read turns can take up to three steer messages at safe checkpoints; approved actions queue later input and never run concurrently with another approved action for the same site. `/status`, `/jobs`, `/cancel`, and Oscar-only `/reconcile` are Telegram receiver controls. Messenger enqueues each webhook event asynchronously in `AZOM_DATA_DIR/messenger_jobs.sqlite3` (`messenger_queue.py`) with bounded background workers and per-peer ordering, returning HTTP 200 immediately in production (inline drain during synchronous Flask tests unless `AZOM_MESSENGER_ASYNC_IN_TESTS=1`). It does not render Telegram's live status board.

## Mail providers

- `gmail` — SMTP+IMAP (app password or OAuth2 XOAUTH2 / browser consent)
- `outlook` — SMTP+IMAP (app password or OAuth2 XOAUTH2); no dashboard OAuth UI
- `exchange_graph` — Microsoft Graph REST (client credentials)
- `generic_imap` / `generic_pop3` — custom hosts via env

Details: `docs/MAIL_PROVIDERS.md`.

## Cases AI rails

`config/cases_ai.yaml`: suggest-approve for `order_status`/`shipping` (default); `never_suggest_categories` includes `abuse`/`return`/`billing` (Path B2 still drafts those richer, never ★). Auto-send **off** and **not wired** into poll unless Oscar enables after FU9 gates (`AZOM_AUTO_SEND_KILL`). Null-send / Shadow Live Ledger: `AZOM_NULL_SEND` / `--null-send` (default off); Oscar `cases shadow-report`.

## Woo / WordPress

See `docs/WOO_WORDPRESS.md` — shipment trackings, multi-site `domain=`, WordPress Application Passwords, webhook HMAC, retries. A homepage source review uses [`skills/wordpress-review/SKILL.md`](../wordpress-review/SKILL.md) and does not call order tools. Theme, site-plugin/MU-plugin dev, plugin lifecycle/recovery ops, iterative visual/UI sandbox, and net-new page builder workflows live in [`skills/wordpress-theme-dev/SKILL.md`](../wordpress-theme-dev/SKILL.md), [`skills/wordpress-plugin-dev/SKILL.md`](../wordpress-plugin-dev/SKILL.md), [`skills/wordpress-plugin-ops/SKILL.md`](../wordpress-plugin-ops/SKILL.md), [`skills/wordpress-ui-iteration/SKILL.md`](../wordpress-ui-iteration/SKILL.md), and [`skills/wordpress-page-builder/SKILL.md`](../wordpress-page-builder/SKILL.md).

Site operations are separate from the WordPress content client: [`docs/WORDPRESS_OPS.md`](../../docs/WORDPRESS_OPS.md). The chat exposes named, site-bound reads for registry/status, backup status, security findings, and open operations, plus locked update/restore plan previews. The previews cannot be approved or executed. The server enforces actor/site RBAC and bounded calls (3 calls, 30 seconds, 4 KiB per result / 10 KiB total). The 12-hour site context is scoped to actor, channel, and conversation. There is no model-created shell, PHP, or WP-CLI. Security scanning, version discovery, live health checks, update execution, and production restore are not implemented; outputs must report mock or unknown states accurately.

Phase 1 is mock health. Phase 2 is a local mock backup manifest and a staging drill. Oscar can read a window of one existing theme, plugin, or MU-plugin file through `wp_read_theme_plugin_file`. The default window is 8192 bytes and the maximum is 20480, with `next_offset` when bytes remain (or a 1-based line range via `start_line`/`end_line`). The tool is site-bound, Oscar-only, and read-only. Chat may also list one directory, search a literal string, and propose a named diff or a new file in an existing parent directory. The proposal stores hashes only. The diff body lives under `AZOM_DATA_DIR/wp_file_payloads` and is excluded from the normal backup tarball. Chat does not write the file. A staging write happens only after the server button `wpfile:stage:{plan_id}` and only on the local mock tree. The production review POST checks the plan id and both hashes and returns `BackupNotVerified`. `wp_deploy_production` and `wp_rollback_file` do not write. Live WordPress file access uses SFTP with the pinned host key and per-site key; it never opens a shell or sends a password. Live access stays off until a dedicated `*-wp-reader` account is configured for forced read-only SFTP and `AZOM_WP_FILES_SSH=1` is explicitly enabled. The separate VPS SSH health runner has a fixed command allowlist and remains off until `AZOM_SSH_READ_ENABLED=1`, explicit pinned `known_hosts`, an OpenSSH identity, and a dedicated non-root `*-ssh-reader` account are configured. Production WordPress file writes over SSH are refused. A normal backup excludes `ssh-keys/` and `wp_file_payloads/`. `wp show` does not include the identity path. `restore` refuses. A verified mock backup is not off-site and not green. This is not Phase 4.

The separate WordPress file and workspace loop caps one turn at **16** calls, **10** reads, **5** searches, and **60s** (`60` seconds; max 28 KiB per result / 80 KiB total — see `docs/WP_SKILL_GUARDRAILS.md`), with a web sub-budget of **3** `web_search` calls, **2** `web_fetch_extract` calls, **18s** web time, and **>= 8s** remaining wall time. Across `TOOL_REGISTRY` (**77** total WP/file tools: 70 in `ALL_FILE_TOOLS` + 8 in `WP_TOOL_NAMES`, with `wp_read_theme_plugin_file` shared), chat supports single-file `fe-*` plans (`wpfile:stage:{plan_id}`), playbooks (`wp_playbook` across 20 topics), SEO Studio tools (`wp_seo_audit` with `gsc`/`parity`/`deep_crawl`/`include_diff` → `seoa-*`, `wp_seo_plan_meta` → `seom-*`, `wp_seo_batch_plan` → `seob-*`, `wp_seo_drift_digest`, `wp_seo_strategy` → `seos-*`, `wp_seo_campaign` → `seoc-*`, `wp_seo_link_package` with `create`/`verify`/`cumulative`/`vendors` → `seol-*` & vetted Nordic SEO shops, and `wp_seo_autopilot` chaining audit → SERP research → strategy → campaign → vetted shop selection → draft `seol-*` — preview/draft-only in chat, requiring Oscar's HITL approval in `/seo` & `/seo/vendors` with Change Vault single/batch rollback and zero silent external link purchases), plugin lifecycle/recovery/MU-plugin tools (`wp_plugin_inventory`, `wp_mu_plugins_and_dropins`, `wp_plugin_diagnostics`, `wp_plan_plugin_updates` → `plu-*` with WooCommerce funnel canary paths `/`, `/shop/`, `/cart/`, `/checkout/`, `wp_plan_plugin_autoupdate` → `pau-*`, `wp_plan_plugin_recovery` → `plr-*` — preview-only in chat, requiring Oscar's hash-pinned HITL approval via `/wp/<site_id>/plugins` or CLI), component/code/page/DOM inspection (`wp_list_components` with `kind="theme"|"plugin"|"mu_plugin"`, `wp_theme_overview`, `wp_plugin_overview`, `wp_code_index`, `wp_template_hierarchy`, `wc_template_overrides_audit`, `wp_page_review_home`, `wp_page_review_content`, `wp_page_review_sources`, `wp_page_review_preview`, `wp_list_pages`, `wp_get_page`, `wp_fetch_page_html`, `wp_inspect_element`, `wp_build_sample_data`, and optional `wp_component_intel` only when `AZOM_WPORG_INTEL=1`), universal research tools (`web_search`, `web_fetch_extract`, `web_research_start`, `web_research_status`, `web_research_get`, and `wp_workspace_attach_research` linking `res-*` briefs outside `work/` across `wp_workspace_fork`), and isolated multi-file `wr-*` workspaces (`wp_open_review_workspace` with `kind="theme"|"plugin"|"mu_plugin"` and optional `base_plan_id` / `variant_group`, `wp_workspace_fork`, `wp_workspace_patch_file`, `wp_workspace_create_file`, `wp_workspace_apply_changes`, `wp_workspace_bulk_replace`, `wp_workspace_theme_json`, `wp_workspace_copy_from_parent`, `wp_workspace_copy_woo_template`, `wp_workspace_scaffold` across all 7 templates, `wp_workspace_product_edit`, `wp_workspace_page_draft`, `wp_workspace_diff`, `wp_workspace_status`, `wp_workspace_static_checks`, `wp_workspace_run_checks`, `wp_preview_page` with `visual_health`, `reuse_live_baseline=True`, side-by-side `compare-desktop.png` composites, and standalone live-shell rendering for `work/page_draft.json`). Multi-file `wr-*` workspaces run `wp_workspace_static_checks` and `wp_workspace_run_checks` and stage only via the explicit server button `wpreview:stage:wr-...` (never via free-text approval; production remains gated and Oscar-escalated). For `work/page_draft.json`, Oscar (`WP_OPS_ADMIN` + `CODE_EDIT`) can also push `status="draft"` only to WordPress via `wpreview:push_draft:wr-...` or `POST /wp/<site_id>/reviews/<plan_id>/push-draft` with pinned `base_sha256`/`work_sha256` (`WpOpsService.push_page_draft()` / `WordPressClient.create_page(..., status="draft")`).

## Marketing Google

See `docs/MARKETING_GOOGLE.md` — mock-first Ads+GA4; live **reads** shipped; HITL mutate + kill-switches (mutate still refused).

## RBAC

- **Jonatan:** `viewer` (+ mail/SSH read, settings non-secret, **CASE_REPLY**, **MARKETING_READ**, **MARKETING_SUGGEST**, **WP_OPS_READ**, **WEB_SEARCH**, **RESEARCH_RUN**)
- **Oscar:** `full_admin` (secrets UI + escalation resolve + probes + **MARKETING_MUTATE** + `shadow-report` / `retention-purge` + **WP_OPS_ADMIN** + **RESEARCH_ADMIN**; restore still refuses)
- **Agent (automation):** `operator` (order/product/support, **MAIL_SEND**+**MAIL_READ**, **CASE_REPLY**, SSH read, case poll, **MARKETING_READ**, **WP_OPS_OPERATE** for staging health, mock backup, and staging drill, **WEB_SEARCH**, **RESEARCH_RUN**)

## Escalation

Everything **critical**, **code edit**, and **non-allowlisted SSH** escalates to **Oscar**.

Tickets: `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Automation

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
./bin/ecom-automation.sh critical "short summary"
./bin/cases-poll.sh
```

`bin/ecom-automation.sh` defaults `AZOM_USE_MOCK=1`, then sources `.env`, which can turn mock off. Prefer the `python -m ecom_ops --mock` form above.

Stop: do not send customer mail, change a live order, or run `sudo bash bin/install.sh` from this card unless Oscar has asked for that exact action.
