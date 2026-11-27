# SEO Studio — AzomOps-Agent (SE / NO / DK)

**Purpose:** Architecture, safety rails, and operator guide for Azom's integrated WordPress/WooCommerce SEO Studio (technical audits, on-page & media SEO, internal linking/cannibalization, Nordic strategies/campaigns, and anti-spam link-package order briefs).  
**Audience:** Coding agents, developers, and internal operators (Jonatan & Oscar).  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md), [`WP_LIVE_BRIDGE_ARCHITECTURE.md`](WP_LIVE_BRIDGE_ARCHITECTURE.md), [`WP_SKILL_GUARDRAILS.md`](WP_SKILL_GUARDRAILS.md).

## Översikt (Overview)

SEO Studio ger Azom ett komplett, mock-first och HITL-skyddat arbetsflöde för organisk tillväxt på `azom.se`, `azom.no` och `azom.dk`:

1. **Teknisk SEO-audit (`seoa-*`), Deep Crawl, Nordisk Paritet & Audit-diff:** HTML `<head>`, titel, metabeskrivning, meta robots, canonical, Open Graph/Twitter, JSON-LD (`Product`, `Offer`, `BreadcrumbList`, `Organization`, `FAQPage`), `robots.txt`, XML-sitemap, begränsad multi-page deep crawl (`scope="deep_crawl"`), `blog_public`, permalänkar, SE/NO/DK `hreflang`-reciprocitet & nordisk paritetsmatris (`/seo/parity`), samt audit-diff trend (`score_delta`, nya och lösta fynd).
2. **On-Page, Bild-SEO (`seom-*`) & Batchplaner (`seob-*`):** Detekterar aktivt SEO-plugin (`yoast`, `rankmath`, `aioseo`, `seopress`, `none`) via `azom-ops-bridge` v1.2.0 (`includes/class-seo.php`), granskar produkter, sidor, inlägg och mediabibliotekets alt-texter/filnamn/dimensioner och skapar SHA-256-låsta enskilda planer (`seom-*`) eller grupperade batchplaner (`seob-*`, `media_alt`/`seo_meta`/`all`) med SERP-förhandsvisning, full Change Vault-snapshot och enskild eller batchvis `batch_id`-återställning.
3. **Internlänkar, kannibalisering & valfri read-only GSC:** Bygger en intern länkgraf, hittar föräldralösa sidor (orphans), svaga ankartexter och URL:er som konkurrerar om samma fokusfras/slug. Med opt-in `AZOM_GSC_ENABLED=1` (isolerad `webmasters.readonly` OAuth-scope, `AZOM_GSC_KILL` av) hämtas även read-only Google Search Console quick-wins (strike-distance pos 4–15, låg CTR, sökordskannibalisering).
4. **Nordiska SEO-strategier (`seos-*`), kampanjer (`seoc-*`), autonom SEO-pipeline & veckovis SEO+plugin-driftdigest (`/seo/digest`):** Kombinerar sajt-audit, WooCommerce-katalog, valfri read-only GSC, GA4 landningssidor, Google Ads söktermer och PII-fri `seo_content_radar`-webbresearch (`research_bridge.py` / `res-*`), samt komplett autonom pipeline (`run_seo_autopilot` / `wp_seo_autopilot` / `/seo autopilot` / `POST /seo/autopilot`: audit → SERP-research → strategi → kampanj → valda nordiska SEO-butiker → beställningsklart `seol-*`-utkast) och samlad veckovis SEO + plugin-driftöversikt (`ecom_ops.seo.digest.build_seo_plugin_drift_digest` / `wp_seo_drift_digest`) kopplad till WooCommerce funnel-kanarier (`/`, `/shop/`, `/cart/`, `/checkout/`).
5. **Kvalitetssäkrade nordiska SEO-butiker (`vendors.py`, `/seo/vendors`), länkpaket-beställare (`länkpaket`, `seol-*`), verifiering & kumulativ ankartextmix:** Utvärderar verifierade nordiska white-hat-plattformar (`bazoom`, `whitepress`, `linkhouse`, `prnews`, `getfluence`, `seeders`) med `seo_vendor_vetting`-research, 0–100-rubrik, 7 hårda anti-PBN-avslag, SEK-golv (`SE/DK >= 1 200 SEK/länk`, `NO >= 1 800 SEK/länk` — rekommenderar färre länkar hellre än billiga PBN:er) och nordiska märkningsregler (`Annons`/`Annonse`/`Annonce`). Genererar och låter redigera utkast för leverantörsklara beställningsunderlag (export som Markdown, JSON och CSV) med per-paket- och sajtövergripande kumulativa anti-spam-guardrails, leveransregistrering (`record_link_delivery`), read-only länkverifiering (`verify_link_package`) och strikt HITL-godkännande av Oscar. Systemet gör **aldrig** automatiska eller tysta externa inköp.

## Arkitektur & lagring

| Lager | Modul / Fil | Ansvar |
|-------|-------------|--------|
| Companion Bridge (PHP v1.2.0) | `wordpress-plugin/azom-ops-bridge/includes/class-seo.php`, `class-plugins.php` | HMAC-autentiserade `/azom-ops/v1/seo/{overview,entities,meta,media}` med meta-allowlist, `expected_before_sha256`, cache-flush + WooCommerce funnel-kanarier (`/`, `/shop/`, `/cart/`, `/checkout/`) vid pluginuppdatering |
| Python Bridge, GSC & WP/Woo-klienter | `skills/ecom_ops/integrations/wp/bridge_client.py`, `integrations/gsc.py`, `wordpress.py`, `woocommerce.py` | `seo_overview`, `seo_entities`, `seo_update_meta`, `seo_media_inventory`, `seo_update_media`, read-only `GoogleSearchConsoleClient` (`webmasters.readonly`), `canary_paths`/`canary_results` |
| SEO Studio kärnmotor, vendors, research-brygga & digest | `skills/ecom_ops/seo/{models,policy,technical,content_audit,planner,store,vendors,research_bridge,service,digest}.py` | `SeoStudioService` (audits, `seom-*`, `seob-*` batch, GSC quick-wins, nordisk paritet, deep crawl, audit diff, kvalitetssäkrade SEO-butiker `vet_link_vendors`/`select_link_package_vendor`, autonom SEO-pipeline `run_seo_autopilot`, utkastredigering/leverans/verifiering/kumulativ mix), `build_seo_plugin_drift_digest`, SQLite WAL `AZOM_DATA_DIR/seo_studio.sqlite3` (`0o600`) |
| Change Vault & Rollback | `skills/ecom_ops/wp_ops/{change_ledger,rollback_service}.py` | `category="seo_meta"` och `category="media_alt"` snapshots med `batch_id` + driftkontroll + enskild och batchvis återställning |
| WP Agent Tools & Playbooks | `skills/ecom_ops/wp_ops/tools/seo.py`, `playbooks.py` | 8 `wp_seo_*`-verktyg (`capability_group="seo"`, `TOOL_REGISTRY=77`, inkl. `wp_seo_autopilot` & `wp_seo_link_package(mode="vendors")`), 4 SEO-playbooks (`PLAYBOOK_TOPICS=20`) |
| Bot & Slash-kommandon | `skills/ecom_ops/bot/seo_handlers.py`, `handlers.py`, `chat_agent.py`, `wp_turn.py`, `dashboard_links.py` | `/seo ...` (inkl. `batch|gsc|parity|diff|digest|verify|vendors|autopilot`), URL-navigeringskort (Telegram + Messenger), `SEO_INTENT_RE`, kollisionsskydd |
| Dashboard `/seo` | `infrastructure/dashboard/seo_routes.py`, `templates/seo/*.html` | Dedikerad toppnavigering under Tillväxt (`/seo`, `/seo/vendors`, `/seo/parity`, `/seo/digest`, `/seo/strategies`, `/seo/campaigns`, `/seo/link-packages`), `POST /seo/autopilot`, `POST /seo/link-packages/<id>/vendor`, filter, SERP-förhandsvisning, Change Vault-länkar, strikt CSP & CSRF |
| CLI | `skills/ecom_ops/cli.py` | `python -m ecom_ops seo ...` |

## Fail-Closed Env-gates & Kill-switches

Alla live-flaggor är skyddade i `PROTECTED_OVERLAY_KEYS` (`skills/ecom_ops/runtime_env.py`) och kan aldrig aktiveras via dashboardens `runtime.env`/`secrets.env`-overlay:

| Process-env | Standard | Funktion |
|-------------|----------|----------|
| `AZOM_USE_MOCK` | `1` (lokalt/tester) | `1` = 100% offline deterministiska mock-fixtures. `0` krävs för live-läge. |
| `AZOM_SEO_WRITE_ENABLED` | `0` (AV) | Krävs (`=1`) tillsammans med `AZOM_USE_MOCK=0` för att verkställa Oscars godkända `seom-*`/`seob-*`-planer live. |
| `AZOM_SEO_KILL` | unset / `0` | Nödstopp som omedelbart blockerar alla SEO-skrivningar och SEO-återställningar. |
| `AZOM_GSC_ENABLED` | `0` (AV) | Krävs (`=1`) tillsammans med `AZOM_USE_MOCK=0` för live read-only Google Search Console (`webmasters.readonly`; återanvänder aldrig Gmail/Marketing-tokens). |
| `AZOM_GSC_KILL` | unset / `0` | Nödstopp som omedelbart stänger live GSC-läsningar. |
| `AZOM_WP_DEPLOY_KILL` | unset / `0` | Globalt WP-deploy-nödstopp som även stoppar SEO-skrivningar. |
| `AZOM_WP_BRIDGE_ENABLED` | `0` (AV) | Krävs för live HMAC-anrop mot `azom-ops-bridge`. |
| `AZOM_WP_RESTORE_ENABLED` | `0` (AV) | Krävs för live Change Vault-rollback (enskild eller `batch_id`) av `seo_meta` / `media_alt`. |

## RBAC-matris (befintliga behörigheter)

| Åtgärd | Jonatan (`viewer`) | Agent (`operator`) | Oscar (`full_admin`) |
|--------|-------------------|--------------------|----------------------|
| Läsa audits, strategier, kampanjer, länkpaket (`WP_OPS_READ` / `MARKETING_READ`) | Ja | Ja | Ja |
| Skapa strategi-, kampanj- och länkpaket-utkast (`MARKETING_SUGGEST`) | Ja | Nej (länkpaket) | Ja |
| Skapa SEO-ändringsplan `seom-*` (staging: `WP_OPS_OPERATE`, prod: `WP_OPS_ADMIN`) | Nej | Endast staging | Ja (staging + prod) |
| Verkställa `seom-*` mot sajt (`WP_OPS_ADMIN` + `expected_sha256`) | Nej | Nej | **Endast Oscar** |
| Godkänna / markera externt beställd / avbryta länkpaket (`MARKETING_MUTATE`) | Nej | Nej | **Endast Oscar** |
| Rulla tillbaka `seo_meta` / `media_alt` (`WP_OPS_RESTORE` + `WP_OPS_ADMIN`) | Nej | Nej | **Endast Oscar** |

## Länkpaket (`länkpaket`, `seol-*`) — Anti-spam & livscykel

- **Ankartext-guardrails:**
  - Varumärke + naken URL + generiska ankartexter **>= 60%** (hård blockering om **< 35%**).
  - Partial-match **<= 25%**.
  - Exact-match **<= 10%** (varning över **10%**, hård blockering av `ready_for_review` / godkännande över **20%**).
  - Enkel mål-URL (ej startsida) flaggas om koncentrationen överstiger **50%** vid `link_count >= 4`.
  - Tvingande nordisk marknads- och språkrelevans (`se`/`no`/`dk`) samt anti-PBN-kvalitetskriterier och månadsvis drip-plan.
- **Livscykel:** `draft` -> `ready_for_review` (`MARKETING_SUGGEST`+, blockeras om `guardrail_blocked=True`) -> `approved_for_manual_order` -> `ordered_external` -> `delivered` / `cancelled` (**endast Oscar `MARKETING_MUTATE`**).
- **Export:** `GET /seo/link-packages/<pkg_id>/export.{md,json,csv}` (aldrig externa köp-API:er).

## Ytor: Bot, Agent-verktyg, Dashboard & CLI

### Bot (`/seo` i Messenger & Telegram)

- `/seo` eller `/seo help` — kommandosammanfattning + `/seo`-länk
- `/seo scan [site-id] [path]` — fullständig sajt-audit (`seoa-*`)
- `/seo product <id|slug>` · `/seo page <id|slug>` · `/seo media [site-id]`
- `/seo batch [site-id] <seoa-id> [media_alt|seo_meta|all]` — skapa SHA-256-låst batchplan (`seob-*`, staging: `WP_OPS_OPERATE`, prod: `WP_OPS_ADMIN`; verkställs aldrig i chatt)
- `/seo gsc [site-id]` · `/seo parity [site-id]` · `/seo diff [site-id]` · `/seo digest [site-id]` · `/seo verify <seol-id>`
- `/seo vendors [se|no|dk] [budget] [antal]` — granska topp 2–3 kvalitetssäkrade nordiska SEO-butiker (read-only + `/seo/vendors`-länk)
- `/seo autopilot [site-id] <ämne> [budget] [antal]` — komplett pipeline (audit → SERP-research → strategi → kampanj → valda SEO-butiker → `seol-*`-utkast; `executed=False`, `external_order_placed=False`, kräver `MARKETING_SUGGEST`)
- `/seo strategy <ämne>` · `/seo campaign <namn>` · `/seo links <mål> [budget] [antal]`
- `/seo plans [site-id]` · `/seo plan <id>` — SHA-256-förhandsvisning + kanalneutrala läs-/navigeringskort (`Öppna i SEO Studio`, `Öppna SEO-butiker`, `Öppna länkpaket`, `Granska i Change Vault`, `Öppna /wp/<site>/plugins` för Telegram + Messenger — utför aldrig skrivningar)

### WP Agent-verktyg (`capability_group="seo"`, 8 verktyg i `TOOL_REGISTRY=77`)

- `wp_seo_audit` (även i driftloopen via `_BRIDGE_READ_TOOLS`; stödjer `scope`: `site`, `technical`, `page`, `product`, `media`, `links`, `cannibalization`, `gsc`, `parity`, `deep_crawl` + `include_diff=True`)
- `wp_seo_plan_meta` (förhandsvisningsplan `seom-*`, körs aldrig i chatt)
- `wp_seo_batch_plan` (förhandsvisningsbatch `seob-*` från audit, staging: `WP_OPS_OPERATE` / prod: `WP_OPS_ADMIN`, körs aldrig i chatt)
- `wp_seo_drift_digest` (read-only veckovis SEO + plugin-driftdigest, `WP_OPS_READ`)
- `wp_seo_strategy` (`seos-*`) · `wp_seo_campaign` (`seoc-*`)
- `wp_seo_link_package` (`mode="create"|"verify"|"cumulative"|"vendors"` → `seol-*`, kvalitetssäkrade nordiska SEO-butiker, verifiering & kumulativ ankartextmix, köper aldrig externt)
- `wp_seo_autopilot` (`WP_OPS_READ` + `MARKETING_SUGGEST`/`MARKETING_MUTATE`, deep phase → audit + SERP-research + `seos-*` + `seoc-*` + valda SEO-butiker + `seol-*`-utkast, alltid `executed=False`, `external_order_placed=False`, `approval_required=True`)

Tillhörande 4 playbooks via `wp_playbook(topic=...)`: `seo_technical_and_onpage`, `seo_product_and_media`, `seo_strategy_and_campaigns`, `seo_link_packages`. Skillkort: [`skills/wordpress-seo/SKILL.md`](../skills/wordpress-seo/SKILL.md).

### CLI-exempel (mock)

```bash
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop --scope media
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop --scope cannibalization
python -m ecom_ops --mock --actor jonatan seo gsc --site mock-shop
python -m ecom_ops --mock --actor jonatan seo parity --site mock-shop
python -m ecom_ops --mock --actor jonatan seo deep-crawl --site mock-shop --max-pages 5
python -m ecom_ops --mock --actor jonatan seo diff --site mock-shop
python -m ecom_ops --mock --actor jonatan seo digest --site mock-shop --days 7
python -m ecom_ops --mock --actor oscar seo fix-propose --site mock-shop --fix-type seo_meta --target-type product --target-id 501 --json-fields '{"meta_title":"CarPlay för Volvo | Azom"}'
python -m ecom_ops --mock --actor oscar seo fix-apply --site mock-shop --plan seom-aaaaaaaaaaaa --expected-sha256 <64-hex>
python -m ecom_ops --mock --actor oscar seo batch-propose --site mock-shop --audit seoa-aaaaaaaaaaaa --scope all
python -m ecom_ops --mock --actor oscar seo batch-apply --site mock-shop --batch seob-aaaaaaaaaaaa --expected-sha256 <64-hex>
python -m ecom_ops --mock --actor jonatan seo strategy --site mock-shop --title "Nordisk CarPlay SEO 2026" --market no
python -m ecom_ops --mock --actor jonatan seo campaign --site mock-shop --name "CarPlay Volvo" --market no
python -m ecom_ops --mock --actor jonatan seo vendors --market no --budget 15000 --links 6 --table
python -m ecom_ops --mock --actor jonatan seo autopilot --site mock-shop --topic "Trådlös CarPlay Volvo & BMW" --market no --budget 15000 --links 6 --table
python -m ecom_ops --mock --actor jonatan seo link-pack --site mock-shop --name "NO CarPlay Q2" --market no --budget 15000 --links 6 --export md
python -m ecom_ops --mock --actor jonatan seo link-verify --id seol-aaaaaaaaaaaa
python -m ecom_ops --mock --actor oscar seo link-pack-status --id seol-aaaaaaaaaaaa --status approved_for_manual_order
```
