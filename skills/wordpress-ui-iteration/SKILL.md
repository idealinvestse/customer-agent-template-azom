---
name: wordpress-ui-iteration
description: >
  Playbook for WordPress and WooCommerce UI/CSS/template work using visual
  intake, live DOM and computed-style inspection, iterative wr-* workspace
  forking, multi-variant sandbox proposals, overlay visual health checks,
  and strict HITL/RBAC staging boundaries.
version: "3.0.0"
---

# WordPress and WooCommerce UI iteration

**Purpose:** Guide visual and iterative WordPress/WooCommerce UI edits inside isolated `wr-*` workspaces so changes are grounded in real rendered DOM/CSS, build cleanly on top of prior sandbox revisions, support multiple coexisting variants, and pass visual health verification without weakening HITL or RBAC gates.  
**Audience:** Coding agents and the OpenClaw chat agent (`Messenger` and `Telegram`) operating on registered sites.  
**Read this first:** `AGENTS.md`, `SOUL.md`, `docs/WP_SKILL_GUARDRAILS.md`, `skills/ecom-ops/SKILL.md`, `skills/change-verification/SKILL.md`, `skills/wordpress-page-builder/SKILL.md`, `skills/wordpress-theme-dev/SKILL.md`, `docs/WORDPRESS_OPS.md`.

> **Net-new pages & templates:** If the user asks to design or build a brand-new page or WooCommerce template (for example a thank-you / `order-received` page, landing page, campaign page, or guide page) rather than tweaking an existing public page, follow `skills/wordpress-page-builder/SKILL.md` (`wp_list_pages`, `wp_build_sample_data`, `wp_workspace_page_draft`, and standalone live-shell `wp_preview_page`).

## 1. Visual intake and DOM/CSS grounding

1. **Extract clues from annotated screenshots:** Read visible product titles, SKU, button labels, URLs, circled regions, and arrows from the attached image and resolve the registered `site_id` (`/wp use <site-id>` or `wp_list_sites`).
2. **Ground in real rendered DOM and computed styles before patching:**
   - Call `wp_inspect_element(site_id=..., path=..., selector=..., text=..., plan_id=...)` to capture bounded `outer_html` (≤ 3 KB), `bounding_box`, `computed_styles` (`display`, `padding`, `margin`, `width`, `height`, `backgroundImage`, `backgroundPosition`, `backgroundSize`, `backgroundRepeat`, `fontSize`, `color`, `border`, `borderRadius`), `pseudo_before` / `pseudo_after`, bounded `matched_rules`, and `health_checks` (`background_icon_overlap_risk`, `broken_images`, `horizontal_overflow`, `small_tap_target`, `warnings`).
   - Call `wp_fetch_page_html(site_id=..., path=..., selector=..., include_styles=...)` when you need targeted rendered HTML/styles from a **public** page.
3. **Public-page URL boundary:** `wp_fetch_page_html` and live-overlay `wp_preview_page` only fetch **public** existing site paths (checkout, cart, and account paths such as `/cart`, `/checkout`, `/my-account`, `/handlekurv`, `/kasse`, `/min-konto` are blocked). **Never** pass a local dashboard review URL (`/wp/<site_id>/reviews/wr-.../preview`) to `wp_fetch_page_html` — use `wp_inspect_element(plan_id=...)` or `wp_workspace_read_file(plan_id=...)` to inspect a sandbox preview or workspace file. For net-new pages (`work/page_draft.json`), `wp_preview_page` and `wp_inspect_element` automatically run in standalone live-shell mode (`preview_kind="standalone_page"`).

## 2. Iterative continuation and multi-variant proposals

1. **Continue on top of active `wr-*` work:** When an active `wr-*` workspace already exists in the conversation, inspect it first (`wp_workspace_status`, `wp_workspace_diff`, `wp_workspace_read_file(plan_id=...)`) instead of opening a fresh workspace from an empty skeleton.
2. **Fork and group variants without clobbering siblings:**
   - Use `wp_workspace_fork(plan_id=..., summary=..., variant_group=...)` (or `wp_open_review_workspace(..., base_plan_id=..., variant_group=...)`) to branch follow-up refinements on top of existing edits.
   - Pass a shared non-empty `variant_group` when producing 2–3 comparable sandbox proposals so `ReviewPlans.open()` does **not** auto-supersede sibling proposals.
3. **Product copy in sandbox:** Use `wp_workspace_product_edit` inside `wr-*` for WooCommerce product text proposals (`product_edits.json`); never mutate live WooCommerce directly from chat text.

## 3. Overlay mechanics and CSS discipline

1. **Understand `apply_overlay()` behavior:** In read-only live previews, `apply_overlay()` injects all modified/created `.css` files in `work/` (`<style data-azom-preview="child-css">`) and applies whitespace-tolerant static HTML fragment diffs (or standalone `work/page_draft.json` inside the cached site shell), while **PHP logic hunks stay `php_skipped`**. Put visual adjustments in `.css` files, static HTML fragments, or `wp_workspace_page_draft` whenever they must render in `wp_preview_page`.
2. **Prevent icon/text collisions and broken layout rules:**
   - Before narrowing a button (`width: auto`) or reducing `padding`, inspect parent CSS and `wp_inspect_element` for `background-image` + `background-position` icons, `::before` / `::after` pseudo-elements, or container `flex` / `trustbadge` rules so text never overlaps icons or leaves broken `<img>` gaps.
   - Read and update existing rules in the workspace `style.css` (`wp_workspace_read_file`) instead of appending conflicting duplicate selectors across iterations.
3. **Fast preview baseline reuse:** Keep `reuse_live_baseline=True` (default) on `wp_preview_page` during iterative CSS tweaks so cached live HTML and baseline screenshots are reused instead of re-capturing baseline pages on every variant.

## 4. Post-edit visual verification

1. Run `wp_workspace_static_checks(plan_id=...)`.
2. Run `wp_preview_page(plan_id=..., path=..., focus_selector=..., focus_text=...)` (with matching `path` and `focus_*`) to capture desktop and mobile previews plus focus crops.
3. Inspect the returned `visual_health` checks and warnings across desktop and mobile (`background_icon_overlap_risk`, `broken_images`, `horizontal_overflow`, `small_tap_target`). If a collision, clipped layout, or broken image is reported, fix it in the workspace and re-run `wp_preview_page` before presenting the preview URL or screenshots to the user.

## 5. Anchored follow-up rule (when to skip `<clarify>`)

- **Skip `<clarify>`** when an active `wr-*` workspace/preview already anchors the target and the user asks for a clear refinement or visual bug-fix (for example `"gör köpknappen större"`, `"ändra färgen till mörkblå"`, or `"fixa ikonöverlappet och visa 3 förslag"`). Proceed directly to `wp_inspect_element` / `wp_workspace_read_file` / `wp_workspace_fork` + `wp_preview_page`.
- Emit `<clarify>` only if the follow-up switches to a new ambiguous target or contains genuinely conflicting instructions (see `skills/change-verification/SKILL.md`).

## 6. Hard HITL and RBAC guardrails

See `docs/WP_SKILL_GUARDRAILS.md` for full shared budgets, untrusted-bytes rules, and approval boundaries.

- Clarification (`clarify:*`), `wp_inspect_element`, `wp_workspace_fork`, and `wp_preview_page` are strictly non-mutating and never write live files or approve staging.
- Staging a multi-file `wr-*` workspace still requires `wp_workspace_static_checks`, passing `wp_workspace_run_checks`, and the explicit server button `wpreview:stage:wr-...` (Oscar `WP_OPS_ADMIN` + `CODE_EDIT`, matching snapshot hashes, non-high risk). Single-file plans still use `wpfile:stage:fe-...` (`WP_OPS_OPERATE` on local mock tree).
- Production deploy stays closed (`BackupNotVerified` / `RestoreNotInMvp`).
