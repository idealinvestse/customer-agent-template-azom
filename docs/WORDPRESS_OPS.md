# WordPress site operations

**Purpose:** Say what the WordPress operations surface does today, which phase it belongs to, and what is still only a proposal.  
**Audience:** Coding agents and developers. Operators can use the CLI and dashboard sections.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

**Document status:** Phase 1 mock health, Phase 2 local mock backup manifests, a bounded read-only bot tool loop, and preview-only update/restore plans are in the repository. A typed edit of one existing theme or plugin file is also available. It is off for live SSH unless mock mode is off and `AZOM_WP_FILES_SSH=1`. Oscar can set each site's document root and SSH login from the dashboard. Security scanning and update discovery/execution are not implemented. Production restore is not available. This is not managed WordPress hosting.

## Glossary

| Term | Meaning |
|------|---------|
| **Site registry** | `config/wp_sites.yaml`. One row per site and environment. No secret values. |
| **Secret ref** | Environment variable name `WP_SECRET_*` for that site only. The value is never returned to the CLI, UI, audit, or state file. |
| **Tone** | `ok` only when a check result is `ok`. `fail`, `unknown`, and a verified local mock backup use tone `attention` and must not be shown as green. |
| **Mock backup** | A metadata manifest in `wp_ops_state.json`. `provider` is `mock`, `offsite` is false, `encryption` is `unconfigured`, and `bytes_copied` is false. The SHA-256 digest covers that metadata. It is not encryption and not a copy of site files or a database. |
| **Confirmation** | How many consecutive failed probes open one incident. The minimum is 2. One failure is not “site down”. |
| **Content client** | `skills/ecom_ops/integrations/wordpress.py`. Posts, pages, settings, a theme list, and a plugin list plus active/inactive. It does not install, update, delete, or switch themes. It is not this operations surface and it still uses shared `WP_USERNAME` / `WP_APP_PASSWORD`. |
| **Typed file edit** | One existing `.php`, `.css`, `.js`, `.json`, `.txt`, or `.html` file inside one theme or plugin directory. The caller supplies the site, kind, slug, relative path, and new text. Azom computes the path. There is no command field. |
| **SSH settings** | Per-site document root, host, explicit non-root user, port, and login key. The registry stays the default. Overrides and the private key live under `AZOM_DATA_DIR`, not in git. |

## Phase map

| Phase | What it is | Where it lives | Status |
|-------|------------|----------------|--------|
| 0 | Owner writes host, URLs, versions, providers, maintenance window, and recovery objectives. No secrets in the doc. | Human record. Not a code task. | Not started. No pilot site is approved. |
| 1 | Per-site registry, secret refs, read-only health, mock uptime, alert dedupe, audit, CLI, dashboard. | Files below. | Mock only. No live site calls. |
| 2 | Local mock backup manifest, integrity of that manifest, staging drill, measured drill duration. | `wp_ops/backup.py`, state keys `backups` and `drills`, runbook below. | Mock only. No off-site copy. No production restore. |
| 3 | Core and plugin checksums, coverage labels, one scanner adapter. | Not created. | Not implemented. |
| 4 | Update plan, approval binding, staging execution, allowlisted adapter. Production writes stay manual. | Bot read tools and locked preview-only plan records. | Preview only. Update discovery, approval callbacks, execution, and production restore adapters are unavailable. A typed file edit is not this phase. |
| 5 | One staging site, then one production site, owner sign-off of runbooks and support hours. | Human soak. Agents must not mark it done. | Not started. |

## Telegram WordPress agent

The bot can select among `wp_list_sites`, `wp_site_status`, `wp_backup_status`, `wp_security_findings`, `wp_prepare_update_plan`, `wp_prepare_restore_plan`, and `wp_list_open_operations`. Every call validates its JSON arguments, registered `site_id`, and actor permission inside the server-side tool dispatcher. A site id is the only accepted site locator; tools do not accept URLs, hosts, paths, credentials, shell, PHP, or WP-CLI commands.

The follow-up loop executes at most 3 WordPress tool calls per turn, spends at most 30 seconds in tools, caps one result at 4 KB and combined tool results at 10 KB. Only the final answer is added to conversation history; model tool protocol and tool transcripts are not persisted. Tool reads and plan previews are audited.

`wp_prepare_update_plan` and `wp_prepare_restore_plan` create immutable, SHA-256-digested previews under `AZOM_DATA_DIR/wp_ops_plans.json`; previews expire after 12 hours. They are explicitly `unsupported`, `executable: false`, and `approval_available: false` while update discovery and restore adapters are missing. No callback can approve them, and no bot tool can execute them. Target versions remain empty until a provider verifies them. Restore still returns `RestoreNotInMvp`.

The current site's continuity record is stored with the existing conversation under `AZOM_DATA_DIR`. It contains only actor, channel, conversation, site id, bounded operation ids, and a 12-hour timestamp. Actor or channel changes clear the context; every subsequent site read is re-authorized and re-resolved against the registry. In Telegram or Messenger, use `/wp sites`, `/wp use <site-id>`, or `/wp clear` to select or clear context.

There is no scanner adapter. `wp_security_findings` returns `unknown`, `coverage: not_configured`, and `findings: null`; this must not be described as a clean scan. Backup status reflects local mock manifest metadata only and does not imply that site files or database bytes were copied.

Shop content on the live WordPress host is a different path. Telegram reads posts, the site title, the tagline, themes, and plugins through `WordPressClient` and returns before this tool loop. One post field, the site title or tagline, and turning one existing plugin on or off wait for Ja. Plugin on/off needs `WP_OPS_ADMIN`. That path does not install, update, delete, switch themes, or edit theme or plugin files, and it does not register the shop in `wp_sites.yaml`.

## Phase 1 behavior

### Data

`config/wp_sites.yaml` holds two synthetic sites, `mock-shop` (production) and `mock-staging` (staging). Hosts are under `example.test`. Do not replace them with a customer URL without a scoped owner approval.

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

The overview badge for a verified mock set says “verifierad, inte off-site”. It is not green. `healthy` stays false while checksum is unknown.

## Typed file edits

This is not Phase 4 and not a plugin or theme update. The model cannot compose a shell command. `sed`, `vim`, and `tee` stay off the general SSH allowlist. The file transport builds only `ls -1`, `cat`, `cp -p`, and `tee` for one path it computed itself.

The path has to stay inside `{files_root}/wp-content/themes|plugins/{slug}/`. `..`, a new file, a delete, and a `.azom-bak` file are refused. The text must be UTF-8 and at most 65536 bytes.

`propose` stores a plan id and two SHA-256 digests. It does not store the file body. `apply` sends the same text again. The current file must still match the earlier digest, and the read-back must match the new digest. The write copies the old bytes to a sibling `.azom-bak` on that machine. That copy is not an off-site backup. `restore()` is unchanged.

Mock mode uses a directory under `AZOM_DATA_DIR/wp_files_mock/`. `AZOM_USE_MOCK=1` forces that directory even if `AZOM_WP_FILES_SSH=1`. Live SSH runs only when mock mode is off and `AZOM_WP_FILES_SSH=1`. A host ending in `.example.test` is refused before any connection. The dashboard cannot turn the flag on through an env overlay. `live_calls` stays false. The separate `ssh` field says whether the live transport was selected.

A staging edit needs `WP_OPS_OPERATE`. A production edit needs `WP_OPS_ADMIN` and `CODE_EDIT`. The same site lock used by health checks returns `SiteBusy` when it is held. Audit records the path, byte length, digest, and plan id. It does not record the file body.

## SSH settings

`config/wp_sites.yaml` remains the default for `files_root` and `ssh_host`. Oscar can override those, plus the SSH user, port, a local identity path, and one public host key, at `GET|POST /wp/<site_id>/ssh`. Submitting the registry root or host again does not pin it. An empty root or host inherits the registry only. An empty user, port, or identity path inherits `SSH_USER`, `SSH_PORT`, and `SSH_IDENTITY_FILE`. Those three names are written to `AZOM_DATA_DIR/runtime.env` when the field is non-empty. A row in `secrets.env` still wins. `SSH_HOST` belongs to the other SSH client and is not the file-transport host.

A saved root must be an absolute path of at least three segments. It must not sit under `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/tmp`, `/run`, `/var/log`, `/var/lib`, `/var/run`, or `/var/spool`. The same check runs when an overlay row is applied and before a file is listed or written. `/var/www/mock-shop` and `/var/www/mock-staging` stay valid.

The private key is pasted once and written to `AZOM_DATA_DIR/ssh-keys/<site_id>` with mode `0600`. That directory is mode `0700` where the operating system allows it. The key is written to a temporary file, the overlay is written next, and only then is the temporary file renamed onto the key. A passphrase-protected key is refused. A later empty paste leaves the file. The clear checkbox removes it. The stored key is used before an external identity path. The key bytes are not returned, logged, or written into `wp_ssh.json`, `runtime.env`, audit, `public_dict`, or the page. `wp show` and `wp sites` do not include the identity path. `bin/backup-azom-data.sh` excludes `ssh-keys/` the same way it excludes `model_oauth/`.

Login for the file transport requires an explicit, validated non-root user, the port, and the key. There is no implicit `root` fallback; a missing or root user is refused before the subprocess starts. Its argv sets `BatchMode=yes`, `IdentitiesOnly=yes`, and `StrictHostKeyChecking=yes`. It does not send `SSH_PASSWORD` and it does not use `accept-new`. Live SSH also requires the pinned public host key (`ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp256`, or `sk-ssh-ed25519@openssh.com`). Oscar sees its SHA256 fingerprint. Jonatan sees only whether it is set. On a real connect the transport writes that line into a known_hosts file under the data directory and then runs ssh. Saving the form does not open a connection, does not scan keys, and does not change `AZOM_WP_FILES_SSH`. A host ending in `.example.test` is refused before any socket. A missing public host key is refused before any socket.

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
| `GET /wp/<site_id>/ssh` | `WP_OPS_READ` | Effective root, host, user, port, and whether a key or host key is stored. No key material. A viewer does not see the identity path. |
| `POST /wp/<site_id>/ssh` | `WP_OPS_ADMIN` | Save per-site SSH settings, a public host key, and optional global user, port, and identity path. CSRF required. Does not open SSH and does not enable `AZOM_WP_FILES_SSH`. |
| `GET /wp/<site_id>/files` | `WP_OPS_READ` | List or read one theme or plugin file. Opening the site page does not read files. |
| `POST /wp/<site_id>/files/propose` | staging: `WP_OPS_OPERATE`; production: `WP_OPS_ADMIN` and `CODE_EDIT` | Store a plan. The file is not written. CSRF required. |
| `POST /wp/<site_id>/files/apply` | same as propose | Write the file when the text and the current digest match. CSRF required. |

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
| SSH settings overlay | `skills/ecom_ops/wp_ops/ssh_settings.py` |
| Live file SSH, off unless enabled | `skills/ecom_ops/integrations/wp_files_ssh.py` |
| CLI | `wp` in `skills/ecom_ops/cli.py` |
| Dashboard | `infrastructure/dashboard/wp_routes.py` |
| Tests | `tests/test_wp_ops.py`, `tests/test_wp_files.py`, `tests/test_wp_ssh.py`, `tests/test_wp_dashboard.py` |

## Do / Do not

**Do:**

- Keep customer sites out of `wp_sites.yaml` until the owner approves a named pilot.
- Treat `unknown` and a missing check as not healthy.
- Ask the owner before any live probe, off-site backup target, scanner, or production change.
- Say that a verified mock manifest is local metadata. `recovery_seconds` is the local drill duration, not a customer recovery-time objective.

**Do not:**

- Call a customer site, use a real application password, or turn on automatic updates from this phase.
- Add a general shell or let a caller compose an SSH command. The typed file transport is the only file write, and live SSH stays off until `AZOM_WP_FILES_SSH=1` with mock mode off.
- Put a private key, or a customer host, in `wp_sites.yaml`. The dashboard overlay and the key file stay in the data directory.
- Send backups to a third party, invent a storage vendor, or generate an encryption key in this phase.
- Treat `WpOpsService.restore` or a production drill target as an allowed restore.
- Market this as managed hosting, a malware scanner, or an availability SLA.
- Wire restore or production updates to free text, Messenger, or Telegram.

## Good and bad

Good: `secret_ref: WP_SECRET_MOCK_SHOP` and the value only in that environment variable.

Bad: putting `WP_APP_PASSWORD` on a site, or pasting the password into `wp_sites.yaml`, a plan, or an audit detail.
