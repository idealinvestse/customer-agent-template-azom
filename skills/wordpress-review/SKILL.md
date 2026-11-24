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
**Read this first:** `skills/ecom-ops/SKILL.md`, then `ecom_ops.wp_ops.review_knowledge`.

## Task

A request such as "granska startsidans källkod" is this task. The server runs four site-bound tools, in order:

1. `wp_page_review_home` — the registered site's real front page and template hierarchy.
2. `wp_page_review_content` — that page's editor/block content from the database.
3. `wp_page_review_sources` — theme and plugin files that can affect the homepage.
4. `wp_page_review_preview` — isolated browser preview, desktop and mobile screenshots, console errors, and network errors.

Use only those tools. Do not pass a URL, host, or path. Do not write shell, PHP, or WP-CLI. Do not write the site.

## WordPress and WooCommerce review knowledge

- For classic themes, use the server-returned candidate order. Common fallbacks include `front-page.php`, `home.php`, `page.php`, `singular.php`, and `index.php`; a static front page and the posts index have different entry points.
- For block themes, inspect `templates/*.html`, `parts/*.html`, `theme.json`, and any database-assigned Site Editor template. Treat a database template name as unverified until the server read that file. `selection` stays `candidate` until that read. `site_editor` stays `unavailable` when no database template was read. Do not promote the name to `selected_template`.
- A child theme inherits from its parent. Treat parent files as read-only evidence and report the active child and parent relationship only when the registered site data confirms it.
- Inspect CSS and JavaScript only when a real source read identifies the file. Enqueue calls, template hooks, and plugin callbacks are evidence of possible influence, not proof that a hook ran on the page.
- WooCommerce overrides belong under the active theme's `woocommerce/` directory. Do not patch WooCommerce core or infer an override from a file name alone.
- For WooCommerce code, check HPOS compatibility declarations and avoid assuming `shop_order` posts are the source of truth. Review Cart and Checkout Blocks separately from shortcode checkout. Never run a real payment, submit an order, or change stock during a review.
- Review capability checks, nonces, input validation, sanitization, context-appropriate escaping, and authorization around writes. A source excerpt is a finding lead, not a completed security audit.
- Review responsive layout and accessibility with actual viewport/DOM evidence when available: keyboard access, labels and names, focus visibility, contrast, heading order, and overflow. Report console and network failures only when the browser tool returned them.

## Evidence and check status

Each finding should include a source file and line or a DOM selector, severity, confidence, reproduction steps, and the checks actually run. Separate confirmed defect from hypothesis. Mark unavailable and not-run checks explicitly.

The current review check runner performs `php -l` only when the PHP binary is present. A timeout is not a pass. WPCS, Plugin Check, PHPUnit, asset lint, egress checks, WordPress runtime, and browser rendering still require isolated runtimes that are not wired in. A static WooCommerce declaration check is `static_precheck` with `runtime_proof` false. A suite is publishable only when `check_run.published` is true for the current base and work hashes. Never describe the review workspace UI or a saved mock record as a rendered preview. The authenticated review link does not approve anything.

This skill is review guidance. It grants no site permission and does not change the approval gates.

## Three layers

Keep these apart in every finding:

| Layer | What it is | What it is not |
|-------|------------|----------------|
| `database` | `show_on_front`, `page_on_front`, and `content.raw` from an authorized editor read. `content.rendered` stored by WordPress is still this layer. | A screenshot or the live DOM. |
| `source_file` | A theme or plugin file the server actually read, with its path. | Proof of what the browser painted. |
| `rendered_dom` | A browser preview that really ran, with its screenshot and console or network evidence. | Metadata, `content.rendered`, or a file. |

The candidate order is `hierarchy_candidates()` in `ecom_ops.wp_ops.review_knowledge`. Do not invent another order. If `show_on_front` is missing, stop. Do not pick the newest page.

## Themes and WooCommerce

- A classic front page uses the PHP hierarchy. A block theme uses `templates/*.html` and `theme.json`.
- Look in the child stylesheet directory first. A parent theme file is read-only evidence. Do not edit the parent to change the child.
- When the stylesheet and template relationship is unknown, say so. Do not guess a slug or create a child theme on the site.
- Hooks that can change the homepage include `template_include`, `template_redirect`, and `pre_get_posts`.
- Styles and scripts that affect the page are those passed to `wp_enqueue_style` and `wp_enqueue_script`.
- A WooCommerce override belongs in the theme's `woocommerce/` directory. Do not patch the `woocommerce` plugin.

## Findings

Each finding names its layer, the file and line or the DOM evidence the tool returned, the risk, whether the bytes are untrusted data, and whether the check was a real read or `not_run/unavailable`.

Report a check as passed only when the tool status is a real execution. A stub, a missing sandbox, and a missing browser stay `not_run/unavailable`. Do not add a screenshot, a console error, a network error, or a source line that the tool did not return.
