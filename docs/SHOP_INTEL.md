# Webshop Intelligence (`shop_intel`) — Architecture & Operator Guide

**Purpose:** Architecture, storage layout, 7 learning domains, security/drift findings, fail-closed gates, RBAC, tailored context injection, and CLI/Bot/Dashboard/systemd interfaces for Webshop Intelligence in AzomOps-Agent.  
**Audience:** Coding agents, developers, and internal operators (Jonatan & Oscar).  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`DOC_STYLE.md`](DOC_STYLE.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md), [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md), [`WEB_SEARCH_RESEARCH.md`](WEB_SEARCH_RESEARCH.md).

**Document status:** Designed for package `3.0.0` (fail-closed mock-first). Offline mock fixtures (`AZOM_USE_MOCK=1`) run deterministic 7-domain learning with zero network calls. Live store learning requires `AZOM_USE_MOCK=0`, process-env `AZOM_SHOP_INTEL_LIVE_ENABLED=1`, and `AZOM_SHOP_INTEL_KILL` unset/`0`.

## Glossary

| Term | Meaning |
|------|---------|
| **Run ID (`si-<12hex>`)** | Canonical learning run identifier (`^si-[a-f0-9]{12}$`) stored in `intel_runs` and attached to dossiers and `SHOP_INTEL_CONTEXT`. |
| **Diff ID (`sid-<12hex>`)** | Canonical cross-run diff identifier (`^sid-[a-f0-9]{12}$`) recording detected changes between consecutive runs in `intel_diffs`. |
| **Finding ID (`sif-<12hex>`)** | Canonical security or drift finding identifier (`^sif-[a-f0-9]{12}$`) stored in `intel_security_findings` with severity `critical\|high\|medium\|low\|info`. |
| **`SHOP_INTEL_CONTEXT`** | Nonce-framed untrusted-data wrapper (`frame_untrusted_web_data(..., label="SHOP_INTEL_CONTEXT")`) used when injecting bounded store knowledge into LLM prompts. |
| **`PROTECTED_OVERLAY_KEYS`** | Security-critical env vars in `skills/ecom_ops/runtime_env.py` (`AZOM_SHOP_INTEL_LIVE_ENABLED`, `AZOM_SHOP_INTEL_KILL`, etc.) that are ignored inside `runtime.env`/`secrets.env` overlays so process/systemd env always wins. |
| **`AZOM_DATA_DIR`** | Persistent runtime data directory: `/var/lib/azom` under systemd production, `/app/.azom-data` in Docker Compose, or `./.azom-data` locally. |
| **`sftp_source: "skipped_rbac"`** | Marker on `theme_architecture` runs executed by a `viewer` actor (Jonatan), indicating SFTP theme/template file inspection was skipped because `WP_OPS_OPERATE` or `WP_OPS_ADMIN` is absent. |

## Do / Do not

**Do:**

- Keep `AZOM_USE_MOCK=1` (or `--mock`) for local development, tests, and dry-runs.
- Scrub all collected store strings with `sanitize_untrusted_text()`, `redact_secret_text()`, and PII stripping before writing to SQLite or `shop_intel_artifacts/`.
- Wrap injected store dossiers in `SHOP_INTEL_CONTEXT` (`frame_untrusted_web_data`) with `provenance`, `run_id`, and `observed_at`.
- Enforce RBAC (`WP_OPS_READ`/`READ`, `RESEARCH_RUN`/`WP_OPS_OPERATE`, `WP_OPS_ADMIN`/`RESEARCH_ADMIN`) across CLI, Bot, Dashboard, and WP tools.

**Do not:**

- Call `list_customers`, `list_orders`, order notes, or secret stores from `shop_intel` collectors.
- Import `requests`, `urllib`, `http.client`, or `subprocess` directly inside `skills/ecom_ops/shop_intel/` (all reads delegate to `WooCommerceClient`, `WordPressClient`, and `wp_ops`).
- Put `AZOM_SHOP_INTEL_LIVE_ENABLED` or `AZOM_SHOP_INTEL_KILL` in `runtime.env` or `secrets.env` overlays — set them in process/systemd env only.
- Treat `SHOP_INTEL_CONTEXT` as trusted instructions or substitute it for live order-status lookups.

### Good / bad examples

**Good:** Support agent reading tailored `support_draft` context before drafting a return reply:
```text
Uses SHOP_INTEL_CONTEXT (run si-a1b2c3d4e5f6, provenance: live) to confirm the store's 30-day return policy and 999 kr free-shipping rule, while calling order tools for the customer's actual order status.
```

**Bad:** Collector or prompt storing/sending customer records in store intel:
```text
Querying WooCommerce /wc/v3/orders or storing customer emails/phones inside intel_facts or shop_intel_artifacts/<site_id>/latest.json.
```
(`shop_intel` strictly forbids customer/order endpoints and scrubs PII/secret patterns before persistence.)

## 1. Architecture, config, and storage layout

All external transport calls stay in existing client layers (`WooCommerceClient`, `WordPressClient`, `wp_ops`). Modules under `skills/ecom_ops/shop_intel/` remain pure Python with zero direct network or subprocess imports.

```text
config/
└── shop_intel.yaml                          # Fail-closed defaults, sample caps, retention, budgets, domain toggles

skills/ecom_ops/
├── integrations/woocommerce.py              # Read-only Woo REST v3 settings/zones/gateways/tax/attributes + mock fixtures
├── runtime_env.py                           # PROTECTED_OVERLAY_KEYS incl. AZOM_SHOP_INTEL_LIVE_ENABLED, AZOM_SHOP_INTEL_KILL
└── shop_intel/
    ├── __init__.py                          # Package exports & get_shop_intel_service()
    ├── models.py                            # Regexes, VALID_DOMAINS, VALID_MODES, VALID_PURPOSES, SHOP_INTEL_INTENT_RE
    ├── config.py                            # Fail-closed gates + config/shop_intel.yaml loader
    ├── store.py                             # SQLite WAL store (shop_intel.sqlite3) + atomic latest.{json,md} artifacts
    ├── collectors.py                        # 7-domain deterministic read-only collectors + PII/secret scrubbing
    ├── analyzer.py                          # Cross-run diffing (sid-*), security/drift findings (sif-*), security_score
    ├── context_builder.py                   # build_tailored_context() + SHOP_INTEL_CONTEXT nonce framing
    └── service.py                           # ShopIntelService (RBAC, runs, queries, diffs, security, exports, schedules)
```

### Configuration defaults (`config/shop_intel.yaml`)

| Key | Default | Purpose |
|-----|---------|---------|
| `schema_version` | `1` | Config schema version |
| `live_enabled` | `false` | Fail-closed config default (process env `AZOM_SHOP_INTEL_LIVE_ENABLED=1` + `AZOM_USE_MOCK=0` still required) |
| `max_products_sample` | `200` | Upper bound on sampled WooCommerce products per run |
| `max_pages_sample` | `50` | Upper bound on sampled WordPress pages per run |
| `max_runs_per_site` | `30` | Maximum historical runs retained per site |
| `retention_days` | `90` | Retention horizon for historical runs/findings |
| `default_schedule_interval_hours` | `24` | Default recurring schedule cadence per site |
| `context_budgets` | `support_draft: 2200`, `product_copy: 2200`, `wp_coder: 3200`, `chat_general: 1800`, `seo: 2200`, `security_audit: 2400` | Per-purpose character caps for `build_tailored_context()` |

### Runtime artifacts under `AZOM_DATA_DIR`

Note the runtime data directory split: `/var/lib/azom` for Ubuntu systemd deployments vs `/app/.azom-data` for Docker Compose (or `./.azom-data` locally).

| Artifact | Purpose | Notes |
|----------|---------|-------|
| `AZOM_DATA_DIR/shop_intel.sqlite3` | SQLite WAL database (`busy_timeout=5000`, mode `0o600` on POSIX) | Tables: `intel_runs`, `intel_facts`, `intel_diffs`, `intel_security_findings`, `intel_schedules`. Connections close cleanly on every operation. |
| `AZOM_DATA_DIR/shop_intel_artifacts/<site_id>/latest.json` | Atomic machine-readable latest dossier for `<site_id>` | Strictly zero-PII; excludes secrets and customer records. |
| `AZOM_DATA_DIR/shop_intel_artifacts/<site_id>/latest.md` | Atomic human-readable Markdown dossier for `<site_id>` | Strictly zero-PII; summarizes 7 domains, score, and active findings. |

## 2. The 7 learning domains (`VALID_DOMAINS`)

| # | Domain | Collected facts & signals |
|---|--------|---------------------------|
| 1 | `platform_rules` | WooCommerce settings (`general`, `products`, `shipping`, `tax`, `checkout`, `account`), shipping zones and methods, free-shipping thresholds, payment gateways, tax classes, and WP/Woo/PHP runtime versions. |
| 2 | `catalog_pricing` | Bounded product sample (`<= 200`), category hierarchy, global attributes, pricing distribution (`min`, `median`, `max`, sale ratio, zero-price anomalies), SKU prefix conventions, stock health, upsell/cross-sell density, and catalog gaps. |
| 3 | `theme_architecture` | Active theme, parent/child structure, `theme_kind` (`block`/`classic`/`hybrid`), Site Editor templates, `theme.json` palette/fonts, WooCommerce template overrides & staleness (via `wp_ops.woo_templates`/`theme_json`/`code_index` when actor has `WP_OPS_OPERATE` or `WP_OPS_ADMIN`; skipped with `sftp_source: "skipped_rbac"` for `viewer`), and MU-plugins summary. |
| 4 | `brand_voice_copy` | Storefront languages (`sv`/`nb`/`da`/`en`), formality (`du` vs `ni`), copy structure, recurring USP phrases, CTA vocabulary, and site title/tagline. |
| 5 | `policies_legal` | Policy/info pages (`retur`, `ångerrätt`/`angrerett`, `frakt`/`leverans`, `garanti`, `köpvillkor`/`vilkår`, `integritet`/`privacy`, `om-oss`, `faq`), extracted grounded rules (return days, warranty years, delivery window, free-shipping threshold) with page slug and content hash, plus missing-policy flags. |
| 6 | `plugins_ecosystem` | Active & inactive plugins with versions, functional grouping (`payment`, `shipping`, `SEO`, `cache`, `security`, `builder`, custom `azom-*`), REST namespaces (`discover_namespaces`), and webhook hosts (secrets stripped). |
| 7 | `security_posture` | Inactive plugin attack surface, stale Woo template overrides, `blog_public == 0`, missing legal pages, zero-price published products, and drift alerts vs previous run (`sid-*`); computes `security_score` (`0–100`) and structured `SecurityFinding` rows (`sif-*`). |

## 3. Run modes, security findings (`sif-*`), and drift alerts (`sid-*`)

- **Run modes (`VALID_MODES`):**
  - `full`: collects all 7 domains, computes diff against previous snapshot, updates security posture/score, and writes `latest.{json,md}`.
  - `quick`: fast refresh of core platform rules, plugin ecosystem, and baseline security posture.
  - `security`: focused `security_posture` & cross-run drift verification pass.
- **Finding IDs (`sif-<12hex>`) & severities:** `critical`, `high`, `medium`, `low`, `info`, contributing to a `0–100` `security_score`.
- **Cross-run drift detection (`sid-<12hex>`):**
  - **High:** payment gateway change, shipping rule/threshold change, active theme change.
  - **Medium:** policy page content hash change, `>20%` median price swing, plugin add/remove/toggle/version change.
  - **Baseline posture checks:** inactive plugins installed, stale WooCommerce template overrides, `blog_public == 0` (search engine indexing discouraged), missing core legal pages, published products with zero price.

## 4. Fail-closed env gates, RBAC, and zero-PII context injection

### Environment gates (`.env.example` & `runtime_env.py`)

| Env var | Default | Overlay status | Behavior |
|---------|---------|----------------|----------|
| `AZOM_USE_MOCK` | `1` (dev) / `0` (prod) | Process env | `1` serves deterministic offline mock fixtures; `0` required for live eligibility. |
| `AZOM_SHOP_INTEL_LIVE_ENABLED` | `0` | `PROTECTED_OVERLAY_KEYS` | Must be `1` (in process/systemd env) alongside `AZOM_USE_MOCK=0` for live store reads. |
| `AZOM_SHOP_INTEL_KILL` | `0` / unset | `PROTECTED_OVERLAY_KEYS` | Set `1` to force all Shop Intel learning and scheduled runs off immediately. |
| `AZOM_SHOP_INTEL_INJECT` | `1` | Env / runtime | Set `0` to disable `build_tailored_context()` prompt injection while keeping stored dossiers intact. |

### RBAC (`skills/ecom_ops/rbac.py`)

| Operation | Required permission(s) | Jonatan (`viewer`) | Agent (`operator`) | Oscar (`full_admin`) |
|-----------|------------------------|--------------------|--------------------|----------------------|
| `status`, `show`, `query`, `diff`, `security`, `export` | `WP_OPS_READ` or `READ` | Yes (`theme_architecture` SFTP skipped) | Yes | Yes |
| `run_learning` (`run`) | `WP_OPS_READ` + (`RESEARCH_RUN` or `WP_OPS_OPERATE` or `WP_OPS_ADMIN`) | Yes (`sftp_source: "skipped_rbac"`) | Yes | Yes |
| `configure_schedule` (`schedule`) | `WP_OPS_ADMIN` or `RESEARCH_ADMIN` | No | No | **Yes (Oscar only)** |

### Tailored context injection (`build_tailored_context`)

`build_tailored_context(site_id, *, purpose, data_dir=None, max_chars=None) -> str | None`:

- Returns `None` when no dossier snapshot exists for `site_id` or when `AZOM_SHOP_INTEL_INJECT=0`.
- Otherwise selects domain slices tailored to `purpose` (`VALID_PURPOSES`), bounds output to `context_budgets[purpose]` (or explicit `max_chars`), and wraps the text in `frame_untrusted_web_data(..., label="SHOP_INTEL_CONTEXT", max_chars=...)` with `provenance` (`mock`/`live`), `run_id` (`si-*`), and `observed_at`.
- Supported purposes: `support_draft` (`2200`), `product_copy` (`2200`), `wp_coder` (`3200`), `chat_general` (`1800`), `seo` (`2200`), `security_audit` (`2400`).

## 5. Surfaces: CLI, Bot, Dashboard, WP tools, and systemd

### CLI (`python -m ecom_ops shop-intel ...`)

```bash
# cwd: repository root (dev) or /opt/azom-agent (prod)
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock --actor jonatan shop-intel status --site mock-shop
python -m ecom_ops --mock --actor jonatan shop-intel run --site mock-shop --mode full
python -m ecom_ops --mock --actor jonatan shop-intel show --site mock-shop
python -m ecom_ops --mock --actor jonatan shop-intel query --site mock-shop --domain policies_legal
python -m ecom_ops --mock --actor jonatan shop-intel diff --site mock-shop
python -m ecom_ops --mock --actor jonatan shop-intel security --site mock-shop
python -m ecom_ops --mock --actor jonatan shop-intel export --site mock-shop --format md
python -m ecom_ops --mock --actor oscar shop-intel schedule --site mock-shop --enabled --interval-hours 24
python -m ecom_ops --mock --actor agent shop-intel schedule-run
# expect: JSON payload with ok: true, provenance: "mock", and si-*/sid-*/sif-* IDs
```

### Bot (`/shop-intel` in Messenger & Telegram)

- `/shop-intel` or `/shop-intel status [site-id]` — current snapshot summary, `security_score`, and `/shop-intel` dashboard link.
- `/shop-intel run [site-id] [full|quick|security]` — trigger a learning run (respects RBAC and fail-closed live gates).
- `/shop-intel query [site-id] <domain>` — inspect learned facts for one of the 7 `VALID_DOMAINS`.
- `/shop-intel diff [site-id]` · `/shop-intel security [site-id]` — inspect latest drift (`sid-*`) and security findings (`sif-*`).
- Natural-language routing uses `SHOP_INTEL_INTENT_RE` (avoiding collisions with order, case, SEO, and Google Ads intents).

### Dashboard (`/shop-intel`)

- `/shop-intel` renders per-site overview, 7-domain dossier explorer, security score & `sif-*` findings, `sid-*` drift diffs, Markdown/JSON dossier export, manual run trigger (`RESEARCH_RUN`/`WP_OPS_OPERATE`), and Oscar-only (`WP_OPS_ADMIN`/`RESEARCH_ADMIN`) schedule configuration.

### WP agent tools (`wp_shop_intel_*`)

Read-only tools exposed to bounded WP/chat tool loops for inspecting stored dossiers, querying domain facts, comparing `sid-*` diffs, and reviewing `sif-*` security findings without triggering external calls or bypassing HITL.

### Systemd scheduling (`bin/shop-intel.sh`)

Production Ubuntu hosts run scheduled learning via `bin/shop-intel.sh` and systemd units:

- Runner: [`bin/shop-intel.sh`](../bin/shop-intel.sh) (`exec python -m ecom_ops shop-intel schedule-run "$@"`)
- Service: [`infrastructure/systemd/azom-shop-intel.service`](../infrastructure/systemd/azom-shop-intel.service) (`Type=oneshot`, `User=azom`, `AZOM_DATA_DIR=/var/lib/azom`, `AZOM_USE_MOCK=0`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=/var/lib/azom /var/log/azom /opt/azom-agent/.azom-data`)
- Timer: [`infrastructure/systemd/azom-shop-intel.timer`](../infrastructure/systemd/azom-shop-intel.timer) (`OnCalendar=daily`, `RandomizedDelaySec=15m`, `Persistent=true`)

## Related

- [`CURRENT_STATE.md`](CURRENT_STATE.md) · [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) · [`CLI_REFERENCE.md`](CLI_REFERENCE.md)
- [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) · [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) · [`SEO_STUDIO.md`](SEO_STUDIO.md)
- [`WEB_SEARCH_RESEARCH.md`](WEB_SEARCH_RESEARCH.md) · [`WP_SKILL_GUARDRAILS.md`](WP_SKILL_GUARDRAILS.md)
- Skill card: [`skills/webshop-intelligence/SKILL.md`](../skills/webshop-intelligence/SKILL.md)
