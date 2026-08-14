# FAQ / knowledge base (v1)

**Purpose:** Living doc for the Azom FAQ corpus, lexical draft enrichment, and WordPress page publish rails.  
**Audience:** Coding agents and developers; Oscar owns live publish.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`CASES.md`](CASES.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Corpus** | Versioned YAML articles under `config/faq/{market}/` (git source of truth). |
| **Lexical search** | Keyword/category scoring with YAML synonyms + prefix match — no embeddings in v1. |
| **Draft enrichment** | Top FAQ hits injected as `FAQ context:` into LLM/template drafts. Draft prompt 1.3 treats FAQ as policy source of truth. |
| **customer_safe** | Article may be used in customer-facing drafts and WP pages. |
| **needs_review** | Parsed YAML flag; does not filter search (still `customer_safe`). Promote always sets both `customer_safe: false` and `needs_review: true`. |
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
| [`config/faq_synonyms.yaml`](../config/faq_synonyms.yaml) | Shared + SE/NO/DK lexical synonym maps (Oscar can extend without code) |
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
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock faq list --market se
python -m ecom_ops --mock faq search --q "spårning" --market se
python -m ecom_ops --mock faq search --q "sporing" --market no
python -m ecom_ops --mock faq coverage
python -m ecom_ops --mock --actor oscar faq sync-draft --market se
python -m ecom_ops --mock --actor oscar faq publish --market se --status publish

# Ingest pipelines (staging under AZOM_DATA_DIR — never silent write to config/faq or WP)
python -m ecom_ops --mock --actor oscar faq dataset export --market se
python -m ecom_ops --mock --actor oscar faq ingest site --market se
python -m ecom_ops --mock --actor oscar faq ingest products --market se
python -m ecom_ops --mock --actor oscar faq suggest articles --from products --market se
python -m ecom_ops --mock --actor oscar faq suggest articles --from guides --market se
python -m ecom_ops --mock --actor oscar faq promote --id se-azom-pro-headset
python -m ecom_ops --mock --actor oscar faq promote --id se-azom-pro-headset --apply --force
python -m ecom_ops --mock faq staging
python -m ecom_ops --mock --actor oscar faq staging purge --days 90
python -m ecom_ops --mock --actor oscar faq staging purge --days 90 --apply
# expect: search hits tracking articles; coverage gaps_vs_se empty for no/dk categories; promote dry-run does not write
```

Live SE publish requires WP credentials + Oscar actor. NO/DK live markets stay blocked until listed in `live_markets_allowed` and Oscar authorizes.

Ingest / suggest: **FAQ_PUBLISH** (Oscar) in live; mock allows agent/operator. Promote always Oscar + explicit `--apply`. Existing `ingest_{id}.yaml` requires `--force`. Promote does **not** check `AZOM_FAQ_INGEST_KILL` (Oscar may finish a reviewed draft).

## Draft integration

After classify, `SupportService.handle` calls `faq.search` when `enabled` and `inject_into_draft`. Hits become `faq_context` for `draft_support_with_llm` and are stored on the case as `faq_article_ids`. Draft prompt **1.3** tells the LLM to treat FAQ context as policy source of truth. Suggest-approve / never-suggest rules are unchanged.

## WordPress

- Target: **pages**, parent slug from `wp_parent_slug` (default `faq`).
- HTML: one parent page, `lang=` wrapper, H2 per category, H3 per article, TOC per category.
- Always sync as draft first; publish is a separate Oscar step.
- Multi-site: `wp_client_from_env(domain=se|no|dk)`.

## Dashboard

- `/faq` — Jonatan + Oscar read-only browse/search with category filter, scores, article detail (`/faq/<id>`, `FAQ_READ`)
- `/oscar/faq` — Oscar sync-draft / publish, corpus reload, search preview, drift vs last sync, staging draft list + promote dry-run/apply (CSRF + confirm). `?all=1` shows non-`customer_safe` articles. Home KPI card shows `faq_hit_rate`.

Env: `AZOM_FAQ_ENABLED`, `AZOM_FAQ_INJECT_INTO_DRAFT`, `AZOM_FAQ_PUBLISH_KILL`, `AZOM_FAQ_INGEST_KILL`. `faq validate` warns if `customer_safe` is omitted. KPI: `python -m ecom_ops kpis --days 7`.

## Ingest pipelines (HITL)

Four sources feed **staging** under `{AZOM_DATA_DIR}/faq_staging/` and `{AZOM_DATA_DIR}/faq_dataset/` (not git):

| Pipeline | Source | Output |
|----------|--------|--------|
| `faq dataset export` | `cases.db` inbound → sent outbound | JSONL Q&A (+ manifest); PII redacted by default |
| `faq ingest site` | WP REST pages/posts (`domain=`) | JSON docs (no `_candidates.json`) |
| `faq ingest products` | Woo `list_all_products` + allowlisted guide URLs | Product/guide JSON |
| `faq suggest articles --from guides` | `faq_staging/guides/` | YAML drafts (`customer_safe: false`) |

`--from products` also consumes leftover guide JSON. Then Oscar promotes with `faq promote --id … --apply` into `config/faq/{market}/ingest_{id}.yaml`. No auto WP publish.

**Hard rules:**

- Own sites only (WP + Woo). Guide fetch requires host in `guide_allowlist_hosts` + explicit `guide_urls`.
- Stale Q&A (`dataset_max_age_days`, default 365) may stay in eval export; article suggest skips stale unless future Oscar override.
- Never promise refund/garanti in generated drafts (template guardrail).
- Mail history: cases.db is primary; IMAP/`mail fetch --all` is capped (~100) and is **not** a full backfill — Graph/POP3 pagination gaps remain documented here.
- Training external models on raw customer mail requires Oscar DPIA — out of scope.
- **Retention (hygiene, not a register):** `POST /oscar/gdpr/delete` also drops `faq_dataset` JSONL rows with matching `case_id`. `cases retention-purge` (Oscar timer) also runs `faq staging purge` by file age (default 90d). Heuristic PII strip on export remains ops hygiene. Site/product JSON is own-site content, purged by age not email.

## Ops gates

| Gate | Owner |
|------|--------|
| Corpus accuracy / translations | Oscar (+ Jonatan review). NO/DK parity articles land as `customer_safe: false` + `needs_review: true` until Oscar flips flags. |
| First live SE FAQ page | Oscar after `probe_wordpress` + wp-admin draft review |
| NO/DK live publish | Oscar written enable + localized content completed |

## Weekly corpus cadence (15 min)

1. Jonatan: note approve edits that changed policy wording this week.
2. Oscar: update matching YAML under `config/faq/` (PR/review).
3. `python -m ecom_ops --mock faq validate` then `faq coverage` (check `gaps_vs_se` and `needs_review` articles).
4. Oscar: review `/oscar/faq` staging drafts; promote with dry-run then apply; flip `customer_safe` in git after review.
5. If WP live: `faq sync-draft --market se` → wp-admin review → publish only if probe green.

## KPI baseline (human-owned)

Track in ops notes (not agent-marked done):

| Metric | How |
|--------|-----|
| FAQ hit rate | `python -m ecom_ops kpis --days 7` → `n_faq_hit` / `n_faq_retrieve`, `faq_by_category`, `faq_by_market`, `faq_top_articles` (from `faq_retrieve` telemetry; a hit is `hit_count > 0`). Also on dashboard home. |
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
