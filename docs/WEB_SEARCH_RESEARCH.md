# Web Search & Async Deep Research

**Purpose:** Architecture, provider configuration, security guardrails, RBAC, CLI, dashboard, and operational contract for bounded web search and async Deep Research in AzomOps-Agent.  
**Audience:** Coding agents, developers, and Oscar (admin/secrets).  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md), [`MODEL_ROUTER.md`](MODEL_ROUTER.md).

**Document status:** Shipped in code (`3.0.0`, fail-closed mock-first). Live egress requires `AZOM_USE_MOCK=0` plus process-env `AZOM_WEB_SEARCH_ENABLED=1` / `AZOM_WEB_FETCH_ENABLED=1` / `AZOM_RESEARCH_ENABLED=1` and kill-switches unset. Live API keys (`PARALLEL_API_KEY`, `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`) and live soak remain human-owned (Oscar).

## Glossary

| Term | Meaning |
|------|---------|
| **Research Job / Brief ID (`res-<12hex>`)** | Canonical identifier (`^res-[0-9a-f]{12}$`) across CLI, bots, WordPress plans (`research_ids`), Dashboard `/research`, SQLite, and artifacts. |
| **Clarification ID (`cl-<12hex>`)** | Single-use scoping proposal token (`clarify:pick\|go\|other\|cancel:{cid}`) used by `<clarify>` pre-research scoping dialogs (max 3 rounds). |
| **Target Reference (`target_ref`)** | Structured optional link (`case:<uuid>`, `wp:<site_id>:<wr-plan_id>`, `wp_site:<site_id>`, `product:<woo_id>`, `marketing:<kind>`, `chat:<channel>:<peer_id>`). Stored **only** in `research_jobs.sqlite3` (never on disk artifacts) so GDPR unlinking is atomic. |
| **SearchRouter** | Multi-provider router in `skills/ecom_ops/integrations/search/router.py` enforcing fail-closed live gates, `OutboundQueryFirewall`, per-route circuit breakers, and failover. |
| **OutboundQueryFirewall** | Fail-closed scrubber/blocker (`skills/ecom_ops/research/firewall.py`) blocking PII, secrets, internal IDs, and private/internal hosts before any outbound query or URL fetch. |
| **EvidenceStatus** | Corroboration rating on results/briefs: `strong`, `moderate`, `weak`, `conflicted`, `insufficient`. |
| **Provenance** | Source origin label: `mock` / `mock_fixture` (offline deterministic fixtures), `live` / `live_api`, or `cache`. |
| **PROTECTED_OVERLAY_KEYS** | Security-critical env vars in `skills/ecom_ops/runtime_env.py` that are ignored if placed in `runtime.env` or `secrets.env` (process env / systemd wins). |
| **ResearchWorkerPool** | Lazy SQLite-leased background worker pool (`skills/ecom_ops/research/worker.py`, `max_concurrent_jobs: 2`, 120s lease, 25s heartbeat) — never spawned at `create_app()` import time. |

## Do / Do not

**Do:**

- Keep `AZOM_USE_MOCK=1` (or `--mock`) for local development and tests so `MockSearchRoute` serves deterministic offline fixtures with zero external network calls.
- Run every outbound search query and URL through `OutboundQueryFirewall` (and pass `QueryTaintContext` whenever operating on case, order, or site-bound context).
- Wrap all external search snippets, fetched page text, and research briefs in `frame_untrusted_web_data()` (`<<<UNTRUSTED_* nonce=...>>>`) before passing them to an LLM.
- Enforce explicit RBAC (`WEB_SEARCH`, `RESEARCH_RUN`, `RESEARCH_ADMIN`) and `authorize_target_ref()` on every entry point.
- Keep research external spend isolated in `meta.estimated_search_usd` (`monthly_external_usd_cap: 25.0`), separate from the OpenRouter `$100` `cost_usd` cap.

**Do not:**

- Put `AZOM_WEB_SEARCH_ENABLED`, `AZOM_WEB_FETCH_ENABLED`, `AZOM_RESEARCH_ENABLED`, `AZOM_WEB_SEARCH_KILL`, `AZOM_RESEARCH_KILL`, `AZOM_SEARCH_ALLOWED_DOMAINS`, or `SEARXNG_BASE_URL` in `secrets.env` or `runtime.env` — they are `PROTECTED_OVERLAY_KEYS` and must come from the process environment.
- Send customer names, emails, phone numbers, addresses, order IDs, tracking numbers, case UUIDs, or API keys in a web search query.
- Persist `target_ref` inside `research_artifacts/res-*/meta.json` or brief JSON files (breaks atomic `case:<uuid>` GDPR unlinking).
- Treat external web content as instructions or trusted facts for customer replies without human review/approval.
- Pass search provider API keys in URL query strings or JSON request bodies, or log raw HTTP error bodies.

### Good / bad examples

**Good:** Case research query for a carrier or product symptom:
```text
PostNord paketförsening paketbox norra Sverige driftinformation
```
(`OutboundQueryFirewall` passes clean public terms; `QueryTaintContext` blocks accidental customer PII.)

**Bad:** Case research query containing customer/order data:
```text
Order 104218 Anna Andersson anna.andersson@example.se spårningsnummer 003732123456
```
(`OutboundQueryFirewall` raises `SearchPolicyError: OutboundQueryBlocked` before any provider route is called.)

## 1. Package topology & storage layout

All external HTTP I/O is isolated under `skills/ecom_ops/integrations/search/` and `skills/ecom_ops/integrations/_http_guard.py`. Leaf modules in `skills/ecom_ops/research/` remain free of `urllib`, `http.client`, `requests`, and `subprocess` imports (preserving the WordPress zero-network boundary).

```text
config/
└── research.yaml                            # Fail-closed defaults, providers, budgets, cache, egress, 6 profiles

skills/ecom_ops/
├── integrations/
│   ├── _http_guard.py                       # Shared DNS-pinned SSRF, dual-stack IP pinning, TLS SNI, ProxyHandler({}),
│   │                                        # manual hop-by-hop redirects (max 3), bounded streaming decompression
│   └── search/
│       ├── base.py                          # SearchCapabilities, SearchRequest, SearchHit, SearchResult, FetchResult
│       ├── errors.py                        # SearchRouteError (Config | Auth | RateLimit | Unavailable | Policy)
│       ├── circuit_breaker.py               # Cross-process JSON breaker (search_router_health.json + file_lock)
│       ├── router.py                        # SearchRouter: fail-closed live gate, priority, breaker, cache, test_route()
│       ├── fetcher.py                       # SafePublicHttpClient + pure-Python allowlist HTML->markdown/text extractor
│       └── providers/
│           ├── mock.py                      # MockSearchRoute (AZOM_USE_MOCK=1 only; profile-keyed fixtures)
│           ├── parallel.py                  # ParallelSearchRoute (POST /v1/search & /v1/extract, x-api-key)
│           ├── exa.py                       # ExaSearchRoute (POST /search, x-api-key)
│           ├── brave.py                     # BraveSearchRoute (GET /res/v1/web/search, X-Subscription-Token)
│           ├── tavily.py                    # TavilySearchRoute (POST /search, Authorization: Bearer)
│           └── searxng.py                   # SearxngSearchRoute (opt-in HTTPS or non-dashboard loopback)
│
├── research/
│   ├── __init__.py                          # ResearchService, get_research_service()
│   ├── config.py                            # Fail-closed gate helpers + mtime-cached config/research.yaml loader
│   ├── models.py                            # res-<12hex>, SearchProfile, ResearchSpec, ResearchJob, ResearchBrief
│   ├── profiles.py                          # 6 builtin SearchProfiles merged with config/research.yaml
│   ├── firewall.py                          # OutboundQueryFirewall, QueryTaintContext
│   ├── sanitizer.py                         # sanitize_untrusted_text(), frame_untrusted_web_data()
│   ├── evaluator.py                         # URL canonicalization, corroboration, EvidenceStatus, claim classification
│   ├── budget.py                            # Daily search/fetch & monthly external USD cap (isolated from OpenRouter)
│   ├── store.py                             # ResearchStore (research_jobs.sqlite3, web_search_cache.sqlite3, artifacts)
│   ├── engine.py                            # Multi-round planner -> search -> fetch -> evaluate -> synthesizer loop
│   ├── worker.py                            # ResearchWorkerPool (lazy start, SQLite leases, heartbeat, test cleanup)
│   ├── clarifier.py                         # should_trigger_research_clarify(), build_research_spec(), 4 categories
│   └── tools.py                             # 5 shared LLM tool schemas + execute_research_tool()
```

### Runtime artifacts under `AZOM_DATA_DIR`

| Artifact | Purpose | Backup / retention |
|----------|---------|--------------------|
| `research_jobs.sqlite3` | Jobs (`res-*`), `target_ref`, events, steering notes, SQLite leases | WAL + `0600`; pruned by `ops gc` after 14d TTL + 24h grace (pinned jobs kept) |
| `research_artifacts/res-*/` | `brief.json`, `brief.md`, `sources.json`, `meta.json` (no `target_ref` stored) | Excluded from `target_ref`; pruned by `ops gc` after 14d TTL + 24h grace |
| `web_search_cache.sqlite3` | LRU + TTL cached search/fetch payloads (`max_entries: 1000`, `max_mb: 50`) | Purged by `ops gc` (`purged_search_cache_rows`) |
| `search_router_health.json` | Cross-process circuit breaker state (+ `.lock`, mode `0600`) | Operational state |

## 2. Providers & mock configuration

Live route priority defaults to `parallel,exa,brave,tavily,searxng` (overridable via `AZOM_SEARCH_ROUTE_ORDER`, or per-profile `preferred_providers` in `config/research.yaml`). All providers pass API keys strictly in HTTP headers (never query params or JSON body) and never log raw HTTP error bodies.

| Provider (`route_id`) | Endpoint & method | Auth header (env var) | Capabilities & notes |
|-----------------------|-------------------|-----------------------|----------------------|
| **`parallel`** | `POST https://api.parallel.ai/v1/search` and `/v1/extract` | `x-api-key` (`PARALLEL_API_KEY`) | Sends `objective`, `search_queries` (1–5 concise queries `<= 200` chars), `mode` (`mode_quick: fast`, `mode_deep: basic`), and `advanced_settings` (`max_results`, `source_policy.include_domains` / `exclude_domains`, `fetch_policy.max_age_seconds`). Supports extract & deep mode. |
| **`exa`** | `POST https://api.exa.ai/search` | `x-api-key` (`EXA_API_KEY`) | Sends `query`, `type` (`search_type_quick: auto`, `search_type_deep: deep`), `numResults`, `contents` (`highlights`, optional `text`), `includeDomains`/`excludeDomains`, `startPublishedDate`. |
| **`brave`** | `GET https://api.search.brave.com/res/v1/web/search` | `X-Subscription-Token` (`BRAVE_SEARCH_API_KEY`) | Supports Nordic `country` (`SE`/`NO`/`DK`), `search_lang`, `freshness` (`pd`/`pw`/`pm`/`py`), and `extra_snippets=true`. |
| **`tavily`** | `POST https://api.tavily.com/search` | `Authorization: Bearer` (`TAVILY_API_KEY`) | Sends `query`, `search_depth` (`search_depth_quick: basic`, `search_depth_deep: advanced`), `max_results`, `include_raw_content`, `include_domains`/`exclude_domains`, `days`. |
| **`searxng`** (opt-in) | `GET <SEARXNG_BASE_URL>/search?format=json` | Optional `Authorization: Bearer` (`SEARXNG_API_KEY`) | Disabled by default (`providers.searxng.enabled: false` in `config/research.yaml`). Requires process-env `SEARXNG_BASE_URL` pointing to public HTTPS or explicit loopback `http://127.0.0.1:<port>` / `http://[::1]:<port>` (`max_redirects=0`, port must not collide with `DASHBOARD_PORT` or `AZOM_DASHBOARD_PUBLIC_URL`). |
| **`mock`** (`MockSearchRoute`) | Offline only (no network) | None | Active **only** when `AZOM_USE_MOCK=1` (or unset/truthy). Serves deterministic profile-keyed fixtures (`provenance="mock_fixture"`, `estimated_cost_usd=0.0`). Raises `SearchConfigError` (`mock_disabled`) if invoked in live mode. |

## 3. Configuration (`config/research.yaml`) & `.env.example`

`config/research.yaml` (`schema_version: 1`) ships with `enabled: false` (fail-closed):

- **Budgets:** `max_queries_per_day: 200`, `max_fetches_per_day: 300`, `monthly_external_usd_cap: 25.0`, `warn_ratio: 0.8`, `per_turn_quick_search_max: 3`, `per_turn_quick_fetch_max: 2`, `max_concurrent_jobs: 2`.
- **Depth tiers:**
  - `quick`: 1 round, 4 queries, 4 fetches, 45s wall, 3 LLM calls
  - `standard`: 2 rounds, 8 queries, 10 fetches, 150s wall, 6 LLM calls
  - `deep`: 3 rounds, 12 queries, 15 fetches, 300s wall, 9 LLM calls
- **Cache:** `enabled: true`, `default_ttl_hours: 6`, `docs_ttl_hours: 24`, `news_ttl_hours: 1`, `max_entries: 1000`, `max_mb: 50`.
- **Retention:** `artifact_ttl_days: 14`, `artifact_grace_hours: 24`.
- **Egress:** `https_only: true`, `max_redirects: 3`, `connect_timeout_s: 5`, `fetch_timeout_s: 10`, `max_raw_bytes: 524288`, `max_decompressed_bytes: 1048576`, `max_extracted_chars: 12000`, `min_domain_interval_s: 2.0`, `max_retry_after_s: 5.0`.

### Environment variables (`.env.example` & `runtime_env.py`)

| Variable | Overlay status | Purpose |
|----------|----------------|---------|
| `AZOM_WEB_SEARCH_ENABLED` | `PROTECTED_OVERLAY_KEYS` (process env only) | Must be `1` (with `AZOM_USE_MOCK=0`) for live search egress. |
| `AZOM_WEB_FETCH_ENABLED` | `PROTECTED_OVERLAY_KEYS` (process env only) | Must be `1` (with `AZOM_USE_MOCK=0`) for live page fetches. |
| `AZOM_RESEARCH_ENABLED` | `PROTECTED_OVERLAY_KEYS` (process env only) | Must be `1` (with `AZOM_USE_MOCK=0`) for live Deep Research jobs. |
| `AZOM_WEB_SEARCH_KILL` | `PROTECTED_OVERLAY_KEYS` (process env only) | Set `1` to force all search/fetch off even when enabled. |
| `AZOM_RESEARCH_KILL` | `PROTECTED_OVERLAY_KEYS` (process env only) | Set `1` to force async Deep Research jobs off. |
| `AZOM_SEARCH_ALLOWED_DOMAINS` | `PROTECTED_OVERLAY_KEYS` (process env only) | Optional comma-separated domain allowlist narrowing `config/research.yaml`. |
| `SEARXNG_BASE_URL` | `PROTECTED_OVERLAY_KEYS` (process env only) | Opt-in SearXNG origin (HTTPS or non-dashboard loopback). |
| `AZOM_SEARCH_ROUTE_ORDER` | Allowlisted (`runtime.env` / `secrets.env`) | Live provider priority (`parallel,exa,brave,tavily,searxng`). |
| `AZOM_RESEARCH_DEFAULT_COUNTRY` | Allowlisted (`runtime.env` / `secrets.env`) | Default market country (`SE`). |
| `AZOM_RESEARCH_DEFAULT_LANG` | Allowlisted (`runtime.env` / `secrets.env`) | Default language (`sv`). |
| `PARALLEL_API_KEY`, `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`, `SEARXNG_API_KEY` | Allowlisted (`secrets.env` / env) | Provider API keys (Oscar-owned). |

`PROTECTED_OVERLAY_KEYS` are never read from `AZOM_DATA_DIR/runtime.env` or `AZOM_DATA_DIR/secrets.env` — systemd / process env always wins so dashboard-written overlays cannot flip live egress gates or kill-switches.

## 4. The 6 Search Profiles

Defined in `skills/ecom_ops/research/profiles.py` and overridable in `config/research.yaml`:

| Profile ID | Preferred providers | Market defaults | Freshness | Corroboration mode | PII strictness |
|------------|---------------------|-----------------|-----------|--------------------|----------------|
| `design_inspiration` | `exa, parallel, tavily, brave` | `en, sv, nb, da` | 730d | `inspiration_and_heuristics` | `standard` |
| `technical_docs` | `parallel, exa, brave, tavily` | `en` | 540d | `authoritative_primary` | `standard` |
| `competitor_market` | `parallel, exa, brave, tavily` | `SE, NO, DK` | 365d | `triangulated_market` | `high` |
| `support_fact_check` | `parallel, brave, exa, tavily` | `SE, NO, DK` | 30d | `strict_factual` | `strict` |
| `marketing_intel` | `exa, parallel, brave, tavily` | `SE, NO, DK, en` | 365d | `triangulated_market` | `high` |
| `deep_general` | `parallel, exa, tavily, brave` | `SE, en` | 365d | `triangulated_factual` | `high` |

## 5. Autonomy, `<clarify>` scoping & async worker pool

- **Autonomous chat tools (`skills/ecom_ops/research/tools.py`):** `web_search`, `web_fetch_extract`, `web_research_start`, `web_research_status`, `web_research_get` are available inside `chat_agent.py`'s bounded non-WP read loop (`_run_non_wp_read_loop`, max `3` calls, max `2` fetches, `25s` timeout) when web search is available (`AZOM_USE_MOCK=1` or `AZOM_WEB_SEARCH_ENABLED=1`), as well as inside the WordPress file/workspace loop (`3` searches, `2` fetches, `18s` web sub-budget; see [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) and [`WP_SKILL_GUARDRAILS.md`](WP_SKILL_GUARDRAILS.md)).
- **`<clarify>` scoping dialog (`skills/ecom_ops/research/clarifier.py`):**
  - Broad/ambiguous research asks (e.g., `/research` without scoping flags or vague `"konkurrentanalys"` / `"marknadsanalys"` / `"gör en research"`) trigger a pre-research `<clarify>` dialog using one of four research categories (`research_design`, `research_market`, `research_tech`, `research_general`), mapped to `cl-<12hex>` proposals (`clarify:pick|go|other|cancel:{cid}`, max 3 rounds, Messenger Quick Replies up to 13 chips).
  - Narrow quick lookups (`"snabbsök"`, `"kolla om postnord"`, explicit URLs, `/search ...`, or `/research` with `--depth`/`--market`/`--profile`) skip `<clarify>` and run directly.
  - `clarify:*` callbacks remain pure scope-lock (`kind="callback"`) and never bypass HITL approvals.
- **Async worker pool (`skills/ecom_ops/research/worker.py`):**
  - `ResearchWorkerPool` uses SQLite leases (`claim_next`, `lease_s=120`, `25s` heartbeat, `max_concurrent_jobs=2` host-wide across bot + dashboard) and stale lease recovery on startup.
  - **Test-safe lazy startup:** `infrastructure/dashboard/app.py` (`create_app()`) **never** spawns worker threads at import or creation time. Dashboard starts its 1-thread worker lazily on live non-mock/non-test `POST /research/jobs` (respecting `AZOM_DISABLE_BACKGROUND_WORKERS=1`); the bot starts its worker in `bot/__main__.py:main()`; `tests/conftest.py` calls `shutdown_research_workers()` between tests.

## 6. Security guardrails (PII, SSRF, prompt injection, RBAC)

- **Outbound PII & secret firewall (`OutboundQueryFirewall`):** Rejects emails, personal/CPR numbers, international phones, JWTs, search/auth tokens, `wr-*`/`fe-*`/`pr-*`/`cl-*`/`res-*` IDs, UUIDs, contextual order/case/tracking numbers, internal paths (`/var/lib/azom`, `/opt/azom-agent`, `wp-admin`, `xmlrpc.php`), internal hosts (`.local`, `.internal`, `.test`, `.example`, loopback/private IPs), and any values in `QueryTaintContext` (case customer names, emails, phones, addresses, order IDs, tracking numbers, site-bound secrets).
- **SSRF & TLS pinning (`skills/ecom_ops/integrations/_http_guard.py`):** HTTPS-only (except explicit opt-in non-dashboard loopback SearXNG), zero ambient proxy inheritance (`ProxyHandler({})`), single-lookup DNS resolution with dual-stack public-IP pinning (`is_public_global_ip`, blocking loopback, RFC1918, link-local, CGNAT `100.64.0.0/10`, metadata `169.254.169.254`/`fd00:ec2::254`, NAT64), TLS SNI + certificate verification, manual hop-by-hop redirects (`max_redirects=3`, stripping `Authorization`/`Cookie`/`x-api-key`/`X-Subscription-Token`), and streaming decompression caps (`512 KiB` raw / `1 MiB` decompressed).
- **Prompt-injection mitigation (`sanitizer.py` & `fetcher.py`):** Pure-Python `_AllowlistHTMLTextParser` drops `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`, `<noscript>`, `<template>`, and hidden DOM attributes (`hidden`, `aria-hidden="true"`, `display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`). `sanitize_untrusted_text()` strips `<tool_call>`, `<clarify>`, spoofed `<<<UNTRUSTED_*>>>` delimiters, zero-width/bidi chars, and secrets; `frame_untrusted_web_data()` wraps untrusted payloads with per-call 16-hex random nonces.
- **RBAC (`skills/ecom_ops/rbac.py`):**

| Permission | Roles | Controls |
|------------|-------|----------|
| `WEB_SEARCH` | `viewer` (Jonatan), `operator` (agent), `full_admin` (Oscar) | Quick search, URL fetch, view/export own or team-visible research (`authorize_research_access`). |
| `RESEARCH_RUN` | `viewer` (Jonatan), `operator` (agent), `full_admin` (Oscar) | Start, steer, and cancel research jobs (`authorize_target_ref` additionally requires `WP_OPS_READ` for `wp:*`/`wp_site:*`, `CASE_REPLY` for `case:*`, `MARKETING_READ` for `marketing:*`). |
| `RESEARCH_ADMIN` | `full_admin` (Oscar) | View/administer any actor's private research jobs and run cross-actor diagnostics. |
| `wp_workspace_attach_research` | `operator` (agent), `full_admin` (Oscar) | Requires **both** `WP_OPS_OPERATE` and `WEB_SEARCH` (denies Jonatan `viewer`). |

- **GDPR `case:<uuid>` unlink:** `cases/retention.py` (`gdpr_delete` and `purge_closed_cases`) calls `ResearchStore.unlink_target_ref("case:<uuid>")`. Because `target_ref` is stored only in `research_jobs.sqlite3` and omitted from disk JSON artifacts, unlinking is atomic with zero residual case references on disk.

## 7. OpenRouter `$100` cap isolation & model profiles

- External search/fetch spend is tracked via `meta.estimated_search_usd` in `telemetry.jsonl` and checked by `skills/ecom_ops/research/budget.py` (`research_budget_status()`, `can_run_search()`, `can_run_fetch()`) against `monthly_external_usd_cap: 25.0`, `max_queries_per_day: 200`, and `max_fetches_per_day: 300`.
- Search calls record `cost_usd=0.0` so they **never** consume OpenRouter's `$100` `cost_usd` budget (`config/limits.yaml`).
- Deep Research LLM steps use two dedicated profiles in `config/model_profiles.yaml`: `research_planner` (strict JSON query decomposition/gap check) and `research_synthesizer` (cited Swedish/Nordic synthesis with `[S#]` markers). Both run through `ModelRouter` with template fallback (`on_all_failed: template_fallback`).

## 8. CLI, Dashboard (`/research`), and `ops gc`

### CLI (`skills/ecom_ops/cli.py`)

```bash
# cwd: repository root (dev) or /opt/azom-agent (prod)
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock search --query "WooCommerce HPOS hooks" --profile technical_docs --max-results 5
python -m ecom_ops --mock fetch-url --url "https://developer.woocommerce.com/docs/hpos-overview/" --profile technical_docs --max-chars 5000
python -m ecom_ops --mock research start --topic "Nordisk CarPlay marknadsanalys" --profile competitor_market --depth standard --market SE --wait
python -m ecom_ops --mock research list --limit 20
python -m ecom_ops --mock research status --id res-000000000001
python -m ecom_ops --mock research show --id res-000000000001 --section summary
python -m ecom_ops --mock research steer --id res-000000000001 --note "Fokusera på Norge"
python -m ecom_ops --mock research cancel --id res-000000000001

# Surface integrations:
python -m ecom_ops --mock cases research --id <case-uuid> --depth quick --wait
python -m ecom_ops --mock marketing research --topic "Google Ads Consent Mode v2 SE" --depth standard --wait
python -m ecom_ops --mock product-desc --product-id 42 --language sv --research
# expect: JSON with ok: true, provenance: "mock", and res-<12hex> brief/results (product-desc --research refuses --publish)
```

### Dashboard routes (`infrastructure/dashboard/research_routes.py`)

| Route | Permission | Purpose |
|-------|------------|---------|
| `GET /research` | `WEB_SEARCH` or `RESEARCH_RUN` | Research Hub: quick search form, async job queue, budget & provider readiness status. |
| `POST /research/search` | `WEB_SEARCH` | Run synchronous quick search from the Hub. |
| `POST /research/jobs` | `RESEARCH_RUN` | Enqueue async Deep Research job (optional `target_ref`, starts lazy worker when live). |
| `GET /research/<res_id>` | `WEB_SEARCH` + `authorize_research_access("view")` | Detail view: brief, `[S#]` citations, claim classification, events, steers. |
| `GET /research/<res_id>/status` | `WEB_SEARCH` + `authorize_research_access("view")` | JSON polling endpoint (`since_seq`, progress, status, evidence_status). |
| `POST /research/<res_id>/steer` | `RESEARCH_RUN` + `authorize_research_access("steer")` | Queue a steering note for an active job. |
| `POST /research/<res_id>/cancel` | `RESEARCH_RUN` + `authorize_research_access("cancel")` | Request cooperative cancellation. |
| `GET /research/<res_id>/export` | `WEB_SEARCH` + `authorize_research_access("export")` | Download brief as Markdown (`?format=md`) or JSON (`?format=json`). |

Linked research panels also appear on `/cases/<id>`, `/marketing`, `/wp/<site_id>`, and `/wp/<site_id>/reviews/<plan_id>`.

### Housekeeping (`ops gc`)

`skills/ecom_ops/ops_gc.py` (`python -m ecom_ops --actor oscar ops gc [--dry-run] [--grace-hours 24] [--json]`) automatically cleans:

- Expired and LRU-excess rows in `web_search_cache.sqlite3` (`purged_search_cache_rows`).
- Expired, non-pinned terminal `research_artifacts/res-*` directories and corresponding `research_jobs` rows past `artifact_ttl_days` (14d) + `artifact_grace_hours` (24h) (`removed_research_artifacts`), strictly enforcing symlink-safe containment inside `AZOM_DATA_DIR`.

## Related

- [`MODEL_ROUTER.md`](MODEL_ROUTER.md) · [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) · [`WP_SKILL_GUARDRAILS.md`](WP_SKILL_GUARDRAILS.md)
- [`CLI_REFERENCE.md`](CLI_REFERENCE.md) · [`COMPLIANCE.md`](COMPLIANCE.md)
- [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) · [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md)
