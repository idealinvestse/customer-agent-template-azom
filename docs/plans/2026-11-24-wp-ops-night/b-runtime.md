# Sandbox and PHP lint runtime

throughput checkpoint: n/a, read-only investigation

This note is from a read of the working tree on `feat/wp-ops-night-review`. `review_plan.py`, `review_checks.py`, and `wp_review_process.py` were already modified, and `tests/test_wp_review_evidence.py` was untracked. Pytest was not run, Docker was not run, and no PHP binary was started.

`code_confirmed` means the source shows the behavior. `test_confirmed` means a test in the tree asserts it and the source matches that assertion. Neither label means the test was executed here.

## Overview

Review execution has one real process and one ledger gate. `run_php_lint` in `skills/ecom_ops/integrations/wp_review_process.py` runs `php -l` when a PHP binary is on `PATH`. `SandboxGate` in `skills/ecom_ops/wp_ops/review_runner.py` never starts a container, never binds a port, and never creates a database. `browser_smoke` has no process caller. A missing tool stays `unavailable` with `executed` false and `simulated` false.

The catalog cannot reach `tests_passed` on its own today. `egress_probe` is always required, and `run_job` leaves it unavailable. A theme with CSS, PHP, JS, or HTML also requires `browser_smoke`, which is the same kind of stub. `ReviewPlans.preview` still returns success if a plan is already publishable and the sandbox flag is off.

## Key concepts

`sandbox_enabled` reads `AZOM_WP_SANDBOX` through `env_flag`. The flag is in `PROTECTED_OVERLAY_KEYS` in `skills/ecom_ops/runtime_env.py`, so `runtime.env` and `secrets.env` cannot turn it on. Unset, empty, or any value other than `1`, `true`, `yes`, or `on` is off.

`SandboxGate.start` returns a record, or raises `ReviewError`. The record uses `engine`, `state`, `container`, `database`, `network`, `port`, `volume`, and `env_names`. `synthetic_env` returns only `AZOM_REVIEW_SYNTHETIC`. It is not a copy of the process environment. `FORBIDDEN_ENV_NAMES` is checked against that synthetic map, not against the PHP child.

`run_job` is the catalog entry. `php_lint` and `woo_static` have runners. Every other name in `JOBS`, including `browser_smoke`, goes to `_unavailable`. `suite_status` turns required rows into `tests_passed`, `tests_failed`, or `checks_incomplete`. `run_is_publishable` trusts those stored rows. It does not run PHP or a browser again.

## How it works

`ReviewPlans.run_checks` snapshots the workspace, calls `run_catalog`, then `stamp_run`. For `php_lint`, `_php_lint` keeps `.php` files and calls `run_php_lint`. `shutil.which` looks up `php` and decides the binary. No binary returns `status` `unavailable` and `reason` `php_binary_missing`, with `executed` false.

When a binary exists, each PHP file is written as `review-{index}.php` under a temporary directory prefixed `azom-wp-php-lint-`. The process is `subprocess.run` with `shell` false, a timeout of 8 seconds, and an environment limited to `PATH`, `SYSTEMROOT`, `WINDIR`, `TEMP`, and `TMP` after the key is uppercased. A nonzero exit keeps that exit code and continues with the next file. `TimeoutExpired` sets `exit_code` to 124, sets `reason` to `timeout`, and breaks. `OSError` around the directory or the process returns `php_process_unavailable` and does not write the artifact.

If `reason` is `timeout`, `status` is `timeout` even when an earlier file already exited nonzero. Otherwise exit code 0 is `passed` and any other exit is `failed`. The check row command is the binary path from `shutil.which`, then `-l`, then the placeholder `<reviewed-file>`. The artifact `php_lint.json` stores the original relative name, the exit code, and at most 500 characters of combined stdout and stderr. `suite_status` treats `timeout` as incomplete, not as `failed`, unless some other row has `status` `failed`.

`browser_smoke` is required for a theme file that ends in `.css`, `.php`, `.js`, or `.html`. `run_job` then returns `wordpress_browser_runtime_unavailable`. Nothing in `wp_ops` imports Playwright, Selenium, or Docker for this job. `config/integrations.yaml` has `selenium: true` for dashboard settings. The review catalog does not read it. `page_review.step_preview` is a separate tool, `wp_page_review_preview`, and it returns `not_run` with `browser_not_run`. It does not call `browser_smoke`.

`SandboxGate.start` with the flag on raises `VersionsUnknown` when PHP, WordPress, or an enabled Woo version is missing or unknown. Known versions still raise `SandboxRuntimeUnavailable`. The flag off skips the version check and returns `engine` `unavailable`, `state` `unavailable`, `container` `unavailable`, `port` null, and `payments_enabled` false. `high_risk` is deleted and does not change the record. `resume` only calls `start` again, and no production caller calls `resume`.

`ReviewPlans.preview` requires `status` `tests_passed`, then `_require_current_evidence`. That helper returns `EvidenceMissing` or `RevisionStale` when `run_is_publishable` or the workspace hashes fail. If evidence passes, `preview` calls `gate.start`. A `ReviewError` becomes a failed result. A returned record is stored and `preview` returns ok, including when that record is `unavailable`. The audit outcome is the record state, and the message says the preview was booked.

`note_process_restart` does not call `SandboxGate`. It only rewrites a stored sandbox whose `state` is `created`, `started`, or `running` to `stopped`, and sets the plan to `sandbox_expired`. `start` never writes those running states, so this path only sees a row that was already stored that way.

## Where things live

`skills/ecom_ops/wp_ops/review_runner.py` owns `sandbox_enabled`, `synthetic_env`, `versions_unknown`, `empty_record`, and `SandboxGate`.

`skills/ecom_ops/integrations/wp_review_process.py` owns `run_php_lint`. It is the module that imports `subprocess`. `test_review_modules_do_not_import_a_process_runner` checks the plan, catalog, and page-review modules, and it does not list this file.

`skills/ecom_ops/wp_ops/review_checks.py` owns `JOBS`, `requirements`, `run_job`, `run_catalog`, `suite_status`, `stamp_run`, and `run_is_publishable`.

`skills/ecom_ops/wp_ops/review_plan.py` owns `ReviewPlans.run_checks`, `ReviewPlans.preview`, `ReviewPlans._require_current_evidence`, and `ReviewPlans.note_process_restart`.

`skills/ecom_ops/runtime_env.py` owns the overlay block for `AZOM_WP_SANDBOX`.

## Existing tests

`test_stub_checks_are_not_passed_and_do_not_open_preview` in `tests/test_wp_page_review.py` sets the flag to `0`, expects `browser_smoke` to be `unavailable`, and expects `SandboxGate.start` to return an unavailable record. It then sets the flag to `1` and expects `SandboxRuntimeUnavailable`. Preview in that test is `PreviewBlocked` because the suite is incomplete or failed. The syntax case allows either `failed` or `unavailable`, so a machine without PHP still passes.

`test_suite_requires_real_process_metadata_for_every_required_pass` shows that a hand-built `browser_smoke` row with `executed` true can make `suite_status` return `tests_passed`. That is a shape check, not a browser run. `test_confirmed` for the shape rule.

`test_static_jobs_and_skills_do_not_invent_a_pass` in `tests/test_wp_review_acceptance.py` allows `php_lint` to be `unavailable` or `passed`. It checks the synthetic sandbox map, not the PHP child environment.

`test_php_lint_timeout_is_not_passed` in `tests/test_wp_review_evidence.py` stubs `shutil.which` and `subprocess.run`. One file that raises `TimeoutExpired` must return `status` `timeout`, `executed` true, and not `passed`. `test_confirmed` for that single-file case. The stub ignores the `env` argument.

`test_suite_rejects_non_passes_duplicates_and_forged_applicability` expects a `timeout` row to make the suite `checks_incomplete`, and a duplicate `php_lint` row to do the same. `test_confirmed` against the current `suite_status`.

`test_old_staging_approval_and_unbound_pass_do_not_open_gates` stores `tests_passed` with one unbound `php_lint` row and expects `EvidenceMissing` from `preview`. `test_change_after_a_published_run_blocks_preview` monkeypatches `run_catalog` to return a passed row for every required job, then edits the tree and expects `RevisionStale`. It does not call `preview` on the untouched published plan.

## Gaps

**Preview success on an unavailable sandbox.** Status is `code_confirmed`. `ReviewPlans.preview` returns ok when `SandboxGate.start` returns a record. With `AZOM_WP_SANDBOX` off, that record is `unavailable`, and no container or browser starts. Honest `run_checks` does not publish today, because `egress_probe` and a visual theme's `browser_smoke` stay unavailable. `preview` can still return success when stored checks already satisfy `run_is_publishable`. The published-run test builds that shape by replacing `run_catalog`. With the flag on, `start` already fails with `SandboxRuntimeUnavailable`. With the flag off, it does not. Owner is `ReviewPlans.preview` in `review_plan.py`. `SandboxGate.start` should stay the place that refuses to invent a container.

**A later PHP timeout hides an earlier syntax error.** Status is `code_confirmed`. In `run_php_lint`, a nonzero `php -l` exit is kept only until `TimeoutExpired`. The handler then sets `exit_code` to 124 and `reason` to `timeout`, and the status rule prefers `timeout`. `suite_status` then returns `checks_incomplete` instead of `tests_failed`. Both block publish. The operator-visible result looks like a missing run, not a syntax failure. The existing timeout test covers one file only. Owner is `run_php_lint` in `wp_review_process.py`.

**The PHP child environment is not locked by a test.** Status is `code_confirmed`. The allowlist is in `run_php_lint`. No test reads the `env` mapping or asserts `shell` is false. `test_php_lint_timeout` replaces `subprocess.run` with a function that drops its arguments. A regression that passed the full process environment would still satisfy that test. This read did not show a leaked secret. No test checks that mapping. Owner is `run_php_lint`, with the assertion in `tests/test_wp_review_evidence.py`.

**The stored lint command is not the argv that ran.** Status is `code_confirmed`. The process uses the temporary `review-{index}.php` path. The check row stores `<reviewed-file>`. The artifact stores the original relative name and does not store that argv. A reviewer cannot replay the process from the check row. Owner is `run_php_lint`.

There is no fifth code defect. `browser_smoke` is absent on purpose. `run_job` returns unavailable, and `step_preview` does not call it. Wiring a browser or a container is not the repair.

## Smallest verifiable delivery

Change `run_php_lint` so a timeout does not erase an earlier nonzero exit. If one file already exited nonzero, keep `status` `failed` and keep that exit code. Still store `timeout` on the file that hung, and still stop the loop. Extend `test_php_lint_timeout_is_not_passed` with two files. The first fake process returns code 255. The second raises `TimeoutExpired`. Assert `status` is `failed`, `exit_code` is 255, and `executed` is true.

That patch is the one a real PHP run can hit without a forged ledger. Do not add Docker, a browser, or a sandbox container in the same change. The preview success gap is the next one-assertion patch in `ReviewPlans.preview`. After evidence passes, an `unavailable` record should return `SandboxRuntimeUnavailable` instead of ok. The published-run test already knows how to fake a full catalog. Leave the tree unchanged and assert `preview.ok` is false while the flag is off.

## Gotchas

`AZOM_WP_SANDBOX=1` does not enable a runtime. It only changes `start` from an unavailable record into `VersionsUnknown` or `SandboxRuntimeUnavailable`.

`high_risk` does not change payments or checkout. The record sets `payments_enabled` and `checkout_enabled` to false, and `preview` sets them false again.

`SandboxGate.resume` has no caller outside the class.

The module docstring and `docs/WORDPRESS_OPS.md` say `php -l` parses and does not execute the reviewed file. The command list is `code_confirmed`. This read did not run PHP, so the interpreter claim stays `doc_only`.

Host PHP presence was `not_reproduced`. The page-review and acceptance tests accept `unavailable` when the binary is missing.

`note_process_restart` can mark a stored `running` row stopped without inspecting a process. That is safe only while `start` never creates one.
