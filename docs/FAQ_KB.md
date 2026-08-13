# FAQ / knowledge base (v1)

**Purpose:** Living doc for the Azom FAQ corpus, lexical draft enrichment, and WordPress page publish rails.  
**Audience:** Coding agents and developers; Oscar owns live publish.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`CASES.md`](CASES.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Corpus** | Versioned YAML articles under `config/faq/{market}/` (git source of truth). |
| **Lexical search** | Keyword/category scoring — no embeddings in v1. |
| **Draft enrichment** | Top FAQ hits injected as `FAQ context:` into LLM/template drafts. |
| **customer_safe** | Article may be used in customer-facing drafts and WP pages. |
| **sync-draft** | Upsert WP page(s) with `status=draft`. |
| **publish** | Flip WP page to `publish` — Oscar + `FAQ_PUBLISH` only. |

## Scope (v1)

**Do:**

- Enrich case drafts with lexical FAQ hits (market + category + query).
- Publish a customer FAQ **page** per market via WordPress REST (`create_page` / `update_page`).
- Keep YAML as source of truth; SQLite publish map under `AZOM_DATA_DIR`.

**Do not:**

- Silent WP publish or Messenger/Telegram publish.
- Embeddings / vector DB.
- Ops chat `/faq` lookup (deferred).
- Treat FAQ hits as auto-send eligibility.
- WP → repo sync (edit corpus in git).

## Config

| File | Role |
|------|------|
| [`config/faq.yaml`](../config/faq.yaml) | Flags: `enabled`, `inject_into_draft`, `max_hits`, kill-switch env, live markets |
| [`config/faq/{se,no,dk}/*.yaml`](../config/faq/) | Articles (git source of truth) |
| [`config/faq_ingest.yaml`](../config/faq_ingest.yaml) | Dataset age, site/product ingest caps, guide allowlist, LLM suggest flag |

Env overrides:

- `AZOM_FAQ_INJECT_INTO_DRAFT=0|1` — override inject flag
- `AZOM_FAQ_PUBLISH_KILL=1` — deny all publish/sync-draft writes to WP
- `AZOM_FAQ_INGEST_KILL=1` — deny dataset export / site / product ingest / suggest

## Permissions

| Permission | Roles |
|------------|--------|
| `FAQ_READ` | viewer, operator, full_admin |
| `FAQ_PUBLISH` | full_admin (Oscar) only |

## CLI

```bash
python -m ecom_ops --mock faq list --market se
python -m ecom_ops --mock faq search --q "spårning" --market se
python -m ecom_ops --mock --actor oscar faq sync-draft --market se
python -m ecom_ops --mock --actor oscar faq publish --market se --status publish

# Ingest pipelines (staging under AZOM_DATA_DIR — never silent write to config/faq or WP)
python -m ecom_ops --mock --actor oscar faq dataset export --market se
python -m ecom_ops --mock --actor oscar faq ingest site --market se
python -m ecom_ops --mock --actor oscar faq ingest products --market se
python -m ecom_ops --mock --actor oscar faq suggest articles --from products --market se
python -m ecom_ops --mock --actor oscar faq promote --id se-azom-pro-headset   # dry-run
python -m ecom_ops --mock --actor oscar faq promote --id se-azom-pro-headset --apply
python -m ecom_ops --mock faq staging
```

Live SE publish requires WP credentials + Oscar actor. NO/DK live markets stay blocked until listed in `live_markets_allowed` and Oscar authorizes.

Ingest / suggest: **FAQ_PUBLISH** (Oscar) in live; mock allows agent/operator. Promote always Oscar + explicit `--apply`.

## Draft integration

After classify, `SupportService.handle` calls `faq.search` when `enabled` and `inject_into_draft`. Hits become `faq_context` for `draft_support_with_llm` and are stored on the case as `faq_article_ids`. Suggest-approve / never-suggest rules are unchanged.

## WordPress

- Target: **pages**, parent slug from `wp_parent_slug` (default `faq`).
- Always sync as draft first; publish is a separate Oscar step.
- Multi-site: `wp_client_from_env(domain=se|no|dk)`.

## Dashboard

- `/faq` — Jonatan + Oscar read-only browse/search (`FAQ_READ`)
- `/oscar/faq` — Oscar sync-draft / publish, corpus reload, search preview, drift vs last sync, **staging counts** + ingest CLI hints

Env: `AZOM_FAQ_ENABLED`, `AZOM_FAQ_INJECT_INTO_DRAFT`, `AZOM_FAQ_PUBLISH_KILL`, `AZOM_FAQ_INGEST_KILL`.

CLI extras: `faq coverage`, `faq validate`, `faq reload`, `faq staging`. FAQ retrieve/hit rates (per category) are on `python -m ecom_ops kpis --days 7` (`n_faq_hit`, `faq_by_category`).

## Ingest pipelines (HITL)

Three sources feed **staging** under `{AZOM_DATA_DIR}/faq_staging/` and `{AZOM_DATA_DIR}/faq_dataset/` (not git):

| Pipeline | Source | Output |
|----------|--------|--------|
| `faq dataset export` | `cases.db` inbound → sent outbound | JSONL Q&A (+ manifest); PII redacted by default |
| `faq ingest site` | WP REST pages/posts (`domain=`) | JSON docs + candidate stubs |
| `faq ingest products` | Woo `list_all_products` + allowlisted guide URLs | Product/guide JSON |

Then `faq suggest articles --from staging|dataset|products` writes YAML **drafts** with `customer_safe: false` and `needs_review: true`. Oscar promotes with `faq promote --id … --apply` into `config/faq/{market}/`. No auto WP publish.

**Hard rules:**

- Own sites only (WP + Woo). Guide fetch requires host in `guide_allowlist_hosts` + explicit `guide_urls`.
- Stale Q&A (`dataset_max_age_days`, default 365) may stay in eval export; article suggest skips stale unless future Oscar override.
- Never promise refund/garanti in generated drafts (template guardrail).
- Mail history: cases.db is primary; IMAP/`mail fetch --all` is capped (~100) and is **not** a full backfill — Graph/POP3 pagination gaps remain documented here.
- Training external models on raw customer mail requires Oscar DPIA — out of scope.
- FAQ dataset/staging is **not** on the cases GDPR export/delete/retention path. Heuristic PII strip on export is ops hygiene, not a register.

## Ops gates

| Gate | Owner |
|------|--------|
| Corpus accuracy / translations | Oscar (+ Jonatan review) |
| First live SE FAQ page | Oscar after `probe_wordpress` + wp-admin draft review |
| NO/DK live publish | Oscar written enable + localized content completed |

## Weekly corpus cadence (15 min)

1. Jonatan: note approve edits that changed policy wording this week.
2. Oscar: update matching YAML under `config/faq/` (PR/review).
3. `python -m ecom_ops --mock faq validate` then `faq coverage`.
4. If WP live: `faq sync-draft --market se` → wp-admin review → publish only if probe green.

## KPI baseline (human-owned)

Track in ops notes (not agent-marked done):

| Metric | How |
|--------|-----|
| FAQ hit rate | `python -m ecom_ops kpis --days 7` → `n_faq_hit` / `n_faq_retrieve` and `faq_by_category` (from `faq_retrieve` telemetry; a hit is `hit_count > 0`) |
| Policy rewrite rate | Approves where Jonatan rewrote shipping/return policy text |
| Zero silent publish | Kill-switch + Oscar-only publish; confirm no unexpected WP publishes |

## Soft-soak checklist (SE FAQ page)

```bash
python -m ecom_ops --mock faq validate
python -m ecom_ops --mock faq search --q "spårning" --market se
python -m ecom_ops --mock cases poll   # confirm faq_article_ids on new cases
# Live (Oscar):
# 1) probe_wordpress green
# 2) AZOM_FAQ_PUBLISH_KILL unset
# 3) python -m ecom_ops --actor oscar faq sync-draft --market se
# 4) wp-admin review draft page
# 5) python -m ecom_ops --actor oscar faq publish --market se --status publish
```

Agents must **not** mark the live SE FAQ gate complete.
