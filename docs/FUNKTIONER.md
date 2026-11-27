# Funktioner — så fungerar AzomOps

**Purpose:** Berätta vad paket 3.0.0 gör, hur delarna sitter ihop, och var gränsen går. Det här är produktguiden. Procedurer och spärrar ligger i de länkade driftfilerna.  
**Audience:** Oscar, Jonatan, och den som öppnar repot på GitHub. Coding agents ska fortfarande börja i [`CURRENT_STATE.md`](CURRENT_STATE.md).  
**Read this first:** [`../README.md`](../README.md), sedan [`CURRENT_STATE.md`](CURRENT_STATE.md) om något här och koden verkar olika.

## Ordlista

| Term | Betydelse |
|------|-----------|
| **En kund** | En installation, en butik. Ingen gemensam kontrollpanel för flera kunder. |
| **HITL** | Human-in-the-loop. Kundmejl, kampanjändring och filskriv väntar på ett explicit godkännande. |
| **★ / suggest-approve** | Systemet flaggar att ett ärende ser säkert ut. Flaggan skickar inget. |
| **Fail-closed** | I drift (`AZOM_USE_MOCK=0`) nekar tom Messenger- eller Telegram-lista alla. En satt karta nekar den som inte finns med. |
| **Mock** | `AZOM_USE_MOCK=1` eller `--mock`. Lokalt arbete utan riktiga anrop. |
| **Null-send** | `AZOM_NULL_SEND=1` eller `--null-send`. Kundmejl vägras, och poll skriver vad auto-send *skulle* ha gjort. |
| **FU9** | Att koppla auto-send till poll. Rälsen finns. Kopplingen finns inte. |

## Löftet

Azom ska ta bort halva supporttiden utan att agenten hittar på orderfakta eller skickar mejl själv. Koden för kön, utkasten, chatten, butiken, webben och annonserna är levererad. Målet på tre månader kräver en baslinje och en live-soak som bara Oscar och Jonatan kan bocka av.

Agenten pratar svenska med kunden och med er. Orderns sanning kommer från WooCommerce, inte från modellens minne. Det står i [`../SOUL.md`](../SOUL.md).

## En arbetsdag

1. Timern `azom-cases-poll.timer` hämtar inkorgen var femte minut.
2. Nytt mejl blir ett ärende, eller hamnar i en befintlig tråd.
3. Klass och utkast skrivs. Kända order- och fraktärenden kan få ★.
4. Jonatan ser kön i Messenger, Telegram eller dashboarden, rättar utkastet och trycker godkänn.
5. Svaret går med trådhuvuden (`In-Reply-To` / `References`).
6. Samma chatt kan slå upp en order, läsa en sida eller granska ett tema. Varje skrivning får en egen knapp.
7. Oscar ser eskaleringar, hemligheter, modellstatus och webbplatser på `/oscar` och `/wp`.

## Ärenden

Supportmejlet slutar som en rad i `cases.db` med status `open`, `escalated`, `sending`, `replied` eller `closed`.

Poll hämtar, trådar, klassar och skriver utkast. Utkastet kan fyllas med order från Woo. `order_status` och `shipping` kan få ★ när förtroendet räcker och ett order-id finns. Retur, betalning och missbruk får rikare utkast och **aldrig** ★.

Godkännande finns på fyra ställen som alla kräver en explicit handling: dashboard, CLI `cases reply`, Messenger-knapp och Telegram-knapp. Fritexten “godkänn” visar en bekräftelse och skickar inte.

`cases draft` och `cases regenerate` skriver om utkastet och rör inte inkorgen. `cases close` stänger utan svar. Bulk-stäng finns. Bulk-godkänn finns inte.

Null-send är av som standard. När den är på vägras kundmejl, och Oscar kan läsa skuggspåret med `cases shadow-report`. Det är ett sätt att se hur auto-send skulle betett sig, inte ett sätt att slå på det.

Djupet: [`CASES.md`](CASES.md).

## Order, produkttext och support

| Kommando | Vad det gör |
|----------|-------------|
| `order-status` | Sätter WooCommerce-status på ett validerat order-id. |
| `product-desc` | Skriver produkttext. Mall är standard. Modellen är valfri. Språk: `sv`, `no`, `dk`. |
| `support` | Klassar ett meddelande och skriver ett svarsutkast, med mall om modellen inte svarar. |

De tre är kärnan från V1 och sitter kvar i 3.0. I chatten blir samma förmågor verktyg: läsningen sker direkt, skrivningen väntar på knapp. Ett “ja” i fri text är inte ett godkännande.

## Mejl

En connector räcker för att poll och svar ska fungera:

| Provider | Så ansluter du |
|----------|----------------|
| `gmail` | App-lösenord, eller webbläsar-OAuth på `/oauth/gmail/start` |
| `outlook` | SMTP + IMAP. Ingen OAuth-guide i dashboarden. |
| `exchange_graph` | Microsoft Graph med client credentials |
| `generic_imap` / `generic_pop3` | Egen värd via env |

Varje brevlåda kan ha ett eget `env_prefix`, så SE-inkorgen inte delar lösen med en annan. NO och DK ligger av tills Oscar både har uppgifter och säger ja.

Oscar kan prova anslutningen live. I drift (`AZOM_USE_MOCK=0`) är en misslyckad probe stängd: den låtsas inte att inkorgen lever.

Djupet: [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) · Gmail: [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md).

## Dashboard

Dashboarden är systemets fulla vy. Jonatan sköter kön. Oscar sköter hemligheter, modeller och eskaleringar. Gränssnittet byggs med Tailwind vid byggtid. I drift behövs ingen Node. Inga externa CDN-skript. Content-Security-Policy släpper bara samma origin.

| Du vill… | Gå till |
|----------|---------|
| Se kö och badges | `/` |
| Komma igång och koppla Gmail | `/onboarding` |
| Godkänna eller stänga | `/cases` |
| Prova ett utkast utan att skicka | `/interact` |
| Läsa Ads och GA4 | `/marketing` |
| Se webbplatser och granskningar | `/wp` |
| Ändra icke-hemlig config | `/settings` |
| Se förbrukning och spår | `/data/telemetry`, `/data/audit` |
| Probes, hemligheter, modeller | `/oscar` |
| Ändra promptar | `/oscar/behavior` |

`/metrics` kräver Bearer-token och ska inte exponeras mot internet. `/health` och `/ready` berättar om processen lever och om mejlpoll är färskt.

Drift: [`PILOT_OPS.md`](PILOT_OPS.md).

## Messenger och Telegram

Båda använder samma `BotHandler` och samma röst.

**Messenger** är vardagsytan. Meta anropar `POST /webhooks/messenger` på dashboarden. Det finns ingen egen Messenger-tjänst. Händelsen läggs i `messenger_jobs.sqlite3`, webhooken svarar HTTP 200 direkt, och en arbetare skriver “skriver…” och delar långa svar. Omstart tar upp jobb som låg mitt i ett anrop.

**Telegram** är backup, med mer synlig arbetsyta. Inkorgen är `telegram_jobs.sqlite3`. Upp till tre läsjobb kan gå samtidigt per chatt. Godkända skrivningar köas och krockar inte med varandra. `LiveStatusBoard` redigerar ett statusmeddelande med steg, tid och modellfas. Du kan svara på det kortet för att styra just det jobbet. `/status`, `/jobs` och `/cancel` fungerar medan arbete pågår.

Slash-kommandon täcker hjälp, status, ärenden, order, hälsa, brief, marknadsföring och vald WordPress-sajt. Fritext väljer läsverktyg. Kundmejl kräver `/cases approve` eller godkänn-knappen.

I drift är tom tillåten-lista eller tom aktörskarta ett nej till alla. Det är avsiktligt.

Djupet: [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) · [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md).

## Marknadsföring

Google Ads och GA4 läses in i samma liggare som Woo-intäkten. Tre siffror får inte bli en “sann ROAS”:

1. Woo-order är handelns sanning.
2. Ads-ROAS är Ads egen attribuering.
3. GA-intäkt kan vara modellerad och påverkad av samtycke.

Jonatan kan läsa och bygga förslag, till exempel sökordssvinn och pacing. Oscar äger OAuth, utvecklartoken och varje ändring. Live-läsning (GA4 `runReport`, Ads GAQL) är levererad. Ändring av kampanj, Measurement Protocol och Merchant-skriv är inte inkopplade. Kill-switchar finns och ska förbli på tills Oscar skriver något annat.

Djupet: [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md).

## Butik och innehåll

WooCommerce-klienten talar REST mot `/wp-json/wc/v3/`: order, produkter, spårningsnummer, paginering och webhooks med HMAC. WordPress-klienten talar `/wp-json/wp/v2/`: sidor, inlägg, media, användare och inställningar, med Application Password.

`domain=se|no|dk` väljer rätt bas-URL. Det är innehålls-API:t. Det installerar inte teman och det är inte driftgranskningen nedan.

Inkommande Woo-händelser landar på `POST /webhooks/woo`.

Djupet: [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md).

## Webbplats, granskning och kod

Driftlagret är en egen yta, `wp_ops`, med sajtregistret i `config/wp_sites.yaml`. Mock använder `config/wp_sites.mock.yaml`. Hemligheter är namn på env-variabler, aldrig värden i git.

Det du kan be om i chatten:

| Begäran | Vad som händer |
|---------|----------------|
| Status, backup, öppna åtgärder | Högst 3 anrop och 30 sekunder, bundet till vald sajt. |
| “Granska startsidans källkod”, eller en namngiven sida | Fyra steg: sida och mallhierarki, redaktörens innehåll, tema/plugin-källa, förhandsvisning. Inga orderverktyg. |
| Tema- eller pluginarbete | Egen tur: 16 anrop, 10 läsningar, 5 sökningar, 60 sekunder, 28 KiB per svar och 80 KiB totalt. Diffen sparas. Filen skrivs inte. |

Arbetsytan kan köra rena Python-kontroller offline, `php -l` när PHP finns, och en lokal sandlåda på `127.0.0.1` när `AZOM_WP_SANDBOX=1`. Standard är av. En grön check hittas inte på. Saknad körning står som `unavailable`.

Staging skriver bara ett lokalt mock-träd, och bara efter en serverknapp med plan-id (`wpfile:stage:…` eller `wpreview:stage:…`). Produktions-POST returnerar `BackupNotVerified` och skriver inte. Återställning returnerar `RestoreNotInMvp`.

Live filläsning är SFTP, bara nyckel, pinnad värd, tak på storlek, och av tills `AZOM_WP_FILES_SSH=1` och ett låst `*-wp-reader`-konto finns. Den öppnar inget skal.

Hälsokoll och backupmanifest i fas 1–2 är mock. Ett verifierat manifest är inte en kopia av sajten och ska inte visas som grönt.

Djupet: [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md) · runbooks [`runbooks/wp-ops-backup.md`](runbooks/wp-ops-backup.md) och [`runbooks/wp-ops-files.md`](runbooks/wp-ops-files.md).

## Webbsök och research

Chatten och CLI kan söka på webben och starta ett research-jobb (`res-` plus 12 hextecken). Resultatet är källmärkt. Det är inte en orderfaktum och inte ett kundmejl.

I mock används fasta offline-svar. Mot nätet krävs `AZOM_USE_MOCK=0` och att Oscar satt `AZOM_WEB_SEARCH_ENABLED`, `AZOM_WEB_FETCH_ENABLED` och `AZOM_RESEARCH_ENABLED` i processmiljön. En brandvägg stoppar personuppgifter, hemligheter och interna värdar innan något lämnar huset. Kostnaden räknas mot ett eget tak på 25 USD, inte mot OpenRouter-taket på 100 USD.

Jonatan och agenten får söka och köra research. Oscar administrerar. Dashboarden är `/research`.

Djupet: [`WEB_SEARCH_RESEARCH.md`](WEB_SEARCH_RESEARCH.md).

## Modeller

All text som modellen skriver går genom `ecom_ops.llm`. Ordningen är:

1. ChatGPT via OAuth som Azom själv roterar (Oscar kopplar på `/oauth/openai/start`).
2. xAI Grok Build, om den är påslagen. Azom läser inte dess `auth.json`.
3. xAI API-nyckel, om den finns. Kostnaden räknas inte mot OpenRouter-taket.
4. OpenRouter, med tak **100 USD** i `config/limits.yaml`.

Sju profiler styr klassning, ärendeutkast, produkttext, chat, verktygsval och djup WordPress-kod. Oscar kan testa en route utan fallback, nollställa brytaren och spara versionerade promptar på `/oscar/behavior`.

När taket är slut och ingen annan route finns kvar hoppar phrasing över. Order, ärenden, status och sidläsning fungerar ändå via verktyg.

En route som fallerar loggas som fallerad. Mallen tar vid där det är tänkt. Felet döljs inte som en lyckad fallback.

Djupet: [`MODEL_ROUTER.md`](MODEL_ROUTER.md) · Oscars steg: [`runbooks/model-oauth.md`](runbooks/model-oauth.md).

## Hälsa, mätning och install

| Verktyg | Vad du ser |
|---------|------------|
| `python -m ecom_ops status` | Mock, profiler, vägar. |
| `python -m ecom_ops.launcher --check` | Python, config, `.env`, OAuth, modeller, portar, allowlists. |
| `soak-preflight` | Checklista inför live. Sätter alltid `soak_complete` till false. |
| `kpis` | Ärenden och kostnad, med baslinje när ni lagt in en. |
| `classify-eval` | Hur klassningen står sig mot fixturer. |
| `cases calibration-report` | Oscar. Rödad jämförelse, inte ett beslut att sänka trösklar. |
| `bin/mock-soak-azom.sh` | Mjuk genomkörning i mock. Ersätter inte live-soak. |

Installation på tom Ubuntu-VPS: [`AUTO_INSTALL.md`](AUTO_INSTALL.md). Storlek och nät: [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md). Docker-volymen är `/app/.azom-data`, inte systemd-sökvägen `/var/lib/azom`.

`./bin/launch.sh` eller `.\bin\launch.ps1` startar förkontroll och kan lyfta dashboard och bot.

## Vem gör vad

| | Jonatan | Oscar | Agenten |
|--|---------|-------|---------|
| Roll | `viewer` | `full_admin` | `operator` |
| Ärenden | Godkänner och stänger | Ser eskaleringar | Pollar och skriver utkast |
| Marknad | Läser och föreslår | Får ändra, när det en dag kopplas | Läser |
| Webb | Läser | Admin. Återställning vägrar ändå | Hälsokoll och mock-backup på staging |
| Research | Söker och kör jobb | Administrerar | Söker och kör jobb |
| Hemligheter | Ser att de finns | Skriver och provar | Nej |

## Medvetet avstängt

- Auto-send i poll, och tyst mejl från “godkänn” i chatten.
- Live-soak, baslinje och sänkta ★-trösklar utan Oscars och Jonatans prov.
- NO/DK-brevlådor.
- Ads-mutate, Measurement Protocol, Merchant-skriv.
- Produktionsdeploy och återställning av WordPress-filer.
- Hanterad hosting, autonom uppdatering, malware-städning.
- Multi-tenant SaaS och en FAQ-bas.
- IMAP IDLE. Poll sker på timer.
- Outlooks webbläsar-OAuth. Env och probe räcker där.
- Direkt xAI-OAuth ägd av appen. Den vägen finns inte.

När något stannar: [`runbooks/`](runbooks/). När ni forkar till en annan butik: [`TEMPLATE_GUIDE.md`](TEMPLATE_GUIDE.md). När koden och den här texten skiljer sig: [`CURRENT_STATE.md`](CURRENT_STATE.md) vinner.
