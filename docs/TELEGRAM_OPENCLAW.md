# Telegram-bot — OpenClaw hybrid (backup-yta)

**Purpose:** Beskriva Telegram som backup-ops-chat: slash-katalog, hybrid free-text, HITL och fail-closed actors.  
**Audience:** Jonatan, Oscar, coding agents.  
**Read this first:** [`SOUL.md`](../SOUL.md), [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) (daily driver), [`CASES.md`](CASES.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Backup chat** | Telegram — samma `BotHandler` som Messenger, men inte primär yta. |
| **Fail-closed** | Tom `TELEGRAM_ALLOWED_CHAT_IDS` eller tom `TELEGRAM_ACTOR_MAP` i live (`AZOM_USE_MOCK=0`) nekar. Icke-tom map nekar unmapped chat_id. |
| **Confirm UX only** | NL “godkänn …” visar bekräftelse — skickar **inte** mail. |
| **tool_digest** | Kort sammanfattning av senast körda read-only tools för följdfrågor. |
| **Statusutkast** | Redigerbart Telegram-meddelande (`LiveStatusBoard`) med aktiv uppgift (`#id8`), steg (`[RUN]`/`[OK]`/`[ERR]`/`[SKIP]`), tid, datakälla (`live`/`mock`) och modellfas. |
| **Svarutkast** | Assistentens text medan den kommer in; ersätts med det slutliga svaret när turen är klar. |
| **Hybrid styrning / parallell task** | Reply på ett status- eller svarskort styr alltid den uppgiften; otrådad fritext klassas smart som styrning eller ny parallell task (upp till 3 samtidiga läsuppgifter per chatt). |
| **Förslagsbunden knapp** | `proposal:yes:pr-<id>` / `proposal:no:pr-<id>` låser payload, domän och sajt vid förslagstillfället (engångskonsumtion). |
| **Förtydligande innan arbete (`clarify:*`)** | `<clarify>{...}</clarify>` och knapparna `clarify:pick` / `clarify:go` / `clarify:other` / `clarify:cancel` (högst 3 rundor, engångskonsumtion, `cl-<id12>`) låser bara tolkning/vägval före arbete och ger ingen skrivbehörighet. |

**Kod:** `skills/ecom_ops/bot/`  
**Identitet:** [`SOUL.md`](../SOUL.md)  
**Entry:** `python -m ecom_ops.bot` · `./bin/dedicated-bot.sh` · systemd `azom-bot.service`

---

## Design

```text
Incoming update
    │
    ├─ allowlist (TELEGRAM_ALLOWED_CHAT_IDS)?
    ├─ actor map (TELEGRAM_ACTOR_MAP) — non-empty ⇒ unmapped denied
    ├─ slash → openclaw_commands.dispatch_openclaw_command
    │            (/cases approve → real send via CaseService)
    ├─ mid-flow multi-step (order_lookup …)
    ├─ "granska startsidans källkod" / "granska sidan <slug|id> källkod" → jobbkind page_review
    │            fyra sajtbundna steg: startsida (eller target_page) + site_editor för blockteman, editorinnehåll, källfiler, browser-preview
    │            inga orderverktyg och ingen orderdigest
    │            php -l körs med fast kommando när PHP finns; browser/runtime = unavailable
    └─ free text → chat_agent.run_chat
                     intent heuristics + pre-change ambiguity check (change_clarifier.py)
                     read tools + confirm-only write proposals
                     bounded named WordPress ops tool loop (site-bound, RBAC checked; Oscar-only read-only SFTP file reads)
                     model-router phrasing (optional; AZOM_TELEGRAM_MODEL_ROUTE pins one route)
                     BotReply + optional clarify / approve / triage keyboards + safe_workspace_attachments
```

**Invariant:** free-text och NL “godkänn &lt;id&gt;” skickar aldrig mail. Send kräver slash approve, inline-knapp kopplad till samma path, dashboard eller CLI. Vid oklara ändringsförfrågningar, layoutönskemål eller beskrivningar av markerade skärmdumpar (`"ringat in på bilden"`) håller `_SafeTextStream` inne `<clarify>`-block från svarutkastet och botten ställer en fokuserad motfråga med rekommenderade engångsknappar (`clarify:pick`, `clarify:go`, `clarify:other`, `clarify:cancel`, högst 3 rundor). `clarify:*`-callbacks är icke-muterande (`kind="callback"`, inte `_ACTION_CALLBACK`) och ersätter aldrig `proposal:yes:pr-...`, `action:yes`, `wpfile:stage:fe-...`, `wpreview:stage:wr-...` eller RBAC. En sidgranskning anropar inte orderchatten (`extract_page_review_target` hämtar valfri `target_page` för `"granska sidan <slug|id> källkod"`, och för blockteman läses Site Editor-mallar via `WordPressClient.list_templates()`, annars `site_editor: "unavailable"`). En följdfråga medan jobbet körs stannar i sidgranskningen. Fritext “ja” och `action:yes` godkänner inte granskningen. Den redan körande botprocessen har inte startats om, så `page_review` är inte live förrän en begärd omstart.

En filplansnotis kan länka till dashboardens planvy när `AZOM_DASHBOARD_PUBLIC_URL` är en HTTPS-URL. Länken innehåller bara registrerat `site_id` och `plan_id`, kräver dashboardinloggning och RBAC, och utför eller godkänner ingenting. Utan giltig HTTPS-bas visas ingen absolut länk. Efter `wp_workspace_run_checks` eller `wp_preview_page` skickar Telegram-sändaren (`_send_attachments` → `_send_photo`) tillåtna PNG-skärmdumpar (`base|work|live|preview|change|focus|compare` från `wp_workspaces/.../artifacts/`) via `BotReply.attachments` och `safe_workspace_attachments()` (inklusive sid-vid-sid-jämförelser `compare-desktop.png` för före/efter och `variant_group`-varianter); absoluta filsökvägar läggs aldrig i LLM-meddelanden.

För äldre knappcallbacks (`order:set:*`, `cases:approve:*`, `product:desc:*`) binder `handlers.py` och `job_store.py` knappen hållbart i SQLite (`bot_proposals`) med `(actor, domain/market, target_id, expiry)` så att ett marknadsskifte i chatten eller dubbelklick/utgången länk nekas eller körs mot fastlåst marknad och aktör. Pollerns underhållsvarv (`bot/__main__.py`) loggar sanerade driftfel till `runtime_errors.jsonl` och kör `run_ops_gc` (`skills/ecom_ops/ops_gc.py`) periodiskt.

### Live-status och strömmande svar

Längre textturer, slash-kommandon och godkända åtgärder visar ett strukturerat live-statuskort (`ProgressEvent` → `LiveStatusBoard`) och vid fritext även ett separat svarutkast:

1. Om turen tar mer än ungefär tre sekunder skickas ett statuskort som svar på användarens ursprungsmeddelande (`reply_to_message_id` + `allow_sending_without_reply`). Varje steg uppdateras på plats från `[RUN]` till `[OK]`, `[ERR]` eller `[SKIP]` med stegnummer (`1/4`), körtid, källa (`live`/`mock`) och ett kort, sanerat utfall.
2. Under längre modell- eller verktygskörningar (t.ex. 30–180 s Grok-resonemang) håller en periodisk heartbeat (var 8:e sekund) både `typing`-indikatorn och statuskortets förflutna tid + aktiva modellfas (`väntar`, `resonerar`, `strömmar`, `förnyar OAuth`, `byter rutt`) levande.
3. Transportlagret (`telegram_transport.py`) normaliserar Telegram HTTP 400 `message is not modified` som idempotent `ok`, respekterar HTTP 429 `retry_after` via en per-chatt-rate-limiter (privat 1,0 s / grupp 3,2 s), schemalägger trailing-edge flush och håller alltid statuskortet under 3 800 UTF-16-kodpunkter.
4. När modelltext kommer in skapas ett separat svarutkast. Om flera uppgifter körs parallellt i samma chatt buffras chunk 2+ tills `finish()` för att undvika sönderhackad sammanblandning i chatten.
5. När turen är klar avslutas statuskortet tydligt efter verkligt utfall: `✓ Svar klart`, `🛑 Avbrutet` eller `⚠️ Misslyckades (<felklass>)`. Alla timers stängs ned direkt.

Statuskortet visar bara steg, namngivet verktyg, källa och kort säkert utfall; det visar aldrig intern kedja-av-tankar, belopp/PII, argument, tokens, källkodscitat eller råa provider-svar. En ren strömrensning vid nästa läsrunda eller styrning (`on_stream_clear`) visar inte längre falskt ruttbyte — endast verkliga modellruttbyten via `ModelRouter` visar `↻ Byter modellrutt`.

Modellens textström kommer från ChatGPT OAuth, OpenRouter, xAI API key eller Grok Build när respektive route stöder strömning. Om en route fallerar och routern provar en annan tas det partiella svarutkastet bort och börjar om. Om strömning eller Telegram-redigering inte fungerar (eller ett utkast raderats i chatten) faller botten automatiskt tillbaka till `sendMessage`.

Snabba svar som blir klara före statusfördröjningen skickar bara slutsvaret (kopplat till användarens meddelande). Messenger använder fortsatt den delade `BotHandler` utan dessa Telegram-callbacks; detta ändrar inte Messenger-webhookens svarsbeteende.

**Modell:** när `AZOM_TELEGRAM_MODEL_ROUTE` är `xai_grok_build` använder fritext den routen och `XAI_MODEL`. Ett fel faller inte vidare till nästa route. Om Grok fortfarande resonerar eller skriver svaret nollställs vilotiden på 30 sekunder. `AZOM_XAI_GROK_HARD_TIMEOUT` stoppar anropet ändå, normalt efter 180 sekunder. `/model` sparar en session-pin, men pin skickas bara när Telegram-routen är `openrouter`. Övriga anrop följer `AZOM_MODEL_ROUTE_ORDER`.

Läsvarv för flera ordrar hämtar högst åtta kända ordernummer först och låter sedan `grok-4.3` med `--reasoning-effort low` be om upp till fem läsningar, med 20 sekunders vila, 45 sekunders tak per runda och 90 sekunder för hela fasen; kooperativa checkpoints kontrollerar `/cancel` och styrningar före varje orderhämtning och varje modellrunda, och ett avbrutet modellsteg behåller redan hämtad text.

---

### Bakgrundsjobb, parallella uppgifter och hybrid styrning

Telegram tar emot uppdateringar oberoende av modell- och verktygsjobb. En SQLite-inbox sparar varje `update_id` innan poll-cursorn flyttas. Två arbetare kör som standard (`AZOM_TELEGRAM_WORKERS`, tillåtet 1–4); en databaslease hindrar en andra process från att poll:a samma bot. Varje jobb har ett eget `task_id` och upp till **3 läsuppgifter** (`read_turn`, `page_review`, läs-callbacks) kan köras parallellt i samma chatt, medan godkända skrivåtgärder (`kind='action'`) alltid serialiseras både per chatt och via SQLite-resurslås (`bot_resource_locks`).

- **Hybrid styrning vs ny parallell uppgift (`steer_router.py`):**
  1. `/task <text>` startar alltid en ny parallell uppgift.
  2. `/steer [<id8>] <text>` styr uttryckligen angivet (eller enda) körande läsjobb.
  3. Övriga slash-kommandon (t.ex. `/order 1001`, `/cases`, `/brief`) startar som nya parallella uppgifter.
  4. **Telegram-reply** på ett statuskort, svarskort eller ursprungsmeddelande styr alltid just den pågående uppgiften.
  5. Under en aktiv `page_review` stannar otrådad fritext alltid i sidgranskningen (orderverktyg körs aldrig).
  6. Otrådad fritext under `read_turn` klassas smart efter ny-task-fras, domänskifte (`order`/`cases`/`marketing`/`wp`/`catalog`), nytt ordernummer eller korta bekräftelse-/stilfraser. Om 2+ läsuppgifter körs samtidigt och inget entydigt mål finns startas hellre en ny uppgift än en gissad styrning.
- Högst tre följdfrågor (`steer`) injiceras per jobb vid nästa säkra kontrollpunkt; `/cancel` respekteras alltid vid varje checkpoint även efter tre styrningar.
- **Förslagsbundna HITL-knappar (`bot_proposals`):** Skriv- och eskaleringsförslag i Telegram skapar en hållbar rad i `bot_proposals` med fryst payload, `domain`, `site_id` och `task_id`, samt unika engångsknappar (`proposal:yes:pr-<id>` / `proposal:no:pr-<id>`). Två parallella uppgifter kan därmed aldrig krocka över en gemensam `action:yes`, och fritext `"ja"` kör inte tyst en väntande Telegram-åtgärd.
- **Atomisk kontext-merge (`append_turn_atomic`):** Parallella läsuppgifter slår samman sina meddelandedeltan och `tool_digest` atomiskt inuti SQLite-transaktion utan att skriva över en annan uppgifts väntande flow/förslag.
- `/status` visar samtliga pågående jobb (`#id8`, typ, senaste steg) och ködjup. `/jobs` visar väntande jobb. `/stop` eller `/cancel [job-id]` begär kooperativ avbrytning (och ber om `<id8>` om flera jobb körs samtidigt).
- Ett läsjobb kan köas om efter processomstart. En godkänd åtgärd som kan ha startat får `needs_reconciliation` och körs inte om. Endast Oscar kan stämma av den med `/reconcile <job-id> done|not_applied` efter kontroll av externa system. Kommandot utför ingen åtgärd.
- Direkt före sidoeffekten gör arbetaren en förkontroll av åtgärdsformat, chattens allowlist, actor-mappning och öppet förslag (`bot_proposals`) respektive bekräftelsesnapshot. Handlern kontrollerar därefter behörighet, mål och kill-switch igen innan tjänsteanropet.
- Slutliga svar och Telegram-meddelande-id sparas för leveransförsök med exponentiell backoff (`delivery_attempts`, `next_delivery_at`). Leverans är minst en gång; vid fel kan ett svar komma dubbelt.

Inbox, jobb, leveransmetadata och Telegram-samtalshistorik ligger i `AZOM_DATA_DIR/telegram_jobs.sqlite3`. Vid första start migreras `telegram_state.json` en gång och den gamla filen lämnas kvar. Databasfilerna har begränsade filrättigheter; avslutade jobb behålls i sju dagar och olösta avstämningar tills de löses. Backup-jobbet tar en onlinekopia av jobbkön och utesluter Node/Python-bibliotek från tarballen. Messenger fortsätter använda sin befintliga synkrona lagring.

## Environment

```bash
TELEGRAM_BOT_TOKEN=...
# Krävs i prod (AZOM_USE_MOCK=0):
TELEGRAM_ALLOWED_CHAT_IDS=111111111,222222222
# chat_id → actor; non-empty map → unmapped denied
TELEGRAM_ACTOR_MAP=111111111:jonatan,222222222:oscar
# TELEGRAM_FAIL_CLOSED=1
```

Vid start registrerar boten `TELEGRAM_MENU_COMMANDS` via Telegram `setMyCommands` (inklusive `/wp` för WordPress-sajt och granskningsplaner: `sites|use|plans|plan|clear`). Chatten får veta den mappade aktören i varje tur. En fritext som «jag är Oscar» ändrar inte rollen, och Oscar ombeds inte eskalera till sig själv.

### Bra / dåligt exempel

**Bra (prod):**

```bash
TELEGRAM_ALLOWED_CHAT_IDS=111111111
TELEGRAM_ACTOR_MAP=111111111:jonatan
```

**Dåligt (prod):** Tom allowlist eller tom map i live — **alla nekas** (fail-closed). Sätt båda enligt Oscars policy innan go-live.

### Dashboard

Oscar öppnar `/oscar/telegram`. Sidan visar om token finns, hur många id som ligger i processens allowlist, hur många poster actor map har, och senaste probe.

Token byts under `/oscar/secrets`. Allowlist och actor map sätts i processmiljön (systemd eller skalet). `TELEGRAM_ALLOWED_CHAT_IDS` och `TELEGRAM_ACTOR_MAP` är skyddade overlay-nycklar. Ett värde i `secrets.env` laddas inte in och öppnar inte boten.

---

## Slash-katalog (OpenClaw-kompatibel + Azom)

| Kommando | Beskrivning |
|----------|-------------|
| `/help` | Kort intro (även `/start`) |
| `/commands` | Full katalog |
| `/status` | Alla pågående bakgrundsjobb (`#id8`, senaste steg, kö), annars runtime-status |
| `/jobs` | Köade och avstämningskrävande jobb i chatten |
| `/steer [<id8>] <text>` | Styr ett pågående läsjobb vid nästa säkra kontrollpunkt |
| `/task <text>` | Starta en ny parallell uppgift bredvid pågående körning |
| `/whoami` (`/id`) | chat_id + resolved actor |
| `/new [model]` | Rensa dialog; valfri model pin |
| `/reset` · `/reset soft` | Full reset eller behåll session settings |
| `/stop` (`/cancel [job-id]`) | Begär avbrytning vid säker punkt; utan jobb avbryts sparat flow |
| `/reconcile <job-id> done|not_applied` | Oscar-only: registrera manuellt kontrollerat utfall; kör inte jobbet igen |
| `/tools` · `/tools verbose` | Chat tools (inkl. `wp_list_components`, `wp_theme_overview`, `wp_plugin_overview`, `wp_open_review_workspace / wp_workspace_*`, `wp_code_index`, `wp_template_hierarchy`, `wc_template_overrides_audit`, `wp_component_intel`, `wp_playbook`) vs slash/CLI tools |
| `/tasks` | Open cases + open escalations count |
| `/usage` · `/usage cost\|off` | Cost / footer mode |
| `/model` · `/model <name>\|default` | Session model pin |
| `/verbose` · `/think` | Session style knobs |
| `/skill` | ecom-ops skill summary |
| `/context` | Flow, turns, tool_digest, session keys |
| `/health` | SSH health (actor-scoped) |
| `/brief` | Customer + cost brief |
| `/order [id]` | Order status read-only; multi-step om bart |
| `/cases` | list · show · approve · regenerate · close · help |
| `/wp sites` | Lista registrerade sajter för WordPress driftuppslag |
| `/wp use <site-id>` | Välj sajt i aktuell chattkontext |
| `/wp plans [site-id]` | Lista öppna `wr-*`-granskningsarbetsytor och stagingstatus |
| `/wp plan <plan-id>` | Visa `wr-*`-plan, ändrade filer, kontroller och (om stagingredo + `WP_OPS_ADMIN` + `CODE_EDIT`) stagingknapp `wpreview:stage:wr-...` |
| `/wp clear` | Rensa vald WordPress-sajt (och cachade review-hashar) |
| `/ops` | Oscar-only (`full_admin`): visar poller-lease, jobbköer, circuit breakers, ChatGPT OAuth TTL, `.azom-data`-diskanvändning och senaste sanerade driftfel (`runtime_health.py`). Viewer/operator nekas |
| `/oauth [chatgpt\|xai\|test\|status]` | Oscar-only: länka, testa och hantera ChatGPT Plus OAuth och xAI Grok Build direkt i Telegram (knappar + efemär callback-URL utan SQLite-lagring) |

### `/cases` subcommands

```text
/cases                  # open + escalated (escalated → high → ★suggest → newest)
/cases show <id8>       # detail + draft + approve keyboard
/cases approve <id8>    # send draft (RBAC CASE_REPLY / admin)
/cases regenerate <id8> # new draft from inbound (never sends)
/cases close <id8>      # close without reply
/cases help
```

Suggest-approve rader visar `★föreslå` (+ confidence när den finns).

---

## Hybrid free-text

Telegram inbox, jobb och samtalshistorik ligger i `AZOM_DATA_DIR/telegram_jobs.sqlite3`. `telegram_state.json` migreras automatiskt en gång vid uppgradering:

- **TTL:** 24h idle (refreshas vid aktivitet)
- **History:** senaste ~40 turns
- **Sticky:** order/case/market/content context plus `session.wp_ops_context` for a WordPress site and operation ids. WP-kontexten är knuten till aktör, kanal och konversation och går ut efter 12 timmar.
- **tool_digest** för följdfrågor (“och frakten?”, “samma order”)

Prefetch (`chat_agent.gather_tool_results`):

| Tool | När |
|------|-----|
| `lookup_order` | Order-id eller sticky follow-up |
| `list_recent_orders` | «senaste ordrarna», «kontrollera integrationen», «kolla nu», enbart «azom.no», «fler ordrar» eller «nästa 20». Fortsättning hoppar över de redan visade. Rå `<tool_call>` visas inte. |
| `list_cases` / `show_case` | Ärende / triage / id8 / sticky case |
| suggest filter | “föreslagna”, ★ |
| `ops_snapshot` / capabilities | status / budget / “vad kan du” |
| approve **confirm-only** | NL “godkänn id8” → UX, no send |
| `propose_order_status` | → **confirm button** (inte silent) |
| `list_products` / `show_product` | «visa produkter», «produkt 501», «produktdata». Id, namn, sku, pris, lager, kort utdrag |
| `propose_product_update` | «ändra produkt 501 namn/pris/lager/text till …» → **confirm button**. Inget sparas före Ja |
| `list_pages` / `show_page` | «visa sidor», «visa sidan Om oss», «sidan 2» |
| `propose_page_update` | «ändra sidan 2 titel/text till …» → **confirm button**. Temfilar rörs inte |
| `list_posts` / `show_post` | «visa inlägg», «inlägg 1» |
| `propose_post_update` | «ändra inlägg 1 titel/text/status till …» → **confirm button** |
| `show_site` | «sajttitel», «tagline», «vad heter sajten» |
| `propose_site_update` | «ändra sajttitel till …» eller «ändra tagline till …» → **confirm button** |
| `list_themes` | «visa teman», «vilket tema är aktivt». Inget temabyte |
| `list_plugins` | «visa plugins». Aktiv eller inaktiv |
| `propose_plugin_status` | «aktivera plugin …» eller «stäng av plugin …» → **confirm button**. Bara Oscar. Inaktivering av kritiska e-handelsplugins (`woocommerce`, Klarna, Vipps, Stripe, Svea) visar en explicit varning före bekräftelse |
| `propose_product_desc` | → **confirm button** |
| `propose_regenerate` | → **confirm button** (skickar aldrig) |

WordPress driftverktygen är separata från WordPress-innehållets `list_pages`/`show_page`-verktyg:

| Namngivet verktyg | Användning |
|-------------------|------------|
| `wp_list_sites` | Läs registrerade sajter |
| `wp_site_status` | Läs status för en behörig sajt |
| `wp_backup_status` | Läs backupstatus och kända manifest |
| `wp_security_findings` | Läs tillgängliga säkerhetsfynd; detta kör ingen skanning |
| `wp_prepare_update_plan` | Skapa låst preview; den kan inte godkännas eller köras |
| `wp_prepare_restore_plan` | Skapa låst restore-preview; restore kan inte köras |
| `wp_list_open_operations` | Läs öppna driftärenden för sajt/konversation |

Servern begränsar driftloopens tur till 3 anrop, 30 sekunder, 4 KiB per resultat och 10 KiB totalt. Användaren måste välja en registrerad sajt; servern gör RBAC-kontroll vid varje verktygskörning. Driftstatus kan vara mock eller okänd. Livehälsokontroller, säkerhetsskanning, versionsupptäckt, uppdatering och produktions-restore är inte implementerade. Update- och restoreplaner är förhandsgranskningar utan godkännande- eller exekveringsväg.

Fil- och arbetsyteverktyg körs i en egen bunden tur (`run_bounded_file_tool_loop()`, `ALL_FILE_TOOLS`). Driftloopen kör dem inte, men inne i fil-/arbetsyteturen får även driftläsningar (`WP_TOOL_NAMES`) köras utan att bryta turen. När en sajt är vald med `/wp use <site-id>` (`session.wp_ops_context`) passerar WP-kodfrågor (`WP_CODE_INTENT_RE`) förbi innehålls-, katalog- och produktfällan (`site_content`, `catalog_write_turn`, `catalog_read_turn`) direkt till `wp_ops`-verktygen i stället för att stoppas eller misstas för produkt-/siduppslag (`site_content` med `allow_wp_ops_fallthrough` släpper också igenom kod-/filfrågor medan temabyte, installation och radering nekas utan sajtstöd). Vid `WP_CODE_INTENT_RE` injiceras även upp till två kompakta `wp_playbook`-guider (`format_playbook_hints`, max 750 tecken) som vägledning. Turen får högst **16** anrop, **10** läsningar, **5** sökningar och **60** sekunder (16 KiB per resultat, 48 KiB totalt). Om något tak nås tvingar `_finish_on_budget()` fram en slutsummering av insamlade resultat och vad som återstår okänt. Statusraden namnger verktyg, sajt och sökväg och citerar inte källan.

| Namngivet verktyg | Användning |
|-------------------|------------|
| `wp_component_context` | Registeruppgifter utan hemligheter, värd eller filrot. Inget REST-anrop när mock är på |
| `wp_list_components` | Lista tema- eller plugin-sluggar (`WP_OPS_READ`) |
| `wp_theme_overview` | Aktivt tema/föräldratema, `block`/`classic`, filträd (djup 3, max 120) och `woocommerce/`-overrides |
| `wp_plugin_overview` | Samlad pluginöversikt (djup 2, max 80), hooks, HPOS-deklaration och `WC tested up to`. Bara Oscar |
| `wp_list_component_tree` | En katalognivå. Jonatan får lista |
| `wp_search_component` | Sökning med `mode="literal"\|"regex"`, `context_lines` (`0`–`3`), `plan_id` (`wr-*`), `extensions` (max 6) och `case_insensitive` (default `true`). Bara Oscar; utdrag skrubbas från hemligheter |
| `wp_read_theme_plugin_file` | Bytefönster (4096 byte normalt, max 8192, `next_offset`) eller radintervall (`start_line`/`end_line`, 1-baserat, `next_line`). Bara Oscar |
| `wp_code_index` | Statiskt symbolindex (`function`/`method`/`class`/`hook`/`shortcode`/`rest_route`/`block`/`constant`/`header`) för installerad komponent eller `wr-*`. Bara Oscar |
| `wp_template_hierarchy` | Beräknar WordPress-/WooCommerce-mallhierarki (`TEMPLATE_CONTEXTS`) och matchar befintliga kandidater (`WP_OPS_READ`) |
| `wc_template_overrides_audit` | Granskar temats `woocommerce/`-overrides och `@version`-drift (`WP_OPS_READ`) |
| `wp_component_intel` | WordPress.org-metadata (`WP_OPS_READ`). `AZOM_WPORG_INTEL` är avstängd som standard (endast opt-in med `AZOM_WPORG_INTEL=1` och `AZOM_USE_MOCK=0`; mock-läge använder offline-fixturer) |
| `wp_playbook` | Noll-IO-guide för `PLAYBOOK_TOPICS` (alla chattaktörer; ger ingen behörighet) |
| `wp_open_review_workspace` / `wp_list_workspaces` | Öppna eller lista isolerade `wr-*`-arbetsytor under `AZOM_DATA_DIR/wp_workspaces/`. Skriver inte sajten |
| `wp_workspace_list_files` / `wp_workspace_read_file` / `wp_workspace_diff` / `wp_workspace_status` | Lista filer (`WP_OPS_READ`), läsa radintervall (Oscar) och hämta skrubbad `unified_diff` (Oscar + `CODE_EDIT`) i `wr-*` |
| `wp_workspace_patch_file` / `wp_workspace_create_file` / `wp_workspace_apply_changes` / `wp_workspace_bulk_replace` | Enfilspatch, ny fil, atomiska ändringar (`edit`/`create`/`delete`/`revert`) och massersättning (`dry_run=true` först) i `wr-*` |
| `wp_workspace_scaffold` / `wp_workspace_theme_json` / `wp_workspace_copy_from_parent` / `wp_workspace_copy_woo_template` | Skapa grundstruktur (`child_theme_basics`/`site_plugin_basics`/`woo_compat_declaration`), uppdatera `theme.json` med JSON-pekare eller kopiera ren modertema-/Woo-mall till `wr-*` |
| `wp_workspace_static_checks` | Snabb ren Python-kontroll (säkerhet, Woo, JSON-lint) offline; publicerar inte `check_run` och öppnar inte staging |
| `wp_workspace_run_checks` | Kör `ReviewPlans.run_checks()` (sandlåda när `AZOM_WP_SANDBOX=1`, annars `run_catalog` med `unavailable` för saknade körningar), sparar skrubbade sammanfattningar/fynd, bifogar upp till 4 PNG-skärmdumpar via `_send_photo` och visar stagingknapp `wpreview:stage:wr-...` endast när `tests_passed` + `published` på staging utan hög risk |
| `wp_propose_file_diff` / `wp_propose_new_file` | Enfils diffplan (`fe-*`) med bas- och resultat-hash. Tredjepartsplugins inspekteras skrivskyddat; egna sajtutökningar läggs i `azom-site-<site_id>` |
| `wp_validate_plan` / `wp_apply_staging` | Kontrollerar hashar resp. föreslår enfils-staging via knappen `wpfile:stage:fe-...` |
| `wp_request_production` / `wp_deploy_production` / `wp_verify_change` / `wp_rollback_file` | Produktion ger alltid stopp (`BackupNotVerified`), rollback ger `RestoreNotInMvp`; ingen Telegram-knapp för produktion |

`action:yes` och fritext «ja»/«godkänn» ändrar aldrig en `fe-*`-plan eller en `wr-*`-arbetsyta. Enfiliga planer använder inline-knappen `wpfile:stage:fe-...` / `wpfile:no:fe-...` (`WP_OPS_OPERATE` på mock-staging). Flerfilsarbetsytor (`wr-*`) använder inline-knappen `wpreview:stage:wr-...` / `wpreview:no:wr-...` (antingen direkt efter godkänd `wp_workspace_run_checks` eller via `/wp plan <plan-id>`), med strikt fail-closed RBAC för Oscar (`WP_OPS_ADMIN` + `CODE_EDIT`) samt snapshot-bindning (`wp_review_hashes` med `base_sha256` och `work_sha256`). Skrivningen sker enbart till lokalt mock-träd; produktion deployas aldrig tyst eller från chatten (`ProductionWriteDisabled` / `BackupNotVerified`).

### Site write rails

| Action | Callback | RBAC |
|--------|----------|------|
| Order status | `order:set:{id}:{status}` | `ORDER_STATUS_UPDATE` (operator/Oscar) |
| Product desc | `product:desc:{id}:{0\|1}` | `PRODUCT_DESC_WRITE` |
| Product data | `action:yes` / `action:no` | `PRODUCT_DESC_WRITE` (texten ligger i sessionen, inte i knappen) |
| Page content | `action:yes` / `action:no` | `WP_OPS_OPERATE` |
| Post field | `action:yes` / `action:no` | `WP_OPS_OPERATE` |
| Site title or tagline | `action:yes` / `action:no` | `WP_OPS_OPERATE` |
| Plugin on/off | `action:yes` / `action:no` | `WP_OPS_ADMIN` (Oscar) |
| Stagingfil (`fe-*`) | `wpfile:stage:fe-...` / `wpfile:no:fe-...` | `WP_OPS_OPERATE` på registrerad mock-staging. Produktion har ingen knapp |
| Staging-arbetsyta (`wr-*`) | `wpreview:stage:wr-...` / `wpreview:no:wr-...` | `WP_OPS_ADMIN` + `CODE_EDIT` (Oscar) + snapshot-hashar + `tests_passed`. Bara lokal mock-staging; aldrig tyst produktionsdeploy |
| WordPress-utkast (`wr-*` `page_draft`) | `wpreview:push_draft:wr-...` | `WP_OPS_ADMIN` + `CODE_EDIT` (Oscar) + fastlåsta `base_sha256`/`work_sha256`. Skapar endast `status="draft"` via `WpOpsService.push_page_draft()` (`edit_url`); publicerar aldrig live |
| Förtydligande innan arbete | `clarify:pick:cl-<id>:<opt>` · `clarify:go:cl-<id>` · `clarify:other:cl-<id>` · `clarify:cancel:cl-<id>` | **Ingen skrivning.** Låser endast omfattning/val (`flow="change_clarify"`, max 3 rundor); slutlig ändring kräver alltid egen skriv-/stagingknapp och RBAC |
| Regen draft | `cases:regen:{id8}` | `CASE_REPLY` (Jonatan OK) |

Äldre skrivcallbacks (`order:set:*`, `cases:approve:*`, `product:desc:*`) binds dessutom hållbart i `bot_proposals` med `(actor, domain/market, target_id, expiry)` för engångskonsumtion och skydd mot marknadsdrift i sessionen.

Jonatan kan approve:a cases men **inte** skriva Woo order/product om hen inte är mappad till `agent`/`oscar`.

---

## Actors & RBAC

`resolve_telegram_actor(chat_id)` styr approve/close/health.

| Läge | Allowlist / map | Beteende |
|------|-----------------|----------|
| Mock (`AZOM_USE_MOCK=1`) | Tom map | unmapped → `jonatan` |
| Live | Tom allowlist **eller** tom map | **denied** (fail-closed) |
| Live | Icke-tom map | unmapped → **denied** |
| Live | Allowlist satt | chat utanför listan → **denied** |

Samma modell som Messenger (`skills/ecom_ops/bot/actors.py`).

---

## Failure modes

| Condition | Behavior |
|-----------|----------|
| Ingen modellroute konfigurerad | Tools körs; annars prior_digest/sticky eller fast svensk hjälp |
| OpenRouter-budget at cap och ingen annan route | Samma — tools utan LLM-phrasing |
| Inte i allowlist | Denial + hint (Oscar + `TELEGRAM_ALLOWED_CHAT_IDS`) |
| Approve fail | Svenskt fel + Regenerera / Visa / Lista |
| Tom case-kö | Hint: nästa poll (~5 min) eller dashboard `/cases` |
| Okänt slash | “Okänt kommando … /commands · /help” |

---

## Local run

```bash
# cwd: repo-root
export AZOM_USE_MOCK=1
export TELEGRAM_BOT_TOKEN=...   # optional for pure unit tests
python -m ecom_ops.bot
```

Tester: `tests/test_telegram_state.py`, `test_telegram_actors.py`, `test_chat_agent.py`, `test_suggest_triage_ux.py`, `test_cases_v2.py`.

## Relaterat

- Messenger (primär): [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md)
- Cases: [`CASES.md`](CASES.md)
- Pilot: [`PILOT_OPS.md`](PILOT_OPS.md)
