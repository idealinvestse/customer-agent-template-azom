# Operator flow for homepage review and the dashboard link

**Purpose:** Trace `site_id`, `plan_id`, revision, and run id through the Telegram homepage job and the dashboard review page, and say whether a review link can approve.  
**Audience:** Coding agents on the 2026-11-24 WordPress ops pass.  
**Read this first:** [`../../TELEGRAM_OPENCLAW.md`](../../TELEGRAM_OPENCLAW.md), [`../../WORDPRESS_OPS.md`](../../WORDPRESS_OPS.md).

This note is a read of the current source. Pytest was not re-run. A claim marked `test_confirmed` is an assertion already in a test file. A claim marked `not_reproduced` was not executed in this pass.

## Overview

Two operator paths share the word review and do not share ids.

The homepage job is kind `page_review`. It reads one selected site and returns text. It does not open a plan, mint a run id, or build a dashboard link. `code_confirmed` in `run_page_review_job`.

The dashboard link belongs to a file-workspace plan. `review_plan_url` builds `GET /wp/<site_id>/reviews/<plan_id>`. Opening that URL does not approve. Oscar can approve local mock staging only by a later POST. `code_confirmed` in `wp_review` and `_post_review_staging`. `test_confirmed` in `test_dashboard_review_link_is_fail_closed` for login, CSRF, a bad hash, and production refusal. A successful staging POST through the test client is `not_reproduced`.

## Key concepts

`site_id` is the registered site slug, such as `mock-staging`. The homepage job copies it from session context. A review plan stores the registry site id.

`plan_id` is `wr-` plus 12 hex digits from `new_plan_id`. It is not the Telegram `job_id`. The homepage job never has one.

Revision is not an id. The current tree is `base_sha256` and `work_sha256`. `check_generation` counts refreshes and check runs. `RevisionChanged` means the tree moved during `run_checks`. `RevisionStale` means the tree no longer matches the plan or the published run.

Run id is `check_run.run_id`. `run_checks` sets it to `cr-` plus 12 hex digits from `secrets.token_hex(6)`. The Telegram job id is a separate `uuid` hex in `bot_jobs.job_id`.

## How it works

### Homepage job

`TelegramJobStore` sets kind `page_review` when `is_page_review` matches the text. The payload is message type, chat id, text, and source message id. It does not store `site_id`, `plan_id`, or a run id. `code_confirmed` in the insert path in `job_store.py`. `test_confirmed` in `test_telegram_job_is_page_review_and_not_chat`.

`_process_job` in `skills/ecom_ops/bot/__main__.py` calls `run_page_review_job` for that kind. The worker's `job_id` is only the lease, the event row, and `complete_job`. It is not passed into the loop.

`run_page_review_job` resolves the actor, clears `tool_digest`, and reads `site_id` only when `context_matches` accepts `wp_ops_context` for that actor, channel, and `channel:chat_id`. With no match it tells the operator to use `/wp use` and fetches nothing. `page_review_client` may return `None` in mock mode. `run_bounded_page_review_loop` stamps `site_id` on each step and in the reply. `_format` does not mention a plan, a revision, or a run id. The reply is `BotReply(text=outcome.text)` with no markup.

A follow-up while the job runs is a steer. `complete_job` consumes leftover steers for kind `page_review` and does not queue a `read_turn`. Free text `ja` stays on that job. `test_confirmed` in `test_page_review_steer_is_not_promoted_to_read_turn`.

### Dashboard link

`ReviewPlans.open` stores `plan_id`, `site_id`, empty `check_run`, and `check_generation` 0. `review_path` is `/wp/<site.id>/reviews/<plan_id>`.

The file-tool loop, not the homepage job, calls `_review_notice`. That calls `review_plan_url`. The absolute URL exists only for an HTTPS `AZOM_DASHBOARD_PUBLIC_URL` with no user, password, query, or fragment, and only when `site_id` and `plan_id` match the id patterns. The URL contains those two ids. It has no run id, no hash, and no approve token. `test_confirmed` in `test_review_link_requires_https_and_contains_only_site_and_plan` and `test_open_review_states_a_login_path_and_does_not_write`.

`GET /wp/<site_id>/reviews/<plan_id>` is `wp_review`. It requires login. `_render_review` loads `ReviewPlans.show` for that pair. A plan id that belongs to another site is 404 and the body does not include the real site. `test_confirmed` in `test_dashboard_review_link_is_fail_closed`.

`show` returns `check_run`, including `run_id`, `site_id`, `plan_id`, both hashes, and `published`, plus `check_generation`. `wp_review.html` prints `plan.plan_id`, `plan.site_id`, risk, status, both hashes, and each check as job and status. It does not print `check_run.run_id`, `published`, or `check_generation`. `code_confirmed`. No test asserts those fields on the page. `not_reproduced`.

`run_checks` mints the run id, stamps every check row with that id plus `site_id` and `plan_id`, and stores the snapshot on `check_run`. If the tree changes before the snapshot is saved, status stays incomplete, `published` stays false, and `error_class` is `RevisionChanged`. The plan hashes are then the new tree, not the run hashes. `test_confirmed` in `test_run_checks_records_a_bound_snapshot_and_discards_a_mid_run_edit`.

### Whether a review link can approve

No. The link is a GET. `wp_review` renders. It does not call `approve_staging`.

The page behind the link can approve local mock staging, and only as a separate POST.

Jonatan, role `viewer`, gets the metadata and no forms. `test_confirmed` that the comment control is absent and that comment and extend POSTs are 403.

Oscar, `WP_OPS_ADMIN` and `CODE_EDIT`, sees the forms because `can_review` is true. The staging form posts `_csrf`, `base_sha256`, and `work_sha256` from the plan fields. It does not post `run_id`. `_post_review_staging` calls `approve_staging`. CSRF failure is 400 and leaves status unchanged. A wrong hash renders `DigestMismatch`. `test_confirmed`.

`approve_staging` still refuses unless status is `tests_passed`, the current tree matches the plan hashes, and `_require_current_evidence` accepts the stored run. That check wants a published run, the same `site_id` and `plan_id`, and the same hashes. Otherwise the result is `EvidenceMissing`, `RevisionStale`, or `EvidenceMismatch`. A forged `approved_for_staging` flag does not pass. `test_confirmed` in `test_old_staging_approval_and_unbound_pass_do_not_open_gates`. On success the method writes the local mock tree, sets status `staged`, and returns `executed` true with `bytes_copied` false. `code_confirmed`. No dashboard test posts that success. `not_reproduced` at HTTP.

Production POST calls `request_production`, returns `BackupNotVerified`, and does not write. `test_confirmed`. Free text and `action:yes` do not approve. `text_is_review_approval` returns false. `test_confirmed` in `test_hash_risk_and_free_text_do_not_write`.

`docs/TELEGRAM_OPENCLAW.md` says the link does not approve. That matches the URL and the GET. It does not say the Oscar form is incapable of local staging.

## Where things live

| Piece | Symbol |
|-------|--------|
| Homepage job | `skills/ecom_ops/bot/page_review_job.py` `run_page_review_job` |
| Read client | `skills/ecom_ops/bot/page_review_runtime.py` `page_review_client` |
| Link builder | `skills/ecom_ops/bot/dashboard_links.py` `review_plan_url` |
| Job kind and steers | `skills/ecom_ops/bot/job_store.py` `TelegramJobStore.complete_job` |
| Worker | `skills/ecom_ops/bot/__main__.py` `_process_job` |
| Notice text | `skills/ecom_ops/wp_ops/agent_tools.py` `_review_notice` |
| Plan and run | `skills/ecom_ops/wp_ops/review_plan.py` `ReviewPlans.run_checks`, `approve_staging`, `_require_current_evidence` |
| Routes | `infrastructure/dashboard/wp_routes.py` `wp_review`, `_post_review_staging` |
| Page | `infrastructure/dashboard/templates/wp_review.html` |

## Gotchas

`docs/TELEGRAM_OPENCLAW.md` says a file-plan notice can link to the dashboard plan view. The built URL is the reviews path, not `GET /wp/<site_id>/files/plan/<plan_id>`. That other page uses `fe-` ids. `doc_only` for the word plan view. `code_confirmed` for the reviews path.

The same doc says the running bot was not restarted, so `page_review` is not live until a requested restart. This pass did not inspect the process. `doc_only`. The live process is `not_reproduced`. Oscar owns the restart. Do not mark soak complete.

## Gaps

At most five. Each line is the smallest delivery, not a redesign.

1. Homepage job has no plan, revision, or run, and no link. `run_page_review_job` returns loop text only. Tests cover kind, digest clear, and steer hold, not a URL. Evidence `code_confirmed`. Owner is the bot path. Smallest delivery is no approval link on this job. One sentence in the reply that no plan was opened is enough if operators need it said.

2. The review page hides the run and the revision. `ReviewPlans.show` already returns `check_run` and `check_generation`. `wp_review.html` does not render them. Evidence `code_confirmed`. Page text is `not_reproduced`. Owner is the dashboard template. Smallest delivery is to print `check_run.run_id`, `published`, `check_generation`, and `error_class`. Do not add the run id to the URL.

3. The staging form does not send the run id. The server uses the stored `check_run`, so a missing form field is not a way to pick another run. Evidence `code_confirmed` in `_post_review_staging` and `_require_current_evidence`. A successful HTTP approve is `not_reproduced`. Owner is the dashboard tests. Smallest delivery is one test-client POST with matching hashes on a `tests_passed` plan, plus one POST after a tree change that expects `RevisionStale`.

4. Telegram names a plan view for a file notice, and the next paragraph can be read as if the homepage job emitted it. The notice is `_review_notice` on the file-tool loop. Evidence `code_confirmed` against `doc_only` wording in `docs/TELEGRAM_OPENCLAW.md`. Owner is docs. Smallest delivery is two sentences. The URL is `/wp/<site_id>/reviews/<plan_id>`. Kind `page_review` does not call `review_plan_url`.

5. `page_review` is documented as not live until the bot restarts. Evidence `doc_only`. Process check `not_reproduced`. Owner is Oscar. Smallest delivery is a requested restart of `azom-bot` after this code is what the host runs. This note does not do that.
