# Slutrapport och överlämning: UX/UI-, typografi-, fält- och statusgranskning av Azom webbdashboard

**Purpose:** Fullständig, evidensbaserad granskningsrapport och implementeringsunderlag för hela Azoms Flask-baserade webbdashboard (`infrastructure/dashboard/`, 23 Jinja2-templates och 14 Python-moduler). Nästa kodagent ska kunna ta över och åtgärda samtliga fynd i prioriterade, självständiga arbetspaket utan återupptäckt.  
**Audience:** Övertagande kodagent, utvecklare och Oscar (projektägare / godkännare).  
**Read this first:** [`docs/CURRENT_STATE.md`](../CURRENT_STATE.md), [`AGENTS.md`](../../AGENTS.md), [`SOUL.md`](../../SOUL.md), [`docs/DOC_STYLE.md`](../DOC_STYLE.md), [`skills/azom-safe-ops/SKILL.md`](../../skills/azom-safe-ops/SKILL.md), [`skills/model-router-health/SKILL.md`](../../skills/model-router-health/SKILL.md), [`skills/wordpress-review/SKILL.md`](../../skills/wordpress-review/SKILL.md).

**Document status:** Färdig gransknings- och överlämningsrapport (enbart dokumentation på branch `cursor/dashboard-ux-audit-report-61c4`). Ingen produktionskod i `infrastructure/dashboard/` har ändrats i denna leverans.

---

## Glossary

| Term | Betydelse i denna rapport |
|------|---------------------------|
| **CSP** | `Content-Security-Policy` HTTP-header satt i `infrastructure/dashboard/app.py:120-128`. |
| **HITL** | Human-in-the-loop: explicit mänsklig bekräftelse krävs innan kundmail skickas, marketing-mutation körs eller WordPress-staging/produktion skrivs. |
| **PRG** | Post-Redirect-Get: POST-rutter bör redirecta till GET för att förhindra dubbla anrop vid omladdning. |
| **RBAC** | Rollbaserad åtkomstkontroll: `jonatan` (`viewer` + `CASE_REPLY` + `MARKETING_READ`/`SUGGEST` + `WP_OPS_READ`) och `oscar` (`full_admin` + `CODE_EDIT` + `WP_OPS_ADMIN` + `MARKETING_MUTATE`). |
| **Fail-closed** | Okända, ogiltiga eller saknade parametrar/tillstånd ska neka åtgärd — aldrig falla igenom till utskick eller skrivning. |
| **Null-send** | `AZOM_NULL_SEND=1` (Shadow Live Ledger): kundmail nekas, skuggbeslut loggas. |
| **Path B2** | Rikare utkast för `return`/`billing`, men får aldrig `★` suggest-approve. |

---

## 1. Sammanfattning och verifieringsmetod

Granskningen utfördes i två kompletterande lager utan några live-anrop mot externa tjänster:

1. **Parallell kod- och templategranskning med 5 Gemini Neon-subagenter** (`gemini-4-neon-high`) som rad för rad inspekterade samtliga 23 templates och 14 Python-moduler i `infrastructure/dashboard/` samt relevanta tjänster i `skills/ecom_ops/` och `config/model_profiles.yaml`.
2. **Empirisk runtime-verifiering i isolerat mock-läge (`AZOM_USE_MOCK=1`)** mot lokal Flask-server och headless `google-chrome` via Chrome DevTools Protocol (CDP) på både desktop (`1440x900`) och mobil (`390x844`) för både `oscar` (`full_admin`) och `jonatan` (`viewer`).

### Bekräftade resultat från live-körning i headless Chrome (`AZOM_USE_MOCK=1`)

- **100 % av sidorna (28/28 besökta vyer) saknar Alpine.js (`window.Alpine === undefined`)** på grund av CSP-blockering:
  ```text
  Loading the script 'https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js'
  violates the following Content Security Policy directive:
  "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com"
  ```
- **100 % av sidorna laddar 0 webbtypsnitt (`document.fonts` loaded = 0)** på grund av CSP-blockering:
  ```text
  Loading the stylesheet 'https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=Source+Sans+3:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap'
  violates the following Content Security Policy directive:
  "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com"
  ```
- **Mobilmenyn (`<aside>`) är permanent låst i öppet läge (256 px av 390 px, dvs. 65,6 % av skärmen)** på samtliga 390x844-vyer (`/`, `/cases`, `/oscar`, `/oscar/secrets`, `/wp`) och går inte att stänga.
- **Blanka knappar i produktions-HTML:** `/onboarding` (`Uppdatera status`) och `/cases` (`Hämta mail -> skapa ärenden`) renderar 0 tecken inuti knappen eftersom `x-text` aldrig körs och fallback-text saknas.
- **Okontrollerat JS-kraschfel på `/cases` vid tom kö:** `TypeError: Cannot read properties of null (reading 'addEventListener')` (`cases.html:168`).
- **Osynlig text (kontrast ~1,01:1) i WordPress-formulär:** på `/wp/mock-staging` mättes `select[name="backup_id"]` och `select[name="target_site_id"]` till `color: rgb(232, 238, 246)` (`#e8eef6`) mot `background-color: rgb(239, 239, 239)` (`#efefef`). Samma defekt drabbar `<textarea>` i `wp_files.html:64,77` och `wp_review.html:59`.
- **RBAC bekräftad men odesignad:** `jonatan` på `GET /oscar` returnerar HTTP `403`, dock som oformaterad `text/plain` utanför `base.html`. `GET /favicon.ico` ger HTTP `404` på alla sidor.

### Prioriterad åtgärdsöversikt (P0–P3)

| Prioritet | Antal huvudfynd | Exempel på kritisk påverkan |
|-----------|-----------------|-----------------------------|
| **P0 — Blockerande / Säkerhet / Trasig UI** | 12 | CSP blockerar Alpine.js & Google Fonts; HITL-bekräftelsedialog vid kundmailskick (`case_detail.html:112-121`) uteblir; `cases_routes.py:97` defaultar saknad `action` till `"reply"` (fail-open kundmail!); `cases_routes.py:113` skickar gammalt utkast om textrutan är tom; 5 fält i WP har osynlig text (~1:1 kontrast); Telegram-bot-token kan läcka via `str(exc)` i `secret_probes.py:122-131`; `/telemetry` & `/logs` kringgår `_SECRET_META`-maskering; dubbel-`?` URL i `oscar_routes.py:488-493` kraschar eskalationsflash; `settings_store.py:161` tillåter newline-injektion i `secrets.env`; `wp_code_deep` faller på HTML5 `step="0.05"` för `0.12`. |
| **P1 — Allvarlig UX / Tillgänglighet / Datatapp** | 14 | 25 förekomster av 7 odefinierade Tailwind-färger; WCAG AA-brott på `.az-btn-primary`, `.az-btn-danger`, `.az-missing`, `mist-500`; `.az-select` tar bort dropdown-pilen utan ersättning; 17 av 44 `EDITABLE_SECRET_KEYS` saknas i `/oscar/secrets` medan 11 icke-hemligheter maskeras som `type="password"` med `autocomplete="off"`; `budget_cap_llm` på `/settings` ignoreras tyst och `mock_mode` i live visar `"Sparat"` trots ingen effekt; `wp_review.html` visar aldrig sparade kommentarer eller kontrollfynd; `marketing_routes.py:23-25` sväljer fel tyst; `interact.html` raderar inmatat meddelande och utelämnar det vid eskalering. |
| **P2 — Informationsarkitektur & Layout** | 10 | Dubbletter i sidomeny (`/secrets` vs `/oscar/secrets`, `/data/escalations` vs `/oscar/escalations`, `Hantera`-kort på `index.html`); dubbel `"Telegram"`-punkt i `Integrationer`; `oscar.html` expanderar 39 modell-slots på en enda sida; `<h1>`/`<h2>`-hierarki trasig på alla 23 sidor; saknad brödsmula på `wp_file_plan.html` & `wp_review.html`; saknat GDPR-UI för befintliga endpoints i `oscar_routes.py:503-547`. |
| **P3 — Språk, typografi & finputs** | 8 | Utbredd svengelska (t.ex. blandade svenska/engelska tabellhuvuden på samma rad, `"redigerade"` i stället för `"maskerade"` i `data_audit.html:8`, `"inte i den här formen"` i `oscar_telegram.html:36`); saknad `font-mono` / `tabular-nums` på ID:n, hashar, kostnader och tidsstämplar; råa Python-booleans (`True`/`False`) och Python `dict`-repr; rå Unix-epoch i `Expires`; `/favicon.ico` 404. |

---

## 2. Designsystem, grafik, typografi och CSP-analys

### 2.1 CSP vs. laddade resurser (`app.py:120-128` vs. `base.html:7-10, 363`)

`infrastructure/dashboard/app.py:120-128` skickar:

```text
Content-Security-Policy:
  default-src 'self';
  script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com;
  style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;
  img-src 'self' data: https:;
  connect-src 'self';
  frame-ancestors 'none'
```

**Fyra konkreta fel:**

1. **Alpine.js blockeras dubbelt:** `base.html:363` laddar `https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js`, men `script-src` tillåter `unpkg.com` (oanvänd) och inte `cdn.jsdelivr.net`. Dessutom kräver standard-Alpine `'unsafe-eval'` (`new Function(...)`). **Rekommendation:** Använd antingen självvärdad `@alpinejs/csp` (eller ersätt de få interaktionerna med ~60 rader ren CSP-säker lokal JS i `static/dashboard.js`) och ta bort `https://unpkg.com`.
2. **Google Fonts blockeras dubbelt:** `base.html:7-9` begär `https://fonts.googleapis.com` och `https://fonts.gstatic.com`, men `style-src` saknar `fonts.googleapis.com` och `font-src` saknas helt (faller tillbaka på `default-src 'self'`). **Rekommendation:** Självvärda WOFF2-filer för `Sora`, `Source Sans 3` och `JetBrains Mono` i `infrastructure/dashboard/static/fonts/` (bäst för drift, GDPR och offline-säkerhet), eller lägg uttryckligen till `https://fonts.googleapis.com` i `style-src` och `font-src 'self' https://fonts.gstatic.com`.
3. **Opinnad Tailwind Play CDN i produktion:** `base.html:10` laddar `https://cdn.tailwindcss.com` utan version eller SRI (`integrity`) och kompilerar `<style type="text/tailwindcss">` i webbläsaren vid varje sidladdning (orsakar FOUC och externt driftberoende). **Rekommendation:** Bygg en deterministisk CSS-fil (t.ex. via Tailwind CLI i byggsteg eller lokal versionerad `static/dashboard.css`) och ta bort runtime-kompilering i webbläsaren.
4. **Saknade försvarsdirektiv:** Lägg till `base-uri 'self'; form-action 'self'; object-src 'none';` i CSP.

### 2.2 Färgpalett och WCAG 2.1 kontrastrevision (`base.html:16-47`)

| Förgrund | Bakgrund | Kontrast | WCAG AA (4,5:1) | Var det används | Rekommenderad fix |
|----------|----------|---------:|-----------------|-----------------|-------------------|
| `#e8eef6` (`mist-100`) | `#efefef` / `#ffffff` (browser default) | **1,01–1,16:1** | **KRITISKT FEL** | `wp_site.html:104,114`, `wp_files.html:64,77`, `wp_review.html:59` | Applicera `.az-select` / `.az-textarea` (`bg-ink-950`) |
| `#ffffff` (`white`) | `#3d8bfd` (`steel-500`) | **3,33:1** | **FEL** | `.az-btn-primary` (`base.html:89`, `14px` semibold) | Mörka primärknapp till `#2563eb` / `#1d64d8` (>= 4,8:1) eller använd mörk text |
| `#f31260` (`danger`) | `danger/10` över `#131c2a` | **3,12–3,92:1** | **FEL** | Global felflash (`base.html:338`), inline-fel (`interact.html:23`, `oscar_behavior.html:12`) | Inför `danger-text: #fb7185` (~6,8:1 på `ink-900`) för text; behåll stark röd för kant/ikon |
| `#f31260` (`danger`) | `#131c2a` (`ink-850`) / `#182334` (`ink-800`) | **3,81–4,13:1** | **FEL** | `.az-missing` (`base.html:130`) i korttabeller | Byt `.az-missing` till `#fb7185` (`rose-400`) |
| `#ffffff` (`white`) | `#f31260` (`danger`) | **4,14:1** | **FEL** | `.az-btn-danger` (`base.html:96`, `hover:brightness-110` försämrar ännu mer) | Mörka bakgrund till `#be123c` (`rose-700`, 5,9:1) med `hover:bg-[#9f1239]` |
| `#6b7f97` (`mist-500`) | `#0f1621` / `#131c2a` / `#182334` | **3,84–4,41:1** | **FEL** | Sidomeny-rubriker `10.4px` (`base.html:200,206,235`), `Agent`, header-subtitel (`base.html:301`) | Höj `mist-500` till minst `#7e93ad` (>= 4,8:1 på `ink-850`) |
| `#8496ad` (`mist-400`) | `#0b1017`..`#182334` | 5,23–6,31:1 | OK (AA) | `.az-label`, `.az-muted`, `.az-table th`, `.az-section-title` | Behåll |
| `#3dd68c` (`ok`) | `#0b1017`..`#182334` | 8,43–10,17:1 | OK (AAA) | `.az-present`, ok-badges & flashes | Behåll |
| `#f5a524` (`warn`) | `#0b1017`..`#182334` | 7,74–9,35:1 | OK (AAA) | Varningar & null-send-banner | Behåll |

### 2.3 Alla 25 användningar av 7 odefinierade Tailwind-färgtokens

`base.html:16-47` definierar endast `ink.{600,700,800,850,900,950}`, `steel.{300,400,500,600}`, `teal.{400,500}` (helt oanvända i templates), `mist.{100,300,400,500}` och `oscar.{400,500,600}`. Tailwind genererar **noll CSS** för följande 7 tokens:

| Odefinierad token | Antal | Filer och radnummer | Visuell konsekvens |
|-------------------|------:|---------------------|--------------------|
| `mist-200` | 15 | `case_detail.html:24`, `cases.html:123`, `data_telemetry.html:21`, `oscar.html:118`, `settings.html:59,88`, `wp_files.html:23,25`, `wp_site.html:49,53,86,88,90`, `wp_sites.html:44,48` | Textfärg uteblir på badges och checkbox-etiketter |
| `steel-100` | 3 | `base.html:240`, `case_detail.html:21`, `cases.html:122` | Textfärg på `★ Föreslå godkänn`-badges saknas |
| `steel-900` | 3 | `case_detail.html:11`, `cases.html:11`, `cases.html:114` | Messenger-bannerns och `★`-radernas bakgrundstoning blir helt genomskinlig |
| `steel-200` | 1 | `index.html:19` | `★`-antal tappar markeringsfärg |
| `steel-700` | 1 | `case_detail.html:48` | Orderpanelens kantfärg försvinner |
| `steel-950` | 1 | `case_detail.html:101` | Suggest-approve-kortets bakgrundstoning försvinner |
| `oscar-300` | 1 | `oscar.html:278` | `anpassad`-badge tappar färg |

**Åtgärd:** Utöka färgskalorna komplett i Tailwind-konfigurationen (`steel.{100,200,300,400,500,600,700,900,950}`, `mist.{100,200,300,400,500}`, `oscar.{300,400,500,600}`) eller mappa samtliga 25 rader till verifierade semantiska tokens.

### 2.4 Typografi, texttyper och komponentbrister (`.az-*`)

- **Typsnittsval (`Sora` + `Source Sans 3`):** Bra typografisk kombination för en operatörskonsol, men **laddas aldrig idag** på grund av CSP och saknar uttalad monospace-stack (`fontFamily.mono` är ej definierad i `base.html:49-52`).
- **Saknad `font-mono` / `.az-code` på tekniska fält:**
  - Alla 6 WP-templates har **0 användningar** av `.az-code` eller `font-mono` (trots 64-teckens SHA-256-hashar, filsökvägar, PHP-kod, OpenSSH-nycklar, fingeravtryck, `fe-*`/`wr-*`-ID:n).
  - `oscar.html:76,107,111,115,182,246,342` och `oscar_behavior.html:22` använder proportionell text för modell-slugs (`deepseek/deepseek-v4.1-flash-1017`), device auth-koder och systemprompter med JSON-scheman.
  - `data_telemetry.html:53-54` använder brödtext för `route_id`/`model` bredvid `.az-code` för `action`; `case_detail.html:34,38,40` använder brödtext för `order_id` och `escalation_id`.
- **Saknad `tabular-nums`:** Kostnader (`cost_usd`), tokens, KPI-tal och ISO-tidsstämplar i tabeller saknar `tabular-nums`, vilket gör kolumner taggiga.
- **`.az-section-title` (`base.html:134`) förstör skiftlägeskänsliga sökvägar:** `uppercase` gör `wp_files.html:55` (`MyPlugin/Inc/Cart.php`) till `MYPLUGIN/INC/CART.PHP`.
- **`.az-select` (`base.html:80-82`):** `@apply az-input appearance-none;` tar bort webbläsarens dropdown-pil utan att lägga till SVG-chevron eller `pr-9`. `<select class="az-select">` ser identisk ut med en vanlig textruta. Dessutom använder `oscar.html:137,296,312,336` `<select class="az-input">` i stället för `.az-select`.
- **`.az-input` (`base.html:72`):** Hårdkodad `max-w-md` (448 px) tvingar fram 18+ `!max-w-none` / `!max-w-[...]`-överskrivningar i templates. Bredd ska styras av formulär-/grid-containern, inte av grundklassen.
- **No-op hover-regler & oanvänd knapp:** `.az-btn-secondary` (`base.html:92`) har `border-ink-600 ... hover:border-ink-600`; `.az-nav-link-oscar` (`base.html:112`) har `text-oscar-400 ... hover:text-oscar-400`; `.az-btn-danger` (`base.html:95-97`) används inte på någon destruktiv knapp trots flera destruktiva åtgärder.
- **Checkbox-styling saknar `@tailwindcss/forms`:** Klasserna `text-steel-500` på `<input type="checkbox">` (`settings.html:60,89`, `secrets.html:26`) har ingen effekt utan `accent-steel-500`; i `data_telemetry.html:22`, `oscar.html:343` och `wp_ssh.html:109` är kryssrutor helt ostylade.
- **Rubrikhierarki bruten på alla 23 sidor:** `base.html:298` hårdkodar `<h1>Azom Agent Dashboard</h1>` i toppmenyn. Undersidorna lägger både sidtiteln (`.az-page-title`) och alla kortrubriker (`.az-section-title`) på `<h2>`. `index.html` och `oscar.html` saknar `.az-page-title` helt. **Fix:** Gör varumärket i toppmenyn till `<span>`/`<a>`, höj `.az-page-title` till `<h1>`, behåll kort som `<h2>` och underrubriker som `<h3>`.
- **Utbredd språkblandning (svengelska):** Samtliga 23 templates har `<html lang="sv">` men blandar svenska och engelska inom samma tabellrad (t.ex. `data_telemetry.html:38-45`: `When | Action | Route | Modell | Tokens | Cost | Fel | Detalj`; `data_audit.html:17-22`: `When | Aktör | Action | Mål | Lyckad | Details`), skriver råa Python-booleans (`OK: True · Eskalerad: False`, `Purchase key event: True`), blandar `Spara draft` med `Regenerera utkast` på samma rad (`case_detail.html:147-148`) och felöversätter `"redacted"` till `"redigerade"` (`data_audit.html:8`, bör vara `"maskerade"`) samt `"this form"` till `"den här formen"` (`oscar_telegram.html:36`).

---

## 3. Analys av dold, maskerad, avklippt och oavsiktligt synlig text

Denna sektion besvarar uttryckligen frågan *"Huruvida texten är dold eller inte"* ur fyra perspektiv:

### 3.1 Avsiktligt dolda fält som fungerar korrekt (behåll)
- **CSRF-tokens (`<input type="hidden" name="_csrf">`)** finns i samtliga POST-formulär och valideras i `auth.py:126-143`.
- **Kryptografisk hash-bindning (`before_digest`/`after_digest` i `wp_file_plan.html:41-42`, `base_sha256`/`work_sha256` i `wp_review.html:67-68,76-77`)** verifieras strikt mot serverns tillstånd och visas även läsbart i sammanfattningen (bör dock få `font-mono break-all`).
- **Write-only SSH-privatnyckel på servern (`wp_ssh.html:105`):** Servern skickar aldrig tillbaka lagrad privatnyckel på GET (enbart `ssh_key_stored: true/false`).

### 3.2 Text som är FELAKTIGT dold/maskerad (åtgärda)
1. **11 icke-hemliga konfigurationsfält maskeras blint som `type="password"` i `oscar_secrets.html:62-68`:** `WOO_BASE_URL`, `MAIL_PROVIDER`, `MAIL_USERNAME`, `MAIL_FROM`, `MAIL_OAUTH_REDIRECT_URI`, `GRAPH_TENANT_ID`, `GRAPH_USER`, `SSH_HOST`, `SSH_USER`, `SSH_PORT`, `AZOM_DASHBOARD_PUBLIC_URL`. Operatören kan inte se stavfel i URL:er, e-postadresser, portar eller värdnamn. Ingen visa/dölj-knapp finns på något fält, och `autocomplete="off"` ignoreras av moderna webbläsare för `type="password"` (risk för att webbläsarens Basic Auth-lösenord autofylla hemlighetsfält!).
2. **5 synliga fält i WordPress har osynlig text (~1,01–1,16:1 kontrast):** `wp_site.html:104,114` (`select`), `wp_files.html:64,77` (`textarea`), `wp_review.html:59` (`textarea`) saknar `.az-select`/`.az-textarea` -> ljus text `#e8eef6` på ljus webbläsarbakgrund `#efefef`/`#ffffff`.
3. **Permanenta `x-cloak`-element och blanka `x-text`-knappar (CSP blockerar Alpine.js):**
   - `cases.html:60` — hela filterformuläret (`status`, `mailbox`, `category`, `suggest`) är permanent dolt (`display: none !important`).
   - `cases.html:27` & `onboarding.html:40` — primära knappar saknar fallback-text och renderas helt tomma.
   - `onboarding.html:89-121` — `<template x-for>` renderar 0 rader; Secrets-checklista och Health checks är helt tomma, samtidigt som `x-show`-par (`connected`/`not connected`, `OK`/`Issues`) visas **båda samtidigt**.
   - `base.html:217-230` — sidomenyns `Gmail · anslut` är permanent dold via `x-cloak`, medan `Gmail · connected` renderas som en tom klickbar länk utan text.
4. **Kritiska fält dolda i osynliga `data-*`-attribut eller helt bortlämnade ur UI:**
   - `wp_review.html:5,25`: `check_run.published`, `RestoreNotInMvp`, `mock`-label och `executed` finns **bara** som `data-*`-attribut.
   - `wp_review.html`: `plan.comments` (inlämnade synpunkter!), kontrollernas `summary`, `reason`, `findings`, `exit_code` (PHP-lintfel!), `plan.sandbox` (`state`, `engine`, `network`), `expires_at`, `extended` visas **aldrig**.
   - `oscar.html:73-79`: Circuit breaker-tabellen visar bara `route:*` och döljer helt `slot:<route>:<model>`-breakers (så "Nollställ circuit breaker"-knappen kan synas medan varje rad falskt säger `closed`).
   - `marketing.html:78-80`: `MarketingSuggest.evidence` och `payload` döljs helt — operatören ombeds godkänna en mutation i blindo.
   - `data_escalations.html:20`: Döljer `details` (vilka nycklar eller vilket utkast som eskalerats), medan `oscar_escalations.html:23` döljer `ID` och `Reason`.
   - `case_detail.html:66-68`: `order_panel.tracking_link` finns i datat men renderas aldrig; `case.language`/`market`/`site`/`classify_method` saknas.
   - `oscar_secrets.html:55`: **17 av 44 `EDITABLE_SECRET_KEYS`** (`WP_*`, `SMTP_*`, `IMAP_*`, `WOO_WEBHOOK_SECRET`, `GOOGLE_*`, `GA4_*`, `AZOM_GA4_*`, `AZOM_GADS_*`) finns i backend men renderas inte alls eftersom loopen går över `secrets_status()`.
5. **Tysta trunkeringar utan "+N fler"-indikator eller tooltip:**
   - `cases_routes.py:36`: hårdkodad `limit=100` utan paginering (avviker från sidomenyns totalsiffra).
   - `core_routes.py:293,313,322`: 200-raders tak på telemetry/audit/eskaleringar utan varning.
   - `support.py:69`: `_ops_counts()` skannar bara de senaste 500 raderna av `escalations.jsonl`.
   - `index.html:91`: `last_probe.results[:8]` klipper tyst av återstående probes.
   - `oscar.html:387`: `open_escalation_tickets[:8]` klipper tyst av utan räknare.
   - `order_context.py:69,86`: `line_items[:5]` och `customer_note[:200]` klipper tyst av.
   - `data_escalations.html:26`: `id[:8]` utan `title` eller kopieringsknapp.
   - `cases.html:122-123`, `case_detail.html:24,146`: Kritiska `★`- och skuggbeslutsförklaringar ligger enbart i hover-`title` (osynliga på mobil och tangentbord).
   - `base.html:284` + `oscar.html:173,254` + `oscar_behavior.html:22`: Sticky header (`h-16`) täcker ankarlänkar (`#prompt-*`, `#model-profiles`, `#xai-login`) eftersom `scroll-mt-20` saknas.

### 3.3 Text/hemligheter som är OAVSIKTLIGT SYNLIGA (säkerhetsrisk — åtgärda)
1. **Telegram bot-token i `secret_probes.py:122-131`:** URL `https://api.telegram.org/bot{token}/getMe` kan inkluderas i `str(exc)[:200]` vid nätverks-/HTTP-fel, vilket visas i `oscar_secrets.html:33` (o-redigerat, till skillnad från `oscar_routes.py:397-398`), sparas i `probe_last.json` och loggas i `probe_history.jsonl`. Samma o-skrubbade `str(exc)[:200]` finns i alla 11 probes (`secret_probes.py:62,93,131,196,218,240,289,320,348,383,402`).
2. **Råa JSON-endpoints `/telemetry` och `/logs` (`core_routes.py:379-393`):** Returnerar `_tail_jsonl("telemetry.jsonl")` **utan** `_telemetry_rows()`-filtreringen (`_SECRET_META`, `core_routes.py:18,64-69`) och är öppna även för `jonatan`.
3. **SSH-privatnyckel visas i klartext vid inklistring (`wp_ssh.html:105`):** Skärmdelning/axeltittningsrisk i `<textarea name="ssh_private_key">` utan maskerings-/blur-toggle.
4. **Råa undantag till `viewer` (`status.py:58,87,104,112`)** och **användarnamnsläcka på 401 (`auth.py:178`):** `"Authentication required (jonatan or oscar)"` exponerar giltiga inloggningsnamn.

---

## 4. Fullständig inventering och validering av alla textrutor, `<input>`, `<textarea>` och `<select>`

Sammanlagt granskades **131 template-kontroller** fördelade över samtliga 23 templates (8 templates har 0 formulärkontroller: `index.html`, `onboarding.html`, `data_audit.html`, `data_escalations.html`, `wp_sites.html`; `base.html` har inga `<input>`).

### 4.1 Core, Settings, Secrets, Interact & Data (`core_routes.py`)

| Fil:Rad | Tag / `type` | `name` / `id` | Synlig/Maskerad | `<label>` / ARIA | Storlek & Validering | Fynd & Rekommendation |
|---------|--------------|---------------|-----------------|------------------|----------------------|-----------------------|
| `settings.html:14` | `input[hidden]` | `_csrf` / — | Dold | N/A | Server CSRF | OK. |
| `settings.html:18` | `input[text]` | `customer` / `customer` | Synlig | Explicit `for` | `.az-input` (`max-w-md`), `required`; server `<=64` + `.lower()` | Lägg till `maxlength="64"`, `placeholder`, `autocomplete="organization"`. |
| `settings.html:22` | `input[text]` | `domains` / `domains` | Synlig | Explicit `for` | `.az-input` (`max-w-md`), `required`; server split `,`/`;` | Lägg till `placeholder="se, no, dk"`, mönstervalidering och bredare fält. |
| `settings.html:26` | `input[number]` | `budget_cap_llm` / `budget_cap_llm` | Synlig | Explicit `for` | `step="0.1"`, saknar `min`/`required` | **P0-bugg (`settings_store.py:183-188,241-245`):** Skuggas och skrivs tyst över av `openrouter_cap` vid varje POST! Ta bort dubbletten eller lagra separat; avvisa `<=0`/`NaN`/`inf`. |
| `settings.html:30` | `input[number]` | `openrouter_cap` / `openrouter_cap` | Synlig | Explicit `for` | `step="0.1"`, saknar `min`/`max`/`required` | Tom sträng ger rå Python `ValueError` oescapad i `?err=`. Lägg till `min="0" max="10000" required` + servervalidering + `_flash_q()`. |
| `settings.html:34-38` | `select` | `mail_provider` / `mail_provider` | Synlig | Explicit `for` | `.az-select` (`appearance-none` utan pil!) | **P1-UI:** Ser ut som textruta. Lägg till SVG-pil och mänskliga etiketter för `exchange_graph`/`generic_imap`. |
| `settings.html:42` | `input[text]` | *(ingen)* / `jonatan_role` | Synlig (`disabled opacity-60`) | Explicit `for` | Disabled | Visar `read_only` i stället för `viewer` (`base.html:308`). Gör till vanlig läsbar faktarad. |
| `settings.html:60-66` | `input[checkbox]` | `mock_mode` / — | Synlig (endast Oscar) | Implicit wrap | Ingen `accent-*` | **P1-fälla (`settings_store.py:277-283`):** I live-läge (`AZOM_USE_MOCK=0`) ignoreras rutan tyst men flashar ändå `"Sparat"`. Inaktivera rutan i live-läge med tydlig infotext. |
| `settings.html:89-95` (x11) | `input[checkbox]` | `email_enabled`..`full_agent_tools` / — | Synlig (båda roller) | Implicit wrap | Ingen `id`, ingen `accent-*` | Gruppera per område och lägg till svenska hjälptexter för kraftfulla flaggor (`full_agent_tools`, `selenium`). |
| `secrets.html:26` (loop) | `input[checkbox]` | `key` / — | Synlig (endast Jonatan) | **SAKNAS** | `form="req"`, ingen `aria-label` | **WCAG-fel:** Lägg till `id` + `<label for>` eller `aria-label="Begär {{ s.name }}"`. |
| `secrets.html:37` | `input[hidden]` | `_csrf` / — | Dold | N/A | Server CSRF | OK. |
| `secrets.html:40` | `textarea` | `note` / `note` | Synlig (endast Jonatan) | Explicit `for` | `.az-textarea`, saknar `maxlength`; server klipper `[:500]` | Lägg till `maxlength="500"`, teckenräknare och bevara texten om 0 nycklar valts. |
| `interact.html:14-15` | `input[hidden]` x2 | `_csrf`, `action` | Dold | N/A | Server CSRF | OK. |
| `interact.html:18` | `textarea` | `message` / `message` | Synlig | Explicit `for` | `.az-textarea`, `required`, **ingen maxlängd på server** | **P1-UX:** Inmatat meddelande raderas vid submit! Eka tillbaka `message` och lägg till `maxlength="4000"` klient+server. |
| `interact.html:36-39` | `input[hidden]` x4 | `_csrf`, `action`, `category`, `draft` | Dold | N/A | Server klipper `draft[:2000]` | **P1-datatapp:** Saknar ursprungligt `message` vid eskalering till Oscar. |
| `data_telemetry.html:15` | `input` (implicit `text`) | `action` / `action` | Synlig | Explicit `for` | `!max-w-none`, server `[:80]` | Sätt `type="text" maxlength="80" placeholder="t.ex. case_reply"`. |
| `data_telemetry.html:19` | `input` (implicit `text`) | `route` / `route` | Synlig | Explicit `for` | `!max-w-none`, server `[:80]` | Sätt `type="text" maxlength="80" placeholder="t.ex. chatgpt_oauth"`. |
| `data_telemetry.html:22` | `input[checkbox]` | `errors` / — | Synlig | Implicit wrap | Helt ostylad + sned `items-end`-justering | Sätt `h-4 w-4 accent-steel-500` och centrerad vertikal justering. |

### 4.2 Ärenden och Marknadsföring (`cases_routes.py`, `marketing_routes.py`)

| Fil:Rad | Tag / `type` | `name` / `id` | Synlig/Maskerad | `<label>` / ARIA | Storlek & Validering | Fynd & Rekommendation |
|---------|--------------|---------------|-----------------|------------------|----------------------|-----------------------|
| `cases.html:26,97,150` | `input[hidden]` x3 | `_csrf` | Dold | N/A | Server CSRF | OK. |
| `cases.html:37-39` | `input[hidden]` x3 | `status`, `category`, `suggest` | Dold | N/A | Filterbevarande | Validera `status` mot tillåten enum på servern. |
| `cases.html:42` | `select` | `mailbox` / `mailbox-quick` | Synlig | Explicit `for` | `.az-select !max-w-[14rem]`, `onchange="submit()"` | Saknar dropdown-pil; `onchange`-submit är fientligt för tangentbordsanvändare. |
| `cases.html:63,74` | `select` x2 | `status`, `mailbox` | **DOLD (trasig `x-cloak`)** | Explicit `for` | `.az-select !max-w-[12/14rem]` | Permanent dolda under CSP; dubbletter med övre fältet; råa engelska statusvärden. |
| `cases.html:83` | `input` (implicit `text`) | `category` / `category` | **DOLD (trasig `x-cloak`)** | Explicit `for` | `!max-w-[12rem]`, placeholder `"return"` | Gör till `<select>` med kända kategorier (skiftlägeskänslig exakt matchning ger tom kö vid stavfel) och URL-encoda i alla `href`. |
| `cases.html:86` | `input[checkbox]` | `suggest` / `suggest` | **DOLD (trasig `x-cloak`)** | Explicit `for` | Standard-checkbox | Permanent dold under CSP; dubblett med filterknapp på rad 32. |
| `cases.html:98` | `input[hidden]` | `case_ids` / `bulkCaseIds` | Dold | N/A | Fylls via JS | Onödig JS-brygga — läs `getlist("case_id_list")` direkt på servern. |
| `cases.html:107` | `input[checkbox]` | *(ingen)* / `bulkSelectAll` | Synlig | **SAKNAS** | Inline `onchange` | Saknar `aria-label="Välj alla ärenden"`, saknar `indeterminate`-läge, visas även på stängda vyer. |
| `cases.html:115` | `input[checkbox]` | `case_id_list` / — | Synlig (per rad) | **SAKNAS** | Ignoreras av servern! | Saknar `aria-label="Välj ärende {{ c.subject }}"`, litet touch-mål. |
| `case_detail.html:123-127` | `input[hidden]` x5 | `_csrf`, `q_status`, `q_mailbox`, `q_category`, `q_suggest` | Dold | N/A | Filter + CSRF | OK. |
| `case_detail.html:128` | `textarea` | `body` / — | Synlig (`open`/`escalated`) | **SAKNAS** | `.az-textarea` (`h=112px` uppmätt!), `required`, ingen `rows` | **P0/P1:** Lägg till `<label for="draft-body">`, `id`, `rows="10"` / `min-h-[16rem]`, `placeholder`, teckenräknare. **Fixa `cases_routes.py:113`** (`body or None` skickar gammalt sparat utkast om fältet töms!). Ta bort död `data-approve-guard`. |
| `marketing.html:83,88,104` | `input[hidden]` x3 | `_csrf` | Dold | N/A | Server CSRF (dubbelkontrollerad i `marketing_routes.py`) | Inga synliga filter (`days`, `status`) finns på `/marketing`. |

### 4.3 Oscar-konsolen (`oscar_routes.py`)

| Fil:Rad | Tag / `type` | `name` / `id` | Synlig/Maskerad | `<label>` / ARIA | Storlek & Validering | Fynd & Rekommendation |
|---------|--------------|---------------|-----------------|------------------|----------------------|-----------------------|
| `oscar.html:86-87,98,103,170,174,258-259,284-285,291-292,334,354-356` | `input[hidden]` (loopade -> >150 DOM-noder) | `_csrf`, `route_id`, `profile_id`, `slot_id` | Dold | N/A | Server CSRF + allowlist | OK funktionellt, men visar hur fragmenterad sidan är (39 separata testformulär). |
| `oscar.html:107,111,115` | `input` (implicit `text`) x3 | `OPENAI_MODEL`, `XAI_MODEL`, `OPENROUTER_MODEL` | Synlig | Explicit `for` | `!max-w-none`, server `<=128` | Sätt `type="text" font-mono maxlength="128" spellcheck="false"`, koppla `list="rec-..."`, förklara att tomt fält behåller nuvarande värde (och ge sätt att rensa). |
| `oscar.html:119-125` | `input[checkbox]` | `AZOM_XAI_GROK_BUILD` / — | Synlig | Implicit wrap (`text-mist-200` odefinierad!) | Boolean | Byt till `text-mist-100` + `accent-oscar-500` + `id`. |
| `oscar.html:133` | `input` (implicit `text`) | `model_q` / `model_q` | Synlig | Explicit `for` | `.az-input` (`max-w-md`), placeholder `deepseek flash` | Använd `type="search" maxlength="80"` och ankare så inte scrollen hoppar upp till toppen. |
| `oscar.html:137-140` | `select` | `catalog` / `catalog` | Synlig | Explicit `for` | `.az-input` (ej `.az-select`) | Konsekvent `.az-select` med chevron. |
| `oscar.html:296-300` (x7) | `select` | `prompt_ref` / **SAKNAS** | Synlig | `<label>` **utan `for`** | `.az-input !max-w-none` | Lägg till unikt `id="prof-{{ pid }}-prompt_ref"` + `for`, använd `.az-select`. |
| `oscar.html:304` (x7) | `input[number]` | `max_output_tokens` / **SAKNAS** | Synlig | `<label>` **utan `for`** | `min="32" max="8192"`, saknar `required` | Tom sträng ger `ValueError` på servern (`profiles.py:551`). Lägg till `id`/`for` + `required step="1"`. |
| `oscar.html:308` (x7) | `input[number]` | `temperature` / **SAKNAS** | Synlig | `<label>` **utan `for`** | **`step="0.05"` `min="0" max="1.5"`** | **P0-bugg:** `wp_code_deep` har default `temperature: 0.12` (`config/model_profiles.yaml:463`), som **misslyckas på HTML5 `stepMismatch`** vid spara! Byt till `step="0.01"` + `required` + `id`/`for`. |
| `oscar.html:312-316` (x7) | `select` | `reasoning_effort` / **SAKNAS** | Synlig | `<label>` **utan `for`** | `.az-input !max-w-none` | **Paritetsfel:** Saknar alternativet `'xhigh'` som stöds av backend (`profiles.py:16`). Lägg till `id`/`for`. |
| `oscar.html:320,324` (x7) | `input[number]` x2 | `idle_s`, `hard_s` / **SAKNAS** | Synlig | `<label>` **utan `for`** | `min`/`max`, saknar `required` | Lägg till `id`/`for`, `required`, och kontroll att `hard_s >= idle_s`. |
| `oscar.html:336-340` (x39) | `select` | `route_id` / **SAKNAS** | Synlig | **SAKNAS** | `.az-input !max-w-none` | Lägg till `aria-label`; vid byte av `route_id` uppdateras INTE syskonets `list="rec-..."`-datalist! |
| `oscar.html:342` (x39) | `input` (implicit `text`) | `model_id` / **SAKNAS** | Synlig | **SAKNAS** | `!max-w-none`, saknar `min-w-[14rem]` | Sätt `type="text" font-mono maxlength="120" placeholder="inherit_env" aria-label="..."` + dynamisk `datalist`. |
| `oscar.html:343` (x39) | `input[checkbox]` | `enabled_{{ sidx }}` / **SAKNAS** | Synlig | **SAKNAS** | Helt ostylad | Lägg till `h-4 w-4 accent-oscar-500` + `aria-label="Aktiv för {{ slot.slot_id }}"`. |
| `oscar_behavior.html:17` | `input[hidden]` | `_csrf` | Dold | N/A | Server CSRF | OK. |
| `oscar_behavior.html:22` (x5) | `textarea` | `chat`, `classify`, `draft`, `product_desc`, `wp_coder` | Synlig | Explicit `for="prompt-{{ name }}"` | `.az-textarea` (`min-h-[7rem]`), `required maxlength="8000"` | För låg (`112px` för 1500+ tecken), saknar `font-mono`, saknar teckenräknare; **vid valideringsfel raderas osparade ändringar** (`oscar_routes.py:374-377`). |
| `oscar_telegram.html:40` | `input[hidden]` | `_csrf` | Dold | N/A | Server CSRF | OK (inga synliga fält avsiktligt). |
| `oscar_secrets.html:15-16,36-37,53` | `input[hidden]` | `_csrf`, `probe` | Dold | N/A | Server CSRF | OK. |
| `oscar_secrets.html:62-68` (x25) | `input[password]` | `{{ s.name }}` / **SAKNAS** | **MASKERAD för ALLA 25 nycklar** | `<label>` **utan `for`** | `autocomplete="off"`, placeholder `••••••••` / `ange värde` | **P1-paket:** (1) Byt de 11 icke-hemliga fälten till `text`/`url`/`email`/`number`/`select`. (2) Behåll `type="password"` på riktiga hemligheter men lägg till `autocomplete="new-password"`, visa/dölj-knapp, `id`/`for` och tydlig `Konfigurerad`-badge i stället för falskt `••••••••`. (3) Exponera samtliga 44 `EDITABLE_SECRET_KEYS` grupperade per tjänst. |
| `oscar_escalations.html:40-41` | `input[hidden]` x2 | `_csrf`, `ticket_id` | Dold | N/A | Server CSRF | Saknar valfri lösningsnotering och sök/filterfält. |
| *(Saknas i UI)* | *GDPR-formulär* | `email` (export & delete) | — | — | `oscar_routes.py:503-547` finns men har **0 UI-fält** | Om GDPR ska vara nåbart i UI: bygg isolerad `"Farozon / GDPR"` med separat export + tvåstegs bekräftad radering (skriv in e-post igen). |

### 4.4 WordPress Ops & Kodgranskning (`wp_routes.py`)

| Fil:Rad | Tag / `type` | `name` / `id` | Synlig/Maskerad | `<label>` / ARIA | Storlek & Validering | Fynd & Rekommendation |
|---------|--------------|---------------|-----------------|------------------|----------------------|-----------------------|
| `wp_site.html:31,35,101` | `input[hidden]` x3 | `_csrf` | Dold | N/A | Server CSRF | OK. |
| `wp_site.html:104` | `select` | `backup_id` / — | **OSYNLIG TEXT (`#e8eef6` på `#efefef`)** | Implicit wrap | `class="mt-1"` (**saknar `.az-select`**), saknar `required` | **P0:** Lägg till `class="az-select mt-1"`, `id`/`for`, `required`. Dölj/inaktivera drill-formuläret när 0 `verified` backups finns! |
| `wp_site.html:114` | `select` | `target_site_id` / — | **OSYNLIG TEXT (`#e8eef6` på `#efefef`)** | Implicit wrap | `class="mt-1"` (**saknar `.az-select`**), saknar `required` | **P0:** Lägg till `class="az-select mt-1"`, `id`/`for`, `required`. |
| `wp_ssh.html:81` | `input[hidden]` | `_csrf` | Dold | N/A | Server CSRF | OK. |
| `wp_ssh.html:84,88,92` | `input` (implicit `text`) x3 | `files_root`, `ssh_host`, `ssh_user` | Synlig | Explicit `for` | `!max-w-none`, `autocomplete="off"` | Lägg till `type="text" font-mono spellcheck="false"`, `placeholder`, samt `pattern` (`-wp-reader`) på `ssh_user`. |
| `wp_ssh.html:96` | `input` (implicit `text`) | `ssh_port` / `ssh_port` | Synlig | Explicit `for` | `!max-w-none`, `inputmode="numeric"` | Byt till `type="number" min="1" max="65535" font-mono max-w-[10rem]`. |
| `wp_ssh.html:100` | `input` (implicit `text`) | `host_public_key` / `host_public_key` | Synlig (klipps horisontellt) | Explicit `for` | `!max-w-none`, `spellcheck="false"`, saknar `maxlength="2000"` | RSA/ECDSA-nycklar är 100–800+ tecken -> använd `az-textarea font-mono text-xs rows="2" maxlength="2000"` med enkelradsvalidering. |
| `wp_ssh.html:105` | `textarea` | `ssh_private_key` / `ssh_private_key` | **Klartext vid inklistring** (write-only på server) | Explicit `for` | `.az-textarea`, saknar `font-mono`, `rows`, `maxlength="16384"` | Lägg till `font-mono text-xs rows="6" maxlength="16384"` samt valfri maskerings-/blur-toggle vid axeltittningsrisk. |
| `wp_ssh.html:109` | `input[checkbox]` | `clear_ssh_key` / — | Synlig | Implicit wrap | Ostylad; krockar med `ssh_private_key` | Styla med `h-4 w-4 accent-steel-500`, `id`, inaktivera om ingen nyckel är lagrad eller om ny nyckel klistrats in. |
| `wp_files.html:58-61,70-74` | `input[hidden]` x9 | `_csrf`, `kind`, `slug`, `path`, `plan_id` | Dold | N/A | Server validerar `_PLAN_ID` | OK. |
| `wp_files.html:64` | `textarea` | `content` / — | **OSYNLIG TEXT (`#e8eef6` på `#ffffff`)** | Implicit wrap | `class="mt-1 w-full" rows="16"` (**saknar `.az-textarea` och `font-mono`**) | **P0:** Lägg till `class="az-textarea font-mono text-xs mt-1"`, `spellcheck="false"`, `id`/`for`. |
| `wp_files.html:77` | `textarea` | `content` / — | **OSYNLIG TEXT (`#e8eef6` på `#ffffff`)** | Implicit wrap | Samma sak; **dubblett 16-raders editor** | **P0/P1:** Ta bort den duplicerade redigerbara editorn; visa diff-sammanfattning + länk till `wp_file_plan` + dolt/readonly fält så oavsiktlig redigering inte bryter hash-bindning. Dölj helt när `notice == 'applied'`. |
| `wp_file_plan.html:39-44` | `input[hidden]` x6 | `_csrf`, `plan_id`, `before_digest`, `after_digest`, `environment`, `path` | Dold | N/A | Strikt hash- och miljömatchning på servern | OK säkerhetsmässigt; visa tydlig svensk förklaring vid `DigestMismatch`. |
| `wp_review.html:58,66-68,75-77,84,92,99` | `input[hidden]` x10 | `_csrf`, `base_sha256`, `work_sha256` | Dold | N/A | Strikt `_gate()` på servern | OK säkerhetsmässigt. |
| `wp_review.html:59` | `textarea` | `text` / — | **OSYNLIG TEXT (`#e8eef6` på `#ffffff`), pytteliten** | **INGEN `<label>` ALLS** | **0 CSS-klasser**, ingen `rows`, `maxlength="500"`, saknar `required` | **P0:** Lägg till `<label class="az-label" for="review-comment">`, `class="az-textarea"`, `rows="3"`, `required`, teckenräknare — och rendera befintliga `plan.comments`! |

---

## 5. Informationsarkitektur, navigation och global shell (`base.html`, `auth.py`)

### 5.1 Kritiska brister i nuvarande navigation och shell

1. **Dubbletter i sidomenyn för Oscar (`base.html:198-274`):**
   - `Drift -> Secrets` (`/secrets`) **och** `Oscar -> Oscar secrets` (`/oscar/secrets`) visas samtidigt, trots att `/secrets` bara är en återvändsgränd för Oscar.
   - `Support -> Eskaleringar` (`/data/escalations`) **och** `Oscar -> Oscar eskaleringar` (`/oscar/escalations`) visas samtidigt med **exakt samma `open_escalations`-badge dubblerad**.
   - `Översikt > Översikt` och `Oscar > Oscar` upprepar gruppnamnet ordagrant.
2. **Felplacerade och inkonsekventa länkar:**
   - `/marketing` ligger under **`Support`** (`base.html:250`) trots att det är Google Ads + GA4.
   - `Gmail · anslut` (`base.html:212-231`) är en omedelbar GET-OAuth-redirect förklädd till en menylänk, skjuter en extra `fetch('/oauth/gmail/status')` på **varje** sidladdning (trots att `gmail_connected` redan finns i template-kontexten!) och saknar text under CSP.
   - `★ Föreslå godkänn` (`base.html:248`) får aldrig aktiv markering (alltid `Ärenden` som markeras).
3. **Toppmeny (`base.html:284-317`):**
   - Hårdkodar `Oscar · full_admin` / `Jonatan · viewer` (`base.html:305-309`) i stället för `{{ user }} · {{ role }}` (bryter custom användarnamn i `config/profile.yaml`), samtidigt som samma info upprepas i sidomenyns sidfot (`base.html:278`).
   - Fast `h-16` med `flex-wrap` på badges riskerar vertikal krock på `<400px`.
   - Default-subtitel `"Ops control room"` är engelsk och dold på mobil (`hidden sm:block`).
4. **Mobil-drawer (`base.html:172-192`):**
   - Saknar statisk `-translate-x-full` på `<aside>` -> låst öppen över 256 px på mobil när Alpine blockeras.
   - Saknar stängknapp (`x`) inuti sidomenyn, saknar `inert`/`aria-hidden` när stängd, saknar fokusfälla och saknar `Hoppa till innehåll`-länk.
5. **Basic Auth UX & sessionsproblem (`auth.py`):**
   - Ingen utloggnings- eller "Byt användare"-funktion (webbläsaren cachar Basic Auth tills omstart).
   - Alla 400/401/403/429-svar returneras som rå `text/plain` utanför `base.html`.
   - Om `DASHBOARD_SECRET_KEY` saknas härleds `app.secret_key` ur lösenordet (`auth.py:54`) — att byta lösenord i `/oscar/secrets` ogiltigförklarar omedelbart pågående CSRF-session.
   - `AZOM_USE_MOCK` parsas på **3 motstridiga sätt** mellan `app.py:67`/`settings_store.py:182` (default `True`, accepterar `"on"`), `auth.py:30` (default `False`, accepterar `"on"`) och `support.py:21`/`status.py:42`/`secret_probes.py` (default `False`, **ignorerar `"on"`**!).

### 5.2 Rekommenderad ny informationsarkitektur (Målstruktur för sidomenyn)

- **Översikt:** `Start` (`/`), `Onboarding` (`/onboarding`)
- **Support & Ärenden:** `Ärenden` (`/cases?status=open,escalated` med `★`- och kö-badge), `Utkast-sandlåda` (`/interact`), `Eskaleringar` (en enda rollmedveten vy `/data/escalations` där Oscar ser `Markera löst`; `/oscar/escalations` redirectar hit)
- **Tillväxt:** `Marknadsföring (Ads & GA4)` (`/marketing`)
- **Drift & Webbplatser:** `Webbplatser (WP)` (`/wp`), `Inställningar` (`/settings`), `Secrets & Integrationer` (en enda rollmedveten vy — `/secrets` för Jonatan med begäran, `/oscar/secrets` för Oscar med redigering + probes; ta bort Gmail-action från menyn och lägg under `Integrationer`/`Onboarding`), `Telemetri` (`/data/telemetry`), `Granskningslogg` (`/data/audit`)
- **Oscar (endast `is_oscar`):** `Modellrouter` (`/oscar`), `Modellprofiler` (`/oscar/profiles` — lyft ut från nuvarande överfulla `/oscar`), `Beteende & Prompter` (`/oscar/behavior`), `Telegram & Kanaler` (`/oscar/telegram`)

---

## 6. Sid- och avsnittsgranskning (samtliga 23 templates)

Nedan ifrågasätts layout och UX/UI för **varje subsida och dess avsnitt**, med konkret åtgärdsförslag.

### 6.1 `index.html` (`GET /`) — Översikt
- **Nuvarande avsnitt:** 9 kort i en 3-kolumnsgrid: `Telemetry cost`, `Ärenden`, `Support KPI 7d`, `Cases poll readiness`, `Integrationer`, `Runtime`, `Hantera`, `Recent telemetry` (20 rader), `Escalations -> Oscar` (20 rader).
- **Ifrågasättande:** Saknar `.az-page-title`; 7 toppkort ger ojämn sista rad (1 ensamt kort + 2/3 tomyta); `Integrationer` är 3x högre än grannkorten och renderar **två olika `"Telegram"`-rader** (`support.py:216-222` duplicerar `settings_store.py:391`); `Integrationer` har en helt felplacerad `Eskaleringar`-knapp (`index.html:110`); `Hantera`-kortet är 100 % dubblett av sidomenyn; `Recent telemetry` visar helt andra kolumner (`Site`, `Units`) än `/data/telemetry`; undantag i `core_routes.py:167-186` sväljs tyst så kort bara försvinner.
- **Målstruktur:** Lägg till `<h1>Översikt</h1>`; toppremsa med 4 lika höga KPI-kort (`Öppna ärenden`, `Poll-beredskap`, `LLM-kostnad`, `Öppna eskaleringar`); balanserad 2-kolumns mittrad (`Integrationer` som kompakta statuspiller utan dubblett-Telegram + `Runtime & Support KPI 7d`); ta bort `Hantera`-kortet; synka tabellkolumner med `/data/telemetry`.

### 6.2 `onboarding.html` (`GET /onboarding`, `/onboarding/status`)
- **Nuvarande avsnitt:** Header + uppdatera-knapp, `Onboarding wizard` (5 steg), `Secrets checklist`, `Health checks`.
- **Ifrågasättande:** Helt beroende av Alpine.js (`x-for`/`x-init`) som blockeras av CSP -> blanka tabeller, blank knapp och dubbla `connected`/`not connected`-badges! Steg 2 (`växla`) och Steg 4 (`Koppla Gmail`) visas som aktiva åtgärder även för Jonatan (`viewer`). `Secrets checklist` duplicerar hela `/secrets`. Inga stegmarkeringar (`X/5 klart`). Fetch-fel loggas bara till `console.error`.
- **Målstruktur:** Server-rendera initiala rader med Jinja (progressive enhancement för uppdatera-knappen), visa tydlig checklista med `X/5 klart`, rollanpassa steg 2–4 för Jonatan, ersätt full secrets-tabell med sammanfattning + länk till Secrets, och visa inline-felruta om statushämtning misslyckas.

### 6.3 `settings.html` (`GET/POST /settings`)
- **Nuvarande avsnitt:** Ett stort kort `Icke-hemliga inställningar` med 6 fält + kollapsbar `Flaggor` (Oscar `mock_mode` + 11 kryssrutor).
- **Ifrågasättande:** `budget_cap_llm` ignoreras tyst vid spara; `mock_mode` i live-läge flashar `"Sparat"` trots ingen ändring; `.az-select` saknar pil; 11 vitt skilda flaggor (SMTP/IMAP/POP3, Woo/WP, Selenium, Full agent tools) dumpas i en platt grid utan förklaringar; `jonatan_role` visar `read_only` i stället för `viewer`.
- **Målstruktur:** Dela upp i 3 tydliga delavsnitt (`Kund & LLM-budget`, `Mailleverantör`, `Integrationer & agentflaggor`), ta bort/laga `budget_cap_llm`, lägg till enrads förklaringar på varje flagga, inaktivera `mock_mode` med infobox i live-läge.

### 6.4 `secrets.html` (`GET/POST /secrets`)
- **Nuvarande avsnitt:** Ett kort med platt 30+ raders tabell + `#note`-begäran för Jonatan (eller länk till `/oscar/secrets` för Oscar).
- **Ifrågasättande:** Onödig återvändsgränd för Oscar (renderar dessutom tom `<th>`/`<td>`-kolumn); saknar gruppering per tjänst; olablade kryssrutor; valideringsfel raderar `#note`.
- **Målstruktur:** Redirecta Oscar till `/oscar/secrets` (eller slå ihop till en rollmedveten vy); gruppera nycklar per integration; märk obligatoriska vs valfria nycklar för vald `mail_provider`; koppla `<label>` till varje kryssruta och bevara `note` vid fel.

### 6.5 `interact.html` (`GET/POST /interact`)
- **Nuvarande avsnitt:** Ett kort `Support-draft` med `#message`, valfri felruta och `Resultat` + `Eskalera till Oscar`.
- **Ifrågasättande:** Raderar inskickad kundtext efter submit; visar råa `OK: True · Eskalerad: False`; saknar `"Kopiera utkast"`-knapp; saknar laddningsindikator under LLM-anrop; eskalering tappar bort kundens ursprungliga meddelande; säger `"Jonatan kan inte skicka mail"` även när Oscar är inloggad och låter Oscar eskalera till sig själv.
- **Målstruktur:** Behåll inmatat `message` (och skicka med det vid eskalering), 2-kolumns layout på desktop (inmatning vänster, utkast höger), svenska statusbadges, `"Kopiera utkast"`, tydlig sandlåde-infobox och laddningstillstånd.

### 6.6 `data_telemetry.html` (`GET /data/telemetry`)
- **Nuvarande avsnitt:** Filterkort (`action`, `route`, `Bara fel`) + tabellkort `Rader (N)` (8 kolumner).
- **Ifrågasättande:** Ingen summering (totalkostnad USD, antal fel, tokens); ingen `"Rensa filter"`-knapp; ingen upplysning om 200-raders taket; `Fel`-kolumnen saknar röd badge; `safe_meta` renderar rå Python `dict`-repr även när `{}` är tomt; blandade svenska/engelska kolumnrubriker.
- **Målstruktur:** Lägg till sammanfattningsrad (rader av max 200, felantal, total USD), `"Rensa filter"`, röd badge för `error_class`, `tojson(indent=2)` endast när `safe_meta` är icke-tom, och konsekventa svenska kolumnrubriker.

### 6.7 `data_audit.html` (`GET /data/audit`)
- **Nuvarande avsnitt:** Ett tabellkort (6 kolumner, ingen kortrubrik, inga filter).
- **Ifrågasättande:** Helt utan filtrering (`Aktör`, `Action`, `Misslyckade`); dumpar rå Python `dict` direkt i cellen utan `<details>` eller `break-words`; felöversättning `"Details är redigerade"` (ska vara `"maskerade"`); saknar 200-raders notis.
- **Målstruktur:** Matcha `data_telemetry.html` med filterrad, kortrubrik `Rader (N / max 200)`, hopfällbar formaterad JSON i `Detaljer` och korrekt svensk text.

### 6.8 `data_escalations.html` (`GET /data/escalations`) & `oscar_escalations.html` (`GET/POST /oscar/escalations`)
- **Nuvarande avsnitt:** Två separata sidor som visar **olika delmängder** av samma `escalations.jsonl`: `/data/escalations` visar `ID` + `Reason` men saknar `details` och statusfilter; `/oscar/escalations` har filter och `details` (rå Python dict) + `Markera löst` men saknar `ID` och `Reason`!
- **Ifrågasättande:** Dubbel navigation med dubbel badge; **aktiv dubbel-`?` redirect-bugg** i `oscar_routes.py:488-493` kraschar flash och förstör `show_filter`; `Status` och `Severity` är ostylad text; `"Visa lösta"` visar egentligen *alla*; `Markera löst` saknar bekräftelse, `resolved_by`/`resolved_at` och `log_action()`-audit.
- **Målstruktur:** Slå ihop till en gemensam rollmedveten vy med flikarna `Öppna` / `Lösta` / `Alla`, fulla kolumner (`Tid`, `ID`, `Status`-badge, `Allvarlighetsgrad`-badge, `Orsak`, `Sammanfattning` + snyggt formaterade detaljer) och bekräftad `Markera löst`-knapp + auditloggning för Oscar.

### 6.9 `cases.html` (`GET /cases`, `POST /cases/poll`, `POST /cases/bulk-close`)
- **Nuvarande avsnitt:** Ev. Messenger-banner, sidhuvud, `Kö`-åtgärdsrad (Poll + statusknappar + brevlåde-select), hopfällbart `Filter`-kort, 10-kolumners kötabell med bulk-stängning och tom-kö-kort.
- **Ifrågasättande:** Poll-knapp blank under CSP; filterkort permanent dolt under CSP och duplicerar övre raden; 10 kolumner svämmar över på mobil/surfplatta och ämnesraden är inte klickbar; `cases.html:159-174` kraschar med `TypeError` när kön är tom; `★`-ikon saknas på radens badge; Path B2 (`return`/`billing`) syns inte i listan; bulk-kryssrutor visas även på `closed`/`replied`; poll och bulk-stäng tappar aktiva filter; `category`/`mailbox` URL-encodas inte i länkar; `limit=100` utan paginering.
- **Målstruktur:** Separera primär åtgärd (`Hämta nya mail`) i sidhuvudet från en enda enhetlig filterrad (statusflikar + brevlåda + kategori-`<select>` + `★`-filter); gör ämnesraden klickbar och stapla rader som kort på `< md`; visa tydliga badges för `★ Föreslå godkänn`, `Path B2 (manuell)` och `Eskalerad`; visa bulk-stängning enbart på öppna/eskalerade och med varning om eskalerade ärenden ingår; bevara filter efter poll och bulk-stängning.

### 6.10 `case_detail.html` (`GET/POST /cases/<case_id>`) — Kritisk daglig HITL-arbetsyta
- **Nuvarande avsnitt:** Ev. Messenger-banner, ärendehuvud (12+ fält på en sammanhängande punktseparerad rad), orderpanel, meddelandetråd, utkastkort med 112 px `<textarea>` och 5 åtgärdsknappar.
- **Ifrågasättande:**
  - **Säkerhetskritiskt:** CSP slår ut `@submit`-bekräftelsen -> klick på `Godkänn och skicka` skickar kundmail direkt utan dialog! `cases_routes.py:97` defaultar saknad `action` till `"reply"`; `cases_routes.py:113` skickar gammalt sparat utkast om rutan är tom.
  - Mottagaradress (`case.from_addr`) och utgående ämne (`Re: ...`) syns inte intill skicka-knappen; `Godkänn och skicka` och `Godkänn & nästa` har identisk blå knappstil bredvid varandra; `null-send`-läge avspeglas inte på själva skicka-knappen.
  - Saknar **"Eskalera till Oscar"**-knapp och **"Öppna igen"** på stängda ärenden.
  - Inkommande kundmail och utgående agentsvar ser likadana ut i tråden (utgående visar dessutom `outbound · <tid> · ` med hängande punkt p.g.a. tom `from_addr`).
  - `Regenerera utkast` kastar osparade ändringar tyst och visar ingen 60 s cooldown; diffen använder otydlig `text-xs` monospace.
  - `order_panel.tracking_link` renderas inte; orderfel visas i anonym grå `.az-muted`.
  - `sending`-status lämnar en tom sida utan förklaring; `Tillbaka till kön` på stängda ärenden tappar alla filter; `Nästa i kö` använder POST.
- **Målstruktur:** På desktop (`lg:`), dela 2 kolumner: **vänster** = kompakt metadata-strip + orderpanel (med klickbar spårningslänk) + färgkodad konversationstråd (`Kund` vs `Azom`); **höger (sticky)** = generös `<textarea rows="10">` med `<label>`, utkast-diff med tydlig avgränsning, mottagar-/ämnesförhandsvisning direkt ovanför skicka-knappen, distinkt primär vs sekundär knapphierarki, CSP-oberoende bekräftelsedialog + laddningslås, explicit `Null-send`-anpassning och ny `Eskalera till Oscar`-knapp.

### 6.11 `marketing.html` (`GET /marketing`, `POST /marketing/suggests/*`)
- **Nuvarande avsnitt:** Introkort, `Digest (7d)` (4 KPI), `Hälsa` (punktlista), `Konsistens Woo <-> GA <-> Ads`, `Öppna förslag`, Oscar OAuth-kort.
- **Ifrågasättande:** Visar varken kill-switch-status (`ads_mutate_allowed`, `mp_allowed`, `merchant_write_allowed`) eller tydlig `mock` vs `live`-banner; `marketing_routes.py:23-25` sväljer fel från `health`/`consistency`/`list_suggests` tyst; `0` ROAS blir `—`; råa Python-booleans (`True`/`False`) och ofärgade `Band:`-strängar; `landing_flags` och `MER` visas inte; `Bygg waste/pacing-förslag` **försvinner helt** så fort >= 1 förslag finns; förslag döljer `evidence` och `payload` inför godkännande; Jonatan får ingen förklaring när godkänn-knappen döljs; OAuth-kortet visar inte anslutningsstatus.
- **Målstruktur:** Lägg en tydlig statusremsa överst (`Läge: Mock/Live`, `Ads-mutationer: Spärrade (läsläge)`, `MP: Spärrad`, `Google OAuth: Ansluten/Ej ansluten`); lägg till periodväljare (`7d`/`14d`/`30d`) och `MER` i Digest; ersätt råa booleans/band med semantiska badges; gör `"Bygg förslag"` alltid tillgänglig i förslagsrubriken; lägg till `<details>` på varje förslag med `evidence` + exakt `payload`, samt tydlig roll-/spärrförklaring på knappar.

### 6.12 `oscar.html` (`GET /oscar`, `/oscar/models/*`)
- **Nuvarande avsnitt:** 4 KPI-kort, enormt monolitiskt `Modellrouter`-kort (status-tabell + breaker + globala env-modeller + katalogsök + ChatGPT OAuth + xAI device login + 7-raders friskrivningsvägg), alla **7 modellprofiler fullt expanderade (39 slots + 39 testknappar)**, snabblänkar, senaste 8 eskaleringar.
- **Ifrågasättande:** Extrem vertikal densitet (>250 DOM-kontroller på en sida); rå Unix-epoch i `Expires`; dolda `slot:*`-breakers; duplicerade `Anslut ChatGPT` / `Återanslut`-knappar; katalogsök laddar om hela sidan och tappar osparade ändringar; `xai-login-link` saknar `target="_blank"` så klick **avbryter polling-skriptet**; redigering av slot + direktklick på `Testa` testar gammal sparad konfiguration; `wp_code_deep` faller på `step="0.05"`; `'xhigh'` saknas i `reasoning_effort`; destruktiva knappar saknar bekräftelse; slottest-fel scrollar ner och gömmer `Senaste test`-rutan; `Senaste test` visar inte modell/leverantör/tidpunkt och visar råa Python-klassnamn; GDPR-endpoints saknar helt UI.
- **Målstruktur:** Lyft ut `Modellprofiler & Monterade modeller` till egen flik/undersida (`/oscar/profiles`) eller hopfällbara `<details>`-kort; dela `Modellrouter` i separata kort (`Rutter & Hälsa`, `OAuth & Inloggning`, `Globala standardmodeller & Katalog`); formatera `Expires` läsbart; visa `slot:*`-breakers och `recommended_model`/`tier`; lägg `target="_blank" rel="noopener noreferrer"` på xAI-länken + stor `font-mono`-kod med kopieringsknapp; visa inline-testresultat vid respektive rad/profil med svensk felklass (`auth`/`config`/`rate_limit`/`unavailable`); lägg till bekräftelsedialoger och en isolerad GDPR-sektion.

### 6.13 `oscar_behavior.html` (`GET/POST /oscar/behavior`)
- **Nuvarande avsnitt:** Ett stort formulär med alla 5 prompter (`chat`, `classify`, `draft`, `product_desc`, `wp_coder`).
- **Ifrågasättande:** Introtexten glömmer `wp_coder`; trånga `112px`-textrutor i proportionellt typsnitt utan teckenräknare; valideringsfel raderar alla osparade ändringar; inga individuella spara-knappar, ingen diff mot inbyggda standardvärden och ingen återställ-knapp; ankarlänkar täcks av sticky header (`scroll-mt-20` saknas).
- **Målstruktur:** Eget kort (med `scroll-mt-20`) per prompt med version, `font-mono`, `rows="10"`, live teckenräknare (`X / 8000`), `"Visa standard / diff"` och bevarad inmatning vid valideringsfel.

### 6.14 `oscar_telegram.html` (`GET /oscar/telegram`, `POST /oscar/telegram/probe`)
- **Nuvarande avsnitt:** Statuskort (`Token`, `Tillåtna chattar`, `Mappade aktörer`, senaste probe) + `Kör Telegram-probe`-knapp.
- **Ifrågasättande:** Ogiltig `<dl>`-HTML (saknar `<dt>`/`<dd>`); mycket sparsam driftinsyn (saknar bot-användarnamn, jobbkö-status och icke-hemlig översikt av mappade aktörer / fail-closed-varning när `allow_count == 0` i live); ofärgad probestatus med rå ISO-tid; felöversättning `"inte i den här formen"`; grön flash även när proben misslyckas!
- **Målstruktur:** Korrekt `<dl>`, semantiska badges (`OK`/`VARNING`/`FEL`), tydlig fail-closed-varningsruta om allowlist/actor-map är tom i live-läge, läsbar tidstämpel, och ärlig flash-klass baserad på `single.status`.

### 6.15 `oscar_secrets.html` (`GET/POST /oscar/secrets`, `POST /oscar/secrets/test`)
- **Nuvarande avsnitt:** `Testa anslutningar`-tabell + platt 2-kolumnsformulär med 25 `type="password"`-fält.
- **Ifrågasättande:** Kall GET kör alla 11 nätverksprobes **två gånger** synkront (`oscar_routes.py:434-439`); enskild `"Testa"` raderar övriga 10 rader och saknar PRG; `warn` renderas som texten `"error"` medan `error` stylas gul; grön flash för `"0 ok, 11 problem"`; 11 icke-hemliga fält maskeras som lösenord; 17 av 44 redigerbara nycklar saknas helt; `autocomplete="off"` + saknade `id`/`for`; oredigerad Telegram-token i feltext.
- **Målstruktur:** Ta bort dubbel probe-körning, kör PRG efter probe-test och behåll hela tabellen, separera `ok` (grön) / `warn` (gul) / `missing` (grå) / `error` (röd), gruppera samtliga 44 nycklar per integration med rätt input-typ per fält + `autocomplete="new-password"` + visa/dölj-toggle på verkliga hemligheter.

### 6.16 `wp_sites.html` (`GET /wp`) & `wp_site.html` (`GET /wp/<site_id>`, `/check`, `/backup`, `/restore-drill`)
- **Nuvarande avsnitt:** Inventarietabell på `wp_sites.html`; på `wp_site.html`: sidhuvud + länkar (`Filer`, `SSH`), 2 staplade åtgärdsknappar, `Kontroller`, `Öppna incidenter`, `Mock-backuper` + `Kör staging-drill`-formulär, `Staging-drills`.
- **Ifrågasättande:** `wp_sites.html` använder rå `<table>` i stället för `.az-table` och hårdkodar `okänd` utan förklaring; `fail`/`failed`-badges stylas med samma neutrala grå `bg-ink-700` som `okänd` (omöjligt att se riktiga fel!); **P0-osynlig text** i båda `<select>`-rutorna på `wp_site.html:104,114`; drill-formuläret är aktivt även vid 0 `verified`-backuper; **`wp_site.html:132` hårdkodar `ordrar oförändrade`** även om `item.orders_mutated` är sant; inga roll-informationsrutor för Jonatan (`WP_OPS_READ`); `RestoreNotInMvp` syns inte på `wp_site.html`.
- **Målstruktur:** Använd `.az-table`, skilj tydligt på `fel` (röd), `verifierad, inte off-site` (gul/neutral uppmärksamhet) och `okänd` (grå), lägg `Kör läskontroll` + `Skapa mock-backup` på en snygg åtgärdsrad, fixa `.az-select` + `verified`-vakt + `item.orders_mutated`, lägg till fliknavigering (`Översikt | Filer | SSH`) och tydlig roll-infobox för skrivskyddat läge.

### 6.17 `wp_ssh.html` (`GET/POST /wp/<site_id>/ssh`)
- **Nuvarande avsnitt:** Säkerhetsbeskrivning, `<dl>`-sammanfattning av aktuella inställningar, Oscar-formulär (7 fält).
- **Ifrågasättande:** Kritisk `Mock`/`Live SSH`-status göms i grå löptext; `registry_ssh_host` saknas i sammanfattningen; dubblerad mening om lösenfras (`line 106` och `113`); `host_public_key` klipps i enradig input; `ssh_private_key` visas i klartext vid inklistring utan `font-mono` eller maskering; `clear_ssh_key` är ostylad och varnar inte för krock med ny nyckel; ingen förklaring till Jonatan varför fält och fingeravtryck döljs.
- **Målstruktur:** Toppremsa med tydliga statusbadges (`Mock: PÅ/AV`, `Live SSH: AV/PÅ`, `Nyckel: Lagrad/Saknas`), `font-mono` på alla tekniska fält, flerrads `host_public_key`, maskerings-toggle på `ssh_private_key`, numerisk port, rensa duplicerad text och lägg till brödsmula + rollnotis.

### 6.18 `wp_files.html` (`GET /wp/<site_id>/files`, `/propose`, `/apply`) & `wp_file_plan.html` (`GET /plan/<id>`, `POST /deploy`)
- **Nuvarande avsnitt:** På `wp_files.html`: `Teman`/`Plugin`-bläddrare + 16-raders editor + duplicerad 16-raders `Skriv filen`-editor. På `wp_file_plan.html`: planmetadata + unified diff + `Produktion`-kort.
- **Ifrågasättande:** **P0-osynlig text** i båda 16-raders `<textarea>`; 0 `font-mono`; `.az-section-title` gör filsökvägar `UPPERCASE`; bläddraren tappar bort katalogträdet så fort en fil öppnas och har ostylade länkar utan tom-tillstånd; duplicerad redigerbar 16-raders editor efter förslag (ingen diff, ingen länk till `wp_file_plan`, ligger kvar även efter `notice == 'applied'`); `data-ssh` visas aldrig visuellt; `wp_file_plan.html` saknar helt tillbakalänk, har ogiltig `<dl>` utan `<dt>`/`<dd>`, har 64-teckens hashar utan `break-all`, använder blå primärknapp för alltid-blockerad produktionsdeploy (`BackupNotVerified`) och kastar bort `notice`.
- **Målstruktur:** Lägg till brödsmula (`Webbplatser / <site> / Filer / <kind> / <slug> / <path>`) + tydlig `Mock-träd` vs `Live SFTP (skrivskyddad)`-badge; 1 kod-editor (`az-textarea font-mono text-xs spellcheck="false"`) med vanlig skiftlägesbevarande rubrik; efter förslag visa diff + länk till `wp_file_plan` + enkel bekräftelseknapp (ingen dubblett-textarea); på `wp_file_plan.html`, fixa `<dl>`, `break-all`, tydlig varningsbanner för `BackupNotVerified` / `RestoreNotInMvp` och tillbakalänk.

### 6.19 `wp_review.html` (`GET/POST /wp/<site_id>/reviews/<plan_id>/*`)
- **Nuvarande avsnitt:** 10 vertikalt staplade `.az-card`-block (Sammanfattning, Diff, Synpunkt, Staging mock, Produktion, Tid, Kontroller, Förhandsvisning, Syntetiska skärmdumpar).
- **Ifrågasättande:**
  - **P0:** `<textarea name="text">` saknar alla CSS-klasser (osynlig text, pytteliten, inget `<label>`), och sparade kommentarer (`plan.comments`) **visas aldrig**!
  - **P0/P1:** Alla 6 POST-rutter saknar PRG, kastar bort `result.message`, ger **noll bekräftelse vid lyckad åtgärd** och sätter felaktigt `"Ingen produktionsfil ändrades."` på alla fel (t.o.m. `InvalidComment`, `PreviewBlocked`, `ExtendUsed`).
  - **RBAC-bugg:** `wp_routes.py:649-684` tillåter `operator` att köra `Kontroller` och `Förhandsvisning`, men `wp_review.html:45` gömmer båda bakom `{% if can_review %}` (endast Oscar)!
  - **Dold kritisk evidens:** `check_run.published`, `RestoreNotInMvp`, `mock`, `sandbox`, `expires_at`, `extended`, `row.summary`, `row.reason`, `row.findings` (PHP-fel, `woo_static`-fynd) saknas i synlig HTML; `screenshots` kan renderas samtidigt som rad 34 säger `"Ingen renderad förhandsvisning"`; `<img>` saknar storleks-/ramklasser; saknar helt tillbakalänk; extrema 1-knapps-kort staplade på höjden.
- **Målstruktur:** Lägg till brödsmula till `/wp/<site_id>`; gör överst ett tydligt statuskort med synliga badges (`Status`, `Publicerade kontroller: Ja/Nej`, `Läge: Mock/Sandbox`, `Giltig till`, `Restore: Ej i MVP`); visa komplett kontrolltabell med `status`-badge, `policy`, `summary`/`reason` och expanderbara `findings`/PHP-lintutdata; rendera `plan.comments` med en korrekt stylad kommentarbox; samla åtgärder i en strukturerad 2-kolumns åtgärdspanel med PRG + flash, korrekt RBAC (`can_drill` för Kontroller/Förhandsvisning) och villkorligt inaktiverade knappar med förklaring vid `not_resumable` / opublicerade kontroller / blockerad produktion.

---

## 7. Statusmeddelanden, informationsrutor och återkoppling

### 7.1 Systemiska brister i nuvarande feedback-lager

1. **Query-sträng-flash (`?msg=` / `?err=`, `support.py:44-51`, `base.html:329-355`):**
   - Ligger kvar i URL:en efter omladdning/bokmärke, läcker i accessloggar/historik och kan förfalskas via länk.
   - Stängknappen (`@click="show = false"`) är trasig under CSP.
   - **Aktiv bugg (`oscar_routes.py:488-493`):** dubbel-`?` URL förstör både flash och filter.
   - **Oescapade redirects:** `core_routes.py:251` (`?err={exc}`) och `cases_routes.py:86` (`?err={result['message']}`).
   - **Bara 2 nivåer (`ok:` och `error:`):** Tvingar partiella/misslyckade resultat (`"Test klart: 0 ok, 11 problem"` i `oscar_routes.py:477`, misslyckad Telegram-probe i `oscar_routes.py:421`) in i gröna `ok:`-banners!
   - **ARIA-fel (`base.html:342`):** Fel använder `role="status"` i stället för `role="alert"`, medan den statiska `null-send`-bannern (`base.html:324`) använder `role="status"` och läses upp vid varje sidbyte.
   - **OAuth-fel återvändsgränder (`oauth_routes.py:83-94,136-147`):** Gmail- och Google Marketing-callbacks returnerar rå `text/plain` 400/502 i stället för redirect med flash (som `/oauth/openai/callback` gör korrekt).
2. **Noll laddnings- och dubbelklicksskydd på långsamma anrop:**
   - LLM-utkast (`interact.html`), regenerering & godkänn-och-skicka (`case_detail.html`), poll & bulk-stäng (`cases.html`), modell-/slottester upp till 30 s (`oscar.html`), 11 sekventiella probes (`oscar_secrets.html`), Telegram-probe (`oscar_telegram.html`), WP-kontroller/sandbox (`wp_review.html`, `wp_site.html`), marketing-förslag (`marketing.html`).
3. **Saknade eller bristfälliga tomma tillstånd:** `onboarding.html` (helt tomt under CSP), `secrets.html`, `oscar_secrets.html` (tom probe-tabell), `oscar.html` (0 träffar i katalogsök ger inget besked), `wp_files.html` (tomma `slugs`/`files`), `wp_review.html` (inga körda kontroller / inga kommentarer), `cases.html` (ingen `"Rensa filter"`-knapp vid tom filtrerad kö), `data_telemetry.html` (skiljer inte på tomt loggflöde vs 0 filterträffar).
4. **Saknade förklarande informationsrutor för RBAC, läge och spärrar:**
   - **Jonatan (`viewer`):** Kontroller försvinner tyst på `wp_site.html`, `wp_ssh.html`, `wp_files.html`, `wp_review.html`, `marketing.html` och `settings.html` utan infobox som förklarar begränsningen och vem som äger åtgärden (Oscar). `null-send`-bannern i `base.html:326` visar Oscars CLI-kommando även för Jonatan.
   - **Mock vs Live:** Endast en liten gul `mock`-pill i hörnet — otillräckligt på `marketing.html` (där mock-intäkter ser ut som riktiga SEK), `wp_files.html` (visar inte om trädet är lokalt mock eller live SFTP) och `wp_review.html` (`mock` dolt i `data-label`).
   - **Spärrade produktions-/mutationsytor:** `marketing.html` saknar kill-switch-banner; `wp_file_plan.html` och `wp_review.html` stylar alltid-blockerade produktionsknappar som blå primärknappar och döljer `RestoreNotInMvp`.

### 7.2 Rekommenderat enhetligt status- och infokomponentsmönster

Inför en liten uppsättning återanvändbara Jinja-makron (t.ex. `infrastructure/dashboard/templates/_ui.html`):

1. **`flash_banner(kind, text)`** (via Flask `session` flash `flask.flash(..., category)` i stället för `?msg=`/`?err=`, med 4 nivåer: `ok` grön, `info` blå, `warn` gul, `error` röd med AA-kontrast; `role="alert"` för `error`, `role="status"` för övriga; CSP-säker stängknapp).
2. **`info_callout(tone, title, body)`** — standardiserad informationsruta för:
   - Rollbegränsning (`info`: *"Skrivskyddad vy (viewer) — kräver Oscar för ändringar."*),
   - Driftläge (`warn`: *"Mock-läge aktivt — siffror och åtgärder påverkar inga live-system."*),
   - Spärrad gate (`warn`: *"Produktion är låst (`BackupNotVerified` / `RestoreNotInMvp`)."*).
3. **`status_badge(status, label)`** — central mappning så att `ok`/`verified` (grön, aldrig för mock-backup), `warn`/`attention`/`partial`/`escalated`/`unavailable` (gul), `error`/`fail`/`failed`/`missing` (röd med AA-kontrast) och `neutral`/`not_run`/`closed` (grå) alltid har konsekvent färg, ikon och svensk etikett över alla 23 templates.
4. **`empty_state(title, description, action_label, action_href)`** — enhetlig tom-vy med tydlig `"Rensa filter"`-variant när filter är aktiva.
5. **CSP-säker `data-busy-label`-attributhanterare** (~25 rader i lokal `static/dashboard.js`) som på `submit` förhindrar dubbelklick (via hidden input för klickad submit-knapp så `name="action"` aldrig tappas!) och byter knapptext till `"Skickar..."` / `"Testar..."` / `"Kör..."`, samt hanterar `data-confirm="..."` oberoende av Alpine.js.

---

## 8. Implementeringsplan för övertagande kodagent

Nästa kodagent bör implementera förändringarna i **6 fokuserade, testbara arbetspaket (WP1–WP6)** i nedanstående ordning.

### Hårda säkerhets- och produktregler (får ALDRIG brytas)

- **Aldrig skicka kundmail tyst:** Alla kundsvar måste gå via explicit godkännandeflöde med mänsklig bekräftelse. Koppla aldrig in FU9 auto-send (`AGENTS.md`, `docs/CASES.md`).
- **Behåll `AZOM_USE_MOCK=1` vid lokal utveckling och testning** — inga live-anrop mot WooCommerce, Gmail, Ads/GA4, ChatGPT, xAI, OpenRouter, Telegram eller WordPress SFTP utan Oscars skriftliga godkännande.
- **Inga hemligheter i UI, loggar, telemetri, SQLite eller export** (`SOUL.md`, `AGENTS.md`). Läs/kopiera aldrig Grok Build `auth.json`.
- **Bevara RBAC och fail-closed:** Jonatan får aldrig skrivbehörighet till secrets, Oscar-endpoints, marketing-mutationer eller WP-produktion; WordPress produktionsdeploy (`BackupNotVerified`) och restore (`RestoreNotInMvp`) ska förbli spärrade; ingen bulk-approve för ärenden.
- **Ärliga modell- och kontrollresultat:** En misslyckad modellroute eller opublicerad/unavailable WordPress-kontroll får aldrig döljas bakom en grön status eller OpenRouter-fallback.
- **Försvaga aldrig tester** för att få CI grönt (`ruff` + `pytest` med >= 70 % coverage).

### Arbetspaket (rekommenderad PR-uppdelning)

#### WP1 — P0: CSP, lokal JS/CSS/typsnitt, WCAG-kontrast & trasiga formulärkontroller
- **Målfiler:** `infrastructure/dashboard/app.py`, `infrastructure/dashboard/templates/base.html`, `infrastructure/dashboard/static/` (ny: lokal `dashboard.js` + självvärdade typsnitt/CSS eller uppdaterad CSP), `infrastructure/dashboard/templates/wp_site.html`, `wp_files.html`, `wp_review.html`, `cases.html`, `onboarding.html`.
- **Åtgärder:**
  1. Lös CSP-blockeringen av Alpine.js och Google Fonts (självvärda eller uppdatera CSP + ersätt kritiska HITL-bekräftelser, mobilmeny och flash-stängning med CSP-säker lokal JS; lägg statisk `-translate-x-full` på `<aside>` och stängknapp).
  2. Lägg till fallback-text i alla `x-text`-knappar (`cases.html:27`, `onboarding.html:40`, `base.html:229`) och server-rendera initiala tabeller i `onboarding.html`.
  3. Definiera samtliga 7 saknade Tailwind-färgtokens (`mist-200`, `steel-{100,200,700,900,950}`, `oscar-300`), fixa kontrastfel (`danger`-text, `.az-btn-primary`, `.az-btn-danger`, `mist-500`), lägg till SVG-pil på `.az-select`, ta bort tvingande `max-w-md` ur `.az-input`.
  4. Applicera `.az-select`/`.az-textarea` på de 5 osynliga kontrollerna i `wp_site.html:104,114`, `wp_files.html:64,77` och `wp_review.html:59`.
  5. Åtgärda JS-kraschen vid tom kö i `cases.html:159-174` och lägg till enkel SVG-favicon (`/favicon.ico`).
- **Acceptanskriterier:** 0 CSP-fel och 0 JS-undantag i headless Chrome på alla 23 vyer; laddade `Sora`/`Source Sans 3`; mobilmeny stängd som default på `390x844` och öppnings-/stängningsbar; 0 blanka knappar; 0 light-on-light-kontroller.

#### WP2 — P0: Säkerhets-, validerings- och tillståndsbuggar i backend-rutter
- **Målfiler:** `infrastructure/dashboard/cases_routes.py`, `oscar_routes.py`, `core_routes.py`, `oauth_routes.py`, `settings_store.py`, `secret_probes.py`, `status.py`, `support.py`, `auth.py`.
- **Åtgärder:**
  1. `cases_routes.py:97`: ändra saknad/okänd `action` från `"reply"` till fail-closed fel; `cases_routes.py:113`: kräv icke-tom `body` vid `reply`/`reply_next` (fall inte tillbaka till gammalt utkast); URL-encoda `?err=` på rad 86 via `_flash_q()`.
  2. `secret_probes.py`: skrubba URL:er/tokens (`bot<token>`, auth-parametrar, bearer-tokens) ur alla `str(exc)` innan UI/JSONL; korrigera `"Prod fail-open"` -> `"fail-closed"` (`lines 115,166`); fixa död `SSH_HOST`-kontroll (`223-227`) och `use_mock`-tvång (`49,307`).
  3. `core_routes.py`: sanitera `/telemetry` och `/logs` via `_telemetry_rows()` (eller Oscar-skydda); URL-encoda `?err=` på rad 251; deduplicera `"Telegram"` i `support.py:216`.
  4. `settings_store.py`: avvisa `\r`/`\n` i `save_secrets()` och skriv `secrets.env` atomärt med `0o600` vid skapande; fixa `budget_cap_llm` vs `openrouter_cap`, finit positiv float-validering och ärlig hantering av `mock_mode` i live-läge; harmonisera `AZOM_USE_MOCK`-parsning via en enda helper (`ecom_ops.runtime_env.env_flag`).
  5. `oscar_routes.py`: fixa dubbel-`?`-redirect (`488-493`), ta bort dubbel `run_all_probes()` (`434-439`), PRG på `oscar_secrets_test`, ärlig `error:`/`warn:`-flash vid probe-problem (`421,477`), behåll inmatade prompter vid valideringsfel (`374-377`), fixa `step="0.01"` och `'xhigh'` i `oscar.html:308,313`.
  6. `oauth_routes.py` & `auth.py`: redirecta Gmail/Google-fel till flash-banner; ta bort användarnamnsläcka på 401 (`auth.py:178`) och rendera 403/404-sidor inuti `base.html`.

#### WP3 — P1: Enhetligt status-, flash- och infokomponentssystem (`_ui.html`)
- **Målfiler:** `infrastructure/dashboard/templates/_ui.html` (ny), `base.html`, `support.py` + samtliga 23 templates.
- **Åtgärder:** Migrera från query-sträng-flash (`?msg=`/`?err=`) till Flask `session`-flash med 4 nivåer (`ok`, `info`, `warn`, `error`) och korrekt `role="alert"`/`role="status"`; inför enhetliga `status_badge`, `info_callout` (roll, mock, null-send, spärrad gate) och `empty_state`-makron; lägg till `data-busy-label` och `data-confirm` på alla långsamma/destruktiva formulär.

#### WP4 — P1: Dagliga operatörsflöden (`cases.html`, `case_detail.html`, `marketing.html`, `interact.html`)
- **Målfiler:** `cases.html`, `case_detail.html`, `marketing.html`, `interact.html`, `cases_routes.py`, `marketing_routes.py`, `core_routes.py`.
- **Åtgärder:**
  1. `cases.html`: slå ihop filterrad, gör kategori till `<select>`, URL-encoda filterlänkar, gör ämne klickbart + mobilvänliga kort, visa `★` och Path B2, bevara filter efter poll/bulk-stäng, läs `getlist("case_id_list")` direkt på servern, lägg till `aria-label` på kryssrutor och paginerings-/taksignalering (`limit=100`).
  2. `case_detail.html`: 2-kolumns desktop-layout, rymlig `<textarea rows="10">` med `<label>`, mottagare + `Re:` intill skicka-knappen, anpassning vid `null-send`, färgkodad tråd (`Kund` vs `Azom`), spårningslänk i orderpanelen, ny `Eskalera till Oscar`-knapp, `sending`-förklaring, bevarade filter vid tillbaka-länk.
  3. `marketing.html`: lägg till drift-/kill-switch-banner, yttra fel i stället för att svälja dem, fixa `0` ROAS och råa booleans, visa `evidence` + `payload` i `<details>`, håll `"Bygg förslag"` alltid tillgänglig, lägg till `MER` + periodväljare + Google OAuth-status.
  4. `interact.html`: bevara inmatad text, skicka med den vid eskalering, kopiera-knapp, svenska badges, rollanpassad text.

#### WP5 — P1/P2: Oscar-konsolen & Secrets/Escalations-konsolidering
- **Målfiler:** `oscar.html`, `oscar_behavior.html`, `oscar_telegram.html`, `oscar_secrets.html`, `oscar_escalations.html`, `secrets.html`, `data_escalations.html`, `data_telemetry.html`, `data_audit.html`, `oscar_routes.py`, `core_routes.py`, `settings_store.py`.
- **Åtgärder:**
  1. Lyft ut/gör 7 modellprofiler hopfällbara (eller `/oscar/profiles`); formatera `Expires`, visa `slot:*`-breakers, `model`/`provider`/`at` och svensk felklassning i `Senaste test`; lägg `target="_blank"` på xAI-länken + stor `font-mono`-kod; lägg till bekräftelser för destruktiva åtgärder och isolerat GDPR-UI.
  2. Uppgradera `/oscar/secrets` med samtliga 44 nycklar grupperade per tjänst, rätt input-typer (`text`/`url`/`email`/`number`/`select` för icke-hemligheter; `password` + `autocomplete="new-password"` + visa/dölj-toggle för riktiga hemligheter) och `id`/`for`-etiketter.
  3. Konsolidera `/secrets` + `/oscar/secrets` och `/data/escalations` + `/oscar/escalations` (enhetliga kolumner, auditlogg på resolve); lägg till filter och formaterad JSON i `data_audit.html` och `data_telemetry.html`.
  4. Förbättra `oscar_behavior.html` (monospace, `rows="10"`, teckenräknare, `scroll-mt-20`, `wp_coder` i beskrivning) och `oscar_telegram.html` (korrekt `<dl>`, fail-closed-varning, badges).

#### WP6 — P1/P2: WordPress Ops, Kodgranskning, IA & Språkstädning
- **Målfiler:** `wp_sites.html`, `wp_site.html`, `wp_ssh.html`, `wp_files.html`, `wp_file_plan.html`, `wp_review.html`, `wp_routes.py`, `index.html`, `onboarding.html`, `settings.html`, `base.html`.
- **Åtgärder:**
  1. Uppdatera `wp_review.html`: visa `plan.comments`, kontrollernas `summary`/`reason`/`findings`, synliga badges för `published`/`mock`/`RestoreNotInMvp`/`sandbox`/`expires_at`, fixa `can_review` vs `_can_drill`-RBAC, inför PRG + flash, strukturera åtgärdspanel, lägg till brödsmula.
  2. Uppdatera `wp_files.html` & `wp_file_plan.html`: ta bort duplicerad 16-raders editor, behåll katalogbrödsmula, ta bort `uppercase` på sökvägar, visa `Mock` vs `Live SFTP`, lägg `font-mono break-all` på hashar/sökvägar, fixa ogiltiga `<dl>` och ärlig sekundär/varningsstil på blockerad produktionsdeploy.
  3. Uppdatera `wp_sites.html`, `wp_site.html` & `wp_ssh.html`: fixa `item.orders_mutated`, `verified`-vakt på drill-formuläret, röd badge på `fail`/`failed`, `font-mono` + maskeringstoggle på SSH-nyckel, och enhetlig WP-brödsmula/fliknavigering.
  4. Slutför ny sidomeny-IA, korrekt `<h1>`/`<h2>`/`<h3>`-hierarki på alla 23 templates, omarbetad `index.html`-grid samt full språkstädning (svenska etiketter, tekniska identifierare på engelska enligt `docs/DOC_STYLE.md`).

---

## 9. Verifieringskommandon för övertagande kodagent

Efter varje arbetspaket ska följande köras från reporoten i mock-läge:

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
AZOM_USE_MOCK=1 pytest tests/test_dashboard.py tests/test_dashboard_auth.py tests/test_dashboard_modules.py tests/test_dashboard_settings.py tests/test_model_dashboard.py tests/test_wp_dashboard.py
# expect: samtliga tester gröna utan att försvaga befintliga assertions

# Full testsvit + linter (CI-krav: ruff + coverage >= 70%)
AZOM_USE_MOCK=1 pytest
ruff check skills tests infrastructure/dashboard
# expect: 0 ruff-fel och full svit grön
```

Dessutom ska varje UI-ändring verifieras i verklig webbläsare (eller headless Chrome CDP i `AZOM_USE_MOCK=1`) för både `oscar` och `jonatan` på `1440x900` och `390x844`:
- 0 CSP-överträdelser och 0 JS-konsolfel,
- 0 blanka knappar och 0 formulärkontroller med kontrast < 4,5:1,
- mobilmenyn stängd som standard och korrekt öppnings-/stängningsbar,
- `GET /oscar` som `jonatan` fortfarande `403`,
- `POST /cases/<id>` med saknad `action` nekar fail-closed utan kundmail,
- `POST /wp/<site_id>/files/deploy` och `POST /wp/<site_id>/reviews/<plan_id>/production` förblir blockerade (`BackupNotVerified`).
