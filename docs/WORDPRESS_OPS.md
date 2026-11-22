# WordPress site operations

**Purpose:** Say what the WordPress operations surface does today, which phase it belongs to, and what is still only a proposal.  
**Audience:** Coding agents and developers. Operators can use the CLI and dashboard sections.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

**Document status:** Phase 1 mock health and Phase 2 local mock backup manifests are in the repository. Phases 0 and 3–5 are not implemented. Production restore is not available. This is not managed WordPress hosting.

## Glossary

| Term | Meaning |
|------|---------|
| **Site registry** | `config/wp_sites.yaml`. One row per site and environment. No secret values. |
| **Secret ref** | Environment variable name `WP_SECRET_*` for that site only. The value is never returned to the CLI, UI, audit, or state file. |
| **Tone** | `ok` only when a check result is `ok`. `fail`, `unknown`, and a verified local mock backup use tone `attention` and must not be shown as green. |
| **Mock backup** | A metadata manifest in `wp_ops_state.json`. `provider` is `mock`, `offsite` is false, `encryption` is `unconfigured`, and `bytes_copied` is false. The SHA-256 digest covers that metadata. It is not encryption and not a copy of site files or a database. |
| **Confirmation** | How many consecutive failed probes open one incident. The minimum is 2. One failure is not “site down”. |
| **Content client** | `skills/ecom_ops/integrations/wordpress.py`. Posts, pages, and settings. It is not this operations surface and it still uses shared `WP_USERNAME` / `WP_APP_PASSWORD`. |

## Phase map

| Phase | What it is | Where it lives | Status |
|-------|------------|----------------|--------|
| 0 | Owner writes host, URLs, versions, providers, maintenance window, and recovery objectives. No secrets in the doc. | Human record. Not a code task. | Not started. No pilot site is approved. |
| 1 | Per-site registry, secret refs, read-only health, mock uptime, alert dedupe, audit, CLI, dashboard. | Files below. | Mock only. No live site calls. |
| 2 | Local mock backup manifest, integrity of that manifest, staging drill, measured drill duration. | `wp_ops/backup.py`, state keys `backups` and `drills`, runbook below. | Mock only. No off-site copy. No production restore. |
| 3 | Core and plugin checksums, coverage labels, one scanner adapter. | Not created. | Not implemented. |
| 4 | Update plan, approval binding, staging execution, allowlisted adapter. Production writes stay manual. | Permission `WP_OPS_ADMIN` is reserved. No plan store. | Not implemented. |
| 5 | One staging site, then one production site, owner sign-off of runbooks and support hours. | Human soak. Agents must not mark it done. | Not started. |

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
| `WP_OPS_READ` | `viewer`, `read_only`, `operator`, `full_admin` | List and show sites. |
| `WP_OPS_OPERATE` | `operator`, `full_admin` | Run checks, create a mock backup, and record a staging drill on `staging`. |
| `WP_OPS_ADMIN` | `full_admin` | Run checks and create a mock backup on `production`. Reserved for later update approval. |
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
# expect: JSON with ok true or false. live_calls is false.
# expect: a verified backup has green false, offsite false, encryption unconfigured, bytes_copied false.
```

`wp check` takes `--id` or `--due`, not both. `wp restore` is not a command. A drill refuses a production target with `TargetNotStaging`.

## Phase 2 behavior

A backup is one id for the labels `files` and `database`. Success requires the manifest digest to match. An integrity failure is stored as `completion: failed` and cannot be drilled. The default retention is 14 days (allowed range 1–90) and is evaluated when the record is read. An expired manifest stays in the state file and is not a success.

`create_backup` on `staging` needs `WP_OPS_OPERATE`. The same call on `production` needs `WP_OPS_ADMIN`. Both stay `live_calls: false`. A viewer can list backups. The site lock is the same one checks use.

`restore_drill` needs `WP_OPS_OPERATE` and a staging target. The source manifest must be verified and inside retention. The drill stores `started_at`, `finished_at`, `recovery_seconds` for the local verify, both parts, the source backup id, and the target site id. A WooCommerce source sets `order_reconciliation: required` and `orders_mutated: false`. The same backup and target inside the target site’s dedupe window returns the previous drill and does not write another one. `restore()` is unchanged and still refuses every actor, including Oscar.

The overview badge for a verified mock set says “verifierad, inte off-site”. It is not green. `healthy` stays false while checksum is unknown.

### Dashboard

Routes sit behind the existing login:

| Path | Who | Purpose |
|------|-----|---------|
| `GET /wp` | `WP_OPS_READ` | Overview. Unknown and verified-mock backups are not green. |
| `GET /wp/<site_id>` | `WP_OPS_READ` | Checks, incidents, manifests, drills, secret ref name only. |
| `POST /wp/<site_id>/check` | staging: `WP_OPS_OPERATE`; production: `WP_OPS_ADMIN` | Mock read-only check. CSRF required. |
| `POST /wp/<site_id>/backup` | same as check | Create one local mock manifest. CSRF required. |
| `POST /wp/<site_id>/restore-drill` | `WP_OPS_OPERATE` | Staging drill. The form lists staging targets only. CSRF required. |

Operator steps: [`runbooks/wp-ops-backup.md`](runbooks/wp-ops-backup.md).

## Code

| Piece | Path |
|-------|------|
| Registry and validation | `skills/ecom_ops/wp_ops/registry.py`, `models.py` |
| Secret binding | `skills/ecom_ops/wp_ops/credentials.py` |
| Mock probes | `skills/ecom_ops/wp_ops/connectors.py` |
| Mock backup manifest | `skills/ecom_ops/wp_ops/backup.py` |
| State | `skills/ecom_ops/wp_ops/store.py` |
| Service | `skills/ecom_ops/wp_ops/service.py` |
| CLI | `wp` in `skills/ecom_ops/cli.py` |
| Dashboard | `infrastructure/dashboard/wp_routes.py` |
| Tests | `tests/test_wp_ops.py`, `tests/test_wp_dashboard.py` |

## Do / Do not

**Do:**

- Keep customer sites out of `wp_sites.yaml` until the owner approves a named pilot.
- Treat `unknown` and a missing check as not healthy.
- Ask the owner before any live probe, off-site backup target, scanner, or production change.
- Say that a verified mock manifest is local metadata. `recovery_seconds` is the local drill duration, not a customer recovery-time objective.

**Do not:**

- Call a customer site, use a real application password, or turn on automatic updates from this phase.
- Add a general shell, SSH, or PHP execution tool for these operations.
- Send backups to a third party, invent a storage vendor, or generate an encryption key in this phase.
- Treat `WpOpsService.restore` or a production drill target as an allowed restore.
- Market this as managed hosting, a malware scanner, or an availability SLA.
- Wire restore or production updates to free text, Messenger, or Telegram.

## Good and bad

Good: `secret_ref: WP_SECRET_MOCK_SHOP` and the value only in that environment variable.

Bad: putting `WP_APP_PASSWORD` on a site, or pasting the password into `wp_sites.yaml`, a plan, or an audit detail.
