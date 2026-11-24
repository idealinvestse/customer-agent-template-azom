# Homepage review resolution

**Purpose.** This note records how homepage review resolves `show_on_front`, page templates, child and parent themes, and block themes, and whether the 45 second budget aborts a running call. It is evidence for the 2026-11-24 plan, not a living operator doc.

**Audience.** Coding agents changing homepage review under `skills/ecom_ops/wp_ops`.

**Read this first.** `docs/WORDPRESS_OPS.md`, section Homepage source review. If this note and that section disagree, the symbols cited here win.

**Glossary.** A candidate is a path `step_home` tried, including a miss. The selected template is the single winner in `selected_template` and `selected_template_path`. A layer is `database`, `source_file`, or `rendered_dom`.

Labels mean the following. `code_confirmed` means the cited function does this. `test_confirmed` means a cited test asserts it, and that test passed in this session. `doc_only` means a doc or skill says it and the cited function does not do it. `not_reproduced` means no cited test executes that path. `blocked` means the source could not be read. Nothing below is `blocked`.

Pytest command, from the repository root, with `AZOM_USE_MOCK=1` and `AZOM_DATA_DIR` set to a temp directory. `python -m pytest tests/test_wp_page_review.py tests/test_wp_review_acceptance.py::test_page_review_stops_on_call_and_time_budgets -q --tb=line`. Result measured this session. Exit code 0. No site was contacted. `.azom-data` was not read.

## Overview

`run_bounded_page_review_loop` owns the tool order and the 45 second budget. `step_home` owns front-page resolution and template selection. `hierarchy_candidates` owns the candidate order and does no IO. `step_content` owns editor raw and stored rendered HTML. `step_sources` owns a file inventory and does not select the template. `step_preview` owns the DOM layer and never reads it.

The four stores stay separate. Editor raw and stored rendered HTML are two strings on the REST page. Source text is file bytes. DOM fields stay null. `code_confirmed` in `step_content`, `step_sources`, and `step_preview`. The separation flags and the null DOM are `test_confirmed` in `test_homepage_review_separates_layers_and_does_not_write`.

## Key concepts

`show_on_front` is accepted only as `page` or `posts` after strip and lower case. Any other value, including a missing setting, stops `step_home` with `settings_incomplete` before a page fetch. `code_confirmed` in `step_home`. `test_confirmed` in `test_missing_front_setting_does_not_guess_a_page`.

`page` also requires `page_on_front` to parse as an int greater than 0. Otherwise the reason is `page_on_front_missing`. `code_confirmed`. `not_reproduced`.

`posts` does not call `get_page`. `page_id` stays null, `front_page` is null, and `page_for_posts` is kept. `code_confirmed`. `test_confirmed` for those fields in `test_homepage_review_resolves_posts_index_without_inventing_static_page`. That test does not assert the raw and rendered flags. Those flags are false in the `posts_index` branch. `code_confirmed` only.

Theme identity comes from `list_themes`, not from `style.css`. Exactly one row with `status == "active"` supplies `stylesheet` and `template`, both lowercased. `stylesheet` must match `_SLUG`. Otherwise `step_home` returns `status` `read` and `reason` `active_theme_unknown` with empty `candidates`. `code_confirmed`. `not_reproduced`.

`theme_relationship` is `child` when both slugs match `_SLUG` and they differ. It is `no_child` when they are equal. It is `unknown` when `template` is missing or fails `_SLUG`. `code_confirmed`. The shared fixtures set the slugs equal, so this path is `no_child` in `step_home`. `test_homepage_review_separates_layers_and_does_not_write` asserts `template_origin` `theme` and does not assert `theme_relationship`. The origin on that fixture is `test_confirmed`. The relationship field itself is `not_reproduced`.

`theme_kind` is decided only from the stylesheet directory. `list_dir` failure or an empty listing yields `unknown`, and the candidate tuple stays empty. A root name `theme.json` yields `block`. Otherwise a `templates` child whose name ends in `.html` yields `block`. Anything else is `classic`. `code_confirmed` in `_theme_kind`. The parent directory is not consulted. `not_reproduced`.

## How resolution works

### Candidate order

`hierarchy_candidates` returns one tuple and does not read the site.

| Kind | `show_on_front` | Order |
| --- | --- | --- |
| `block_theme` true | ignored | `templates/front-page.html`, `templates/home.html`, `templates/index.html`, `theme.json` |
| classic | `posts` | `front-page.php`, `home.php`, `index.php` |
| classic | `page` | `front-page.php`, then `page-{slug}.php` when `slug` is non-empty, then `page-{id}.php` when `page_id` is non-empty, then `page.php`, `singular.php`, `index.php` |
| classic | anything else | empty tuple |

`code_confirmed` in `hierarchy_candidates`, `BLOCK_CANDIDATES`, and `CLASSIC_POSTS`. `docs/WORDPRESS_OPS.md` repeats this table. That part is not `doc_only`.

No test calls `hierarchy_candidates`. The page-review fixture writes `theme.json` and `templates/front-page.html`, so `_theme_kind` returns `block` before the classic tuple can run. The block winner is `test_confirmed`. The classic rows are `not_reproduced`.

`step_home` walks that tuple on `{files_root}/wp-content/themes/{stylesheet}` via `component_dir`. For each relative path it reads the stylesheet first with role `stylesheet`. If that read returns `None` and `theme_relationship` is `child`, it reads the same relative path under `template` with role `parent_read_only`. A decoded string, including an empty file, counts as present and blocks the parent read. A transport error, a file over 65536 bytes, or the 32-file text cap also returns `None`, so those cases fall through to the parent. `code_confirmed` in `_read` and `step_home`. `not_reproduced` for the fallback.

`candidate_hits` contains every attempted row, with `present` true or false. The returned `candidates` list is those paths, including misses. If the parent read is attempted, the recorded path and role become the parent even when the parent file is also missing. The child path is then absent from `candidates`. `code_confirmed`. `not_reproduced`.

### What selected means

`selected` is the first `candidate_hits` row with `present` true. It is not the whole list.

A page `raw["template"]` that is non-empty and whose lower case is not `default` wins before that file. `template_origin` is `database_assignment`. `selected_template` is that string. `selected_template_path` is `None`. `candidate_hits` is still filled. `code_confirmed` in `step_home`. `not_reproduced`.

`skills/wordpress-review/SKILL.md` says a database-assigned Site Editor template can take precedence. The code reads only `WpPost.raw["template"]` from `get_page(..., context="edit")`. It does not query `wp/v2/templates`. The Site Editor lookup is `doc_only`.

Otherwise a present file sets `selected_template` and `selected_template_path` to that path. `template_origin` is `parent_theme` when the row role is `parent_read_only`. It is `child_theme` when the relationship is `child` and the hit was on the stylesheet. It is `theme` for `no_child` and for `unknown`. If nothing is present and no assignment won, the origin is `unresolved` and both selected fields are null. `code_confirmed`. `not_reproduced` except the block fixture, which is `test_confirmed` as `template_origin` `theme` and path `themes/twentytwentyfive/templates/front-page.html`.

`step_sources` builds a different list. It does not call `hierarchy_candidates` and it does not write `selected_template`. It lists stylesheet root files ending in `.php`, `.css`, `.js`, `.json`, or `.html`, always adds `functions.php`, `header.php`, `footer.php`, `style.css`, and `theme.json`, then one level of `templates`, `parts`, `styles`, `assets`, and `woocommerce`. For a child it queues those same relative names on the parent as well. It also reads at most four active plugin entry files. A missing read is skipped. `code_confirmed`. Inventory membership for `style.css`, `theme.json`, and `plugins/woocommerce/woocommerce.php` is `test_confirmed` in `test_homepage_review_separates_layers_and_does_not_write`.

### Raw, rendered, source, and DOM

`step_content` reads `content.raw` and `content.rendered` from the page already loaded by `step_home`. `editor_raw_present` and `stored_rendered_present` are separate booleans. `stored_rendered_is_dom` is the literal `False`. There is no HTML parser. `code_confirmed`. The two booleans `editor_raw_present` true and `stored_rendered_is_dom` false are `test_confirmed`.

The database finding text is `editor or stored`. When raw is non-empty, the excerpt is raw and the stored HTML is not the excerpt. When raw is empty and stored HTML is not, the database excerpt is the stored HTML. It is still layer `database`. `code_confirmed`. The admin report contains the raw prefix. `test_confirmed` in the same homepage test. The empty-raw collapse is `not_reproduced`.

`step_sources` and the template reads use `transport.read_bytes`. Those findings use layer `source_file`. They are not the REST content strings. `code_confirmed`. A cited `front-page.php` line and a `woocommerce/checkout.php` line are `test_confirmed`.

`step_preview` always returns `not_run`, reason `browser_not_run`, and a `rendered_dom` object whose screenshots, console errors, and network errors are null. It does not fetch a URL. `code_confirmed`. `test_confirmed` in `test_homepage_review_separates_layers_and_does_not_write`.

`_layers` copies the content `database` object, a short sources status object, and the `rendered_dom` object. It does not merge the four stores into one body. `code_confirmed`.

`_read` caches by absolute path so a parent file and a child file with the same relative name stay distinct. `test_confirmed` in `test_parent_theme_text_is_not_reused_for_the_child`. That test calls `_read` directly. It does not run `step_home` with `template != stylesheet`. The selection fallback remains `not_reproduced`.

`file_roles` is keyed only by the relative path and is overwritten when the parent is read later. Nothing reads `file_roles`. `code_confirmed`. It does not affect `selected_template`.

### Timeout

`PAGE_REVIEW_MAX_SECONDS` is 45.0 and `PAGE_REVIEW_MAX_CALLS` is 4. `run_bounded_page_review_loop` samples the clock once for `started`, then once before each tool. The test is `now() - started > max_seconds`. Equal to 45 still allows the next step. `calls >= max_calls` is checked first. If both budgets are already exceeded, the reason is `call_budget`.

On a trip, the loop appends `not_run` with `executed` false and does not call the step. For `wp_page_review_preview` that stub includes a null `rendered_dom`. It does not call `step_preview`, so it does not add the preview finding. `code_confirmed`.

The clock is not sampled inside `step_home`, `step_content`, `step_sources`, `step_preview`, or `prepare_context`. None of them take a deadline. There is no cancel handle. A running REST call or file read finishes. Later tools are then skipped if the clock is already over. `prepare_context` runs after `started` and before the loop, so a hang there is also not aborted. After it returns, the first tool can be `time_budget` without having run. `code_confirmed`.

`WordPressClient.timeout` defaults to 30 seconds per `transport.request`. `get_settings`, `list_themes`, `get_page`, and `list_plugins` each get that timeout. The loop does not pass the remaining 45 seconds into the client. One step can outlive the budget and still return. `code_confirmed` in `WordPressClient.__init__` and `get_page`.

`test_page_review_stops_on_call_and_time_budgets` is `test_confirmed` for both budgets. `max_calls=2` marks tools 3 and 4 `call_budget` without running them. A clock of `0.0`, `0.0`, then `46.0` lets tool 1 return `read` and marks the rest `time_budget`. That is a check between steps. An abort of an in-flight call is `not_reproduced` because the production path has no abort to run.

## Where things live

| Decision | Owner |
| --- | --- |
| Candidate order only | `hierarchy_candidates` in `skills/ecom_ops/wp_ops/review_knowledge.py` |
| `show_on_front`, theme kind, child or parent read, selected versus candidates | `step_home`, `_theme_kind`, `_read_relative` in `skills/ecom_ops/wp_ops/page_review.py` |
| Editor raw versus stored rendered | `step_content` in the same file |
| Source inventory, not selection | `step_sources` in the same file |
| DOM stub | `step_preview` in the same file |
| Tool order, 45 second check, layer assembly | `run_bounded_page_review_loop` and `_layers` in `skills/ecom_ops/wp_ops/page_review_loop.py` |
| REST page, settings, and theme rows | `WordPressClient.get_page`, `get_settings`, `list_themes` in `skills/ecom_ops/integrations/wordpress.py` |
| Theme directory | `component_dir` in `skills/ecom_ops/wp_ops/files.py` |
| Excerpt visibility | `prepare_context` sets `include_evidence` from `Permission.WP_OPS_ADMIN` |

`config/rbac.yaml` maps `jonatan` to `viewer` and `oscar` to `full_admin`. `viewer` has `WP_OPS_READ` and not `WP_OPS_ADMIN`. `full_admin` has every permission. `test_viewer_omits_source_and_editor_excerpt` is `test_confirmed` that the viewer text omits raw and source excerpts.

## Existing tests

`tests/test_wp_page_review.py` locks the block-fixture homepage, the posts index without a static page, the absolute-path cache, viewer redaction, missing `show_on_front`, unbound REST, and foreign-tool refusal. It also locks chat and Telegram job routing. Those routing tests do not resolve templates.

`tests/test_wp_review_acceptance.py` function `test_page_review_stops_on_call_and_time_budgets` locks the between-step budgets.

No test in those files builds a classic tree, sets `template` to a different parent slug, or sets `raw["template"]` to a non-default value.

## Gaps

Five gaps. No production change is justified by this note alone.

**Block order ignores the front mode, and classic order is untested.** `hierarchy_candidates` returns `BLOCK_CANDIDATES` for every block theme, including `posts`, and `theme.json` can be the first present file. The shared fixture is a block theme, so the classic page tuple never runs. `code_confirmed`. Classic selection is `not_reproduced`.

**A non-default page template does not resolve a file.** `database_assignment` stores the raw string and sets `selected_template_path` to `None`. There is no Site Editor read. `code_confirmed`. The Site Editor sentence in `skills/wordpress-review/SKILL.md` is `doc_only`. The assignment branch is `not_reproduced`.

**Child versus parent selection is untested, and kind does not come from the parent.** A classic child of a block parent looks for PHP names and will not select the parent HTML templates. A miss can be recorded as the parent path. `code_confirmed`. End-to-end fallback is `not_reproduced`. `test_parent_theme_text_is_not_reused_for_the_child` does not cover it.

**The 45 second budget does not abort a running call.** It only refuses to start the next step. The 30 second HTTP timeout is a different clock and is not the remaining budget. `code_confirmed`. The between-step skip is `test_confirmed`. In-flight abort is `not_reproduced`.

**One database finding collapses raw and stored HTML.** The flags stay separate, and DOM stays unread. An empty raw string can cite stored rendered HTML inside layer `database`. `code_confirmed`. The flags and the null DOM are `test_confirmed`. The empty-raw citation is `not_reproduced`.

## Smallest verifiable delivery

Add one test to `tests/test_wp_page_review.py` and do not edit `page_review.py` in that change. Build a classic child and a classic parent with no `theme.json` and no `templates` directory.

Assert three current outcomes. A present child `front-page.php` selects `themes/{child}/front-page.php` with `template_origin` `child_theme`. After that child file is removed, the parent file selects `themes/{parent}/front-page.php` with `parent_theme`. With `raw["template"]` set to `full-width.php`, `template_origin` is `database_assignment`, `selected_template_path` is `None`, and `candidate_hits` still lists the PHP file.

Run the same pytest command. This unit locks the non-default page template, the child versus parent fallback, and the classic page order. Leave the block `theme.json` winner and the in-flight budget for the next unit.

## Gotchas

`get_page` runs inside `step_home`, not inside `step_content`. The homepage test sees one `GET` ending in `/pages/2`. `test_confirmed`.

If `get_page` raises, `step_home` can still return `read` and select files with an empty slug. `step_content` then returns `page_read_failed`. `code_confirmed`. `not_reproduced`.

`page_for_posts` is not loaded as editor content in `posts` mode. `code_confirmed`.

The text cache holds 32 files for the whole context. A later inventory read that hits the cap looks like a missing file and can fall through to the parent. `code_confirmed`. `not_reproduced`.

`_format` prints that stored rendered HTML is not DOM even when a raw excerpt is present. `test_confirmed` by the substring `är inte DOM`. The branch that prints that the DOM was read is unreachable with the current preview step, because preview status is never `read`. `code_confirmed`.
