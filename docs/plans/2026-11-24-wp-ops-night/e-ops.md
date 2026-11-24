# WordPress ops evidence

**Purpose:** Record how each site capability behaves in code, and where that behavior disagrees with the WordPress ops docs.  
**Audience:** Coding agents on the 2026-11-24 WordPress ops pass.  
**Read this first:** [`../../WORDPRESS_OPS.md`](../../WORDPRESS_OPS.md), [`../../runbooks/wp-ops-backup.md`](../../runbooks/wp-ops-backup.md).

The stored capability enum is only `available`, `unavailable`, or `unknown`. A missing key becomes `unknown` in `parse_site`. The labels below are the behavior the code actually runs. They are not extra enum values. `verified` is not a capability mode.

## Capability behavior

| Capability | Behavior | What the code does | Evidence |
|------------|----------|--------------------|----------|
| `hosting_api` | registry_only | `parse_site` stores it and `public_dict` returns it. No reader in `service.py`, `backup.py`, `registry.py`, or `store.py` calls a host API. | code_confirmed |
| `wp_cli` | registry_only | Same store-and-return path. `connectors.py` states there is no WP-CLI adapter. | code_confirmed |
| `staging` | registry_only | Drills use `Site.environment`, not this flag. `config/wp_sites.mock.yaml` sets `mock-staging` to `staging: unavailable` while `environment` is `staging`. | code_confirmed |
| `backup` | mock | `create_backup` writes a `MockBackup` metadata manifest. `provider` is `mock`, `offsite` is false, `encryption` is `unconfigured`, `payload` is `metadata`, and `bytes_copied` is false. The registry flag stays unread. | code_confirmed, test_confirmed |
| `restore` | unavailable | `WpOpsService.restore` returns `RestoreNotInMvp` and does not read the manifest. | code_confirmed, test_confirmed |
| `uptime` | mock | `WpOpsService` defaults to `MockUptime`. The registry flag is not read. `*.example.test` can be `ok`. Any other host is `unknown` with `UptimeProviderUnconfigured`. | code_confirmed |
| `security_scanner` | unconfigured | `WpOpsTools._security` always returns `status: unknown`, `coverage: not_configured`, and `findings: null`. It echoes the registry token and does not branch on it. | code_confirmed, test_confirmed |

`backup` can reach completion `verified` after `MockBackup.verify` matches the metadata digest. That word is a manifest completion value. It does not make the capability verified.

## Backup and restore

`new_manifest` and `MockBackup.verify` cover labels `files` and `database`. They do not copy site files or a database. `create_backup` stores that record in `wp_ops_state.json` and, on a matching digest, sets `completion` to `verified`. `restore` ignores the record for every actor, including Oscar. `restore_drill` rechecks the digest, refuses a production target with `TargetNotStaging`, and appends a drill row with `bytes_copied` false. `recovery_seconds` is the local verify duration.

`bin/backup-azom-data.sh` is a different backup. Its tarball is the agent data directory. The exclude list drops `wp_file_payloads`, `wp_workspaces`, `ssh-keys`, and `wp_ssh.lock`. It does not exclude `wp_ops_state.json`, `wp_review_plans.json`, or `wp_files_mock`. Optional `DEST` rsync copies that directory off the machine. A restored review plan whose workspace is missing is `not_resumable`. `WpOpsService.restore` does not unpack this tarball.

The runbook says the mock manifest does not leave the machine. That matches `offsite: false` on the manifest. It does not match the host script, which can copy the state file that holds the manifest. This script was not executed. Running it was blocked because this pass must not back up a real host.

## Whether a mock backup can look green

No. A verified mock backup is not a green badge.

`_summary` forces `backup.green` to false. When the catalog result is `verified`, tone is `attention` because the result is not `ok`. `infrastructure/dashboard/templates/wp_sites.html` renders `verifierad, inte off-site` with `data-tone="attention"` for that result. `wp_site.html` does the same for a check whose result is `verified`, and for a manifest whose completion is `verified`. `healthy` stays false because `_collect` always stores `checksum` and the raw `backup` check as `unknown`.

The same page can still show a green `ok` badge for `http_uptime`, `tls`, `dns`, or `wp_rest` when that row's result is `ok`. On `*.example.test`, `MockUptime` does that. Those rows are not the backup.

`_public_backup` does not include `green`. `wp backup` prints that object, with `ok` true and message `backup verified`. The `green: false` field is on the site summary from `list_sites` and `show_site`, not on the backup command payload. `WORDPRESS_OPS.md` says a verified backup has `green` false. That is true for the summary and false as a claim about the backup object itself.

The overview template paints `bg-ok` when `site.backup.tone` is `ok`, and it does not read `green`. Current backup results are `verified`, `fail`, or `unknown`, so that branch does not run. A later result of `ok` would look green while the JSON field stayed false.

## Gaps

At most five. Pytest was not executed, so a passing run is not_reproduced. `test_confirmed` means the named test source asserts the fact.

1. `config/wp_sites.mock.yaml` sets `capabilities.staging` to `unavailable` on `mock-staging`. `WpOpsService.restore_drill` allows the drill because `target.environment` is `staging`. `parse_site` only stores the flag. code_confirmed. `test_staging_drill_records_recovery_and_production_restore_stays_refused` in `tests/test_wp_ops.py` confirms the environment gate. No test asserts the unused capability. That assertion is not_reproduced.

2. `WORDPRESS_OPS.md` says a verified manifest makes the `backup` check result `verified`. `_collect` always appends `backup` as `unknown` with `NotImplemented`. `_run_locked` returns those stored rows. `_catalog_backup_view` and `show_site` via `_overlay_backup_check` report `verified` instead. code_confirmed. `test_mock_backup_verifies_one_local_manifest_and_stays_not_green` confirms the site summary. It does not confirm the raw `checks` list after a manifest exists.

3. `WORDPRESS_OPS.md` tells the reader to expect `green` false on a verified backup. `_public_backup` omits `green`. `_summary` sets it false. code_confirmed. The same test confirms the summary field and does not assert the backup object lacks `green`.

4. `wp_sites.html` and the check list in `wp_site.html` choose the green class from `tone == 'ok'`, not from `green`. A verified mock backup does not take that branch today. code_confirmed. `test_dashboard_mock_backup_is_not_green_and_has_no_production_restore` in `tests/test_wp_dashboard.py` confirms the attention badge and `data-healthy="false"`. It does not assert that `bg-ok` is absent.

5. `docs/runbooks/wp-ops-backup.md` says the manifest does not leave the machine. `bin/backup-azom-data.sh` can rsync the data directory, and the tarball includes `wp_ops_state.json`. `restore` still returns `RestoreNotInMvp`. code_confirmed. Script execution is blocked.

## What matches

These claims in `WORDPRESS_OPS.md` and the runbook match the code that was read. Phase 2 backup is local metadata. `AZOM_WP_OPS_LIVE` is unset in `service.py`, and `test_operator_cannot_probe_production_and_restore_never_runs` still sees `live_calls` false. Production drill targets get `TargetNotStaging`. There is no `wp restore` command in `cli.py`. `security_scanner` is not a clean scan. The host script exclusions for `wp_workspaces` and `wp_file_payloads` match the review-plan paragraph in `WORDPRESS_OPS.md`.
