---
name: research-profiles
description: >
  Goal- and ambition-adaptive catalog of 13 search profiles that act as modular
  research skills and starting points, orchestrating Parallel.ai (/v1/search &
  /v1/extract), Exa Search, Brave Search, Tavily, opt-in SearXNG, and the
  DNS-pinned SafePublicHttpClient in failover, ensemble, and staged combinations.
version: "3.0.0"
---

# Research Profiles & Multi-Tool Combinations

**Purpose:** Give AzomOps-Agent, operators (Jonatan/Oscar), and developers ready-made research skills that automatically combine the right search providers, extraction paths, query templates, freshness windows, and deliverable formats for each goal and ambition level.  
**Related:** [`skills/web-research/SKILL.md`](../web-research/SKILL.md) · [`docs/WEB_SEARCH_RESEARCH.md`](../../docs/WEB_SEARCH_RESEARCH.md) · [`config/research.yaml`](../../config/research.yaml)

## How tool combinations work

Every profile in `skills/ecom_ops/research/profiles.py` (merged with `config/research.yaml`) configures four axes:

1. **`ambition` (`quick` | `standard` | `deep`):** Signal for depth tier (`45s / 1 round`, `150s / 2 rounds`, `300s / 3 rounds`) and query/fetch budget.
2. **`combination_mode` (`failover` | `ensemble` | `staged`):**
   - `failover`: Calls the highest-priority healthy route (`SearchRouter.search()`); fails over automatically via circuit breakers.
   - `ensemble`: Fans out **concurrently** across `fanout_by_depth[depth]` active live providers (`SearchRouter.search_multi()` with bounded `ThreadPoolExecutor`), fuses hits via Reciprocal Rank Fusion (RRF), deduplicates canonical URLs, attaches corroboration (`corroborated_by` / `provider_count`), and keeps the richest excerpt/content.
   - `staged`: Rotates provider combinations round-by-round via `round_providers` (`providers_for_round(profile, round_num)`) so round 1 uses primary semantic/authoritative or Nordic engines (`parallel`, `exa`, `brave`), round 2 expands coverage (`brave`, `exa`, `tavily`), and round 3 triangulates remaining gaps (including opt-in `searxng` where configured).
3. **`fetch_strategy` (`snippet_only` | `direct` | `parallel_extract_first` | `hybrid`):**
   - `snippet_only`: Zero full-page fetches (`max_fetch_default: 0`) for ultra-fast status/news checks.
   - `direct`: Concurrent DNS-pinned HTTPS fetch + `_AllowlistHTMLTextParser` (`SafePublicHttpClient.fetch_many()` with per-domain politeness throttling).
   - `parallel_extract_first`: Concurrent `SafePublicHttpClient.extract_many()` trying `ParallelSearchRoute.extract_many()` (`/v1/extract`) -> `ExaSearchRoute.extract_many()` (`/contents`) -> `SafePublicHttpClient.fetch()` fallback.
   - `hybrid`: Uses `ParallelSearchRoute.extract_many()` / `ExaSearchRoute.extract_many()` on the top highest-scoring URLs and `SafePublicHttpClient.fetch_many()` for the remaining candidates.
4. **Skill blueprint metadata:** `goal`, `when_to_use`, `query_templates`, `deliverable_sections`, `output_format_default`, and `recommended_tools` (`profile="auto"` resolves automatically via `recommend_profile()` in `execute_research_tool` and Dashboard `/research`).

## The 13 built-in search profiles

| Profile ID | Ambition | Combination | Fetch strategy | Preferred providers | Primary goal |
|------------|----------|-------------|----------------|---------------------|--------------|
| `quick_pulse` | `quick` | `failover` | `snippet_only` | `brave, parallel, tavily, exa` | Ultra-fast 14d news, status & release checks without page fetches. |
| `support_fact_check` | `quick` | `failover` | `direct` | `parallel, brave, exa, tavily` | 30d strict fact-checking for PostNord/Bring/DHL outages, consumer law & OEM basics. |
| `technical_docs` | `standard` | `failover` | `parallel_extract_first` | `parallel, exa, brave, tavily` | Official WordPress, WooCommerce (HPOS/Blocks), PHP & CSS reference checks. |
| `design_inspiration` | `standard` | `ensemble` (1/2/3) | `parallel_extract_first` | `exa, parallel, tavily, brave` | UX/UI conversion patterns & layout benchmarks (Baymard, NN/g, Web.dev) before WP edits. |
| `marketing_intel` | `standard` | `ensemble` (1/2/3) | `direct` | `exa, parallel, brave, tavily` | Google Ads, GA4, Consent Mode v2 updates, seasonality & negative-keyword context. |
| `product_spec_deep` | `standard` | `ensemble` (1/2/3) | `parallel_extract_first` | `parallel, exa, brave, tavily` | OEM fitment, CarPlay/Android Auto/CAN-bus specs, pinouts & hardware limits. |
| `seo_content_radar` | `standard` | `ensemble` (1/2/3) | `direct` | `exa, brave, tavily, parallel` | Nordic search intent, buyer FAQs, schema markup & product/category copy gaps. |
| `seo_vendor_vetting` | `standard` | `staged` (R1-R2) | `direct` | `parallel, brave, exa, tavily` | White-hat Nordic SEO link-shop & PR platform vetting (SE/NO/DK pricing, anti-PBN, disclosure rules). |
| `competitor_market` | `deep` | `staged` (R1-R3) | `hybrid` | `parallel, exa, brave, tavily` | SE/NO/DK competitor positioning, pricing, warranty norms & USP matrix. |
| `regulatory_compliance` | `deep` | `staged` (R1-R3) | `direct` | `parallel, brave, tavily, exa, searxng` | Traceable Nordic/EU legal & compliance briefs (Konsumentverket, IMY, Lovdata, EUR-Lex). |
| `conversion_teardown` | `deep` | `staged` (R1-R3) | `parallel_extract_first` | `exa, parallel, tavily, brave` | Empirical CRO audit combining Exa UX research + Parallel extract + live store benchmarks. |
| `deep_general` | `deep` | `staged` (R1-R3) | `hybrid` | `parallel, exa, tavily, brave` | Balanced multi-round strategic & operational research when no narrow profile fits. |
| `metasearch_triangulation` | `deep` | `ensemble` (2/3/4) | `hybrid` | `parallel, exa, brave, tavily, searxng` | Maximum cross-engine consensus & conflict detection across all live routes + SearXNG. |

## Discovering or picking a profile automatically

- **Python / Agent:** `recommend_profile(goal, ambition="quick|standard|deep")` selects the highest-scoring profile and returns the resolved `SearchProfile` skill blueprint.
- **CLI (mock or live):**
  ```bash
  python -m ecom_ops --mock research profiles
  python -m ecom_ops --mock research profiles --id conversion_teardown
  python -m ecom_ops --mock research profiles --recommend "konverteringsanalys tacksida" --ambition deep
  ```
- **Dashboard:** Open `/research` (`Sökprofiler & Research-skills`) or pick any profile directly in the Quick Search and Deep Research forms.
- **Custom YAML profiles:** Add entries under `profiles:` in `config/research.yaml` setting `combination_mode`, `fanout_by_depth`, `round_providers`, `fetch_strategy`, `preferred_providers`, `query_templates`, and `authority_domains` (always bounded by `OutboundQueryFirewall`, `AZOM_SEARCH_ROUTE_ORDER`, `AZOM_SEARCH_ALLOWED_DOMAINS`, and daily/monthly caps).
