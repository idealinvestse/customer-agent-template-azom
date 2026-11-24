# WP Ops night journal — 2026-11-25

**Purpose:** Checkpoint so a later session can continue this branch without repeating the same pass.  
**Audience:** The next coding agent on `feat/wp-ops-night-review`.  
**Read this first:** [`../../CURRENT_STATE.md`](../../CURRENT_STATE.md), [`../../WORDPRESS_OPS.md`](../../WORDPRESS_OPS.md).

Start SHA: `e0bc03247f953691f9b114825eed49a075c72666` on `feat/wp-ops-night-review`. This file does not record an end SHA. The morning report does, after the local commit.

Labels: `code_confirmed` means the symbol was read in this tree. `test_confirmed` means a pytest command in this session exited as stated. `blocked` means a required runtime is absent. `not_reproduced` means the behavior was not executed.

## Actual chain

```
intent (chat or dashboard)
  ├─ page review: is_page_review
  │    → job kind page_review (site_id only; no plan_id, no run_id)
  │    → run_page_review_job / chat_agent
  │    → run_bounded_page_review_loop
  │         wp_page_review_home → content → sources → preview
  │    → page_review_client returns None when AZOM_USE_MOCK=1
  │
  └─ file review: WordPressToolDispatcher._open_review
       → ReviewPlans.open (plan id wr- + 12 hex)
       → workspace base/work under AZOM_DATA_DIR/wp_workspaces/<site>/<plan>/
       → requirements() in review_checks.py   # server policy only
       → ReviewPlans.run_checks                # no dashboard or CLI caller
            snapshot, check_generation++, run_id cr- + 12 hex
            run_catalog outside the plan lock
            stamp_run + suite_status
       → dashboard GET /wp/<site>/reviews/<plan>     # show, no approve
       → POST preview → ReviewPlans.preview → SandboxGate.start
       → POST staging → ReviewPlans.approve_staging
       → POST production → request_production → BackupNotVerified, no write
```

`ReviewPlans.run_checks` is called from tests. `code_confirmed` in `review_plan.py`. `wp_routes.py` posts preview, staging, production, comment, and extend. It does not post a catalog run. `cli.py` `run_checks` is `WpOpsService.run_checks` (health probes), not the review catalog.

## Older single-file flow

`file_changes.propose` stores a plan id, path, and hashes. The diff body is under `AZOM_DATA_DIR/wp_file_payloads/<plan_id>`. Dashboard `wp_file_propose` / `wp_file_apply`. Staging button `wpfile:stage:{plan_id}`. `action:yes` does not apply. Production POST returns `BackupNotVerified`. `restore()` returns `RestoreNotInMvp`. `code_confirmed` in `file_changes.py` and `wp_routes.py`.

## Ops flow

`WpOpsService.create_backup` writes mock metadata (`offsite` false, `bytes_copied` false, `green` false on the site summary). `restore_drill` uses `Site.environment == staging`, not `capabilities.staging`. `mock-staging` has `capabilities.staging: unavailable` and `environment: staging` in `config/wp_sites.mock.yaml`. `code_confirmed`. `restore()` stays `RestoreNotInMvp`. `test_confirmed` by the existing cases in `tests/test_wp_ops.py` inside the suite below.

## Packages

| Package | Owner | Files | Acceptance | Last result | Next |
|---------|--------|--------|------------|-------------|------|
| 1 Evidence and gates | Controller | `review_checks.py`, `review_plan.py`, `wp_review_process.py`, `tests/test_wp_review_evidence.py` | Timeout, duplicate, stale revision, missing workspace, old staging flag, and unavailable sandbox do not open preview or staging | `test_confirmed` in the 111-pass suite | Keep the gate. Do not loosen `requirements()`. |
| 2 Vertical runtime | Controller | `review_runner.py` unchanged | One synthetic theme renders base and work, desktop and mobile, plus a negative fixture | `blocked`. No PHP binary. No WordPress image pulled. `tests/test_wp_runtime_e2e.py` skips unless `AZOM_WP_RUNTIME_E2E=1`, then fails | Pinned local runtime, then a dashboard or CLI caller of `ReviewPlans.run_checks`. |
| 3 Homepage evidence | Controller | `page_review.py`, `page_review_loop.py`, `tests/test_wp_review_home_matrix.py` | Unread database template is not `selected_template`. In-flight REST uses the remaining budget. Instruction text does not call order tools | `test_confirmed` for that matrix. Classic PHP hierarchy and a real Site Editor read are `not_reproduced` | Fixture matrix for posts vs static vs child fallback when a runtime exists. |
| 4 Operator | Controller, template only | `wp_review.html`, acceptance assertion | Review page shows run id, profile, generation, and that it is not a rendered preview | `test_confirmed` via HTML assertion. Browser was not opened | Wire `run_checks` behind the existing CSRF POST pattern. Fake Telegram adapter was not added. |
| 5 Ops readiness | Controller, docs | `wp-ops-backup.md`, `wp-ops-read-pilot.md`, `CURRENT_STATE.md`, `WORDPRESS_OPS.md` | Mock backup is not green. Host rsync is not a site restore. Read pilot stays off | Docs match `bin/backup-azom-data.sh` and `models.py` capability enum. Script was not executed | Oscar decides the rsync destination policy. |

## Verification in this session

```
.\.venv\Scripts\python.exe -m pytest tests/test_wp_review_evidence.py tests/test_wp_page_review.py tests/test_wp_review_acceptance.py tests/test_wp_review_home_matrix.py tests/test_wp_ops.py tests/test_wp_dashboard.py tests/test_wp_files.py tests/test_wp_file_changes.py tests/test_wp_ssh.py tests/test_wp_agent_tools.py tests/test_wp_runtime_e2e.py -q --tb=line -ra
```

Exit 0. `111 passed, 1 skipped in 7.14s`. Skip: `AZOM_WP_RUNTIME_E2E` unset.

Same file with `AZOM_WP_RUNTIME_E2E=1`: exit 1, `AssertionError: PHP binary missing`. That failure is the closed opt-in. It is not a WordPress or browser run.

Ruff on the touched Python files: exit 0.

## Skills and subagents

Read this session: `verification-before-completion`. Earlier in the same task: `using-superpowers`, `test-driven-development`, `wordpress-review` skill. Reports from read-only subagents: `b-runtime.md`, `c-evidence.md`, `d-operator.md`, `e-ops.md`. `b-runtime.md` still describes the old preview success on an unavailable sandbox. `ReviewPlans.preview` now returns `PreviewBlocked` unless `state` is `running` and `engine` is not `unavailable`. `test_confirmed` in `test_published_evidence_does_not_open_an_unavailable_preview`.

## Follow-up after the independent review

The reviewer reported no blocking gate hole. These mistakes were still real:

- `run_is_publishable` accepted any profile name when the rows matched the snapshot. It now requires `wp-review-catalog` version `1`.
- An unread database template set `selection` to `verified`. It is `candidate` until that file is read.
- `ReviewPlans._row` left a workspace root without `base` or `work` resumable. It now marks `not_resumable`.
- Check audit recorded timeout and unavailable as success. Success is only status `passed`.
- `POST /wp/<site_id>/reviews/<plan_id>/checks` is the catalog caller. It uses the existing CSRF check and `WP_OPS_OPERATE`. A real catalog on this host stays unpublished.
- The homepage fixture imports `test_wp_page_review.py` by file path so another `tests` package on `PYTHONPATH` cannot shadow it.

The same WP file list as the morning report, plus these tests, exited 0: `113 passed, 1 skipped in 7.45s`. The skip is still the unset `AZOM_WP_RUNTIME_E2E` flag. Ruff on the touched Python files exited 0.

## What stayed closed

`AZOM_WP_SANDBOX` off. `AZOM_WP_FILES_SSH` not enabled. No image pull. No customer site. No Telegram. No production write. `request_production` still returns `BackupNotVerified`.
