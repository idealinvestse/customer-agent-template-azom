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
| [`config/faq/{se,no,dk}/*.yaml`](../config/faq/) | Articles |

Env overrides:

- `AZOM_FAQ_INJECT_INTO_DRAFT=0|1` — override inject flag
- `AZOM_FAQ_PUBLISH_KILL=1` — deny all publish/sync-draft writes to WP

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
```

Live SE publish requires WP credentials + Oscar actor. NO/DK live markets stay blocked until listed in `live_markets_allowed` and Oscar authorizes.

## Draft integration

After classify, `SupportService.handle` calls `faq.search` when `enabled` and `inject_into_draft`. Hits become `faq_context` for `draft_support_with_llm` and are stored on the case as `faq_article_ids`. Suggest-approve / never-suggest rules are unchanged.

## WordPress

- Target: **pages**, parent slug from `wp_parent_slug` (default `faq`).
- Always sync as draft first; publish is a separate Oscar step.
- Multi-site: `wp_client_from_env(domain=se|no|dk)`.

## Dashboard

Oscar-only `/oscar/faq` — list articles, sync draft, publish (respects kill-switch and live market allowlist).

## Ops gates

| Gate | Owner |
|------|--------|
| Corpus accuracy / translations | Oscar (+ Jonatan review) |
| First live SE FAQ page | Oscar after `probe_wordpress` + wp-admin draft review |
| NO/DK live publish | Oscar written enable + localized stubs completed |
