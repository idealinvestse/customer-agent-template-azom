---
name: wordpress-review
description: >
  Review a WordPress homepage as three separate layers: database content,
  theme and plugin source files, and rendered DOM. Covers classic and block
  themes, child themes, theme.json, template hierarchy, hooks, asset enqueue,
  and WooCommerce theme overrides. Does not grant permission and does not
  write production.
version: "3.0.0"
---

# WordPress homepage review

**Purpose:** Tell an agent how to review the homepage source without treating a page lookup, a file, and the rendered page as the same evidence.  
**Audience:** Coding agents running a homepage source review.  
**Read this first:** `docs/WP_SKILL_GUARDRAILS.md`, `skills/ecom-ops/SKILL.md`, `skills/wordpress-theme-dev/SKILL.md`, `skills/wordpress-plugin-dev/SKILL.md`, `skills/wordpress-ui-iteration/SKILL.md`, then `ecom_ops.wp_ops.review_knowledge`.

**What you get:** A source review that keeps the editor's content, the theme files, and the rendered page as three different facts. It can name what is wrong. It cannot approve a deploy.

## Task

A request such as "granska startsidans källkod" is this task. The server runs four site-bound tools, in order:

1. `wp_page_review_home` — the registered site's real front page and template hierarchy.
2. `wp_page_review_content` — that page's editor/block content from the database.
3. `wp_page_review_sources` — theme and plugin files that can affect the homepage.
4. `wp_page_review_preview` — isolated browser preview, desktop and mobile screenshots, console errors, and network errors.

Use only those tools. Do not pass a URL, host, or path. Do not write shell, PHP, or WP-CLI. Do not write the site.

## WordPress and WooCommerce review knowledge

- For classic themes, use the server-returned candidate order. Common fallbacks include `front-page.php`, `home.php`, `page.php`, `singular.php`, and `index.php`; a static front page and the posts index have different entry points.
- For block themes, inspect `templates/*.html`, `parts/*.html`, `theme.json`, and any database-assigned Site Editor template. `theme.json` is inventory. Do not select it as the template. Treat a database template name as unverified until the server read that file. `selection` stays `candidate` until that read, and becomes `unavailable` when no hierarchy file was read. `site_editor` stays `unavailable` when no database template was read. Do not promote the name to `selected_template`. A file over the read limit is `clipped`. The text cap is `capped`. Either mark stops the hierarchy. Later candidates are not read or recorded, and the file is not read from the parent. `front-page.php` wins over a later page template. A safe assigned `.php` path comes after `front-page.php` on a classic static front. A missing child file can fall back. A missing parent file keeps the child path.
- A child theme inherits from its parent. Treat parent files as read-only evidence and report the active child and parent relationship only when the registered site data confirms it.
- Inspect CSS and JavaScript only when a real source read identifies the file. Enqueue calls, template hooks, and plugin callbacks are evidence of possible influence, not proof that a hook ran on the page.
- WooCommerce overrides belong under the active theme's `woocommerce/` directory. Do not patch WooCommerce core or infer an override from a file name alone.
- For WooCommerce code, check HPOS compatibility declarations and avoid assuming `shop_order` posts are the source of truth. Review Cart and Checkout Blocks separately from shortcode checkout. Never run a real payment, submit an order, or change stock during a review.
- Review capability checks, nonces, input validation, sanitization, context-appropriate escaping, and authorization around writes. A source excerpt is a finding lead, not a completed security audit.
- Review responsive layout and accessibility with actual viewport/DOM evidence when available: keyboard access, labels and names, focus visibility, contrast, heading order, and overflow. Report console and network failures only when the browser tool returned them.

## Evidence and check status

Each finding should include a source file and line or a DOM selector, severity, confidence, reproduction steps, and the checks actually run. Separate confirmed defect from hypothesis. Mark unavailable and not-run checks explicitly.

The current review check runner performs fixed `php -l` when a PHP binary is present (a timeout is not a pass). Local loopback WordPress + SQLite + headless browser sandbox rendering is opt-in and off by default (`AZOM_WP_SANDBOX=1` with a verified runtime), and read-only live page preview is opt-in and off by default (`AZOM_WP_LIVE_PREVIEW=1` with mock mode off); when disabled or unconfigured, those checks stay `not_run` / `unavailable`. WPCS, Plugin Check, PHPUnit, and asset lint remain `unavailable`. A static WooCommerce declaration check is `static_precheck` with `runtime_proof` false. A suite is publishable only when `check_run.published` is true for the current base and work hashes. Never describe the review workspace UI or a saved mock record as a rendered preview. The authenticated review link does not approve anything.

This skill is review guidance. It grants no site permission and does not change the approval gates.

## Isolated `wr-*` workspaces and file-loop caps

See `docs/WP_SKILL_GUARDRAILS.md` for full shared budgets, untrusted-data rules, and approval boundaries.

- **File/workspace turn caps:** When moving from read-only homepage review (4 calls / 45s) into component inspection or isolated `wr-*` workspace engineering, the file loop is capped at **16** tool calls, **10** file reads, **5** searches, and **60s** (`60` seconds) per turn (max 28 KiB per result / 80 KiB total).
- **Discovery and playbook tools:** Use `wp_playbook`, `wp_list_components`, `wp_theme_overview`, `wp_plugin_overview`, `wp_template_hierarchy`, `wp_code_index`, `wp_fetch_page_html`, `wp_inspect_element`, and `wc_template_overrides_audit` (plus optional `wp_component_intel` only when `AZOM_WPORG_INTEL=1`) to inspect hierarchy, rendered DOM/computed styles on public pages, hooks, and `@version` override drift before editing.
- **Multi-file `wr-*` workspace tools:** `wp_open_review_workspace` (with optional `base_plan_id` and `variant_group`) opens an isolated `wr-...` workspace, and `wp_workspace_fork` branches an existing `wr-*` without clobbering sibling variants. Edits use `wp_workspace_patch_file`, `wp_workspace_create_file`, `wp_workspace_apply_changes`, `wp_workspace_bulk_replace` (`dry_run=True` first), `wp_workspace_theme_json`, `wp_workspace_copy_from_parent`, `wp_workspace_copy_woo_template`, `wp_workspace_scaffold`, `wp_workspace_product_edit`, `wp_workspace_diff`, `wp_workspace_status`, `wp_workspace_static_checks` (fast security/syntax/JSON lint), `wp_preview_page` (read-only live overlay with `visual_health` checks), and `wp_workspace_run_checks`. See `skills/wordpress-ui-iteration/SKILL.md` for iterative UI/CSS workflows.
- **Staging buttons:** Single-file `fe-*` plans use `wpfile:stage:fe-...`, whereas multi-file `wr-*` workspaces require `wp_workspace_static_checks` + `wp_workspace_run_checks` and stage only via the explicit server button `wpreview:stage:wr-...`. Free-text approval never writes, and production deploy remains closed/Oscar-escalated.

## Three layers

Keep these apart in every finding:

| Layer | What it is | What it is not |
|-------|------------|----------------|
| `database` | `show_on_front`, `page_on_front`, and `content.raw` from an authorized editor read. `content.rendered` stored by WordPress is still this layer. | A screenshot or the live DOM. |
| `source_file` | A theme or plugin file the server actually read, with its path. | Proof of what the browser painted. |
| `rendered_dom` | A browser preview that really ran, with its screenshot and console or network evidence. | Metadata, `content.rendered`, or a file. |

The candidate order is `hierarchy_candidates()` in `ecom_ops.wp_ops.review_knowledge`. Do not invent another order. If `show_on_front` is missing, stop. Do not pick the newest page.

## Themes and WooCommerce

- A classic front page uses the PHP hierarchy. A block theme selects `templates/*.html`. `theme.json` is inventory, not the selected template.
- Look in the child stylesheet directory first. A parent theme file is read-only evidence. Do not edit the parent to change the child.
- When the stylesheet and template relationship is unknown, say so. Do not guess a slug or create a child theme on the site.
- Hooks that can change the homepage include `template_include`, `template_redirect`, and `pre_get_posts`.
- Styles and scripts that affect the page are those passed to `wp_enqueue_style` and `wp_enqueue_script`.
- A WooCommerce override belongs in the theme's `woocommerce/` directory. Do not patch the `woocommerce` plugin.

## Findings

Each finding names its layer, the file and line or the DOM evidence the tool returned, the risk, whether the bytes are untrusted data, and whether the check was a real read or `not_run/unavailable`.

Report a check as passed only when the tool status is a real execution. A stub, a missing sandbox, and a missing browser stay `not_run/unavailable`. Do not add a screenshot, a console error, a network error, or a source line that the tool did not return.
