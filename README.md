# AzomOps-Agent

En kundagent för **en** WooCommerce-butik. Den läser order, mejl, annonser och webbplats, skriver utkast på svenska, och väntar på en människa innan något skickas till kunden, publiceras eller ändrar en kampanj.

**Paket 3.0.0** · en kund per installation · Ubuntu 26.x eller 24.04 på Hetzner (CX22 / CPX21, 2 vCPU, 4 GB).

| För dig som… | Börja här |
|--------------|-----------|
| Vill förstå vad systemet gör | [`docs/FUNKTIONER.md`](docs/FUNKTIONER.md) |
| Kör piloten (svenska) | [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) · [`docs/CASES.md`](docs/CASES.md) |
| Bygger eller ändrar kod | [`AGENTS.md`](AGENTS.md) · [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) |
| Vill se kartan | [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) |

Hur dokumenten skrivs: [`docs/DOC_STYLE.md`](docs/DOC_STYLE.md) · fullständigt index: [`docs/README.md`](docs/README.md)

## Tre ytor, samma regler

1. **Inkorgen blir en kö.** Var femte minut hämtas supportmejl, trådas, klassas och får ett utkast med orderfakta. Jonatan godkänner. Först då går svaret.
2. **Chatten är arbetsbänken.** Messenger är vardagsytan. Telegram är backup med kö, livestatus och flera läsjobb samtidigt. Fritext får läsa. Skrivningar väntar på en knapp.
3. **Butik, webb och annonser sitter i samma agent.** WooCommerce, WordPress, Google Ads och GA4. Produktionsfiler och kampanjändringar stannar bakom ett explicit godkännande.

```text
Kundmejl ──poll──► ärende + utkast ──människa──► svar
Messenger / Telegram ──läsverktyg──► förslag ──knapp──► order, sida eller mejl
Dashboard ──kö, marknad, webb, research, Oscar──► samma spärrar, full vy
```

## Vad agenten kan

| Funktion | Det du får | Spärren |
|----------|------------|---------|
| **Ärenden** | Mejl blir tråd, klass och orderberikat utkast. ★ betyder att ärendet ser säkert ut att godkänna snabbt. | Skicka kräver godkännande. Auto-send är **inte** inkopplat. |
| **Orderstatus** | Rätt WooCommerce-status, med order-id som valideras. | Åtkomstfel går till Oscar. |
| **Produkttext** | Beskrivning på SE/NO/DK, mall eller modell. | Publicering är ett eget steg. |
| **Supportutkast** | Klass + svarsutkast, med mall om modellen saknas. | Missbruk, juridik och kritiskt går till Oscar. |
| **Mejl** | Gmail, Outlook, Exchange Graph, IMAP, POP3 och SMTP. Gmail har webbläsar-OAuth. | Kundsvar går via ärendet, inte via fri text. |
| **Dashboard** | Kö, utkast-sandlåda, marknadsföring, webbplatser, onboarding och Oscars admin. Tailwind-gränssnitt, kommandopalett, strikt CSP. | Hemligheter bara för Oscar. |
| **Messenger** | Vardagschatt. Webhooken svarar direkt och jobbar i en kö, så långsamma utkast tappar inte händelsen. | Tom allowlist i drift nekar alla. |
| **Telegram** | Samma hjärna, plus livestatus, styrning och bakgrundsjobb. | Samma fail-closed-regel. Fritext skickar inte mejl. |
| **Google Ads + GA4** | Digest, svinn, pacing och förslag, med Woo som intäktssanning. Live-läsning finns. | Ändring av kampanj, Measurement Protocol och Merchant-skriv är avstängda. |
| **Woo / WordPress-innehåll** | Order, spårning, sidor, inlägg och inställningar, per marknad `se` / `no` / `dk`. | Innehållsskriv väntar på bekräftelse. |
| **WordPress-drift** | Sajtkarta, hälsokoll i mock, granskning av startsida eller namngiven sida, tema- och pluginarbete i en isolerad arbetsyta. | Produktionsskriv returnerar `BackupNotVerified`. Återställning är avstängd. |
| **Modellrouter** | Ett anropssätt: ChatGPT-OAuth, valfri Grok Build, valfri xAI-nyckel, OpenRouter sist. | En misslyckad route räknas som misslyckad. OpenRouter-taket är 100 USD. |
| **Webbsök och research** | Sök och asynkron djupresearch mot publika källor, med källmärkning. | Av mot nätet tills Oscar slår på process-flaggorna. PII stoppas före anropet. Egen kostnadstak, skilt från OpenRouter. |
| **SSH-hälsa** | Fast lista av läskommandon. | Av i drift tills pinnad värd, egen nyckel och ett icke-root-konto är satta. |
| **Mognad** | `status`, `soak-preflight`, KPI, kalibreringsrapport och skuggspår under null-send. | Ingen av dem får markera live-soak som klar. |

Roller: **Jonatan** läser, godkänner ärenden och marknadsförslag, och får köra webbsök och research. **Oscar** äger hemligheter, eskaleringar, modelltest, webbadmin och research-admin. **Agenten** får polla, läsa, söka och skriva utkast, och skickar inte kundmejl själv.

## Det som medvetet väntar

- Live-soak på riktig värd är **inte körd**. Bara Oscar och Jonatan får bocka av den ([`docs/PILOT_OPS.md`](docs/PILOT_OPS.md)).
- Auto-send (FU9) har räls och nödstopp, och är **inte** kopplat till poll ([`docs/CASES.md`](docs/CASES.md)).
- NO- och DK-brevlådor ligger `enabled: false` tills Oscar ger både uppgifter och ett skriftligt ja.
- Multi-tenant SaaS, FAQ-bas, Meta/TikTok-annonser och hanterad WordPress-hosting ingår inte.

## Kom igång lokalt

```bash
python -m pip install -r requirements.txt
python -m pip install -e .

# Linux/macOS
export AZOM_USE_MOCK=1
# Windows: $env:AZOM_USE_MOCK="1"

python -m ecom_ops version
python -m ecom_ops status
python -m ecom_ops --mock cases poll
python -m ecom_ops --mock support --message "Var är order 1001?"
python -m ecom_ops --mock search --query "WooCommerce HPOS hooks" --profile technical_docs
python -m ecom_ops.launcher --check --mode mock
```

Förväntat: JSON med version **3.0.0**, och mock-anrop utan nät mot Woo, mejl eller modeller.

Windows: `.\bin\launch.ps1 --check --mode mock` · Linux: `./bin/launch.sh`

Mer: [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) · CLI: [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md)

## Produktion

Rekommenderad VPS: **CX22 / CPX21**.

```bash
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash
# Uppgifter: sudo cat /root/azom-install-credentials.txt
```

Install: [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md) · storlek och nät: [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md) · Docker-data i `/app/.azom-data`, image `azom-agent:3.0`: [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md)

```bash
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

Systemd lägger kod i `/opt/azom-agent`, data i `/var/lib/azom` och loggar i `/var/log/azom`. Dashboard och bot:

```bash
./bin/start-dashboard.sh    # 127.0.0.1:8080
./bin/dedicated-bot.sh      # Telegram
./bin/cases-poll.sh         # ett poll; i drift sköter en timer det var 5:e minut
```

## Dashboard

Basic Auth: `jonatan` / `DASHBOARD_PASSWORD` och `oscar` / `DASHBOARD_OSCAR_PASSWORD`. Mock-lösen `jonatan` / `oscar` gäller bara när `AZOM_USE_MOCK=1`.

| Path | Syfte |
|------|--------|
| `/` | Start, kö-badges, probestatus |
| `/onboarding` | Checklista, hälsa, Gmail |
| `/cases` | Ärenden, ★-filter, utkast, godkänn, stäng |
| `/interact` | Utkast-sandlåda |
| `/marketing` | Ads + GA4, förslag som en människa tar |
| `/research` | Webbsök och asynkrona research-jobb (`res-*`) |
| `/wp` | Webbplatser, granskning, lokal staging |
| `/settings` | Icke-hemlig konfiguration |
| `/data/telemetry` · `/data/audit` | Förbrukning och spår |
| `/oscar` | Hemligheter, prober, eskaleringar, modelltest |
| `/oscar/behavior` | Versionerade promptar |
| `/oauth/gmail/start` | Gmail |
| `/oauth/google/start` | Marknads-Google (Oscar) |
| `/oauth/openai/start` | ChatGPT-session (Oscar) |
| `/webhooks/messenger` · `/webhooks/woo` | Meta och Woo (HMAC) |
| `/health` · `/ready` | Liv och om mejlpoll är färskt |

Messenger: [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md) · Telegram: [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md) · ärenden: [`docs/CASES.md`](docs/CASES.md)

## Tester

```bash
pytest
# CI: ruff + coverage ≥ 70%
bash tests/test_spinup.sh
```

## Dokumentation

| Doc | Innehåll |
|-----|----------|
| [`docs/FUNKTIONER.md`](docs/FUNKTIONER.md) | Alla funktioner, hur de hänger ihop (SV) |
| [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) | Version, vad som är levererat, vad som är spärrat (EN) |
| [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) | Arkitektur (EN) |
| [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) | Daglig drift och live-soak (SV) |
| [`docs/CASES.md`](docs/CASES.md) | Ärenden och FU9 (SV) |
| [`docs/MAIL_PROVIDERS.md`](docs/MAIL_PROVIDERS.md) | Mejl (SV) |
| [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md) | Messenger (SV) |
| [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md) | Telegram (SV) |
| [`docs/MARKETING_GOOGLE.md`](docs/MARKETING_GOOGLE.md) | Ads + GA4 (EN) |
| [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md) | Butiks- och innehålls-API (EN) |
| [`docs/WORDPRESS_OPS.md`](docs/WORDPRESS_OPS.md) | Webbdrift, granskning, tema och plugin (EN) |
| [`docs/MODEL_ROUTER.md`](docs/MODEL_ROUTER.md) | Modeller (EN) |
| [`docs/WEB_SEARCH_RESEARCH.md`](docs/WEB_SEARCH_RESEARCH.md) | Webbsök och djupresearch (EN) |
| [`docs/TEMPLATE_GUIDE.md`](docs/TEMPLATE_GUIDE.md) | Forka till en ny butik (EN) |
| [`docs/runbooks/`](docs/runbooks/) | När något stannar (SV) |
| [`SOUL.md`](SOUL.md) | Rösten och de hårda reglerna |
| [`AGENTS.md`](AGENTS.md) | Arbetsregler för kodagenter |
