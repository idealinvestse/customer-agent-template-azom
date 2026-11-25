# WordPress site operations

**Purpose:** Say what the WordPress operations surface does today, which phase it belongs to, and what is still only a proposal.  
**Audience:** Coding agents and developers. Operators can use the CLI and dashboard sections.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

**Document status:** Phase 1 mock health, Phase 2 local mock backup manifests, bounded bot reads, the Etapp 4T theme/plugin engineering toolchain (`ALL_FILE_TOOLS` in isolated `wr-*` workspaces plus single-file `fe-*` diff plans), preview-only update/restore plans, and a read-only homepage/page source review are in the repository. The local runtime registry targets `azom.no` as production; this does not enable live checks, backups, or SSH. Oscar can inspect components, run static checks, review diffs, and stage verified mock changes via explicit buttons. Staging apply writes only the local mock tree after an explicit plan button (`wpfile:stage:fe-...` or `wpreview:stage:wr-...`). The production review POST returns `BackupNotVerified` and does not write. Live file access uses key-only SFTP, has no shell or write path, and remains off until a dedicated restricted account is provisioned and `AZOM_WP_FILES_SSH=1` is explicitly set. External WordPress.org intel (`AZOM_WPORG_INTEL=1`) is disabled by default (`AZOM_USE_MOCK=1` uses offline fixtures). Homepage review does not call order tools. Pure-Python static checks (`wp_workspace_static_checks`) run offline, while `php_lint` runs fixed `php -l` when PHP is present; WPCS, Plugin Check, PHPUnit, and asset lint stay `unavailable`, and `AZOM_WP_SANDBOX=0` remains the default (opt-in loopback sandbox when `AZOM_WP_SANDBOX=1` and a verified runtime exists). Security scanning and update discovery/execution are not implemented. Production restore is not available. This is not Phase 4 and not managed WordPress hosting.

## Glossary

| Term | Meaning |
|------|---------|
| **Site registry** | Live mode (`AZOM_USE_MOCK=0`) uses `AZOM_DATA_DIR/wp_sites.yaml` when present, otherwise `config/wp_sites.yaml`. Mock mode (`AZOM_USE_MOCK=1` or unset) uses `config/wp_sites.mock.yaml`. One row per site and environment. No secret values. |
| **Secret ref** | Environment variable name `WP_SECRET_*` for that site only. The value is never returned to the CLI, UI, audit, or state file. |
| **Tone** | `ok` only when a check result is `ok`. `fail`, `unknown`, and a verified local mock backup use tone `attention` and must not be shown as green. |
| **Mock backup** | A metadata manifest in `wp_ops_state.json`. `provider` is `mock`, `offsite` is false, `encryption` is `unconfigured`, and `bytes_copied` is false. The SHA-256 digest covers that metadata. It is not encryption and not a copy of site files or a database. |
| **Confirmation** | How many consecutive failed probes open one incident. The minimum is 2. One failure is not “site down”. |
| **Content client** | `skills/ecom_ops/integrations/wordpress.py`. Posts, pages, settings, a theme list, and a plugin list plus active/inactive. It does not install, update, delete, or switch themes. It is not this operations surface and it still uses shared `WP_USERNAME` / `WP_APP_PASSWORD`. |
| **Typed file edit** | One `.php`, `.css`, `.scss`, `.js`, `.json`, `.txt`, `.md`, `.html`, `.pot`, or `.po` file inside one theme or plugin directory (or `wr-*` workspace). The caller supplies the site, kind, slug, relative path, and edit/new text. Azom computes the path. There is no command field. |
| **SSH settings** | Per-site document root, pinned host key, dedicated `*-wp-reader` user, port, and private key. The registry stays the default. Overrides and the private key live under `AZOM_DATA_DIR`, not in git. |
| **Homepage source review** | The task “granska startsidans källkod”. Four site-bound reads of one registered site. It is not an order lookup and not the catalog page card. |
| **Review layer** | One of `database`, `source_file`, or `rendered_dom`. Stored `content.rendered` is still database content. A file is not the DOM. A screenshot exists only after a real browser run. |
| **Review check** | A named job such as `php_lint` or `browser_smoke`. Results include command, timestamps, exit code, summary, artifact path, `executed`, and `simulated`. `php_lint` runs a fixed `php -l` process when available. Missing tools/runtimes report `unavailable`; no simulated result passes. |

## Phase map

| Phase | What it is | Where it lives | Status |
|-------|------------|----------------|--------|
| 0 | Owner records host, URLs, versions, providers, maintenance window, and recovery objectives. No secrets in the doc. | Human record. Not a code task. | `azom.no` is registered as the target; host, versions, maintenance window, recovery objectives, and live pilot operations remain unconfirmed. |
| 1 | Per-site registry, secret refs, read-only health, mock uptime, alert dedupe, audit, CLI, dashboard. | Files below. | Mock only. No live site calls. |
| 2 | Local mock backup manifest, integrity of that manifest, staging drill, measured drill duration. | `wp_ops/backup.py`, state keys `backups` and `drills`, runbook below. | Mock only. No off-site copy. No production restore. |
| 3 | Core and plugin checksums, coverage labels, one scanner adapter. | Not created. | Not implemented. |
| 4 | Update plan, approval binding, staging execution, allowlisted adapter. Production writes stay manual. | Bot read tools and locked preview-only plan records. | Preview only. Update discovery, approval callbacks, execution, and production restore adapters are unavailable. A typed file edit is not this phase. A homepage source review is not this phase. |
| 5 | One staging site, then one production site, owner sign-off of runbooks and support hours. | Human soak. Agents must not mark it done. | Not started. |

## Telegram WordPress agent

The bot can select among `wp_list_sites`, `wp_site_status`, `wp_backup_status`, `wp_security_findings`, `wp_prepare_update_plan`, `wp_prepare_restore_plan`, and `wp_list_open_operations`. Every call validates its JSON arguments, registered `site_id`, and actor permission inside the server-side tool dispatcher. A site id is the only accepted site locator; tools do not accept URLs, hosts, paths, credentials, shell, PHP, or WP-CLI commands.

The follow-up loop executes at most 3 WordPress drift tool calls per turn, spends at most 30 seconds in tools, caps one result at 4 KB and combined tool results at 10 KB. Only the final answer is added to conversation history; model tool protocol and tool transcripts are not persisted. Tool reads and plan previews are audited.

A separate file and workspace loop (`run_bounded_file_tool_loop()`, covering `ALL_FILE_TOOLS = FILE_TOOL_NAMES | EXTRA_FILE_TOOLS`) handles theme/plugin engineering, static QA, and isolated `wr-*` workspace workflows. That loop is capped at **16** calls, **10** reads, **5** searches, **60 seconds**, **16 KiB** (`16,384` bytes) per result, and **48 KiB** (`49,152` bytes) total. Inside `run_bounded_file_tool_loop()`, drift read/preview tools (`WP_TOOL_NAMES`) can also execute seamlessly alongside file and workspace tools without ending the turn. When any loop budget (calls, reads, searches, or seconds) is reached, `_finish_on_budget()` injects a final stop prompt so the model summarizes already-collected evidence, `plan_id` references, and remaining gaps (falling back to `_format_file_results_sv()`), while assistant turns in the loop are clipped at 16,000 chars (`next_content[:16000]`).

`wp_prepare_update_plan` and `wp_prepare_restore_plan` create immutable, SHA-256-digested previews under `AZOM_DATA_DIR/wp_ops_plans.json`; previews expire after 12 hours. They are explicitly `unsupported`, `executable: false`, and `approval_available: false` while update discovery and restore adapters are missing. No callback can approve them, and no bot tool can execute them. Target versions remain empty until a provider verifies them. Restore still returns `RestoreNotInMvp`.

The current site's continuity record is stored with the existing conversation under `AZOM_DATA_DIR`. It contains only actor, channel, conversation, site id, bounded operation ids, and a 12-hour timestamp. Actor or channel changes clear the context; every subsequent site read is re-authorized and re-resolved against the registry. In Telegram or Messenger, use `/wp sites`, `/wp use <site-id>`, `/wp plans [site-id]`, `/wp plan <plan-id>`, or `/wp clear` to select context, inspect `wr-*` review workspaces, or clear context.

There is no scanner adapter. `wp_security_findings` returns `unknown`, `coverage: not_configured`, and `findings: null`; this must not be described as a clean scan. Backup status reflects local mock manifest metadata only and does not imply that site files or database bytes were copied.

Shop content on the live WordPress host is a different path. Telegram reads posts, the site title, the tagline, themes, and plugins through `WordPressClient` and returns before this tool loop. One post field, the site title or tagline, and turning one existing plugin on or off wait for Ja. Plugin on/off needs `WP_OPS_ADMIN` (deactivating critical e-commerce plugins such as `woocommerce` or payment gateways shows an explicit warning). When a site is selected in `wp_ops_context` via `/wp use <site-id>`, messages matching `WP_CODE_INTENT_RE` bypass `site_content` and catalog read/write prefetch in `skills/ecom_ops/bot/chat_agent.py` (and `allow_wp_ops_fallthrough=has_wp_site_context` in `skills/ecom_ops/bot/site_content.py` handles code/file fallthrough) so theme/plugin engineering queries route directly to the `wp_ops` file/workspace loop. Outside that context, `site_content` does not install, update, delete, switch themes, or edit theme or plugin files, and it does not register the shop in the site-operations registry. When `WP_CODE_INTENT_RE` matches, `chat_agent.py` also injects up to 2 compact playbook hints (`format_playbook_hints()`, max 750 chars) as guidance only.

For component and homepage reads (`wp_component_reads.py` and `page_review_runtime.py`), when live reads are enabled (`AZOM_USE_MOCK=0` via `rest_reads_allowed()`), a site's `WP_SECRET_*` value formatted as `user:app_password` binds a per-site `WordPressClient` against `site.canonical_url` before falling back to shared env credentials.

## Phase 1 behavior

### Data

The live `config/wp_sites.yaml` and local `AZOM_DATA_DIR/wp_sites.yaml` contain only the approved `azom-no` production target. The record contains the public canonical URL and unknown capability/version values (including optional `woo_version` on `Site`, single-line max 40 chars); it has no document root, SSH host, or credentials. Live checks and backup adapters remain unavailable, and live file SSH remains off. The ignored runtime registry takes precedence in live mode. Mock mode uses `config/wp_sites.mock.yaml`, which contains the synthetic shop and staging fixtures.

Each site has its own `secret_ref`. `WP_USERNAME`, `WP_APP_PASSWORD`, and any name that is not `WP_SECRET_*` are rejected. Two sites cannot share a ref. A caller who asks for another site’s ref is denied before that variable is read.

Check results, incidents, backup manifests, and drills are stored in `AZOM_DATA_DIR/wp_ops_state.json`. Audit rows go through `ecom_ops.audit.log_action` with action `wp_ops_check`, `wp_ops_backup`, `wp_ops_restore_drill`, or `wp_ops_restore`. Neither store receives the secret value, a response body, or an exception string.

### Checks

The probes are in-process. `skills/ecom_ops/wp_ops/` does not import sockets, subprocess, or the content client directly; optional live WordPress.org metadata lookups in `wporg_intel.py` delegate HTTP strictly to `skills/ecom_ops/integrations/wp_org_client.py` (`fetch_wporg_json`, off by default).

| Check | Phase 1 result |
|-------|----------------|
| `http_uptime`, `tls`, `dns` | Mock connector. `*.example.test` can succeed. Any other host is `unknown` / `UptimeProviderUnconfigured`. |
| `wp_rest` | Succeeds only when that site’s secret ref is present. No post or page content. |
| `inventory` | Versions copied from the registry and labeled `version_source: registry`. |
| `backup` | `unknown` until a manifest exists. A verified mock manifest is result `verified`, tone `attention`, and `green` false. A failed digest is `fail`. A record older than `retention_days` is shown as expired and is not a success. The stored row is not deleted. |
| `checksum` | Always `unknown` / `NotImplemented`. It does not turn the site green. |
| Pending updates | Always `null`, shown as unknown. Not zero. |

`AZOM_WP_OPS_LIVE` is ignored. Setting it does not create a network client.

A failed signal check increments a streak. An incident opens at `alert_confirmations` (default 2) and further failures update that same open incident. `unknown` does not increment the streak, does not open an incident, and does not clear a streak. `ok` resets the streak and marks the matching incident recovered.

On-demand checks for the same site inside `dedupe_window_seconds` return the stored result and do not count another failure. `wp check --due` uses `check_interval_seconds` instead. One site runs at a time. A second run while the lock is held returns `SiteBusy`.

### Permissions

Permissions are on roles. Code does not special-case a person’s name.

| Permission | Who has it in the default map | Phase 1 use |
|------------|-------------------------------|-------------|
| `WP_OPS_READ` | `viewer`, `read_only`, `operator`, `full_admin` | List and show sites. List and read one theme or plugin file. |
| `WP_OPS_OPERATE` | `operator`, `full_admin` | Run checks, create a mock backup, and record a staging drill on `staging`. Propose and apply a file edit on `staging`. |
| `WP_OPS_ADMIN` | `full_admin` | Run checks and create a mock backup on `production`. A production file edit also needs `CODE_EDIT`. |
| `WP_OPS_RESTORE` | `full_admin` | `WpOpsService.restore` still returns `RestoreNotInMvp` and executes nothing. There is no `wp restore` command and no production restore control. |

Jonatan is `viewer`. The agent automation actor is `operator`. Oscar is `full_admin`. A content role cannot run a production check, update code, or restore a database.

### Commands

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock --actor jonatan wp sites
python -m ecom_ops --mock --actor jonatan wp status
python -m ecom_ops --mock --actor jonatan wp show --id mock-staging
python -m ecom_ops --mock --actor agent wp check --id mock-staging
python -m ecom_ops --mock --actor oscar wp check --id mock-shop
python -m ecom_ops --mock --actor oscar wp check --due
python -m ecom_ops --mock --actor oscar wp backup --id mock-shop
python -m ecom_ops --mock --actor jonatan wp backups --id mock-shop
python -m ecom_ops --mock --actor agent wp restore-drill --backup <backup-id> --target mock-staging
python -m ecom_ops --mock --actor jonatan wp files --id mock-staging --kind theme
python -m ecom_ops --mock --actor agent wp file-propose --id mock-staging --kind theme --slug twentytwentyfive --path style.css --from ./edit.css
python -m ecom_ops --mock --actor agent wp file-apply --id mock-staging --plan fe-aaaaaaaaaaaa --from ./edit.css
# expect: JSON with ok true or false. live_calls is false. The file body is not in the JSON.
# expect: a verified backup has green false, offsite false, encryption unconfigured, bytes_copied false.
```

`wp check` takes `--id` or `--due`, not both. `wp restore` is not a command. A drill refuses a production target with `TargetNotStaging`.

## Phase 2 behavior

A backup is one id for the labels `files` and `database`. Success requires the manifest digest to match. An integrity failure is stored as `completion: failed` and cannot be drilled. The default retention is 14 days (allowed range 1–90) and is evaluated when the record is read. An expired manifest stays in the state file and is not a success.

`create_backup` on `staging` needs `WP_OPS_OPERATE`. The same call on `production` needs `WP_OPS_ADMIN`. Both stay `live_calls: false`. A viewer can list backups. The site lock is the same one checks use.

`restore_drill` needs `WP_OPS_OPERATE` and a staging target. The source manifest must be verified and inside retention. The drill stores `started_at`, `finished_at`, `recovery_seconds` for the local verify, both parts, the source backup id, and the target site id. A WooCommerce source sets `order_reconciliation: required` and `orders_mutated: false`. The same backup and target inside the target site’s dedupe window returns the previous drill and does not write another one. `restore()` is unchanged and still refuses every actor, including Oscar.

The overview badge for a verified mock set says “verifierad, inte off-site”. It is not green. `healthy` stays false while checksum is unknown. The mock field `offsite` stays false. `bin/backup-azom-data.sh` rsyncs `AZOM_BACKUP_DIR` only when `--dest` or `AZOM_BACKUP_DEST` is set. That tarball can contain `wp_ops_state.json` and `wp_review_plans.json`, excludes `wp_workspaces/`, and is not a site restore. A future read pilot is the Oscar checklist in [`runbooks/wp-ops-read-pilot.md`](runbooks/wp-ops-read-pilot.md). That checklist does not enable a live path.

## Typed file edits

This is not Phase 4 and not a plugin or theme update. Live access uses only the SFTP subsystem; it never asks for a remote shell or runs `ls`, `cat`, `cp`, or `tee`. The model cannot compose a command. Chat can propose a named diff. It cannot write the file.

The path has to stay inside `{files_root}/wp-content/themes|plugins/{slug}/`. Component slugs allow lowercase ASCII letters, digits, hyphens, and underscores (`^[a-z0-9][a-z0-9_-]{0,60}$`). `..`, a delete, and a `.azom-bak` file are refused. The legacy CLI editor still refuses a new file. A chat create-plan may name a new file only when the parent directory already exists and the base hash is the empty-file digest. WooCommerce core (`woocommerce`) is refused. A parent theme is refused only when the active child theme is known and the slug is that parent. Third-party plugins follow a read-only inspection policy (`read_only_extend_via_site_plugin`): inspect them read-only and place custom site extensions in `azom-site-<site_id>` (`site_plugin_target`). The text must be UTF-8 and at most 65536 bytes.

Component inspection, read/search, and guidance tools in `ALL_FILE_TOOLS`:
- `wp_list_components`: lists `theme` or `plugin` slugs (`WP_OPS_READ`).
- `wp_theme_overview`: summarizes active/parent theme relationship, `theme_kind` (`block`, `classic`, or `unknown`), directory entries (depth 3, max 120), and `woocommerce/` overrides (max 40).
- `wp_plugin_overview`: Oscar-only (`WP_OPS_ADMIN`); summarizes plugin tree (depth 2, max 80), optional live REST metadata, main PHP file hooks, `has_woo_hpos_declaration`, `wc_tested_up_to`, and `third_party_edit_policy`.
- `wp_search_component`: Oscar-only (`WP_OPS_ADMIN`); supports `mode="literal"|"regex"` (default `"literal"`), `context_lines` (`0`–`3`), `plan_id` (`wr-*` to search workspace `work/`), `extensions` (up to 6), and `case_insensitive` (boolean, default `true`). Skips `vendor`, `node_modules`, `cache`, `uploads`, and `.git`, and returns secret-scrubbed excerpts via `_scrub_search_text()`.
- `wp_read_theme_plugin_file`: Oscar-only (`WP_OPS_ADMIN`); returns either a UTF-8 byte window (8192 bytes default, 12288 max, plus `next_offset`) **or** a 1-based line range via `start_line`/`end_line` (1–50000, mutually exclusive with `offset`, capped by `limit` bytes — default 120 lines when `end_line` is omitted — with `next_line` when lines remain). The digest covers the whole file. Source content is treated as untrusted data.
- `wp_code_index`: Oscar-only (`WP_OPS_ADMIN`); pure-Python static symbol indexer (`skills/ecom_ops/wp_ops/code_index.py`) over an installed component or `wr-*` workspace (`function`, `method`, `class`, `interface`, `trait`, `hook`, `shortcode`, `rest_route`, `block`, `constant`, `header`; max 60 entries with secret-scrubbed signatures; counts toward the search budget).
- `wp_template_hierarchy`: resolves WordPress/WooCommerce candidate order across `TEMPLATE_CONTEXTS` (`skills/ecom_ops/wp_ops/review_knowledge.py`) and matches existing theme/override candidates (`WP_OPS_READ`).
- `wp_fetch_page_html`: fetches rendered public HTML via credential-free HTTPS GET (`WP_OPS_READ`, gated by `AZOM_WP_LIVE_PREVIEW=1` and `AZOM_USE_MOCK=0`) using `validate_preview_path()` and `fetch_live_html()`, returning metadata (`title`, `lang`, `meta_description`, `canonical`, `h1`, tag counts, external hosts) plus a secret-scrubbed HTML excerpt (supports optional `selector` to target specific elements and `include_styles` for bounded CSS extraction; local `/wp/<site>/reviews/wr-...` preview paths return an actionable error directing to `wp_inspect_element(plan_id=...)` / `wp_workspace_read_file(plan_id=...)`).
- `wp_inspect_element`: inspects a live public page or a workspace's rendered preview overlay HTML (`{site_id, path?, selector?, text?, plan_id?}`) via headless Chrome CDP, returning bounded `outer_html` (≤ 3 KB), `tag`, `classes`, `bounding_box`, `computed_styles`, `pseudo_before` / `pseudo_after`, bounded `matched_rules`, and automated `health_checks` (`background_icon_overlap_risk`, `broken_images`, `horizontal_overflow`, `small_tap_target`, `warnings`).
- `wp_preview_page`: captures read-only headless Chromium screenshots (`WP_OPS_READ` without `plan_id`, `WP_OPS_OPERATE` with `plan_id` via `ReviewPlans.live_preview()`) under CDP `Fetch` interception (`live-desktop.png`, `live-mobile.png`, plus `preview-*`, `focus-*`, and `change-*` crops when overlaying a workspace). Supports `reuse_live_baseline=True` for fast CSS iteration, passes `focus_selector` into interactive sandbox HTML, includes `visual_health` warnings when `focus_selector`/`focus_text` matches, and preserves at least one screenshot per previewed `plan_id` (up to 6 PNGs) across multi-variant turns. The overlay renders static WordPress i18n string literals (`_e`/`__`/`esc_html_e` with a literal string) as text; any other PHP hunk stays `php_skipped`.
- `wc_template_overrides_audit`: audits theme `woocommerce/` overrides and `@version` drift against WooCommerce reference versions and `Site.woo_version` (`WP_OPS_READ`; counts toward the read budget).
- `wp_component_intel`: `WP_OPS_READ`; metadata lookup via `fetch_component_intel()` (`skills/ecom_ops/wp_ops/wporg_intel.py`). Disabled by default (`source: "disabled"`); in `AZOM_USE_MOCK=1`, returns deterministic offline mock fixtures (`source: "mock"`) even if `AZOM_WPORG_INTEL=1` is set. Live `api.wordpress.org` reads execute **only** when `AZOM_WPORG_INTEL=1` **and** `AZOM_USE_MOCK=0`, with HTTP isolated to `skills/ecom_ops/integrations/wp_org_client.py` (`fetch_wporg_json`).
- `wp_playbook`: zero-IO guidance lookup (`WP_OPS_READ`-free, all chat actors) across the 10 `PLAYBOOK_TOPICS` in `skills/ecom_ops/wp_ops/playbooks.py` (`child_theme_edit`, `theme_json_design`, `template_hierarchy_debug`, `woo_template_overrides`, `plugin_hooks_and_compat`, `site_plugin_scaffold`, `bulk_refactor`, `static_and_sandbox_qa`, `staging_and_hitl`, `component_intel_and_audit`). Grants no permission. Accompanying skill cards live in `skills/wordpress-theme-dev/SKILL.md` and `skills/wordpress-plugin-dev/SKILL.md` (alongside `skills/wordpress-review/SKILL.md`).

The file/workspace turn (`run_bounded_file_tool_loop()`) allows at most **16** calls (`FILE_LOOP_MAX_CALLS`), **10** reads (`FILE_LOOP_MAX_READS`), **5** searches (`FILE_LOOP_MAX_SEARCHES`), and **60 seconds** (`FILE_LOOP_MAX_SECONDS`), capped at **16 KiB** per result and **48 KiB** total. Drift read/preview tools (`WP_TOOL_NAMES`) can also run inside the file/workspace loop without breaking the turn. When any budget limit is reached, `_finish_on_budget()` prompts a final summary of collected evidence and remaining gaps (with `_format_file_results_sv()` fallback), and assistant messages are clipped at 16,000 chars (`next_content[:16000]`).

Isolated multi-file theme/plugin engineering runs inside `wr-*` workspaces under `AZOM_DATA_DIR/wp_workspaces/` (`wp_open_review_workspace`, supporting `base_plan_id` and `variant_group`, plus `wp_workspace_fork` to clone `work/` from an existing `wr-*`):
- Coexisting variants & read-only history: plans sharing a non-empty `variant_group` do not auto-supersede each other in `ReviewPlans.open()`, allowing 2–3 comparable proposals to coexist. `_fresh()` allows read-only inspection (`workspace_read_file`, `workspace_diff`, `workspace_list_files`, `live_preview`) on superseded plans while keeping `StalePlan` strictly enforced on staging and mutating operations.
- Inspection & diff: `wp_list_workspaces` (`WP_OPS_READ`), `wp_workspace_list_files` (`WP_OPS_READ`), `wp_workspace_read_file` (Oscar `WP_OPS_ADMIN` + `WP_OPS_OPERATE`, secret-scrubbed line window), `wp_workspace_diff` (Oscar `WP_OPS_ADMIN` + `CODE_EDIT`, scrubbed `unified_diff`), `wp_workspace_status` (`WP_OPS_READ`).
- Edits & scaffolding (`WP_OPS_OPERATE`; `copy_from_parent` and `copy_woo_template` also require `WP_OPS_ADMIN`): `wp_workspace_patch_file`, `wp_workspace_create_file`, `wp_workspace_apply_changes` (atomic `edit`/`create`/`delete`/`revert`, up to 50 items), `wp_workspace_bulk_replace` (literal/regex across `work/`, run `dry_run=true` first), `wp_workspace_scaffold` (`child_theme_basics`, `site_plugin_basics`, `woo_compat_declaration`), `wp_workspace_theme_json` (RFC 6901 JSON pointer `set`/`remove`, up to 128 ops), `wp_workspace_copy_from_parent`, `wp_workspace_copy_woo_template` (copies clean WooCommerce core template into `work/woocommerce/<template>`, blocked on parent themes), `wp_workspace_product_edit` (sandbox product copy proposals in `product_edits.json`). Pure `.css` edits without script/PHP/dangerous URI constructs are not stamped `risk="high"` merely for targeting WooCommerce CSS selectors (PHP/JS/payment/checkout logic remains strict `high`).
- Static QA vs runtime QA:
  - `wp_workspace_static_checks`: pure-Python offline check (`skills/ecom_ops/wp_ops/static_checks.py`) covering unescaped superglobals, unsafe `$wpdb` queries, dangerous exec/eval/unserialize, missing AJAX/POST nonce or `current_user_can`, missing `ABSPATH` guards, deprecated WooCommerce helpers, and JSON lint. Sets `executed: true` and `live_calls: false` without invoking PHP or a sandbox (`excerpt` shown only to `WP_OPS_ADMIN`); it does **not** publish a `check_run` or unlock staging.
  - `wp_workspace_run_checks`: invokes `ReviewPlans.run_checks()` (server `run_catalog` or opt-in loopback sandbox when `AZOM_WP_SANDBOX=1`). Still leaves unavailable checks (`wpcs_php`, `plugin_check`, `phpunit`, `asset_lint`, and `browser_smoke`/`egress_probe`/`php_lint` when their runtimes are absent) as `unavailable`. Preserves compact, secret-scrubbed per-check `summary` and `findings` rows, attaches up to 4 validated PNG screenshots when created, and surfaces the chat staging button (`wpreview:stage:wr-...` / `wpreview:no:wr-...`) **only** when `status == "tests_passed"`, `check_run.published is True`, `environment == "staging"`, and `risk != "high"`.

### Audit hardening fixes

Recent audit fixes in the WordPress engineering path ensure:
1. `LocalTree` mock workspace open: `_verified_theme_pair()` and `_verified_plugin()` inspect local mock themes (`_scan_local_themes`) and `azom-site-<site_id>` plugins on `LocalTree` transports so `wp_open_review_workspace` copies real fixture trees instead of opening empty.
2. New-file staging creation: `ReviewPlans.approve_staging()` uses `LocalTree.create_bytes(..., overwrite=False)` to create new workspace files in the mock staging tree instead of failing with `FileMissing`.
3. `wp_workspace_run_checks` diagnostics: `_execute_workspace()` preserves bounded, secret-scrubbed per-check `summary`, `findings`, and overall `summary` in tool output.
4. Accurate `list_entries` tree walk: `LocalTree.list_entries()` and `SshFileTransport.list_entries()` yield explicit `(name, is_dir)` pairs (skipping symlinks/escapes) for `read_site_component()` and `_read_installed_index_files()`.
5. Secret-scrubbed search & code excerpts: `_scrub_search_text()` (`file_changes.py`) and `_scrub_bounded()` / `_REDACT_SECRET_LINES` (`agent_tools.py`) redact credentials and tokens from search, code index, static checks, read, and diff outputs.
6. Budget-stop summary & 16k history clip: `_finish_on_budget()` summarizes gathered evidence on loop cap, and assistant messages inside `run_bounded_file_tool_loop()` clip at 16,000 chars (`next_content[:16000]`).
7. `site_content` & catalog fallthrough: `WP_CODE_INTENT_RE` bypass in `chat_agent.py` plus `allow_wp_ops_fallthrough` in `site_content.py` route WP code queries past content/catalog prefetch when `/wp use <site-id>` is active.
8. Rejected plan directory cleanup: `ReviewPlans.open()` returns on `rejected` / `fork_not_approved` (`WooCoreRefused`, `ThirdPartyPluginRefused`, `ParentThemeBlocked`) before creating any workspace directory under `AZOM_DATA_DIR/wp_workspaces/`.

A diff plan stores the plan id, site, path, both hashes, risk, and status. The diff body is written under `AZOM_DATA_DIR/wp_file_payloads/<plan_id>` and is not stored in chat, audit, or the plan JSON. The backup script excludes that directory. Plans expire after 12 hours. A missing payload cannot be applied. High-risk plans, including checkout, cart, payment, and gateway paths or text, can be proposed and cannot be staged.

Mock `propose` and `file-apply` remain the local editor. `file-apply` sends the same text again and checks the current and resulting digests. It writes the mock tree. Live SFTP is read-only and refuses `write_bytes` before a socket; a live `file-apply` cannot change the customer site. Chat staging separates single-file plans (`wpfile:stage:fe-...` / `wpfile:no:fe-...`, `WP_OPS_OPERATE` on mock staging) from multi-file review workspaces (`wpreview:stage:wr-...` / `wpreview:no:wr-...`, requiring Oscar `WP_OPS_ADMIN` + `CODE_EDIT`, snapshot hash binding, `tests_passed` + `check_run.published`, and non-high risk on mock staging). `action:yes` does not apply a file plan or workspace. A production site is refused. The dashboard review page shows the stored diff to Oscar (`WP_OPS_ADMIN` and `CODE_EDIT`). Its production POST checks the plan id, both hashes, the path, and the environment, then returns `BackupNotVerified` and does not write. `restore()` remains `RestoreNotInMvp`. A mock manifest with `bytes_copied: false` does not open that gate.

Mock mode uses a directory under `AZOM_DATA_DIR/wp_files_mock/`. `AZOM_USE_MOCK=1` forces that directory even if `AZOM_WP_FILES_SSH=1`. Live SFTP runs only when mock mode is off and `AZOM_WP_FILES_SSH=1`. The client verifies the exact pinned host key before key authentication, uses only the site's stored key, and opens an SFTP subsystem without a shell. `SshFileTransport.open_batch()` reuses that verified SFTP session across nested read/list calls (`_theme_overview`, `_plugin_overview`, component search, and workspace component reads). It refuses password auth, SSH agent/config identities, symlinks, files over 64 KiB (`65536` bytes), and listings over 256 entries. Live SFTP is read-only; writes (`write_bytes`) are refused by the transport (`SshWriteDisabled`) before any socket call. A host ending in `.example.test` is refused before any connection. The dashboard cannot turn the flag on through an env overlay. File results keep `live_calls` false. The separate `ssh` field says whether the live transport was selected.

A staging edit needs `WP_OPS_OPERATE`. A production edit needs `WP_OPS_ADMIN` and `CODE_EDIT`. The same site lock used by health checks returns `SiteBusy` when it is held. Audit records the path, byte length, digest, and plan id. It does not record the file body.

## Homepage source review

“Granska startsidans källkod” (or a named page via `"granska sidan <slug|id> källkod"`, extracted by `extract_page_review_target()` in `skills/ecom_ops/wp_ops/page_review_intent.py`) is its own task. It does not enter the order loop, the drift loop, or the file-edit loop. The server ignores a URL in the message and uses only the site already selected for that actor, channel, and conversation. If no site is selected, the reply asks for `/wp use` and does not fetch a page or an order.

The turn runs these tools, in order, on that site only (passing optional `target_page` into `run_bounded_page_review_loop()` from `chat_agent.py` and `page_review_job.py`, resolved via `_resolve_target_page()` in `skills/ecom_ops/wp_ops/page_review.py`). Each call allows `site_id` and refuses every other argument. A foreign tool, including `lookup_order` and `show_page`, is refused and does not replace these steps. Instructions inside page content stay review data. The budget is 4 calls and 45 seconds. Before each call the server sets the client timeout to the time still left. A call that exceeds it returns `io_timeout` and is not a successful read. A later tool that never starts returns `time_budget`. Progress lines name the tool and the site. They do not quote source. The assistant text reports the steps. `tool_digest` stays empty, so a later order turn does not inherit this review.

| Step | Tool | What a real read returns |
|------|------|--------------------------|
| Front page | `wp_page_review_home` | `latest_posts` or `static_page`, `show_on_front`, `page_on_front`, `page_for_posts`, page id/slug/title/status/template, active theme relationship, and candidates from `hierarchy_candidates()` in `skills/ecom_ops/wp_ops/review_knowledge.py`. |
| Editor content | `wp_page_review_content` | The page from `get_page(..., context="edit")`, including `content.raw` when the actor may see it. |
| Source files | `wp_page_review_sources` | Theme and plugin files the server read, with path and line. |
| Browser preview | `wp_page_review_preview` | Desktop and mobile screenshots plus console and network errors when `AZOM_WP_LIVE_PREVIEW=1` and `AZOM_USE_MOCK=0`; otherwise `not_run/unavailable` (`browser_not_run`). |

Missing `show_on_front` stops the front-page step with `settings_incomplete` (except when a specific `target_page` is requested via `"granska sidan <slug|id> källkod"`, which resolves that page via `_resolve_target_page()`). The server does not guess a page and does not call `list_pages` on a default homepage review. An unknown active theme does not invent classic or block. A page template value is `template_assignment`. `assignment_state` is `verified` only when the selected file is that template. An earlier `front-page.php` stays selected. Otherwise the assignment stays unverified, `selection` is `candidate`, and `selected_template` is the hierarchy file that was read. If no hierarchy file was read, `selection` is `unavailable` and `selected_template` is empty. For block themes (`theme_kind == "block"`), `step_home()` reads Site Editor templates via `WordPressClient.list_templates()` (`/wp-json/wp/v2/templates`), returning `site_editor: "read"` and `site_editor_templates` when non-empty; otherwise `site_editor` stays `"unavailable"`. The candidate order lives only in `review_knowledge.py`. A classic page front uses `front-page.php`, then one safe assigned `.php` path, then `page-{slug}.php` and `page-{id}.php` when those are known, then `page.php`, `singular.php`, and `index.php`. A path that leaves the theme is not a candidate. A posts front uses `front-page.php`, `home.php`, and `index.php`. A block theme uses `templates/front-page.html`, `templates/home.html`, and `templates/index.html`. `theme.json` stays in the source inventory and is not a selected template. Each candidate hit is `read`, `missing`, `clipped`, or `capped`. A file over the 65536-byte read limit is `clipped`. The 32-file text cap is `capped`. Either mark stops the hierarchy. Later candidates are not read, recorded, or selected, and the parent does not replace the stopped file. Each classic name is listed once. The source inventory does not read that same relative path from the parent. The child stylesheet directory is first. A missing child file can be read from the parent as read-only evidence. WooCommerce overrides are files under the theme `woocommerce/` directory. The `woocommerce` plugin is not an edit target.

Keep three layers apart. `database` is settings and editor content. Stored `content.rendered` is still database content, not the DOM. `source_file` is a file the server read. `rendered_dom` is a browser result. A finding names the layer, a file and line or DOM evidence, the risk, that the bytes are untrusted, and whether the step was a real read. A viewer with `WP_OPS_READ` sees metadata without the raw excerpt or the source line. `WP_OPS_ADMIN` sees a short excerpt.

The preview step stays `not_run/unavailable` unless `AZOM_WP_LIVE_PREVIEW=1` and `AZOM_USE_MOCK=0` (or when disabled/mock, it never invents screenshots, console errors, or network errors). It does not open checkout or payment.

## Live preview (read-only)

Read-only live page inspection (`skills/ecom_ops/integrations/wp_live_preview.py`, `skills/ecom_ops/wp_ops/preview_overlay.py`, `ReviewPlans.live_preview()`) runs only when **process env** sets `AZOM_WP_LIVE_PREVIEW=1` (protected from dashboard env overlays in `runtime_env.py`) and `AZOM_USE_MOCK=0`:

- **Read-only fetch (`GET`, no cookies, no credentials):** `fetch_live_html()` fetches rendered public HTML over HTTPS from the registered site host via a cookie-free opener that refuses foreign-host redirects and blocked paths (`wp-admin`, `wp-login`, `wp-json`, `xmlrpc`, `wp-cron`, `admin-ajax`, `/cart`, `/checkout`, `/my-account`, `/handlekurv`, `/kasse`, `/min-konto`).
- **CDP `Fetch` browser isolation:** `capture_preview()` runs headless Chrome/Edge over loopback CDP with `Fetch.enable`, fulfilling the main navigation from the fetched/overlaid HTML, allowing `GET`/`HEAD` static assets from the registered host (plus allowlisted font/CDN hosts), and failing non-`GET`, tracker domains, admin/AJAX/REST/cart endpoints, and foreign hosts before they hit the network.
- **Overlay semantics:** `apply_overlay()` diffs workspace files against `base/` (or `origin/`, populated read-only by `copy_from_parent`), applies whitespace-tolerant static HTML fragment changes (skipping PHP logic hunks as `php_skipped`), and injects `style.css` (`strip_css_header`) as `<style data-azom-preview="child-css">`. PHP logic changes are not executed or visualized. `wp_inspect_element` probes rendered live or overlay DOM/computed styles and reports `health_checks`; `wp_preview_page` reuses cached live HTML/baseline PNGs when `reuse_live_baseline=True` and reports `visual_health` alongside `focus_selector` interactive sandbox highlighting.
- **Artifacts & dashboard gallery:** captures `live-desktop.png`, `live-mobile.png`, and (for overlaid workspaces) `preview-desktop.png`, `preview-mobile.png`, `focus-desktop.png`, `focus-mobile.png`, plus `change-desktop.png` / `change-mobile.png` crops highlighting the first changed element (`_WORKSPACE_PNG` / `_ARTIFACT_PNG`). In the dashboard (`wp_review.html`), `has_live_overlay_preview` renders the Live vs. Förhandsvisning gallery independently of `browser_smoke == "passed"`, alongside `forked_from`/`superseded_by` links, sibling `variant_group` switchers, and a recent `wr-*` workspace list on `wp_site.html`. Staging (`TestsNotPassed` / `EvidenceMissing`) and production (`BackupNotVerified` / `RestoreNotInMvp`) gates remain unchanged.
- **Honest sandbox smoke:** in `wp_browser_smoke.py`, `run_vertical_theme_render()` checks `_classify_theme_render()`. If a local sandbox render hits synthetic host `azom-plugin-host` for a non-host theme slug or hits `Call to undefined function` against a synthetic parent, `browser_smoke` reports `status: "unavailable"` with reason `sandbox_not_representative`, omits unrepresentative `base-*`/`work-*` PNGs, and keeps `representative: false` in `browser_smoke.json`. `php_lint` runs only a fixed `php -l` parse command, with an eight-second timeout, when PHP exists; it does not execute the reviewed PHP. A timeout is status `timeout`. An earlier syntax error stays `failed` if a later file times out. The source inventory reads direct theme templates/assets, one level under selected template/asset/WooCommerce folders, and at most four active plugin entry points when their paths are available. An unreadable plugin list is `unavailable`, not an empty list. WPCS, Plugin Check, PHPUnit, asset lint, browser smoke, and egress probe currently report `unavailable` because the isolated runtimes are not wired. Every check record has command, start/end time, exit code, summary, artifact path, and execution/simulation flags. The plan stores `check_run` with run id `cr-` plus 12 hex digits, site, plan, both hashes, the server policy, and profile `wp-review-catalog` version `1`. `published` is true only for `tests_passed` on that same snapshot. A tree change during the run is `RevisionChanged` and is not published. A newer refresh makes the in-flight run `StaleRun`. An expired plan is not revived. `woo_static` is an internal static pre-check with `evidence_class` `static_precheck` and `runtime_proof` false. Duplicate jobs, a `passed` row on a `not_applicable` policy, timeout, cancellation, and simulation do not open the suite. If a required job is `unavailable`, `not_run`, or simulated, the suite is `checks_incomplete`; a real failure is `tests_failed`. Preview returns `PreviewBlocked` until every required job passed and a sandbox is actually running. An unavailable sandbox record does not count as a booked preview. Local staging returns `TestsNotPassed` or `EvidenceMissing` until the published run matches the current revision. `approved_for_staging` without that run does not write. `AZOM_WP_SANDBOX=0` (default) does not start a container or local sandbox. `AZOM_WP_SANDBOX=1` opts into the local vertical sandbox below when a verified runtime exists; without one, `run_catalog` still leaves sandboxed checks `unavailable`. There is no model-written shell, PHP, or WP-CLI. The review page shows the run id and keeps `data-rendered-preview` `"false"` unless real PNGs and an executed passing `browser_smoke` exist.

`bin/fetch-wp-runtime.py` downloads WordPress 7.1.5 from `wordpress.org` and, on Windows x86 or x64, PHP 8.3.37 NTS from `downloads.php.net` (implementation in `skills/ecom_ops/integrations/wp_runtime_fetch.py`). The default directory is `%LOCALAPPDATA%\azom\wp-runtime` on Windows and `$XDG_DATA_HOME/azom/wp-runtime` (or `~/.local/share/azom/wp-runtime`) elsewhere. Linux, macOS, and Windows ARM exit `php_binary_unpinned` before a download. PHP publishes no official binary for those machines. This script does not compile one and does not call a package manager. The checksums are the published WordPress SHA1 and the PHP SHA256 from `releases.json`. A foreign redirect host, a checksum mismatch, or a zip member that is absolute, a drive path, a symlink, or contains `..` stops the extract. `runtime.json` is written only after `wp-includes/version.php` equals 7.1.5 and `php -v` reports PHP 8.3.37. `sandbox_enabled` and `server_started` are false. The script does not bind a port, start `php -S`, copy the process environment, or set `AZOM_WP_SANDBOX`. `run_php_lint` uses `PATH` by default and looks up the manifest only when an explicit `runtime_dir` is passed; a missing binary stays `unavailable` / `php_binary_missing`. `--check` re-reads the tree and probes `php -v` without downloading. A populated directory alone is not a rendered theme and does not open preview.

An opt-in local vertical render path lives in `skills/ecom_ops/integrations/wp_local_runtime.py` and `skills/ecom_ops/integrations/wp_browser_smoke.py` (tested via `AZOM_WP_RUNTIME_E2E=1` in `tests/test_wp_runtime_e2e.py`, skipped in the default suite). It requires `AZOM_WP_SANDBOX=1` and a local runtime resolved by `resolve_local_runtime()`: a verified `runtime.json` manifest, or on non-Windows (`os.name != "nt"`) a `<runtime_dir>/wordpress/` tree with `wp-includes/version.php` plus a verified system `php` binary on `PATH` (PHP 8.2–8.4 with `pdo_sqlite` in `php -m`). `LocalWordPressSandbox` boots WordPress with an isolated SQLite database bound strictly to `127.0.0.1` without modifying the fetched WordPress tree, and accepts either a theme (`theme_slug`/`theme_dir`, plus optional `parent_theme_slug`/`parent_theme_dir`) or a plugin via optional `plugin_slug` / `plugin_dir` (and optional `plugin_main_file`, serialized as `active_plugins` in SQLite `wp_options` alongside a synthetic host theme `azom-plugin-host` when `theme_dir` is omitted). `run_vertical_theme_render()` renders synthetic themes (`base` and `work`, desktop `1280x800` and mobile `390x844`) through headless Chromium/Edge with `--host-resolver-rules` blocking all non-loopback hosts and WordPress `WP_HTTP_BLOCK_EXTERNAL` + a mu-plugin egress guard; `run_vertical_plugin_smoke()` runs `php_lint`, boots the plugin in `LocalWordPressSandbox`, executes `run_browser_render` (falling back to `_probe_loopback_http` when no browser binary is installed) and `egress_probe`, and keeps `browser_smoke` bound to `requirements("plugin", ...)` policy. `SandboxGate` reports `running` only when an explicit starter proves a live loopback process with no published port, no Docker socket, no broad host mount, and no host secrets (`AZOM_WP_RUNTIME_DIR` is blocked from dashboard overlays). Check rows and artifacts are bound to `run_id`, `site_id`, `plan_id`, both revision hashes, the server check policy, and `wp-review-catalog` version `1`. When `AZOM_WP_SANDBOX=1`, `ReviewPlans.run_checks()` delegates via `_run_sandbox_catalog()` to `run_vertical_theme_render()` or `run_vertical_plugin_smoke()` (falling back to `run_catalog` on `ReviewError`); `AZOM_WP_SANDBOX=0` remains the default and `run_catalog` behavior is unchanged (`wpcs_php` and `plugin_check` stay `unavailable`). This path never contacts `azom.no` or customer sites.

Telegram stores the task as job kind `page_review`. The worker does not call the order chat. A follow-up while the job runs stays on that job and does not become an order lookup. Free-text “ja” and `action:yes` do not approve the review. The running bot process was not restarted with this code.

A review workspace uses plan ids `wr-` plus 12 hex digits. Public fields live in `AZOM_DATA_DIR/wp_review_plans.json`; that metadata is included in the normal backup. File bodies, diffs, manifests, and check artifacts live under `AZOM_DATA_DIR/wp_workspaces/` and are excluded from `bin/backup-azom-data.sh`. Existing theme files can be copied from the selected site. Existing plugin files require a server-verified active plugin slug; WooCommerce core remains refused as an edit target. Reads use the registered, allowlisted theme/plugin roots and reject sensitive paths, non-text files, symlinks, and configured size limits. A restored plan whose workspace is missing is marked `not_resumable`; checks, preview, and staging cannot resume from plan metadata alone. The workspace does not write the customer site. The Telegram notice builds an absolute review URL only from an HTTPS `AZOM_DASHBOARD_PUBLIC_URL`; the link contains only site and plan ids, requires dashboard login/RBAC, and does not approve. `WP_OPS_READ` can see the plan without the diff. The diff and the comment, staging, and production forms require `WP_OPS_ADMIN` and `CODE_EDIT`, plus CSRF. `POST /wp/<site_id>/reviews/<plan_id>/checks` requires `WP_OPS_OPERATE` and CSRF. It delegates to `ReviewPlans.run_checks()` (sandbox when `AZOM_WP_SANDBOX=1`, otherwise `run_catalog` leaving `published` false while a required job is unavailable). `ReviewPlans.artifact_file()` validates PNG artifacts under `artifacts/` (`base|work-desktop|mobile.png`, symlink-safe and PNG-magic checked) for `GET /wp/<site_id>/reviews/<plan_id>/artifact/<name>` (`WP_OPS_ADMIN` and `CODE_EDIT`, served as `image/png` with `Cache-Control: no-store`). In `infrastructure/dashboard/templates/wp_review.html`, synthetic sandbox screenshots are shown when present, and `data-rendered-preview` stays `"false"` unless real PNG artifacts exist alongside an executed passing `browser_smoke` row on the plan. Preview POST requires `WP_OPS_OPERATE` and still returns `PreviewBlocked` while checks are incomplete. Oscar can extend one plan once, by 12 hours, with `POST .../extend` and CSRF. Production POST sets `production_review` and returns `BackupNotVerified`. It does not write. `approved_for_production`, `deployed`, and `verified` are not reachable. The skill card is `skills/wordpress-review/SKILL.md`. It describes the layers and the tools. It does not grant a permission.

## SSH settings

The selected site registry provides defaults for `files_root` and `ssh_host`. Oscar can override those, plus the dedicated SSH user, port, the per-site private key, and one public host key, at `GET|POST /wp/<site_id>/ssh`. Submitting the registry root or host again does not pin it. An empty root or host inherits the selected registry only. Live SFTP requires an explicit per-site user ending in `-wp-reader`; it does not inherit the global user or identity key. `SSH_HOST`, `SSH_USER`, and `SSH_IDENTITY_FILE` belong to other SSH integrations and are not the WordPress SFTP host or identity.

A saved root must be an absolute path of at least three segments. It must not sit under `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/tmp`, `/run`, `/var/log`, `/var/lib`, `/var/run`, or `/var/spool`. The same check runs when an overlay row is applied and before a file is listed or written. `/var/www/mock-shop` and `/var/www/mock-staging` stay valid.

The private key must be unique to the restricted reader account; do not reuse a human or MobaXterm key. It is pasted once and written to `AZOM_DATA_DIR/ssh-keys/<site_id>` with mode `0600`. That directory is mode `0700` where the operating system allows it. Symlinked key files/directories and broad POSIX permissions are refused. Changing the configured SSH user while a key is saved requires a new per-site key. The key is written to a temporary file, the overlay is written next, and only then is the temporary file renamed onto the key. A passphrase-protected key is refused. A later empty paste leaves the file. The clear checkbox removes it. Live SFTP uses only this per-site stored key; global or external identity paths are not used. The key bytes are not returned, logged, or written into `wp_ssh.json`, `runtime.env`, audit, `public_dict`, or the page. `wp show` and `wp sites` do not include the identity path. `bin/backup-azom-data.sh` excludes `ssh-keys/` the same way it excludes `model_oauth/`.

Login requires an explicit non-root user whose name ends in `-wp-reader`, the port, a stored per-site key, and the pinned public host key. The client checks the server key before sending the public-key authentication request. It does not read SSH config, use agent keys, send a password, or request a shell. Oscar sees the SHA256 fingerprint. Jonatan sees only whether a pin is set. Saving settings does not open a connection or change `AZOM_WP_FILES_SSH`. A host ending in `.example.test`, a missing key, or a missing host pin is refused before authentication.

The server account must be provisioned separately before enabling live SFTP. Use a dedicated non-login `*-wp-reader` account; configure sshd to force `internal-sftp -R`, disable password authentication, PTY, agent/port/X11 forwarding for that account, and grant it read/traverse access only to the approved theme/plugin directories. Do not grant access to `wp-config.php`, backups, credentials, or other host files. Ensure those directories contain no symlinks that escape the approved tree; use a restricted chroot where the host layout permits it. Verify effective access as the service account. Live writes remain disabled even if the account has write permission. Do not enable `AZOM_WP_FILES_SSH` while the site is configured with the human `oscar` login or before the restricted account is verified.

One invalid site row in `wp_ssh.json` is skipped. That site keeps the registry values, and the settings page says the row was skipped. Other sites still load. A file that cannot be parsed still fails closed with `wp_ssh.json kan inte läsas.` Saves are serialized with a process lock and a file lock. Jonatan can read the root, host, user, port, and whether a key or host key is present. Jonatan has no form and does not see the identity path. `POST` needs `WP_OPS_ADMIN` on both staging and production.

### Dashboard

Routes sit behind the existing login:

| Path | Who | Purpose |
|------|-----|---------|
| `GET /wp` | `WP_OPS_READ` | Overview. Unknown and verified-mock backups are not green. |
| `GET /wp/<site_id>` | `WP_OPS_READ` | Checks, incidents, manifests, drills, secret ref name only. |
| `POST /wp/<site_id>/check` | staging: `WP_OPS_OPERATE`; production: `WP_OPS_ADMIN` | Mock read-only check. CSRF required. |
| `POST /wp/<site_id>/backup` | same as check | Create one local mock manifest. CSRF required. |
| `POST /wp/<site_id>/restore-drill` | `WP_OPS_OPERATE` | Staging drill. The form lists staging targets only. CSRF required. |
| `GET /wp/<site_id>/ssh` | `WP_OPS_READ` | Effective root, host, user, port, and whether a per-site key or host key is stored. No key material or global SSH settings. |
| `POST /wp/<site_id>/ssh` | `WP_OPS_ADMIN` | Save per-site SSH settings and a public host key. External identity paths and global SSH settings are refused. CSRF required. Does not open SSH or enable `AZOM_WP_FILES_SSH`. |
| `GET /wp/<site_id>/files` | `WP_OPS_READ` | Dashboard/CLI list or read one theme/plugin file. The chat read tool is Oscar-only. Opening the site page does not read files. |
| `POST /wp/<site_id>/files/propose` | staging: `WP_OPS_OPERATE`; production: `WP_OPS_ADMIN` and `CODE_EDIT` | Store a plan. The file is not written. CSRF required. |
| `POST /wp/<site_id>/files/apply` | same as propose | Write the mock file when the text and the current digest match. Live SFTP still refuses the write. CSRF required. |
| `GET /wp/<site_id>/files/plan/<plan_id>` | `WP_OPS_ADMIN` and `CODE_EDIT` | Review one stored diff. Jonatan receives 403. The page does not claim a write. |
| `POST /wp/<site_id>/files/deploy` | `WP_OPS_ADMIN` and `CODE_EDIT` | CSRF POST bound to the plan id, both hashes, path, and environment. Returns `BackupNotVerified` and does not write. |
| `GET /wp/<site_id>/reviews/<plan_id>` | `WP_OPS_READ` | Review metadata. The diff requires `WP_OPS_ADMIN` and `CODE_EDIT`. Login required. |
| `POST /wp/<site_id>/reviews/<plan_id>/comment` | `WP_OPS_ADMIN` and `CODE_EDIT` | Store a comment. CSRF required. Does not write the site. |
| `POST /wp/<site_id>/reviews/<plan_id>/staging` | `WP_OPS_ADMIN` and `CODE_EDIT` | Local staging only when the required checks passed. CSRF and both hashes required. |
| `POST /wp/<site_id>/reviews/<plan_id>/production` | `WP_OPS_ADMIN` and `CODE_EDIT` | Returns `BackupNotVerified` and does not write. CSRF and both hashes required. |
| `POST /wp/<site_id>/reviews/<plan_id>/checks` | `WP_OPS_OPERATE` | Run `ReviewPlans.run_checks()` (sandbox when `AZOM_WP_SANDBOX=1`, otherwise `run_catalog`). CSRF required. |
| `POST /wp/<site_id>/reviews/<plan_id>/preview` | `WP_OPS_OPERATE` | Book a preview. Returns `PreviewBlocked` while a required check is `not_run`. CSRF required. |
| `POST /wp/<site_id>/reviews/<plan_id>/extend` | `WP_OPS_ADMIN` and `CODE_EDIT` | Extend the 12-hour lifetime once. CSRF required. Does not write the site. |
| `GET /wp/<site_id>/reviews/<plan_id>/artifact/<name>` | `WP_OPS_ADMIN` and `CODE_EDIT` | Serve one validated sandbox PNG (`base|work-desktop|mobile.png`) via `ReviewPlans.artifact_file()` as `image/png` (`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`). |

Operator steps: [`runbooks/wp-ops-backup.md`](runbooks/wp-ops-backup.md), [`runbooks/wp-ops-files.md`](runbooks/wp-ops-files.md).

## Code

| Piece | Path |
|-------|------|
| Registry and validation | `skills/ecom_ops/wp_ops/registry.py`, `models.py` |
| Secret binding | `skills/ecom_ops/wp_ops/credentials.py` |
| Mock probes | `skills/ecom_ops/wp_ops/connectors.py` |
| Mock backup manifest | `skills/ecom_ops/wp_ops/backup.py` |
| State | `skills/ecom_ops/wp_ops/store.py` |
| Service | `skills/ecom_ops/wp_ops/service.py` |
| Typed file editor | `skills/ecom_ops/wp_ops/files.py` |
| Named diff plans | `skills/ecom_ops/wp_ops/file_changes.py` |
| Homepage review | `skills/ecom_ops/wp_ops/page_review.py`, `page_review_loop.py`, `page_review_intent.py`, `review_knowledge.py` |
| Review workspace | `skills/ecom_ops/wp_ops/review_workspace.py`, `review_plan.py`, `review_checks.py`, `review_runner.py` |
| Theme/plugin engineering toolchain | `skills/ecom_ops/wp_ops/agent_tools.py`, `code_index.py`, `playbooks.py`, `static_checks.py`, `theme_json.py`, `woo_templates.py`, `wporg_intel.py`, `integrations/wp_org_client.py` |
| Engineering & review skills | `skills/wordpress-theme-dev/SKILL.md`, `skills/wordpress-plugin-dev/SKILL.md`, `skills/wordpress-review/SKILL.md`, `skills/wordpress-ui-iteration/SKILL.md`, `skills/change-verification/SKILL.md` |
| Telegram review job | `skills/ecom_ops/bot/page_review_job.py` |
| SSH settings overlay | `skills/ecom_ops/wp_ops/ssh_settings.py` |
| Live file SSH, off unless enabled | `skills/ecom_ops/integrations/wp_files_ssh.py` |
| Opt-in local WP runtime & render | `skills/ecom_ops/integrations/wp_runtime_fetch.py`, `wp_review_process.py`, `wp_local_runtime.py`, `wp_browser_smoke.py`, `bin/fetch-wp-runtime.py` |
| CLI | `wp` in `skills/ecom_ops/cli.py` |
| Dashboard | `infrastructure/dashboard/wp_routes.py` |
| Tests | `tests/test_wp_ops.py`, `tests/test_wp_files.py`, `tests/test_wp_file_changes.py`, `tests/test_wp_ssh.py`, `tests/test_wp_dashboard.py`, `tests/test_wp_page_review.py`, `tests/test_wp_review_acceptance.py`, `tests/test_wp_review_evidence.py`, `tests/test_wp_review_home_matrix.py`, `tests/test_wp_runtime_fetch.py`, `tests/test_wp_local_runtime.py`, `tests/test_wp_runtime_e2e.py`, `tests/test_wp_toolchain_v2.py`, `tests/test_wp_etapp3.py` |

## Do / Do not

**Do:**

- Add customer sites only after scoped owner approval. `azom.no` is approved as a local registry target; this does not authorize live checks, backups, or production changes.
- Treat `unknown` and a missing check as not healthy.
- Report a homepage review check as `passed` only after that program ran. Leave a stub, a missing sandbox, and a missing browser as `not_run/unavailable`.
- Ask the owner before any live probe, off-site backup target, scanner, or production change.
- Say that a verified mock manifest is local metadata. `recovery_seconds` is the local drill duration, not a customer recovery-time objective.

**Do not:**

- Call a customer site, use a real application password, or turn on automatic updates from this phase.
- Add a general shell or let a caller compose an SSH command. File writes are available only in the local mock editor; live WordPress SFTP is read-only and stays off until a restricted account is verified and `AZOM_WP_FILES_SSH=1` is set with mock mode off.
- Put a private key or an SSH-specific host in either registry file. SSH settings and key material stay in the data directory; the public canonical site URL may be in the registry.
- Send backups to a third party, invent a storage vendor, or generate an encryption key in this phase.
- Treat `WpOpsService.restore` or a production drill target as an allowed restore.
- Market this as managed hosting, a malware scanner, or an availability SLA.
- Wire restore or production updates to free text, Messenger, or Telegram.
- Put order tools or an order digest on a homepage source-review turn.
- Treat stored `content.rendered` or a theme file as the rendered DOM.
- Mark `php_lint`, `browser_smoke`, or `egress_probe` passed when the binary, browser, or sandbox did not run.

## Good and bad

Good: `secret_ref: WP_SECRET_MOCK_SHOP` and the value only in that environment variable.

Bad: putting `WP_APP_PASSWORD` on a site, or pasting the password into `wp_sites.yaml`, a plan, or an audit detail.

Good: `browser_smoke` is `not_run/unavailable` and the suite is `checks_incomplete` when no browser ran.

Bad: marking `browser_smoke` passed because the preview step was named, or treating `content.rendered` as the DOM.
