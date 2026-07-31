# WooCommerce and WordPress integrations (V2.1)

**Purpose:** Describe the shipped WooCommerce and WordPress client capabilities so agents do not re-discover gaps from old reviews.  
**Audience:** Developers and coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

## Glossary

| Term | Meaning |
|------|---------|
| **WooCommerce REST** | Store API under `/wp-json/wc/v3/` authenticated with consumer key/secret. |
| **WordPress REST** | Content API under `/wp-json/wp/v2/` authenticated with Application Passwords. |
| **domain=** | Per-call market selector `se` \| `no` \| `dk` resolving `WOO_BASE_URL_*` / WP base. |
| **Shipment trackings** | Official Woo endpoint for tracking numbers — preferred over meta heuristics. |

## Code locations

| Piece | Path |
|-------|------|
| Woo client | `skills/ecom_ops/integrations/woocommerce.py` |
| WP client | `skills/ecom_ops/integrations/wordpress.py` |
| HTTP transport + retry | `RequestsTransport` in Woo integration module |
| Webhook receiver | `skills/ecom_ops/integrations/webhooks.py` |
| Order context / tracking | `skills/ecom_ops/cases/order_context.py` (heuristic fallback remains) |
| Dashboard Woo webhook | `POST /webhooks/woo` |
| Probes | `infrastructure/dashboard/secret_probes.py` (`probe_wordpress`, Woo probes) |

## Environment

```bash
WOO_BASE_URL=https://azom.se
WOO_CONSUMER_KEY=...
WOO_CONSUMER_SECRET=...
# Optional per-domain:
# WOO_BASE_URL_SE= WOO_BASE_URL_NO= WOO_BASE_URL_DK=

WP_USERNAME=...
WP_APP_PASSWORD=...
# WP_BASE_URL=   # optional; otherwise derived from Woo/convention

WOO_WEBHOOK_SECRET=...   # HMAC for POST /webhooks/woo
```

Create WP Application Passwords in wp-admin (example authorize URL pattern: `/wp-admin/authorize-application.php`).

## WooCommerce — shipped capabilities

### Core order/product (V1, still used)

- `get_order`, `list_orders`, `find_orders_by_email`
- `update_order_status`
- `get_product`, `update_product_description`

Auth: HTTP Basic with `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET`.  
Mock: `InMemoryWooTransport` when `AZOM_USE_MOCK=1`.

### V2.1 extensions (implemented)

1. **Shipment trackings** — `list_shipment_trackings` / `add_shipment_tracking` / `delete_shipment_tracking` on `/wc/v3/orders/{id}/shipment-trackings`. Order context prefers this; meta-key heuristic remains as fallback.
2. **Multi-site per call** — `client_from_env(domain="no|se|dk")` uses `woo_base_url_for_domain`. `resolve_order_context` / panel / `id_from_email` accept `domain=`.
3. **Transport** — `requests.Session` reuse; retry/backoff on 429/5xx; honor `Retry-After` and `RateLimit-Retry-After`; configurable timeout.
4. **Pagination** — `list_all_orders` / `list_all_products` iterators with bounded `max_pages`.
5. **More endpoints** — order notes, refunds, customers, coupons, reports, product variations, webhooks CRUD.
6. **System status** — `get_system_status()` → `WooSystemStatus` (Woo/WP versions, active plugins).
7. **Webhooks inbound** — `WebhookReceiver` verifies HMAC-SHA256, dispatches by topic/resource; route `POST /webhooks/woo`; secret `WOO_WEBHOOK_SECRET`.

## WordPress — shipped capabilities

`WordPressClient` covers `/wp-json/wp/v2/`:

- posts, pages, media, users, comments, settings, discovery

Auth: Application Passwords via `WP_USERNAME` + `WP_APP_PASSWORD`.  
Factory: `wp_client_from_env(domain=)` for multi-site.

Secret redaction includes `WP_USERNAME`, `WP_APP_PASSWORD`, `WOO_WEBHOOK_SECRET`.  
Oscar probe: `probe_wordpress`.

## Do / Do not

**Do:**

- Prefer shipment-trackings API when reading tracking for case drafts.
- Pass `domain=` when the mailbox/market is NO or DK so the correct base URL is used.
- Treat webhook delivery failures seriously — Woo disables webhooks after repeated failures (see runbook `docs/runbooks/woo-webhook-disabled.md`).

**Do not:**

- Assume meta-only tracking extraction is the primary path.
- Put Woo/WP secrets in repo YAML — env / `AZOM_DATA_DIR/secrets.env` only.
- Call write endpoints (order status, product publish) as Jonatan via Telegram without operator/admin actor mapping.

## Tests (evidence)

Primary modules (names may grow; re-run `pytest` for counts):

- `tests/test_woo_v21_extensions.py`
- `tests/test_wordpress_client.py`
- `tests/test_woo_webhooks.py`
- `tests/test_order_context_v21.py`

CI: Ruff + coverage ≥ 65% overall.

## Related

- Cases order enrichment: [`CASES.md`](CASES.md)
- Pilot webhook incidents: [`runbooks/woo-webhook-disabled.md`](runbooks/woo-webhook-disabled.md)
- Env contract: `.env.example`
