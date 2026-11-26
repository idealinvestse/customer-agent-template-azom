---
name: web-research
description: >
  Universal autonomous web search and asynchronous Deep Research for AzomOps-Agent.
  Defines the 5 universal research tools, 6 situation-adaptive profiles, 4-mode
  autonomy rubric, source evaluation & [S#] citation verification, outbound PII/taint
  firewall, DNS-pinned HTTPS SSRF guard, multi-provider router, and strict HITL/RBAC
  boundaries.
version: "3.0.0"
---

# Web search and async deep research

**Purpose:** Enable AzomOps-Agent to autonomously search public HTTPS sources, extract clean text, and run non-blocking multi-round Deep Research (`res-<12hex>`) across chat, WordPress workspaces, cases, product copy, marketing, CLI, and dashboard without leaking PII/secrets or weakening HITL gates.  
**Audience:** Coding agents, prompt maintainers, and the OpenClaw chat agent (`Messenger` and `Telegram`).  
**Read this first:** [`AGENTS.md`](../../AGENTS.md), [`docs/CURRENT_STATE.md`](../../docs/CURRENT_STATE.md), [`SOUL.md`](../../SOUL.md), [`docs/WEB_SEARCH_RESEARCH.md`](../../docs/WEB_SEARCH_RESEARCH.md).

## Universal tool surface (5 core tools)

Registered in `skills/ecom_ops/research/tools.py` and `skills/ecom_ops/wp_ops/tools/research.py` (with WP workspace linking via `wp_workspace_attach_research`):

| Tool | Permission | Purpose & bounds |
|------|------------|------------------|
| `web_search` | `WEB_SEARCH` | Synchronous public HTTPS search (`query`, `profile?`, `locale?`, `freshness_days?`, `max_results?`, `domain_hints?`). Nonce-framed output capped at **5 KiB** (`5120` B). |
| `web_fetch_extract` | `WEB_SEARCH` | Fetch and extract clean text/markdown from one public HTTPS URL (`url`, `profile?`, `focus_question?`, `max_chars?`) via DNS-pinned SSRF guard. Capped at **7 KiB** (`7168` B). |
| `web_research_start` | `RESEARCH_RUN` | Enqueue an asynchronous multi-round Deep Research job (`topic`, `profile?`, `depth?` in `quick\|standard\|deep`, `target_ref?`, `market?`, `focus?`). Returns `res-<12hex>` immediately. |
| `web_research_status` | `WEB_SEARCH` | Inspect progress, current round, query/fetch counters, and `evidence_status` for a `res-<12hex>` job. |
| `web_research_get` | `WEB_SEARCH` | Read scrubbed, nonce-framed sections (`summary`, `findings`, `recommendations`, `gaps`, `conflicts`, `citations`, `markdown`, `all`) of a completed or partial brief. Capped at **8 KiB** (`8192` B). |

## Six situation-adaptive profiles (`config/research.yaml`)

| Profile | Primary use cases | Default markets | Freshness | Corroboration mode | Authority focus |
|---------|-------------------|-----------------|-----------|--------------------|-----------------|
| `design_inspiration` | WP UI iteration, thank-you/landing/PDP layouts, conversion UX | `en`, `sv`, `nb`, `da` | 730 d | `inspiration_and_heuristics` | `baymard.com`, `nngroup.com`, `smashingmagazine.com`, `web.dev`, `wordpress.org` |
| `technical_docs` | WooCommerce 9.x/10.x hooks, Checkout Blocks, HPOS, `theme.json`, PHP/CSS | `en` | 540 d | `authoritative_primary` | `developer.wordpress.org`, `woocommerce.com`, `developer.mozilla.org`, `php.net`, `github.com` |
| `competitor_market` | Nordic e-commerce/car-multimedia positioning, pricing, USP, warranty norms | `SE`, `NO`, `DK` | 365 d | `triangulated_market` | Nordic competitors, trade press, market benchmarks |
| `support_fact_check` | PostNord/Bring/DHL outages, SE/NO/DK consumer law, OEM product specs | `SE`, `NO`, `DK` | 30 d | `strict_factual` | `postnord.*`, `bring.*`, `dhl.com`, `konsumentverket.se`, `forbrukerradet.no`, `lovdata.no`, `forbrug.dk` |
| `marketing_intel` | Google Ads/GA4/Consent Mode v2 updates, seasonality, waste-term context | `SE`, `NO`, `DK`, `en` | 365 d | `triangulated_market` | `support.google.com`, `developers.google.com`, `blog.google` |
| `deep_general` | Open-ended strategic/ops research via `/research` or Dashboard | `SE`, `en` | 365 d | `triangulated_factual` | Balanced credible web (spam/farms penalized) |

## Four-mode autonomy rubric

Choose the lightest mode that answers the question honestly:

1. **Mode A — Local Truth Only:** Use local store/site tools (`lookup_order`, `wp_list_products`, `wp_theme_overview`, `wp_inspect_element`, Cases DB, GA4/Ads data, Azom docs) whenever internal truth answers the ask, or on anchored `wr-*` visual refinements (e.g., `"gör knappen mörkblå"`). **Never** substitute web search for internal Azom store/order truth (`SOUL.md`).
2. **Mode B — Autonomous Quick Search:** When an already-scoped task has a bounded external gap (e.g., Woo hook signature, 2–3 UX inspiration refs for a thank-you/landing page, PostNord outage check, Ads waste-term disambiguation), call `web_search` (+ optional 1–2 `web_fetch_extract`) directly without asking.
   - General chat fallback loop cap: **3** web calls / **2** fetches / **25s** wall-time.
   - Inside the WP 16-call / 60s file loop: max **3** web searches, **2** web fetches, **18s** web time, and requires **>= 8s** remaining loop wall time (otherwise fail-fast to `web_research_start`).
3. **Mode C — Precision Scoping Dialog (`<clarify dialog_kind="research">`):** When a research request is broad, multi-market, or ambiguous (e.g., `"gör en konkurrentanalys"`, bare `/research`), run 1–3 clickable clarification rounds via `skills/change-verification/SKILL.md` (`research_design`, `research_market`, `research_tech`, `research_general`) to lock goal/format, market (`SE`/`NO`/`DK`), focus domains, depth (`quick`/`standard`/`deep`), and optional `target_ref`, confirming with `"✅ Starta research"` (`clarify:go:{cid}`).
4. **Mode D — Async Deep Research (`res-<12hex>`):** For confirmed scoping dialogs, explicit `/research ...` commands, `web_research_start`, Dashboard `POST /research/jobs`, or CLI `research start`. Returns `res-<12hex>` immediately (`<1s`) and runs in `ResearchWorkerPool` (`quick`: 1 round / 4 queries / 45s; `standard`: 2 rounds / 8 queries / 150s; `deep`: 3 rounds / 12 queries / 300s; `max_concurrent_jobs: 2`), allowing normal chat to continue uninterrupted.

## Source evaluation & `[S#]` citation verification (`research/evaluator.py`)

- **Deterministic pipeline:** Strip hostile tags/chars (`sanitize_untrusted_text`), canonicalize URLs (strip `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, fragments), deduplicate, classify `SourceType` (`official_docs`, `regulatory_gov`, `carrier_status`, `ux_research`, `competitor_store`, `trade_press`, `forum_community`, `unknown`), and score (`relevance`, `authority`, `freshness`, `signal_density`).
- **`ClaimKind` & `EvidenceStatus`:** Tag claims as `fact`, `best_practice`, `inspiration`, or `opinion`; detect disagreements (`ConflictNote`); and classify overall evidence honestly via `EvidenceStatus` (`grounded`, `inspiration_only`, `partial`, `conflicting`, `insufficient`, `provider_unavailable`, `mock`). Never present `inspiration` or `mock` results as verified live store or legal facts.
- **Post-synthesis verification (`verify_citations()`):** Every inline `[S#]` reference is verified against evaluated sources; unbacked `[S#]` IDs are stripped and flagged in `gaps`, and `grounded` degrades to `partial` if no valid citations remain.

## Security invariants & provider stack

- **Fail-closed mock-first posture:** `AZOM_USE_MOCK=1` (or unset) uses deterministic offline `MockSearchRoute` fixtures with zero sockets (`provenance="mock_fixture"`). Live egress requires `AZOM_USE_MOCK=0`, process-env `AZOM_WEB_SEARCH_ENABLED=1` (plus `AZOM_WEB_FETCH_ENABLED=1` / `AZOM_RESEARCH_ENABLED=1`), and kill switches (`AZOM_WEB_SEARCH_KILL`, `AZOM_RESEARCH_KILL`) unset.
- **Provider stack (`config/research.yaml`):** Dedicated APIs only — `parallel`, `exa`, `brave`, `tavily`, plus opt-in self-hosted `searxng` (`SEARXNG_BASE_URL` HTTPS or `127.0.0.1` only), and `mock` in mock mode. Unofficial HTML scraping and native LLM search grounding are forbidden; API keys are sent header-only and never logged.
- **Zero PII / taint (`OutboundQueryFirewall`):** `skills/ecom_ops/research/firewall.py` blocks real PII, DK CPR, international phones, secrets/API keys, internal IDs (`wr-/fe-/pr-/cl-/res-`, UUIDs, context-bound numeric order/case/tracking IDs), context customer taint, internal hosts/paths, and oversized queries (>250 chars) or URLs (>2048 chars), raising `SearchPolicyError` with zero failover.
- **Untrusted-data isolation:** `ModelProfile.untrusted_data_isolation=True` enforces `role="user"` random-nonce delimiters (`<<<UNTRUSTED_WEB_DATA nonce=...>>>`) and strips `<tool_call>`, `<clarify>`, zero-width, and bidi override characters before LLM ingestion or storage.
- **DNS-pinned HTTPS SSRF guard (`integrations/_http_guard.py`):** Resolves DNS once, rejects literal/octal/hex/private/loopback/link-local/CGNAT/metadata IPs, pins TCP connect to the validated global IP with TLS SNI verification, enforces `ProxyHandler({})` (no ambient proxy), HTTPS-only, manual redirects (max 3 hops, re-checked per hop), strict MIME allowlist, 512 KiB raw / 1 MiB decompressed caps, and >=2s per-domain politeness.
- **Budget isolation:** External search events log `cost_usd=0.0` so OpenRouter's `$100` cap is never polluted; separate `research_budget_status()` enforces `max_queries_per_day` (200), `max_fetches_per_day` (300), and `monthly_external_usd_cap` ($25.00).

## Zero weakening of HITL & operational gates

- Research permissions (`WEB_SEARCH`, `RESEARCH_RUN` for Jonatan/agent/Oscar; `RESEARCH_ADMIN` Oscar-only) are strictly read-only and grant zero write, send, staging, publish, or Ads-mutate authority.
- **Cases poll isolation:** `cases/ingest.py` (`run_poll`) performs **zero** web search or research calls to protect the 5-minute poll SLA and prevent unsupervised PII leakage.
- **Explicit HITL preserved:** `product-desc --research --publish` is refused in one step; customer mail still requires `/cases approve` or approve buttons; WP staging/drafts still require `wpfile:stage:fe-...`, `wpreview:stage:wr-...`, or `wpreview:push_draft:wr-...`; marketing truth hierarchy never mixes web findings into Woo/GA4/Ads metrics.
