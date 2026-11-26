---
name: wordpress-page-builder
description: >
  End-to-end playbook for designing, authoring, populating with real catalog
  products and synthetic zero-PII order/customer fixtures, and visually
  previewing brand-new WordPress pages and WooCommerce templates (thank-you /
  order-received, landing, campaign, comparison, upsell, and FAQ/guide pages)
  inside isolated wr-* workspaces without touching live or staging.
version: "3.0.0"
---

# WordPress and WooCommerce page builder

**Purpose:** Enable AzomOps to design, build, populate with real catalog products plus strictly synthetic zero-PII example data, and visually verify any net-new WordPress page or WooCommerce template inside an isolated `wr-*` workspace wrapped in the live site's real header, navigation, footer, and theme CSS.  
**Audience:** Coding agents and the OpenClaw chat agent (`Messenger` and `Telegram`) operating on registered sites.  
**Read this first:** `AGENTS.md`, `SOUL.md`, `docs/WP_SKILL_GUARDRAILS.md`, `skills/ecom-ops/SKILL.md`, `skills/change-verification/SKILL.md`, `skills/wordpress-ui-iteration/SKILL.md`, `skills/wordpress-theme-dev/SKILL.md`, `docs/WORDPRESS_OPS.md`.

## Supported page archetypes

Follow this same four-stage workflow uniformly for any net-new page or template request:

- **Thank-you / WooCommerce `order-received`:** `woocommerce/checkout/thankyou.php`, theme `wc-order-confirmation.php`, or a post-purchase thank-you page draft (`page_type="thankyou"`).
- **Landing & campaign pages:** Seasonal/product landing pages (`page_type="landing"` or `"campaign"`), `page-<slug>.php`, or Gutenberg/HTML draft blueprints.
- **Product comparison, upsell & guide pages:** Post-purchase cross-sell showcases (`page_type="upsell"`), spec comparisons (`"comparison"`), and FAQ/installation guides (`"faq"` / `"custom"`).

## Four-stage `wr-*` workflow

### Stage 1 — Live shell, page, and theme grounding (read-only)

1. Resolve `site_id` (`/wp use <site-id>` or `wp_list_sites`).
2. Inspect the live **public** homepage or shop shell (`/` or `/butik/`) with `wp_fetch_page_html` / `wp_inspect_element` / `wp_theme_overview` to extract real fonts, colors, typography (`theme.json`), header/footer layout, and button classes.
3. Discover existing site pages and help/setup guides (such as `/setup-guide/` or installation pages) in one call via `wp_list_pages(site_id=..., search=..., post_type=...)` and `wp_get_page(site_id=..., page_id=... | slug=...)` instead of grepping theme PHP files.
4. **URL & path guard:** Never pass blocked WooCommerce paths (`/checkout`, `/kasse`, `/cart`, `/handlekurv`, `/my-account`, `/min-konto`), uncreated slugs (`/tack`, `/kampanj`), or local dashboard URLs (`/wp/<site>/reviews/wr-.../preview`) to `wp_fetch_page_html`.

### Stage 2 — Real catalog products + synthetic zero-PII fixtures

1. **Real catalog facts:** Call `wp_list_products` / `wp_get_product` (now returning verified `image_url`, `currency`, `permalink`, `upsell_ids`, and `cross_sell_ids`) to populate authentic product names, SKUs, prices, images, and cross-sells. Never invent product specs, compatibility, or prices.
2. **Deterministic zero-PII sample context:** Call `wp_build_sample_data(site_id=..., scenario=..., product_ids=..., product_count=..., guide_search=..., currency=...)` (`scenario` in `"thankyou"`, `"landing"`, `"campaign"`, `"comparison"`, `"upsell"`, `"custom"`). It pairs real catalog products and matched site guides with strictly synthetic order/customer data (`order_number: "AZ-TEST-1042"`, `customer_name: "Test Kund"`, `customer_email: "test@example.invalid"`, `shipping_address: "Exempelgatan 1, 0150 Oslo"`, `contains_real_pii: false`, `synthetic_data: true`).
3. **Hard zero-PII ban:** Never fetch real customer orders or embed real customer names, emails, addresses, or phone numbers in `page_draft.json`, PHP templates, CSS, or preview screenshots. Non-synthetic email domains (anything outside `.invalid`, `.example`, or `@example.com`) are rejected by `wp_workspace_page_draft`.

### Stage 3 — Dual-artifact authoring inside `wr-*`

1. Open an isolated workspace (`wp_open_review_workspace`), or branch an existing one with `wp_workspace_fork` (using a shared non-empty `variant_group` when producing 2–3 layout proposals so variants coexist without superseding each other).
2. **Author the visual preview blueprint (`wp_workspace_page_draft`):**
   - Save sanitized `work/page_draft.json` (`slug`, `title`, `page_type`, `body_html` ≤ 48 KB, `page_css` ≤ 24 KB, `shell_path` defaulting to `"/"`, optional `template_file`, `focus_selector`, and `synthetic_context`).
   - All modified/created `.css` files in `work/` (e.g., `style.css` or `css/thankyou.css`) are automatically injected alongside `page_css`.
   - `sanitize_page_draft_html` strips `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form action=...>`, `on*`, and `javascript:`; keep `<img src>` strictly on the site host or allowlisted CDN URLs returned by `wp_list_products` / `wp_get_product`.
3. **Author deployable theme templates when applicable (`wp_workspace_create_file` / `wp_workspace_patch_file`):**
   - For WooCommerce or theme templates (`woocommerce/checkout/thankyou.php`, `wc-order-confirmation.php`, `page-<slug>.php`), also write clean WordPress PHP in the same `wr-*` workspace (with `ABSPATH` guard, `esc_html` / `esc_attr` / `esc_url` escaping, and HPOS-compatible `wc_get_order()` calls — never direct `shop_order` post assumptions) and link `template_file` in `wp_workspace_page_draft`.

### Stage 4 — Standalone live-shell preview and visual verification

1. Run `wp_workspace_static_checks(plan_id=...)`.
2. Run `wp_preview_page(plan_id=..., focus_selector=..., focus_text=...)`. Because `work/page_draft.json` is present, `build_overlay_html()` automatically runs in **standalone live-shell mode** (`preview_kind="standalone_page"`): it wraps the new page inside the site's cached live `<head>`/CSS/`<header>`/`<footer>` from `shell_path`, skips pointless homepage `live-*.png` captures, renders desktop (`1280x800`) + mobile (`390x844`) + focus screenshots (plus `compare-desktop.png` when comparing `variant_group` siblings side-by-side via `ReviewPlans.build_variant_comparison()`), runs `visual_health` checks, and generates the interactive preview URL (`"SANDBOX · NY SIDA (EXEMPELDATA)"`).
3. Inspect `wp_inspect_element(plan_id=...)` and `visual_health` across **both** desktop and mobile (`broken_images`, `background_icon_overlap_risk`, `horizontal_overflow`, `small_tap_target`). Fix any layout or image issues before presenting screenshots (`compare-desktop.png`, `preview-desktop.png`, `preview-mobile.png`, `focus-*`) to the user.
4. **Never fall back to `/`:** Never present an unmodified homepage screenshot as a new-page preview. Always state clearly that the preview runs in **sandbox** with **syntetisk exempeldata** and is not published on the live site.

## Clarification vs. anchored follow-up

- On an open-ended first request (e.g. `"designa en tacksida på azom.no"`), emit at most one concise `<clarify>` (`mode="options"`) if layout/architecture scope is genuinely open (for example WooCommerce `order-received` template override vs. standalone post-purchase page + guide strategy). Skip `<clarify>` when the user already gave concrete page requirements.
- Once the initial new-page `wr-*` preview exists (`has_active_wp_review=True`), skip `<clarify>` on follow-up refinements or render commands (e.g., `"byt produktkort"`, `"gör den kompaktare på mobil"`, `"rendera och visa screenshots"`) and continue via `wp_workspace_fork` / `wp_workspace_page_draft` + `wp_preview_page`.

## Hard HITL, risk, and RBAC boundaries

See `docs/WP_SKILL_GUARDRAILS.md` for full shared budgets, untrusted-bytes rules, and URL/zero-PII guardrails.

- Neither free text nor `clarify:*` ever creates a published WordPress page, activates a template, or writes live files.
- **HITL WordPress draft-page push (Oscar-only):** For a `wr-*` workspace containing `work/page_draft.json`, Oscar (`WP_OPS_ADMIN` + `CODE_EDIT`) can explicitly push a draft via Telegram button `wpreview:push_draft:wr-...` or Dashboard `POST /wp/<site_id>/reviews/<plan_id>/push-draft` (`WpOpsService.push_page_draft()` / `ReviewPlans.push_page_draft()` -> `WordPressClient.create_page(..., status="draft")`). Pinned `base_sha256` and `work_sha256` digests are verified fail-closed, HTML/zero-PII checks re-run, `status != "draft"` is refused (never auto-published), and the response returns `edit_url` (`/wp-admin/post.php?post=<id>&action=edit`).
- **Risk classification:** Pure `work/page_draft.json` + safe `.css` landing/guide drafts remain `"low"` risk; any workspace modifying or creating checkout/order-confirmation templates (`woocommerce/checkout/thankyou.php`, `wc-order-confirmation.php`) remains `"high"` risk and escalates to Oscar.
- Staging a `wr-*` workspace still requires `wp_workspace_static_checks`, passing `wp_workspace_run_checks`, and the explicit server button `wpreview:stage:wr-...` (Oscar `WP_OPS_ADMIN` + `CODE_EDIT`, snapshot hash binding, non-high risk). Production deploy remains closed/Oscar-gated (`BackupNotVerified` / `RestoreNotInMvp`).
