# WordPress site operations

**Purpose:** Say what the WordPress operations surface does today, which phase it belongs to, and what is still only a proposal.  
**Audience:** Coding agents and developers. Operators can use the CLI and dashboard sections.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

**Document status:** Phase 1 mock health, Phase 2 local mock backup manifests, bounded bot reads, named theme/plugin diff plans, preview-only update/restore plans, and a read-only homepage source review are in the repository. The local runtime registry targets `azom.no` as production; this does not enable live checks, backups, or SSH. Oscar can read a window of one existing theme or plugin file and review a stored diff. Staging apply is the local mock tree after a plan-id button. The production review POST returns `BackupNotVerified` and does not write. Live file access uses key-only SFTP, has no shell or write path, and remains off until a dedicated restricted account is provisioned and `AZOM_WP_FILES_SSH=1` is explicitly set. Homepage review does not call order tools. PHP syntax runs through fixed `php -l` when the binary is present; otherwise it is `unavailable`. WPCS, Plugin Check, PHPUnit, asset lint, egress checks, WordPress runtime, and browser rendering remain unavailable because isolated runtimes are not wired. `AZOM_WP_SANDBOX` stays off. Security scanning and update discovery/execution are not implemented. Production restore is not available. This is not Phase 4 and not managed WordPress hosting.

## Glossary

| Term | Meaning |
|------|---------|
| **Site registry** | Live mode (`AZOM_USE_MOCK=0`) uses `AZOM_DATA_DIR/wp_sites.yaml` when present, otherwise `config/wp_sites.yaml`. Mock mode (`AZOM_USE_MOCK=1` or unset) uses `config/wp_sites.mock.yaml`. One row per site and environment. No secret values. |
| **Secret ref** | Environment variable name `WP_SECRET_*` for that site only. The value is never returned to the CLI, UI, audit, or state file. |
| **Tone** | `ok` only when a check result is `ok`. `fail`, `unknown`, and a verified local mock backup use tone `attention` and must not be shown as green. |
| **Mock backup** | A metadata manifest in `wp_ops_state.json`. `provider` is `mock`, `offsite` is false, `encryption` is `unconfigured`, and `bytes_copied` is false. The SHA-256 digest covers that metadata. It is not encryption and not a copy of site files or a database. |
| **Confirmation** | How many consecutive failed probes open one incident. The minimum is 2. One failure is not “site down”. |
| **Content client** | `skills/ecom_ops/integrations/wordpress.py`. Posts, pages, settings, a theme list, and a plugin list plus active/inactive. It does not install, update, delete, or switch themes. It is not this operations surface and it still uses shared `WP_USERNAME` / `WP_APP_PASSWORD`. |
| **Typed file edit** | One existing `.php`, `.css`, `.js`, `.json`, `.txt`, or `.html` file inside one theme or plugin directory. The caller supplies the site, kind, slug, relative path, and new text. Azom computes the path. There is no command field. |
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

The follow-up loop executes at most 3 WordPress tool calls per turn, spends at most 30 seconds in tools, caps one result at 4 KB and combined tool results at 10 KB. Only the final answer is added to conversation history; model tool protocol and tool transcripts are not persisted. Tool reads and plan previews are audited.

`wp_prepare_update_plan` and `wp_prepare_restore_plan` create immutable, SHA-256-digested previews under `AZOM_DATA_DIR/wp_ops_plans.json`; previews expire after 12 hours. They are explicitly `unsupported`, `executable: false`, and `approval_available: false` while update discovery and restore adapters are missing. No callback can approve them, and no bot tool can execute them. Target versions remain empty until a provider verifies them. Restore still returns `RestoreNotInMvp`.

The current site's continuity record is stored with the existing conversation under `AZOM_DATA_DIR`. It contains only actor, channel, conversation, site id, bounded operation ids, and a 12-hour timestamp. Actor or channel changes clear the context; every subsequent site read is re-authorized and re-resolved against the registry. In Telegram or Messenger, use `/wp sites`, `/wp use <site-id>`, or `/wp clear` to select or clear context.

There is no scanner adapter. `wp_security_findings` returns `unknown`, `coverage: not_configured`, and `findings: null`; this must not be described as a clean scan. Backup status reflects local mock manifest metadata only and does not imply that site files or database bytes were copied.

Shop content on the live WordPress host is a different path. Telegram reads posts, the site title, the tagline, themes, and plugins through `WordPressClient` and returns before this tool loop. One post field, the site title or tagline, and turning one existing plugin on or off wait for Ja. Plugin on/off needs `WP_OPS_ADMIN`. That path does not install, update, delete, switch themes, or edit theme or plugin files, and it does not register the shop in the site-operations registry.

## Phase 1 behavior

### Data

The live `config/wp_sites.yaml` and local `AZOM_DATA_DIR/wp_sites.yaml` contain only the approved `azom-no` production target. The record contains the public canonical URL and unknown capability/version values; it has no document root, SSH host, or credentials. Live checks and backup adapters remain unavailable, and live file SSH remains off. The ignored runtime registry takes precedence in live mode. Mock mode uses `config/wp_sites.mock.yaml`, which contains the synthetic shop and staging fixtures.

Each site has its own `secret_ref`. `WP_USERNAME`, `WP_APP_PASSWORD`, and any name that is not `WP_SECRET_*` are rejected. Two sites cannot share a ref. A caller who asks for another site’s ref is denied before that variable is read.

Check results, incidents, backup manifests, and drills are stored in `AZOM_DATA_DIR/wp_ops_state.json`. Audit rows go through `ecom_ops.audit.log_action` with action `wp_ops_check`, `wp_ops_backup`, `wp_ops_restore_drill`, or `wp_ops_restore`. Neither store receives the secret value, a response body, or an exception string.

### Checks

The probes are in-process. `skills/ecom_ops/wp_ops/` does not import HTTP, sockets, subprocess, or the content client.

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

The path has to stay inside `{files_root}/wp-content/themes|plugins/{slug}/`. `..`, a delete, and a `.azom-bak` file are refused. The legacy CLI editor still refuses a new file. A chat create-plan may name a new file only when the parent directory already exists and the base hash is the empty-file digest. WooCommerce core (`woocommerce`) is refused. A parent theme is refused only when the active child theme is known and the slug is that parent. The text must be UTF-8 and at most 65536 bytes. `wp_read_theme_plugin_file` is Oscar-only. It returns a window, 4096 bytes by default and 8192 at most, plus `next_offset` when bytes remain. The digest is the whole file. Source content is treated as untrusted data.

The file turn is separate from the drift loop. It allows at most 4 reads, 1 literal search, 8 calls, and 45 seconds. Search skips `vendor`, `node_modules`, `cache`, `uploads`, and `.git`, and returns short excerpts. A drift turn does not execute a file tool.

A diff plan stores the plan id, site, path, both hashes, risk, and status. The diff body is written under `AZOM_DATA_DIR/wp_file_payloads/<plan_id>` and is not stored in chat, audit, or the plan JSON. The backup script excludes that directory. Plans expire after 12 hours. A missing payload cannot be applied. High-risk plans, including checkout, cart, payment, and gateway paths or text, can be proposed and cannot be staged.

Mock `propose` and `file-apply` remain the local editor. `file-apply` sends the same text again and checks the current and resulting digests. It writes the mock tree. Live SFTP is read-only and refuses `write_bytes` before a socket; a live `file-apply` cannot change the customer site. Chat staging writes only when the site environment is `staging`, the transport is the local mock tree, the risk is not high, and the button `wpfile:stage:{plan_id}` carries the plan id. `action:yes` does not apply a file plan. A production site is refused. The dashboard review page shows the stored diff to Oscar (`WP_OPS_ADMIN` and `CODE_EDIT`). Its production POST checks the plan id, both hashes, the path, and the environment, then returns `BackupNotVerified` and does not write. `restore()` remains `RestoreNotInMvp`. A mock manifest with `bytes_copied: false` does not open that gate.

Mock mode uses a directory under `AZOM_DATA_DIR/wp_files_mock/`. `AZOM_USE_MOCK=1` forces that directory even if `AZOM_WP_FILES_SSH=1`. Live SFTP runs only when mock mode is off and `AZOM_WP_FILES_SSH=1`. The client verifies the exact pinned host key before key authentication, uses only the site's stored key, and opens an SFTP subsystem without a shell. It refuses password auth, SSH agent/config identities, symlinks, files over 64 KiB, and listings over 256 entries. Live SFTP is read-only; writes are refused by the transport. A host ending in `.example.test` is refused before any connection. The dashboard cannot turn the flag on through an env overlay. File results keep `live_calls` false. The separate `ssh` field says whether the live transport was selected.

A staging edit needs `WP_OPS_OPERATE`. A production edit needs `WP_OPS_ADMIN` and `CODE_EDIT`. The same site lock used by health checks returns `SiteBusy` when it is held. Audit records the path, byte length, digest, and plan id. It does not record the file body.

## Homepage source review

“Granska startsidans källkod” is its own task. It does not enter the order loop, the drift loop, or the file-edit loop. The server ignores a URL in the message and uses only the site already selected for that actor, channel, and conversation. If no site is selected, the reply asks for `/wp use` and does not fetch a page or an order.

The turn runs these tools, in order, on that site only. Each call allows `site_id` and refuses every other argument. A foreign tool, including `lookup_order` and `show_page`, is refused and does not replace these steps. Instructions inside page content stay review data. The budget is 4 calls and 45 seconds. Before each call the server sets the client timeout to the time still left. A call that exceeds it returns `io_timeout` and is not a successful read. A later tool that never starts returns `time_budget`. Progress lines name the tool and the site. They do not quote source. The assistant text reports the steps. `tool_digest` stays empty, so a later order turn does not inherit this review.

| Step | Tool | What a real read returns |
|------|------|--------------------------|
| Front page | `wp_page_review_home` | `latest_posts` or `static_page`, `show_on_front`, `page_on_front`, `page_for_posts`, page id/slug/title/status/template, active theme relationship, and candidates from `hierarchy_candidates()` in `skills/ecom_ops/wp_ops/review_knowledge.py`. |
| Editor content | `wp_page_review_content` | The page from `get_page(..., context="edit")`, including `content.raw` when the actor may see it. |
| Source files | `wp_page_review_sources` | Theme and plugin files the server read, with path and line. |
| Browser preview | `wp_page_review_preview` | Desktop and mobile screenshots plus console and network errors, only after a real isolated browser run. |

Missing `show_on_front` stops the front-page step with `settings_incomplete`. The server does not guess a page and does not call `list_pages`. An unknown active theme does not invent classic or block. A page template value is `template_assignment`. `assignment_state` is `verified` only when that file was read. Otherwise the assignment stays unverified, `selection` is `candidate`, and `selected_template` is the hierarchy file that was read, or empty. `site_editor` stays `unavailable` until a database template is actually read. The candidate order lives only in `review_knowledge.py`. A classic page front uses `front-page.php`, then `page-{slug}.php` and `page-{id}.php` when those are known, then `page.php`, `singular.php`, and `index.php`. A posts front uses `front-page.php`, `home.php`, and `index.php`. A block theme uses `templates/front-page.html`, `templates/home.html`, `templates/index.html`, and `theme.json`. The child stylesheet directory is first. A parent file is read-only evidence. WooCommerce overrides are files under the theme `woocommerce/` directory. The `woocommerce` plugin is not an edit target.

Keep three layers apart. `database` is settings and editor content. Stored `content.rendered` is still database content, not the DOM. `source_file` is a file the server read. `rendered_dom` is a browser result. A finding names the layer, a file and line or DOM evidence, the risk, that the bytes are untrusted, and whether the step was a real read. A viewer with `WP_OPS_READ` sees metadata without the raw excerpt or the source line. `WP_OPS_ADMIN` sees a short excerpt.

The preview step stays `not_run/unavailable` until a real browser runs. It does not invent screenshots, console errors, or network errors. It does not open checkout or payment. `php_lint` runs only a fixed `php -l` parse command, with an eight-second timeout, when PHP exists; it does not execute the reviewed PHP. A timeout is status `timeout`. An earlier syntax error stays `failed` if a later file times out. The source inventory reads direct theme templates/assets, one level under selected template/asset/WooCommerce folders, and at most four active plugin entry points when their paths are available. An unreadable plugin list is `unavailable`, not an empty list. WPCS, Plugin Check, PHPUnit, asset lint, browser smoke, and egress probe currently report `unavailable` because the isolated runtimes are not wired. Every check record has command, start/end time, exit code, summary, artifact path, and execution/simulation flags. The plan stores `check_run` with run id `cr-` plus 12 hex digits, site, plan, both hashes, the server policy, and profile `wp-review-catalog` version `1`. `published` is true only for `tests_passed` on that same snapshot. A tree change during the run is `RevisionChanged` and is not published. A newer refresh makes the in-flight run `StaleRun`. An expired plan is not revived. `woo_static` is an internal static pre-check with `evidence_class` `static_precheck` and `runtime_proof` false. Duplicate jobs, a `passed` row on a `not_applicable` policy, timeout, cancellation, and simulation do not open the suite. If a required job is `unavailable`, `not_run`, or simulated, the suite is `checks_incomplete`; a real failure is `tests_failed`. Preview returns `PreviewBlocked` until every required job passed and a sandbox is actually running. An unavailable sandbox record does not count as a booked preview. Local staging returns `TestsNotPassed` or `EvidenceMissing` until the published run matches the current revision. `approved_for_staging` without that run does not write. `AZOM_WP_SANDBOX=0` does not start a container. `AZOM_WP_SANDBOX=1` still refuses because no runtime is wired. There is no model-written shell, PHP, or WP-CLI. The review page shows the run id and says it is not a rendered preview.

Telegram stores the task as job kind `page_review`. The worker does not call the order chat. A follow-up while the job runs stays on that job and does not become an order lookup. Free-text “ja” and `action:yes` do not approve the review. The running bot process was not restarted with this code.

A review workspace uses plan ids `wr-` plus 12 hex digits. Public fields live in `AZOM_DATA_DIR/wp_review_plans.json`; that metadata is included in the normal backup. File bodies, diffs, manifests, and check artifacts live under `AZOM_DATA_DIR/wp_workspaces/` and are excluded from `bin/backup-azom-data.sh`. Existing theme files can be copied from the selected site. Existing plugin files require a server-verified active plugin slug; WooCommerce core remains refused as an edit target. Reads use the registered, allowlisted theme/plugin roots and reject sensitive paths, non-text files, symlinks, and configured size limits. A restored plan whose workspace is missing is marked `not_resumable`; checks, preview, and staging cannot resume from plan metadata alone. The workspace does not write the customer site. The Telegram notice builds an absolute review URL only from an HTTPS `AZOM_DASHBOARD_PUBLIC_URL`; the link contains only site and plan ids, requires dashboard login/RBAC, and does not approve. `WP_OPS_READ` can see the plan without the diff. The diff and the comment, staging, and production forms require `WP_OPS_ADMIN` and `CODE_EDIT`, plus CSRF. `POST /wp/<site_id>/reviews/<plan_id>/checks` requires `WP_OPS_OPERATE` and CSRF. It runs the server catalog and leaves `published` false while a required job is unavailable. Preview POST requires `WP_OPS_OPERATE` and still returns `PreviewBlocked` while checks are incomplete. Oscar can extend one plan once, by 12 hours, with `POST .../extend` and CSRF. Production POST sets `production_review` and returns `BackupNotVerified`. It does not write. `approved_for_production`, `deployed`, and `verified` are not reachable. The skill card is `skills/wordpress-review/SKILL.md`. It describes the layers and the tools. It does not grant a permission.

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
| `POST /wp/<site_id>/reviews/<plan_id>/preview` | `WP_OPS_OPERATE` | Book a preview. Returns `PreviewBlocked` while a required check is `not_run`. CSRF required. |
| `POST /wp/<site_id>/reviews/<plan_id>/extend` | `WP_OPS_ADMIN` and `CODE_EDIT` | Extend the 12-hour lifetime once. CSRF required. Does not write the site. |

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
| Review skill | `skills/wordpress-review/SKILL.md` |
| Telegram review job | `skills/ecom_ops/bot/page_review_job.py` |
| SSH settings overlay | `skills/ecom_ops/wp_ops/ssh_settings.py` |
| Live file SSH, off unless enabled | `skills/ecom_ops/integrations/wp_files_ssh.py` |
| CLI | `wp` in `skills/ecom_ops/cli.py` |
| Dashboard | `infrastructure/dashboard/wp_routes.py` |
| Tests | `tests/test_wp_ops.py`, `tests/test_wp_files.py`, `tests/test_wp_file_changes.py`, `tests/test_wp_ssh.py`, `tests/test_wp_dashboard.py`, `tests/test_wp_page_review.py`, `tests/test_wp_review_acceptance.py` |

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
