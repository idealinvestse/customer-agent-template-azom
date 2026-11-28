---
name: wordpress-seo
description: >
  End-to-end WordPress and WooCommerce SEO Studio playbook for Azom (SE/NO/DK):
  technical SEO, on-page product/page/media optimization, internal linking and
  cannibalization audits, Nordic SEO strategies and campaigns, and anti-spam
  link-package (länkpaket) order briefs. Never executes live mutations or
  external link purchases from chat; Oscar HITL approval required.
version: "3.0.0"
---

# WordPress & WooCommerce SEO Studio (SE / NO / DK)

**Purpose:** Guide high-precision technical SEO, on-page product/page/media optimization, internal-link and cannibalization audits, Nordic SEO strategies/campaigns, and supplier-ready link-package briefs (`länkpaket`) on registered Azom sites.  
**Audience:** Coding agents and the OpenClaw chat agent (`Messenger` and `Telegram`) operating on registered Azom WordPress/WooCommerce sites.  
**Read this first:** `AGENTS.md`, `SOUL.md`, `docs/WP_SKILL_GUARDRAILS.md`, `docs/SEO_STUDIO.md`, `docs/WORDPRESS_OPS.md`, `docs/WP_LIVE_BRIDGE_ARCHITECTURE.md`, `skills/web-research/SKILL.md`.

## Loop caps, fail-closed env gates, and hard boundaries

See `docs/WP_SKILL_GUARDRAILS.md` for shared loop budgets, untrusted-bytes handling, URL/zero-PII guards, and RBAC rules. Key SEO Studio boundaries:

- **Turn budgets:** Drift loop allows **3** calls, **30s**, **4 KiB / 10 KiB** (supports read-only `wp_seo_audit`). File and workspace loop allows at most **16** tool calls, **10** file reads, **5** searches, and **60s** (`60` seconds) per turn (max **28 KiB** per result / **80 KiB** total), plus the web research sub-budget (**3** `web_search`, **2** `web_fetch_extract`, **18s**).
- **Fail-closed process-env gates (all default OFF, protected in `PROTECTED_OVERLAY_KEYS`):**
  - `AZOM_USE_MOCK=0` required for live eligibility (`1` = deterministic offline mock fixtures).
  - `AZOM_SEO_WRITE_ENABLED=1` — required to apply Oscar-approved `seom-*` / `seob-*` SEO meta & media alt fixes on live sites.
  - `AZOM_SEO_KILL=1` and `AZOM_WP_DEPLOY_KILL=1` — emergency kill-switches immediately halting all SEO writes and SEO rollbacks.
  - `AZOM_GSC_ENABLED=1` (with `AZOM_GSC_KILL` unset) — optional read-only Google Search Console (`webmasters.readonly`, isolated OAuth token — never reuses Gmail or Google Marketing tokens) for strike-distance, low-CTR, and cannibalization quick-wins.
  - `AZOM_WP_BRIDGE_ENABLED=1` — HMAC-authenticated `azom-ops-bridge` v1.2.0 `/azom-ops/v1/seo/*` reads/writes.
  - `AZOM_WP_RESTORE_ENABLED=1` — live Change Vault rollback (single `chg-*` or batch `batch_id`) for `seo_meta` and `media_alt` snapshots.
- **No chat execution & no silent link purchases:**
  - `wp_seo_plan_meta` (`seom-*`) and `wp_seo_batch_plan` (`seob-*`) ALWAYS return `executed: false` and `approval_required: true`. Free-text `"ja"` or `"godkänn"` never writes to WordPress/WooCommerce. Execution requires Oscar (`WP_OPS_ADMIN`) with pinned SHA-256 in `/seo` or CLI.
  - `wp_seo_link_package` (`seol-*`, `mode="create"|"verify"|"cumulative"|"vendors"`) and `wp_seo_autopilot` ALWAYS return `executed: false`, `external_order_placed: false`, and `approval_required: true`. They NEVER contact external link suppliers or place paid orders; they audit, run PII-free SERP & vendor-vetting research (`seo_content_radar`, `seo_vendor_vetting`), rank vetted Nordic white-hat SEO shops, check cross-package cumulative anchor mix, draft/verify `seol-*` briefs, and export (Markdown/JSON/CSV) for Oscar (`MARKETING_MUTATE`) approval in `/seo/link-packages/<id>` and `/seo/vendors`.
- **Data honesty:** Always state which sources backed the audit (`bridge`, WordPress REST, WooCommerce REST, `gsc` mock/live, `ga4`, `google_ads`, `research`, or `mock`). Explicitly include `gsc_note` or `gsc_status` when Google Search Console is disabled/mock — never invent impressions, clicks, or rankings.
- **Zero-PII & catalog truth:** Never fabricate vehicle compatibility, CAN-BUS fitment, warranty promises, or prices. Never include real customer PII in meta tags, schema, research queries, or link briefs.

## Pillar 1 — Technical SEO, Schema, GSC, Nordic Parity & Deep Crawl

1. Call `wp_playbook(topic="seo_technical_and_onpage")` when needed.
2. Run `wp_seo_audit(scope="technical"|"site"|"gsc"|"parity"|"deep_crawl", include_diff=True)`, plus `wp_seo_drift_digest` for combined weekly SEO + plugin drift, complemented by `wp_list_translations` and `wp_fetch_page_html` (on public paths only) when live preview is enabled.
3. Audit core technical controls:
   - Crawlability & indexation: `blog_public` ("Discourage search engines"), `robots.txt` blocks, XML sitemap availability (`sitemap_index.xml` / `wp-sitemap.xml`), bounded multi-page `deep_crawl`, permalink structure, `noindex` / `X-Robots-Tag` leaks.
   - Canonicals & metadata: missing/duplicate `<title>`, meta description length, self-referencing canonicals, Open Graph / Twitter tags, and audit diff trend (`score_delta`, `new_issues`, `resolved_issues`).
   - Structured data (JSON-LD): validate `Product`, `Offer` (price, currency, availability), `BreadcrumbList`, `Organization`, and `FAQPage` markup against real catalog facts.
   - Nordic multilingual `hreflang` & parity (SE/NO/DK): run `scope="parity"` (`/seo/parity`) to verify self-referencing alternates, `x-default`, reciprocity across `azom.se` / `azom.no` / `azom.dk`, no `noindex`/non-200 targets, and parity with Polylang/WPML/TranslatePress IDs (`wp_list_translations`).
   - Optional GSC quick-wins (`scope="gsc"`): read-only strike-distance (pos 4–15), low-CTR, and query cannibalization signals.
4. Report `audit_id` (`seoa-*`), score/grade + diff delta, prioritized critical/warning issues, `data_sources`, `gsc_note`/`gsc_status`, and dashboard links (`/seo/audit/<audit_id>`, `/seo/parity`, `/seo/digest`, Change Vault).

## Pillar 2 — On-Page Product, Page, Post, Image/Media SEO & Batch Plans (`seom-*` / `seob-*`)

1. Call `wp_playbook(topic="seo_product_and_media")` when needed.
2. Ground every recommendation in real entity data via `wp_seo_audit(scope="product"|"page"|"media")`, `wp_get_product`, and `wp_get_page`.
3. Respect the active SEO plugin detected by `azom-ops-bridge` v1.2.0 (`yoast`, `rankmath`, `aioseo`, `seopress`, or `none`):
   - Never overwrite a product's storefront title or WP post title when the user asked to optimize the SEO title/meta description.
   - Keep SEO titles ~50–60 characters, meta descriptions ~135–155 characters, natural focus keyphrases, clean lowercase slugs, and structured `H2`/`H3` + FAQ copy grounded strictly in verified product facts.
4. Audit image & media SEO (`scope="media"`):
   - Flag missing or generic alt texts (`IMG_1234`, `DSC001`, filenames as alt), duplicate alt text across distinct products, non-descriptive filenames, oversized images, missing WebP/AVIF, and missing `width`/`height` CLS risks.
   - Draft concise, descriptive, localized (SV/NB/DA) alt texts describing the actual product/vehicle fitment without keyword stuffing.
5. Propose single changes via `wp_seo_plan_meta` (`fix_type`: `seo_meta`, `media_alt`, `product_seo`, `page_seo`, `schema_jsonld`) → `seom-*`, or group multi-item audit fixes via `wp_seo_batch_plan` (`scope`: `all`, `media_alt`, `seo_meta`) → `seob-*` with pinned `plan_sha256`. Present SERP preview / diff and `/seo` + `/wp/<site>/changes?batch_id=<id>` links for Oscar's approval (every applied change records a Change Vault snapshot under `category="seo_meta"` or `category="media_alt"` with optional `batch_id` for single or full-batch rollback via `wp_change_history`).

## Pillar 3 — Internal Linking & Keyword Cannibalization

1. Run `wp_seo_audit(scope="links")` and `wp_seo_audit(scope="cannibalization")`.
2. Diagnose internal link graph defects:
   - Orphan pages/products with zero inbound internal links.
   - Under-linked commercial category/money pages vs over-linked utility pages.
   - Generic internal anchors (`"klicka här"`, `"läs mer"`) that should use descriptive topical anchors.
   - Keyword cannibalization clusters where multiple products, categories, or guides compete for the same primary term/slug/focus keyphrase — recommend clear consolidation, canonicalization, or distinct intent differentiation (transactional product vs informational guide).

## Pillar 4 — Nordic SEO Strategy, Campaigns & Autonomous Pipeline (`wp_seo_autopilot`)

1. Call `wp_playbook(topic="seo_strategy_and_campaigns")` when needed.
2. Triangulate internal signals (site audit `seoa-*`, WooCommerce catalog, GA4 landing pages, Google Ads converting/waste search terms) with optional read-only web research (`web_search` for quick SERP/schema checks, `web_fetch_extract` for inspecting one competitor/doc URL, or non-blocking `web_research_start` using `seo_content_radar` → `res-*`).
3. Persist structured market-specific strategies via `wp_seo_strategy` → `seos-*` (localized SV/NB/DA terminology — never copy-paste Swedish text onto Norwegian/Danish sites, pillar/cluster architecture, 30/60/90-day roadmap).
4. Break strategies into executable SEO campaigns via `wp_seo_campaign` → `seoc-*` (target keywords, target URLs, funnel intent, on-page/media/schema/internal-link action checklist, linked `strategy_id` and `research_brief_id`).
5. **End-to-end Autopilot (`wp_seo_autopilot` / `/seo autopilot [site] <topic> [budget] [count]`):** Runs site audit (`seoa-*`), then executes PII-free `seo_content_radar` SERP research and `seo_vendor_vetting` Nordic shop research **concurrently** via `seo/research_bridge.py` (`QueryTaintContext` + `OutboundQueryFirewall`) alongside automated strategy (`seos-*`) → campaign (`seoc-*`) → order-ready draft link package (`seol-*`, pre-matched to the #1 vetted shop) → optional `seob-*` batch fix preview. Always returns `executed=false`, `external_order_placed=false`, `approval_required=true`.

## Pillar 5 — Vetted Nordic SEO Shops, Link-Package Builder (`seol-*`), Verification & Cumulative Anchor Mix

1. Call `wp_playbook(topic="seo_link_packages")` when needed.
2. **Vetted Nordic Link-Shop Selector (`wp_seo_link_package(mode="vendors")` / `/seo vendors [se|no|dk] [budget] [count]` / `/seo/vendors`):**
   - Evaluates curated Nordic white-hat platforms (`bazoom`, `whitepress`, `linkhouse`, `prnews`, `getfluence`, `seeders`) augmented by PII-free `seo_vendor_vetting` web research signals.
   - Scores 0–100 (pre-payment transparency, anti-PBN/traffic integrity, SE/NO/DK inventory depth & native copy, editorial/legal disclosure, guarantees/escrow, price-to-authority fit) with **7 hard-reject rules** (blind bundles, PBN/link-farm, guaranteed `#1`, toxic casino/loan/adult neighborhoods, raw machine translation, anchor lockout, no warranty).
   - Enforces white-hat unit floors (**SE/DK >= 1,200 SEK/link**, **NO >= 1,800 SEK/link**): below floor emits a budget warning recommending fewer links — never downgrades to cheap PBNs.
   - Surfaces Nordic commercial-disclosure compliance (**Annons** / **Annonse** / **Annonce** for Konsumentverket / Forbrukertilsynet / Forbrugerombudsmanden).
3. Check existing site-wide cumulative anchor mix with `wp_seo_link_package(mode="cumulative")` before drafting new packages, then build draft supplier order briefs via `wp_seo_link_package(mode="create")` (`WP_OPS_READ` + `MARKETING_SUGGEST`) → `seol-*` (always `status="draft"`, `executed=false`, `external_order_placed=false`).
4. Enforce natural anchor-text mix & anti-spam guardrails (both per-package and cumulative across active/delivered site packages):
   - **Brand + naked URL + generic anchors >= 60%** (hard block below 35%).
   - **Partial-match anchors <= 25%**.
   - **Exact-match anchors <= 10%** (warn above 10%; hard block `ready_for_review` / approval above 20%).
   - **URL concentration:** flag single non-homepage URL concentration > 50% when `link_count >= 4`.
   - **Publisher quality & anti-PBN:** require genuine Nordic editorial sites matching target market (`SE`/`NO`/`DK`) TLD & language, topical automotive/tech relevance, organic traffic, no PBN footprints, no link farms, and a paced monthly drip plan.
5. Lifecycle, delivery & verification: Jonatan (`MARKETING_SUGGEST`) may draft, edit draft targets/anchors, select/switch a vetted shop on `draft` packages, and move `draft -> ready_for_review` (blocked if guardrails fail). **Only Oscar (`MARKETING_MUTATE`)** can transition to `approved_for_manual_order`, `ordered_external`, `delivered`, or `cancelled`, record delivered URLs, and export supplier briefs (`md`, `json`, `csv`) from `/seo/link-packages/<id>`. Run read-only delivery verification via `wp_seo_link_package(mode="verify", package_id=...)` or `/seo verify <seol-id>`.

## Do / Do not

**Do:**
- Run `wp_seo_audit` before proposing `seom-*` fixes, strategies, campaigns, or link packages.
- Present `plan_id`, `plan_sha256`, `gsc_note`, guardrail warnings, and `/seo` dashboard links clearly.
- Keep SEO organic workflows distinct from paid Google Ads campaigns unless the user explicitly asks to compare both.

**Do not:**
- Execute SEO metadata or media alt writes from chat or treat free-text `"ja"`/`"godkänn"` as approval.
- Place or promise automated external link purchases — only draft, audit, and export order briefs.
- Fabricate vehicle compatibility, GSC metrics, or rankings not grounded in tool output.
