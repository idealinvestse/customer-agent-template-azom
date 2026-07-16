# WooCommerce & WordPress kapacitet — genomlysning och beslutsunderlag

**Datum:** 2026-07-17 (Europe/Stockholm) · **Package:** 2.0.0 → 2.1.0 · **Mål:** Beslutsunderlag för vidareutveckling av Woo/WP-kopplingen.

> **Status 2026-07-17:** Alla P0–P3-rekommendationer implementerade och testade. Se §9 nedan.

**Metod:** Genomlysning med alla tillgängliga MCP-servrar i samordnade roller — `sequential-thinking` (struktur/syntes), `parallel-search` + `brave-search` (research), `filesystem` (kodfakta), `mcp-playwright` + `parallel-search.web_fetch` (read-only live-probing mot `azom.no`), `time` (tidsstämpling), `memory` (kunskapsgraf), `exec` (testkörning + täckning). `perplexity-ask` var ogiltig API-nyckel (401) och `github-mcp-server` var nere (connectivity) — ersatta med parallel-search/brave samt lokala git/grep-verktyg. `@21st-dev/magic` exponerar inga verktyg.

---

## 1. Nuläge — WooCommerce

### 1.1 Implementerad klient
`skills/ecom_ops/integrations/woocommerce.py` — `WooCommerceClient`.

| Metod | Endpoint | R/W | Används av |
|-------|----------|-----|------------|
| `get_order(id)` | `GET /wp-json/wc/v3/orders/{id}` | R | order_context, order_status, cases/Telegram |
| `list_orders(per_page)` | `GET /wp-json/wc/v3/orders?per_page=N` | R | secret_probes, smoke |
| `find_orders_by_email(email)` | `GET /wp-json/wc/v3/orders?search=&per_page=` | R | order_context (suggest-approve) |
| `update_order_status(id, status)` | `PUT /wp-json/wc/v3/orders/{id}` | W | order_status action |
| `get_product(id)` | `GET /wp-json/wc/v3/products/{id}` | R | product_desc |
| `update_product_description(id, …)` | `PUT /wp-json/wc/v3/products/{id}` | W | product_desc (publish) |

**Auth:** HTTP Basic med `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET` (env). Mock-läge via `InMemoryWooTransport` (två ordrar, en produkt).

### 1.2 Arkitektur
- `HttpTransport`-Protocol + `RequestsTransport` (live) + `InMemoryWooTransport` (mock) — ren, testbar abstraktion.
- `RequestsTransport` använder `requests.request` per anrop → **ingen session-återanvändning / connection pooling**.
- Timeout hårdkodad 30 s, ej konfigurerbar per anrop.
- Hårdkodat `/wc/v3/` — ingen versionsförhandling.

### 1.3 Identifierade gaps (Woo)
1. **Ingen paginering** — `per_page` max 100; ingen `page`-iteration för listor (orders/products).
2. **Ingen retry/backoff/429-hantering** — `RequestsTransport` kastar `SecurityError` vid ≥400, fångar ej `RateLimit-*`-headers. Woo Store API rate-limit är optional/default off; WP core saknar inbyggd rate-limit (hanteras på host/WAF).
3. **Ingen cachning** — varje order-panel-hämtning slår Woo.
4. **Bara polling, inga webhooks** — cases poll 5 min; `azom.no` stödjer `/wc/v3/webhooks` (HMAC-SHA256, 15 topics). Webhooks avaktiveras efter 5 misslyckade leveranser → retry/loggning behövs.
5. **Smal endpoint-täckning** — saknas: order notes, refunds, customers, coupons, reports/analytics, stock/stock-status, variations, shipping zones, taxes, settings, product categories/tags/attributes/images.
6. **Tracking-extraktion är fragil** — `order_context._extract_tracking` letar heuristiskt i `meta_data`-nyckar. `azom.no` exponerar officiella `/wc/v3/orders/{id}/shipment-trackings` (GET/POST/DELETE) → dedikerat endpoint onödiggör heuristiken.
7. **Multi-site ej kopplat** — `config.py::woo_base_url_for_domain(domain)` löser `WOO_BASE_URL_{SE,NO,DK}`, men `client_from_env` tar single `base_url` och anropar inte den funktionen. Actions har ingen per-anrop domänupplösning → `.no/.se/.dk` löses inte per förfrågan.
8. **`find_orders_by_email`** förlitar sig på Woo `search` (fuzzy) + in-memory `billing.email`-filter som fallback — fungerar men kan returnera fel träff vid brus.

---

## 2. Nuläge — WordPress

### 2.1 Kapacitet i koden: **obefintlig**
- **Ingen `/wp-json/wp/v2/`-klient** finns. Sök efter `wp-json/wp/`, `wp/v2`, `WordPressClient`, `wp_api` → **inga träffar** i koden.
- `config/integrations.yaml` flaggar `wordpress_api: true` — **endast en toggle**, ingen implementation.
- `settings_store.py` + `security.py` listar `WP_APP_PASSWORD` som secret-nyckel (redaction/secret-hantering) — **men ingen kod läser eller använder den**.
- Dashboard `settings.html` visar toggeln "WordPress API" — **endast UI**.

### 2.2 Identifierade gaps (WordPress)
1. **Ingen content-ops-yta** — posts/pages/media/users/comments/settings är outnyttjade trots att `azom.no` exponerar `wp/v2`.
2. **Application Passwords outnyttjat** — `azom.no` stödjer `authentication.application-passwords` (`/wp-admin/authorize-application.php`). `WP_APP_PASSWORD` är redan deklarerad som secret → infrastruktur finns, koden saknas.
3. **Död UI-toggel** — "WordPress API"-toggeln antyder funktion som inte finns → antingen implementera eller ta bort för att undvika missförstånd.
4. **Ingen SEO/blog-automation** — WP är den naturliga ytan för innehållspublicering; agenten har ingen sådan kapacitet idag.

---

## 3. Live-fynd — azom.no (read-only, 2026-07-17)

Källa: `parallel-search.web_fetch` + `mcp-playwright` mot `https://azom.no/wp-json/`.

### 3.1 Tillgängliga REST-namespaces
`wc/v3`, `wc/v2`, `wc/store`, `wc-admin`, `wc-admin-email`, `wc-push-notifications`, `wc-telemetry`, `wccom-site/v3`, `wc/private`, `wc/gla` (Google Listings & Ads), `retainful/v2`, `wp/v2`, `wp-site-health/v1`, `wp-block-editor/v1`, `wp-abilities/v1`.

### 3.2 Auth-yta
- **Application Passwords** tillgängligt → möjliggör `wp/v2`-åtkomst med per-app-autentisering.
- Woo REST kräver consumer key/secret (Basic) — `/wc/v3/system_status` returnerade **401** utan auth (bekräftat via Playwright).

### 3.3 Värdefulla endpoints som koden inte använder
- `/wc/v3/orders/{id}/shipment-trackings` (GET/POST/DELETE) + `/providers` — officiell tracking.
- `/wc/v3/webhooks` (+ `/wc/v2/webhooks/{id}/deliveries`) — webhook-hantering.
- `/wc/v3/taxes`, `/wc/v3/variations`, `/wc/v3/reports`, `/wc/v3/settings`, `/wc/v3/products/brands`.
- `/wp/v2/` — posts, pages, media, users, comments (content-ops).
- `wc/gla` — Google Listings & Ads-integration installerad.
- `retainful/v2` — retention/email-marketing-plugin installerad.

### 3.4 Observations
- Butiken är på **norska (Bokmål)** → språkhantering i product_desc (`no`/`nb`/`nn`) är relevant.
- `azom.no` är en aktiv Woo-butik med rikt plugin-ekosystem → agentens täckning är liten relativt tillgänglig yta.

---

## 4. Testning & gap-analys

### 4.1 Befintliga tester (körda 2026-07-17)
- `pytest -q`: **alla passerar** (269 tester, exit 0).
- `ruff check .`: **6 fel** (import-ordning i testfiler) — pre-existerande, ej Woo/WP-relaterade.
- `python -m ecom_ops smoke --live` (mock): **ok** — woocommerce 1 order, mail ok, telegram skip.

### 4.2 Täckning för Woo/WP-moduler
| Modul | Stmts | Cover | Notering |
|-------|-------|-------|----------|
| `actions/order_status.py` | 48 | **92%** | Stark |
| `actions/product_desc.py` | 89 | **85%** | LLM/error-brancher otestade |
| `integrations/woocommerce.py` | 129 | **78%** | `RequestsTransport` live HTTP (54-68) otestad; `find_orders_by_email`-fallback otestad |
| `order_context.py` | 129 | **76%** | `_extract_tracking`-varianter (115-134) otestade; billing/shipping-helpers |

### 4.3 Test-gap (identifierade, inga nya tester skrivna)
1. **Inga live/contract-tester** mot verklig Woo v3-schema (mock täcker enbart happy path).
2. **`_extract_tracking`** har flera meta-nyckel-varianter otestade — fragilitet osynlig i mock.
3. **Inga tester för paginering** (finns ej implementerat).
4. **Inga tester för 429/retry/backoff** (finns ej implementerat).
5. **`woo_base_url_for_domain`** otestad i integration med `client_from_env` (multi-site ej kopplat).
6. **Inga tester för `/shipment-trackings`-endpoint** (används ej).
7. **Inga tester för webhooks** (används ej).
8. **Inga tester för WordPress `/wp/v2/`** (klient finns ej).

---

## 5. Perspektivmatris

| Perspektiv | Nuläge | Risk/Möjlighet |
|------------|--------|----------------|
| **Funktionell täckning (Woo)** | Smal: order-status + product-desc + read | Hög möjlighet — många endpoints tillgängliga på azom.no |
| **Funktionell täckning (WP)** | Obefintlig | Hög möjlighet — wp/v2 + Application Passwords tillgängligt |
| **Arkitektur** | Ren transport-abstraktion, mock-dubbel | Låg risk; saknar session-återanvändning |
| **Säkerhet/auth** | Basic (Woo), PII-minimering i panel (city+country) | `WP_APP_PASSWORD` deklarerad men oanvänd; raw order exponeras i LLM-drafts (granska PII) |
| **Tillförlitlighet** | Ingen retry/rate/paginering | Medel risk vid skalning / mot azom.no |
| **Datakvalitet** | Tracking-heuristik fragil | Hög risk → lösning finns (shipment-trackings endpoint) |
| **Multi-site** | Deklarerat (.no/.se/.dk) men ej kopplat | Medel risk → låg insats att koppla |
| **Observability** | Telemetry på action-nivå | Saknar Woo-specifik latens/fel-mätning |
| **Utökningsbarhet** | Ny endpoint = ny metod | Låg risk; överväg generisk `request()` |
| **WordPress-specifikt** | Död toggel + oanvänt secret | Hög möjlighet |
| **Testning** | 76–92% på Woo-moduler, mock-only | Medel risk — inga kontrakts/live-tester |
| **PII/compliance** | Panel PII-säker; raw i drafts | Granska raw→LLM-exponering |
| **Prestanda** | Ingen session/caching | Låg-medel vid hög frekvens |
| **Versionering** | Hårdkodat `/wc/v3/` | Låg risk |
| **Dokumentation** | SYSTEM_OVERVIEW nämner Woo; ingen WP-doc | Låg risk |

---

## 6. Prioriterade förbättringar (beslutsrekommendation)

### P0 — Hög impact, låg insats (gör först)
1. **Använd `/wc/v3/orders/{id}/shipment-trackings` istället för meta-heuristik** i `order_context._extract_tracking`. azom.no exponerar endpointet; koden har redan transport-abstraktionen. Eliminerar fragilitet över tracking-plugins.
2. **Koppla `woo_base_url_for_domain` till `client_from_env`/actions** så att `.no/.se/.dk` löses per anrop. Funktionen finns redan — bara att använda.

### P1 — Hög impact, medel insats
3. **WordPress REST-klient (`wp/v2`)** med Application Passwords (`WP_APP_PASSWORD` redan deklarerad). Möjliggör content-ops: posts/pages/media/users/comments. Starta read-only (list/get) för support-kontext, sedan publish för SEO/blogg.
4. **Retry/backoff + `RateLimit-*`-header-hantering + 429** i `RequestsTransport`. Exponentiell backoff för 429/5xx.
5. **Sessionsåteranvändning** (`requests.Session`) + konfigurerbar timeout per anrop.

### P2 — Medel impact, medel insats
6. **Woo webhooks-mottagare** (HMAC-SHA256-verifiering) som komplement till 5-min poll → realtid `order.updated`/`order.created`. Inkludera retry/loggning (Woo avaktiverar efter 5 misslyckade).
7. **Pagineringshjälp** (`page`-iteration) för `list_orders`/products.
8. **Fler Woo-endpoints**: order notes (support-kontext), refunds, customers, coupons, reports/stock — för djupare support-automation.

### P3 — Låg impact, låg insats
9. **Woo version-detektion** (`/wc/v3/system_status` vid auth) + kontrakts-tester mot verklig schema.
10. **Dashboard-toggeln "WordPress API"** — implementera (P1 #3) eller ta bort för att undvika död UI.

### Rekommenderad väg
Börja med **P0** (tracking-endpoint + multi-site) — azom.no exponerar redan dessa och koden har halva infrastrukturen. Sedan **P1 WordPress-klient** eftersom Application Passwords är tillgängligt och `WP_APP_PASSWORD` redan är deklarerad som secret (infrastruktur finns, koden saknas). Därefter P1 reliability (retry/session) för att säkra skalning mot azom.no.

---

## 7. MCP-servrar — användningsöversikt

| Server | Roll i genomlysning | Status |
|--------|---------------------|--------|
| `sequential-thinking` | Struktur + syntes av perspektiv/prioritering | ✅ Använd (5 thoughts) |
| `parallel-search` | Research (web_search) + live-probing azom.no (web_fetch) | ✅ Använd |
| `brave-search` | Aktuell Woo/WP-info, REST-säkerhet | ✅ Använd |
| `perplexity-ask` | Deep research (planerad) | ⚠️ Ogiltig API-nyckel (401) — ersatt |
| `filesystem` | Kodfakta (read_multiple_files) | ✅ Använd |
| `github-mcp-server` | Repo/PR-research (planerad) | ⚠️ Connectivity-fel — ersatt med lokala git/grep |
| `mcp-playwright` | Live-probing azom.no (navigate, 401-bekräftelse) | ✅ Använd |
| `puppeteer` | Alternativ browser-probing | Ej behov (Playwright räckte) |
| `time` | Tidsstämpling (Europe/Stockholm) | ✅ Använd |
| `memory` | Kunskapsgraf (7 entiteter + 7 relationer) | ✅ Använd |
| `@21st-dev/magic` | UI-komponentförslag (planerad) | ⚠️ Inga verktyg exponerade |

---

## 8. Referenser
- Källkod: `skills/ecom_ops/integrations/woocommerce.py`, `skills/ecom_ops/order_context.py`, `skills/ecom_ops/config.py`, `infrastructure/dashboard/secret_probes.py`, `infrastructure/dashboard/settings_store.py`, `skills/ecom_ops/security.py`, `config/integrations.yaml`, `config/sites.yaml`, `.env.example`.
- Live: `https://azom.no/wp-json/` (discovery), `/wp-json/wc/v3/`, `/wp-json/wc/v3/system_status` (401).
- Research: WooCommerce REST API docs (developer.woocommerce.com), Store API rate-limiting, Hookdeck webhook-guide, WordPress REST API Handbook.
- Relaterat: `docs/SYSTEM_OVERVIEW.md`, `docs/ideation/2026-07-11-azom-status-improvement-backlog.md`.

---

## 9. Implementationsstatus (2026-07-17)

Alla P0–P3-rekommendationer implementerade, testade och verifierade.

### P0 — Hög impact, låg insats ✅
1. **Shipment-trackings endpoint** — `WooCommerceClient.list_shipment_trackings/add/delete_shipment_tracking` använder `/wc/v3/orders/{id}/shipment-trackings`. `order_context.resolve_order_panel` hämtar tracking från endpoint först, med meta-heuristik som fallback. Fil: `skills/ecom_ops/integrations/woocommerce.py`, `skills/ecom_ops/order_context.py`.
2. **Multi-site per-anrop** — `client_from_env(domain="no|se|dk")` löser base URL via `woo_base_url_for_domain`. `resolve_order_context/panel/id_from_email` accepterar `domain=`. Fil: `skills/ecom_ops/integrations/woocommerce.py`, `skills/ecom_ops/order_context.py`.

### P1 — Hög impact, medel insats ✅
3. **WordPress REST-klient** — `WordPressClient` i `skills/ecom_ops/integrations/wordpress.py`. Posts/pages/media/users/comments/settings + discovery. Auth via Application Passwords (`WP_USERNAME`+`WP_APP_PASSWORD`). `wp_client_from_env(domain=)`.
4. **Retry/backoff** — `RequestsTransport` har `max_retries`, exponentiell backoff, `Retry-After`/`RateLimit-Retry-After`-headers, retry på 429/5xx. Fil: `skills/ecom_ops/integrations/woocommerce.py`.
5. **Session + timeout** — `RequestsTransport` använder `requests.Session` (connection pooling) + konfigurerbar `default_timeout`.

### P2 — Medel impact, medel insats ✅
6. **Webhooks-mottagare** — `WebhookReceiver` i `skills/ecom_ops/integrations/webhooks.py`. HMAC-SHA256-verifiering + topic/resource-dispatch. Dashboard-route `POST /webhooks/woo`. `WOO_WEBHOOK_SECRET`-env.
7. **Paginering** — `list_all_orders`/`list_all_products` iteratorer (bounded `max_pages`).
8. **Fler endpoints** — order notes, refunds, customers, coupons, reports, product variations, webhooks CRUD.

### P3 — Låg impact, låg insats ✅
9. **Version-detektion** — `get_system_status()` returnerar `WooSystemStatus` (Woo version, WP version, active plugins).
10. **Dashboard-toggeln** — `probe_wordpress` i `secret_probes.py`; `WP_USERNAME`/`WP_APP_PASSWORD`/`WOO_WEBHOOK_SECRET` i secret-redaction.

### Tester
- **Nya testfiler:** `tests/test_woo_v21_extensions.py` (30 tester), `tests/test_wordpress_client.py` (22), `tests/test_woo_webhooks.py` (19), `tests/test_order_context_v21.py` (11).
- **Totalt:** 350 tester, alla gröna (0 fail).
- **Täckning:** webhooks 91%, wordpress 84%, order_context 79%, woocommerce 78%. Totalt 81% (krav: 65%).
- **Ruff:** 0 fel i nya/ändrade filer (5 pre-existerande i test_suggest_approve.py).
