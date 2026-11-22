# OpenClaw-uppgradering 2026-11-22

**Purpose:** Punktrapport för den lokala OpenClaw-klienten. Inte en ny sanningskälla för Azom.  
**Audience:** Oscar och den som äger Windows-klienten.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md). Vid konflikt vinner den filen.

Inga tokens, API-nycklar eller OAuth-värden finns i den här rapporten. Azom är inte driftsatt. Dokumentationsgrenen `docs/openclaw-local-upgrade` är pushad till origin och är inte mergad till `main`.

Avsnitt 1–21 är historiken. Aktuellt läge står i avsnitt 22 och 23.

## 1. Före-version

OpenClaw **2026.6.10** (`aa69b12`). Node **v24.13.0**.

## 2. Efter-version

OpenClaw **2026.11.6** (`06afb10`). Aktiv Node är **v24.16.0** via nvm. Paketet vägrade starta på 24.13.0.

## 3. Installationsmetod

Global npm: `%AppData%\Roaming\npm\node_modules\openclaw`.

Inte en repo-klon, inte Docker, inte systemd. Windows schemalagda uppgifter:

| Uppgift | Läge före och efter |
|---------|---------------------|
| OpenClaw Gateway | Disabled |
| OpenClaw Node | Ready |

`gateway.mode` är `remote`. Workspace är `C:\Users\Django\clawd`. State är `C:\Users\Django\.openclaw`.

## 4. Backup

`C:\Users\Django\openclaw-backup-2026-11-22-002740`

Arkivet innehåller hemligheter. Låt det ligga lokalt.

| Fil | Innehåll |
|-----|----------|
| `version-before.txt` / `doctor-before.txt` / `gateway-before.txt` | Läget före paketbyte |
| `openclaw-state.tar.gz` | Hela state-katalogen |
| `openclaw-config.tar.gz` | Config före doctor |
| `openclaw.json.before-doctor-fix` | Config precis före `doctor --fix` |
| `openclaw-workspace.tar.gz` | Workspace utan `node_modules` och `.git` |
| `openclaw-clawd-onyx.tar.gz` | Oanvänd mallkopia |
| `databases\` | Båda SQLite-filerna plus `-wal` och `-shm` |

Den första workspace-packningen fastnade på `node_modules` och avbröts. Den sparade workspace-filen är den snabba kopian.

## 5. Markdown-inventering

[`openclaw-md-inventory.md`](openclaw-md-inventory.md). 48 styrfiler. `node_modules` ingick inte.

## 6. Ändrade filer

Azom-repot, gren `docs/openclaw-local-upgrade`, pushad som `e66c56f` och inte mergad till `main`:

- `AGENTS.md`, `SOUL.md`, `README.md`
- `docs/CURRENT_STATE.md`, `docs/README.md`, `docs/TELEGRAM_OPENCLAW.md`
- `docs/runbooks/README.md`, `docs/runbooks/model-oauth.md`
- `skills/ecom-ops/SKILL.md`
- Nya docs och runbooks i listan nedan
- Nya skills i listan nedan

OpenClaw-workspace `C:\Users\Django\clawd`:

- `SOUL.md`, `AGENTS.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
- `docs/workspace-playbook.md`
- Stopp och verify i `disk-stadare`, `hemmalasse-ssh`, `video-downloader`

Nodstartare: `.openclaw\node.cmd` och `node.vbs` säger nu 2026.11.6. Noden startades igen.

`doctor --fix` tog bort pensionerade config-nycklar och migrerade SQLite. Primärmodell, workspace och `gateway.mode=remote` finns kvar. Doctor installerade plugin-paketen cerebras och mistral. Deras state-migrering är inte klar.

## 7. Arkiverade filer

Inga. Förslag ligger i [`memory-cleanup-proposals.md`](memory-cleanup-proposals.md).

## 8. Nya skills

Fyra skills, både i Azom-repot och i `clawd\skills`:

- `openclaw-upgrade`
- `model-router-health`
- `oauth-incident`
- `azom-safe-ops`

## 9. Uppdaterade runbooks

- [`runbooks/model-oauth.md`](runbooks/model-oauth.md) avsnitt 10
- [`runbooks/openclaw-upgrade.md`](runbooks/openclaw-upgrade.md)
- [`runbooks/gateway-recovery.md`](runbooks/gateway-recovery.md)
- [`runbooks/secret-leak-response.md`](runbooks/secret-leak-response.md)
- [`memory-hygiene.md`](memory-hygiene.md)

## 10. Config-ändringar

Manuellt: bara versionsraden i nodstartaren.

`openclaw doctor --fix --non-interactive` migrerade state och släppte pensionerade nycklar, bland annat `meta.lastTouchedAt`, LM Studio `contextTokens`/`contextWindow`, flera pruning- och compaction-fält, samt `agents.defaults.heartbeat` och `gateway.tailscale.resetOnExit`.

Gateway-uppgiften lämnades Disabled. Doctor försökte starta den och fick avslag, vilket är det avsedda läget så länge gateway är remote.

## 11. Kommandon

```powershell
nvm use 24.16.0
npm install -g openclaw@2026.11.6
openclaw doctor --lint
openclaw doctor --fix --non-interactive
openclaw doctor --lint
openclaw gateway status
```

Första `npm install` på Node 24.13.0 avbröts av paketets motor-krav. Versionen var då fortfarande 2026.6.10.

## 12. Doctor

Inte grön.

Första `--lint` hade schemafel på pensionerade nycklar. Efter `--fix` är de borta. Andra `--lint` slutade med kod 1 och bara varningar:

- `AGENTS.md` kan fortfarande trunkeras i startup
- gammal Browser Relay-auth
- Node 24.16.0 ligger under rekommenderade 24.22.1
- klartexthemligheter i config (gateway-token och Telegram-token)

Doctor avslutade med att den inte kunde starta den avstängda Gateway-uppgiften. Plugin-migrering för cerebras och mistral är `pending`.

## 13. Gateway

Lokal uppgift: **Disabled**. Ingenting lyssnar på port 18789. `openclaw health` tog timeout.

Fjärrvärden `vps-agent-1` var offline i Tailscale, senast sedd för ungefär 15 dagar sedan. Den lokala klienten kan inte starta den värden.

## 14. Kanaler

| Kanal | Status |
|-------|--------|
| Telegram | BLOCKED. Kanalen är aktiverad i config, men gateway svarar inte. Inget testmeddelande skickades. |
| Messenger | BLOCKED. Inte en kanal i den här OpenClaw-configen. Azom-Messenger ligger på Azom-dashboarden och är inte driftsatt här. |
| Dashboard `127.0.0.1:18789` | BLOCKED. Connection refused. |

## 15. Modellrouter

Azom-enhetstester, utan nätanrop: `tests/test_model_router.py`, `tests/test_model_session_store.py`, `tests/test_model_dashboard.py`. **13 tester, alla gröna.**

Live-test av ChatGPT, xAI, Grok Build och OpenRouter: **BLOCKED**. Inget betalt anrop gjordes.

Lokal primärmodell i config är oförändrad: LM Studio-aliasen som stod där före uppgraderingen.

## 16. OAuth

**BLOCKED.** Ingen live-status lästes. Grok Builds `auth.json` öppnades inte. Gmail- och Codex-klienter återanvändes inte.

## 17. Tester

| Kontroll | Resultat |
|----------|----------|
| `openclaw --version` | 2026.11.6 |
| Modellrouter-tester | 13 gröna |
| `doctor --lint` | kod 1, varningar, inga schemafel efter migrering |
| `gateway status` | stopped, Disabled |
| `health` | timeout |
| Port 18789 | lyssnar inte |

## 18. Blockers

1. Fjärr-gatewayn är offline. Lokal Gateway är avstängd med flit.
2. Node 24.22.1 eller nyare rekommenderas. 24.16.0 räcker för att paketet ska starta.
3. Plugin-migrering för cerebras och mistral är inte klar.
4. Klartexthemligheter ligger kvar i config.
5. Live-kanaler, dashboard, OAuth och betalda modelltester är inte körda.

## 19. Manuella steg

1. Starta `vps-agent-1` och kör sedan `openclaw health` därifrån eller mot den. Aktivera inte den lokala Gateway-uppgiften om läget ska förbli `remote`.
2. Installera Node 24.22.1 eller nyare och kör `nvm use` mot den.
3. När gateway medvetet får vara nere: `openclaw update repair` och sedan `openclaw doctor --fix` för cerebras/mistral. Doctor kommer att försöka starta Gateway-uppgiften och misslyckas så länge den är Disabled.
4. Flytta klartexthemligheter till SecretRefs i ett underhållsfönster.
5. Ta ställning till [`memory-cleanup-proposals.md`](memory-cleanup-proposals.md). Inget är arkiverat.
6. Live-testa Telegram, dashboard och modellrader först när värden är uppe och ett betalt anrop är godkänt.
7. Commit och push av grenen `docs/openclaw-local-upgrade` är gjorda (`e66c56f` på origin). Grenen är inte mergad till `main`. Se avsnitt 23.

## 20. Nästa iteration

Håll klienten på 2026.11.6. Nästa arbete är värden, Node-patchen och hemlighetsflytten. Inte en ny paketinstallation.

**Rekommendation: EJ REDO** för npm-uppgraderingen. Gateway-värden är ett separat manuellt steg.

## 21. Senare: lokal gateway

Operatören bad därefter att systemet skulle testas lokalt, inte mot den offline fjärrvärden. Docker Desktop-motorn svarade inte, så den befintliga Windows-installationen startades i stället.

| Kontroll | Resultat |
|----------|----------|
| `gateway.mode` | `local`, bind loopback, port `18789` |
| Uppgift `OpenClaw Gateway` | `Running`, pid på `127.0.0.1:18789` |
| `openclaw health` | kod 0, event loop ok, Telegram configured |
| `openclaw gateway status` | running, connectivity ok, version 2026.11.6 |
| Dashboard `http://127.0.0.1:18789/` | HTTP 200 |
| `openclaw channels status` | Telegram enabled, configured, running, connected, mode polling. Inget meddelande skickades. |
| Nod `hemmalasse` | `--host 127.0.0.1`, paired, connected, approved |
| Messenger | Fortfarande utanför den här OpenClaw-configen |
| Betalda modell- och OAuth-anrop | Inte körda |

Config före lägesbytet ligger i samma backup som `openclaw.json.before-local-mode`.

Node, plugin-reparation, commit och push är gjorda efter det här avsnittet. Se avsnitt 22 och 23.

## 22. Efterföljande underhåll 2026-11-22

Det här avsnittet ersätter de tidigare manuella stegen för Node och plugin-reparation.

| Kontroll | Resultat |
|----------|----------|
| Node | **24.22.3** installerad och aktiv via nvm. Node.js publicerar detta som en 24.x LTS-patch. |
| OpenClaw CLI | **2026.11.6** ominstallerad i den aktiva Node-installationen. |
| Plugin repair | `openclaw update repair --no-restart --json --yes` avslutade med varningar om äldre service-definition. `openclaw doctor --fix` slutfördes; `doctor --post-upgrade --json` visar **0 findings**. |
| Gateway | Task `OpenClaw Gateway` kör, RPC svarar och port 18789 lyssnar på loopback. |
| Telegram | Konfigurerad, kör och ansluten via polling. Inget meddelande skickades. |
| Dashboard | `http://127.0.0.1:18789/` svarar HTTP 200. |
| Modell/OAuth | Inget betalt modelltest eller OAuth-test kördes. |

### Agentbackup

Kuraterad backup finns i `C:\Users\Django\openclaw-agent-backup-2026-11-22`. Den innehåller OpenClaw-konfiguration och startskript, agentdatabaser/sessioner/auth-data, channel credentials, device/identity-state, plugin skills och `C:\Users\Django\clawd`. Arkivlistorna verifierades: inga `node_modules`, npm-bibliotek, cache, loggar, temp, traces, installerade pluginpaket eller Git-data.

### SecretRefs och kvarvarande blockerare

Sex configfält använder strukturerade `env`-SecretRefs: Gateway token, remote token, Telegram bot token, samt Cerebras-, Mistral- och NVIDIA-API-nycklar. Duplicerade nyckelrader för OpenRouter, NVIDIA, Cerebras, FAL och Hugging Face togs bort från `.env`; motsvarande befintliga user environment-värden finns kvar. Vid det här läget fanns **3 plaintext credentials** kvar i den gemensamma auth-profilstoren. De är migrerade i avsnitt 23.

OpenClaw 2026.11.6 `secrets configure` kan bara ändra en agents lokala store. `secrets apply` med en plan nådde både agent-storen och `state/openclaw.sqlite`. Värdena flyttades inte till OpenClaws SQLite Secret Store.

`doctor --fix` kunde inte uppdatera Task Scheduler XML (`Access is denied`) från ett medium-token. Definitionen uppdaterades senare med ägarens upphöjda token. Se avsnitt 23.

Den breda arkivfilen `C:\Users\Django\openclaw-backup-2026-11-22-131040\openclaw-state.tar.gz` är borttagen. Använd `C:\Users\Django\openclaw-agent-backup-2026-11-22`.

## 23. Stängda punkter 2026-11-22

### Task Scheduler

`openclaw gateway install --force --port 18789 --runtime node` med ägarens upphöjda token skrev om uppgiften. Ett medium-token från samma användare får fortfarande `Access is denied` mot XML-filen. Installationen kördes inte som ett annat konto.

| Fält | Nu |
|------|----|
| `LogonType` | `S4U` |
| Boot-trigger | finns |
| `RestartOnFailure` | `PT1M`, count 3 |
| Kommando | `C:\WINDOWS\System32\cmd.exe` |
| Arbetskatalog | `C:\Users\Django\.openclaw` |

`gateway status` rapporterar **0** `definitionDrift`. Uppgiften är `Running`. `127.0.0.1:18789` lyssnar, `openclaw health` har event loop ok, Telegram är konfigurerad och ansluten, och dashboarden svarar HTTP 200. Inget meddelande skickades.

Auditen är inte helt grön. En rekommenderad finding återstår: `gateway-managed-env-embedded`. Installern skriver hanterade miljövärden i `gateway.cmd`. Processgranskningen kan inte matcha lyssnarens `node.exe` mot uppgiftens `cmd.exe`, så runtime-status visas som `unknown` trots att uppgiften kör.

### Auth-profiler

| Profil | Användning | Åtgärd |
|--------|------------|--------|
| `lmstudio:default` | Primärmodell | `keyRef` mot `LMSTUDIO_API_KEY` |
| `groq:default` | Första fallback | `keyRef` mot `GROQ_API_KEY` |
| `openrouter:default` | Tillåten i modellistan, inte i fallback-kedjan | Behållen. `keyRef` mot `OPENROUTER_API_KEY` |

`GROQ_API_KEY` och `OPENROUTER_API_KEY` matchade redan användarmiljön. `LM_API_KEY` är platshållaren `lm-studio` och matchar inte den nyckel profilen använde. Den befintliga profilnyckeln kopierades till `LMSTUDIO_API_KEY` i användarmiljön. Värdet skrevs inte ut.

`openclaw secrets apply` skrev `keyRef` i agent-storen och tog bort klartext för LM Studio och OpenRouter i den gemensamma storen. Groq saknar `models.providers`-block, så apply lämnade den gemensamma klartexten. Den raden fick samma `keyRef` medan gateway var stoppad. En återställningskopia under `openclaw-media-persistence-recovery-*` fick samma form.

`openclaw secrets audit --check`: **0** plaintext, **0** olösta refs. Två `REF_SHADOWED`-varningar återstår. Auth-profilerna för LM Studio och OpenRouter går före samma env-ref i `openclaw.json`. Inget betalt anrop gjordes.

### Arkiv

`openclaw-state.tar.gz` (442122240 byte) är borttagen. Den kuraterade backupen lästes om: 474 state-poster och 155 workspace-poster, utan `node_modules`, npm, cache, loggar, temp, traces eller `.git`.

### Minnesförslag

Inte utförda. Beslutet står i [`memory-cleanup-proposals.md`](memory-cleanup-proposals.md). Arkivering kräver ett särskilt ja.

### Inte kört

Betalda modelltester och OAuth-tester är inte körda. Messenger ingår inte i den här OpenClaw-configen och verifierades inte här.

### Standardmodell

Primärmodellen är `openrouter/deepseek/deepseek-v4.1-flash-1017` (DeepSeek V4.1 Flash, ögonblicksbilden från 2026-10-17). OpenRouter-routingen sorterar på `price`. Den tidigare lokala LM Studio-modellen ligger först bland fallbacks. Inget modell-anrop gjordes för att prova bytet.

### Azom, fortfarande öppet

A1 live soak, KPI-baslinje, liveklassificering med 20–50 redigerade exempel och veckovis driftkadens ägs av Oscar och Jonatan. De är inte markerade klara.
