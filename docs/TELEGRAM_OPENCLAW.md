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
| **Statusutkast** | Redigerbart Telegram-meddelande med senaste arbetssteg och verktygsstatus. |
| **Svarutkast** | Assistentens text medan den kommer in; ersätts med det slutliga svaret när turen är klar. |

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
    ├─ "granska startsidans källkod" → jobbkind page_review
    │            fyra sajtbundna steg: startsida, editorinnehåll, källfiler, browser-preview
    │            inga orderverktyg och ingen orderdigest
    │            browser, PHP eller sandbox som inte körts = not_run/unavailable
    └─ free text → chat_agent.run_chat
                     intent heuristics
                     read tools + confirm-only write proposals
                     bounded named WordPress ops tool loop (site-bound, RBAC checked; Oscar-only read-only SFTP file reads)
                     model-router phrasing (optional; AZOM_TELEGRAM_MODEL_ROUTE pins one route)
                     BotReply + optional approve / triage keyboards
```

**Invariant:** free-text och NL “godkänn &lt;id&gt;” skickar aldrig mail. Send kräver slash approve, inline-knapp kopplad till samma path, dashboard eller CLI. En sidgranskning anropar inte orderchatten. En följdfråga medan jobbet körs stannar i sidgranskningen. Fritext “ja” och `action:yes` godkänner inte granskningen. Den redan körande botprocessen har inte startats om, så `page_review` är inte live förrän en begärd omstart.

### Live-status och strömmande svar

Längre textturer visar två separata Telegram-meddelanden:

1. Om turen tar mer än ungefär tre sekunder visas ett kort statusutkast. Vid första aktivitet uppdateras det med säkra steg som `WordPress-status pågår…`, `Backupstatus: resultat mottaget` eller `Formulerar svar…`.
2. När modelltext kommer in skapas ett separat svarutkast. Botten redigerar det med begränsad takt i stället för att skicka ett nytt meddelande för varje textbit.
3. När turen är klar ersätts svarutkastet med den slutliga texten och eventuella knappar. Statusutkastet behåller de senaste verktygsstegen och avslutas med `Svar klart`.

Det här följer samma användbara mönster som Datalasse/OpenClaw: synligt arbete, verktygsstatus och redigerat svar. Azom använder sin egen Telegram Bot API-handler (`sendMessage` + `editMessageText`), inte OpenClaws inbyggda streamingläge. Statusen visar bara vilket steg eller namngivet verktyg som körs; den visar inte intern kedja-av-tankar, argument, kunddata eller credentials. Verktygsstatus är avsiktligt neutral: `resultat mottaget` säger inte att sajten är frisk.

Modellens textström kommer från ChatGPT OAuth, OpenRouter, xAI API key eller Grok Build när respektive route stöder strömning. Om en route fallerar och routern provar en annan tas det partiella svarutkastet bort och börjar om. Om strömning eller Telegram-redigering inte fungerar skickas det slutliga svaret på normalt sätt.

Snabba svar som blir klara före statusfördröjningen skickar bara slutsvaret. Messenger använder fortsatt den delade `BotHandler` utan dessa Telegram-callbacks; detta ändrar inte Messenger-webhookens svarsbeteende.

**Modell:** när `AZOM_TELEGRAM_MODEL_ROUTE` är `xai_grok_build` använder fritext den routen och `XAI_MODEL`. Ett fel faller inte vidare till nästa route. Om Grok fortfarande resonerar eller skriver svaret nollställs vilotiden på 30 sekunder. `AZOM_XAI_GROK_HARD_TIMEOUT` stoppar anropet ändå, normalt efter 180 sekunder. `/model` sparar en session-pin, men pin skickas bara när Telegram-routen är `openrouter`. Övriga anrop följer `AZOM_MODEL_ROUTE_ORDER`.

Läsvarv för flera ordrar hämtar högst åtta kända ordernummer först och låter sedan `grok-4.3` med `--reasoning-effort low` be om upp till fem läsningar, med 20 sekunders vila, 45 sekunders tak per runda och 90 sekunder för hela fasen; statusraden säger vilket steg som pågår, och ett avbrutet modellsteg behåller redan hämtad text.

---

### Bakgrundsjobb och fortsatt dialog

Telegram tar emot uppdateringar oberoende av modell- och verktygsjobb. En SQLite-inbox sparar varje `update_id` innan poll-cursorn flyttas. Två arbetare kör som standard (`AZOM_TELEGRAM_WORKERS`, tillåtet 1–4); en databaslease hindrar en andra process från att poll:a samma bot. Jobb körs i ordning per chatt, medan olika chattar kan köras parallellt.

- Vanlig text under ett läsjobb blir en följdfråga. Den läggs i en mailbox och vägs in vid nästa säkra kontrollpunkt; högst tre följdfrågor tas in per jobb. Ett modell- eller verktygsanrop avbryts inte mitt i ett nätverksanrop.
- Text och knappar under en godkänd åtgärd köas som nästa tur. De kan inte ändra den redan godkända åtgärden. Godkända åtgärder serialiseras; WordPress-ändringar låses per vald sajt.
- `/status` visar aktivt jobb och senaste steg. `/jobs` visar väntande jobb. `/stop` eller `/cancel [job-id]` begär kooperativ avbrytning; en åtgärd kan inte avbrytas när sidoeffekten har startat.
- Ett läsjobb kan köas om efter processomstart. En godkänd åtgärd som kan ha startat får `needs_reconciliation` och körs inte om. Endast Oscar kan stämma av den med `/reconcile <job-id> done|not_applied` efter kontroll av externa system. Kommandot utför ingen åtgärd.
- Direkt före sidoeffekten gör arbetaren en förkontroll av åtgärdsformat, chattens allowlist, actor-mappning och aktuell bekräftelsesnapshot. Förkontrollen stoppar en felaktig eller inaktuell köpost; handlern kontrollerar därefter behörighet, mål och kill-switch igen innan tjänsteanropet.
- Slutliga svar och Telegram-meddelande-id sparas för leveransförsök. Leverans är minst en gång; vid fel kan ett svar komma dubbelt.

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

Vid start registrerar boten `TELEGRAM_MENU_COMMANDS` via Telegram `setMyCommands`. Chatten får veta den mappade aktören i varje tur. En fritext som «jag är Oscar» ändrar inte rollen, och Oscar ombeds inte eskalera till sig själv.

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
| `/status` | Aktivt bakgrundsjobb och senaste steg, annars runtime-status |
| `/jobs` | Köade och avstämningskrävande jobb i chatten |
| `/whoami` (`/id`) | chat_id + resolved actor |
| `/new [model]` | Rensa dialog; valfri model pin |
| `/reset` · `/reset soft` | Full reset eller behåll session settings |
| `/stop` (`/cancel [job-id]`) | Begär avbrytning vid säker punkt; utan jobb avbryts sparat flow |
| `/reconcile <job-id> done|not_applied` | Oscar-only: registrera manuellt kontrollerat utfall; kör inte jobbet igen |
| `/tools` · `/tools verbose` | Chat tools vs slash/CLI tools |
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
| `/wp clear` | Rensa vald WordPress-sajt |

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
| `propose_plugin_status` | «aktivera plugin …» eller «stäng av plugin …» → **confirm button**. Bara Oscar |
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

Filverktyg är en egen tur. Driftloopen kör dem inte. Den turen får högst 4 läsningar, 1 sökning, 8 anrop och 45 sekunder. Statusraden namnger verktyg, sajt och sökväg och citerar inte källan.

| Namngivet verktyg | Användning |
|-------------------|------------|
| `wp_component_context` | Registeruppgifter utan hemligheter, värd eller filrot. Inget REST-anrop när mock är på |
| `wp_list_component_tree` | En katalognivå. Jonatan får lista |
| `wp_search_component` | Bokstavlig sökning. Bara Oscar. Inget regex och inget helt filinnehåll |
| `wp_read_theme_plugin_file` | Fönster, normalt 4096 byte och högst 8192, med `next_offset`. Bara Oscar |
| `wp_propose_file_diff` / `wp_propose_new_file` | Namngiven plan med bas- och resultat-hash. Skriver inte filen |
| `wp_validate_plan` | Kontrollerar hashar. Hög risk flyttas inte fram. Ingen PHP-process |
| `wp_apply_staging` | Från modellen: bara ett förslag. Knappen `wpfile:stage:{plan_id}` skriver enbart mock-staging |
| `wp_request_production` / `wp_deploy_production` | Alltid stopp. Ingen Telegram-knapp för produktion |
| `wp_verify_change` | Jämför hash. Sidkontroller körs inte |
| `wp_rollback_file` | `RestoreNotInMvp`. Ingen återställning |

`action:yes` ändrar inte en filplan. Avbryt är `wpfile:no:{plan_id}` och raderar diffen. Fri text «ja» räcker inte.

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
| Stagingfil | `wpfile:stage:{plan_id}` / `wpfile:no:{plan_id}` | `WP_OPS_OPERATE` på registrerad staging. Bara lokal mockträd. Produktion har ingen knapp |
| Regen draft | `cases:regen:{id8}` | `CASE_REPLY` (Jonatan OK) |

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
