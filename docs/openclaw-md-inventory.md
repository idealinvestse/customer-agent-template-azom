# OpenClaw Markdown-inventering

**Purpose:** Inventering av de Markdown-filer som styr den lokala OpenClaw-klienten och Azom-agenten, gjord före uppgraderingen 2026-11-22.  
**Audience:** Oscar och coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`memory-hygiene.md`](memory-hygiene.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **KEEP** | Innehållet stämmer och ska ligga kvar. |
| **UPDATE** | Ska uppdateras i den här omgången. |
| **SPLIT** | För lång för den yta som läser den. Detaljer flyttas ut. |
| **ARCHIVE** | Inte aktiv sökväg. Lämnas kvar. |
| **DELETE_CANDIDATE** | Kandidat för borttagning. Inget raderas automatiskt. |
| **MISSING** | Förväntad fil finns inte. |

Inga API-nycklar, tokens eller OAuth-värden står i den här filen. Styrfiler i workspace innehåller inga live-hemligheter. `openclaw.json` gör det, enligt doctor, och den är inte Markdown.

Node_modules under `clawd\plugins\serper-provider` är tredjepartsdokumentation och ingår inte i tabellen.

## Var filerna bor

| Sak | Sökväg |
|-----|--------|
| npm-klient | `%AppData%\Roaming\npm\node_modules\openclaw` |
| State | `C:\Users\Django\.openclaw` |
| Aktivt workspace | `C:\Users\Django\clawd` |
| Oanvänd kopia | `C:\Users\Django\clawd-onyx` |
| Tom default-workspace | `C:\Users\Django\.openclaw\workspace` |
| Azom-repo | `E:\git\customer-agent-template-azom` |

Gateway-läget är `local` sedan operatören bad om en lokal gateway. Uppgiften `OpenClaw Gateway` lyssnar på `127.0.0.1:18789`. Före det bytet var läget `remote` och uppgiften `Disabled`. Fjärrvärden `vps-agent-1` var offline.

Bootstrap-taket är 3 500 tecken per fil och 8 000 totalt. `AGENTS.md` och `TOOLS.md` i clawd trunkerades redan före den här omgången. Därför kortas de, även om 20 000-teckensgränsen inte var nådd.

## Aktivt workspace

| Fil | Syfte | Status | Problem | Rekommenderad åtgärd | Prioritet |
|-----|-------|--------|---------|----------------------|-----------|
| `clawd\SOUL.md` | Identitet | UPDATE | Engelsk standardmall. Inget namn, inga Azom-stopp. | Ersätt med kort svensk SOUL. | Hög |
| `clawd\AGENTS.md` | Arbetsregler | SPLIT | 4 783 tecken, 27 % trunkeras. Saknar router-, backup- och gitregler. | Korta filen. Flytta gruppchatt och hjärtslag till `docs\workspace-playbook.md`. | Hög |
| `clawd\TOOLS.md` | Lokala noter | SPLIT | 4 859 tecken, trunkeras. Pekar på `~/.clawdbot/openclaw.json`. Upprepar nyckelkopia. | Korta. Rätt sökväg. Nyckelsteg bara i SSH-skillen. | Hög |
| `clawd\IDENTITY.md` | Namn | UPDATE | Tom mall. 0 tecken injicerade för att budgeten tog slut. | Fyll namn OpenClaw. Håll filen kort. | Medel |
| `clawd\USER.md` | Användare | UPDATE | Tom mall. | Sätt Django. Inga hemligheter. | Medel |
| `clawd\HEARTBEAT.md` | Hjärtslag | KEEP | Bara kommentarer, med flit, så att hjärtslag inte gör betalda anrop. | Låt den vara tom på uppgifter. | Låg |
| `clawd\memory\2026-07-11.md` | Dagminne | ARCHIVE | Inte driftfakta. Vuxet uppdrag. | Förslag i [`memory-cleanup-proposals.md`](memory-cleanup-proposals.md). Radera inte. | Medel |
| `clawd\MEMORY.md` | Långt minne | MISSING | Skapas inte av OpenClaw automatiskt. | Skapa inte en tom fil. | Låg |
| `clawd\docs\LLM_MODEL_CATALOG.md` | Modellista | UPDATE | Genererad 2026-06-28. | Regenerera senare, inte för hand. | Låg |
| `clawd\skills\disk-stadare\SKILL.md` | Diskstäd | UPDATE | Har preview och JA. Saknar kort verifieringsrad mot fel maskin. | Lägg till stopp och verify. | Medel |
| `clawd\skills\disk-stadare\references\windows-cleanup.md` | Referens | KEEP | Beskriver undo. | Oförändrad. | Låg |
| `clawd\skills\hemmalasse-ssh\SKILL.md` | SSH | UPDATE | Har verify. Berättar var privat nyckel ska ligga. | Lägg till stopp: skriv aldrig ut nyckeln. | Medel |
| `clawd\skills\video-downloader\SKILL.md` | Nedladdning | UPDATE | Saknar stopp för minderåriga och mot att publicera filer. | Lägg till stopp. Utöka inte sajtlistan. | Hög |
| `clawd\skills\video-downloader\references\supported-sites.md` | Sajter | KEEP | Lista över befintliga källor. | Oförändrad. | Låg |
| `clawd\CLAUDE.md` | Annan agent | MISSING | Finns inte. | Skapa inte. | Låg |
| `clawd\GEMINI.md` | Annan agent | MISSING | Finns inte. | Skapa inte. | Låg |
| `clawd\README.md` | Intro | MISSING | Workspace har ingen README. | Pekare i `docs\workspace-playbook.md` räcker. | Låg |

## Inte aktivt workspace

| Fil | Syfte | Status | Problem | Rekommenderad åtgärd | Prioritet |
|-----|-------|--------|---------|----------------------|-----------|
| `clawd-onyx\SOUL.md` | Mall | ARCHIVE | Standardtext. Inte configens workspace. | Lämna. | Låg |
| `clawd-onyx\AGENTS.md` | Mall | ARCHIVE | Samma. | Lämna. | Låg |
| `clawd-onyx\BOOTSTRAP.md` | Första körning | ARCHIVE | Säger åt agenten att radera sig själv. Inte aktiv. | Lämna. | Låg |
| `clawd-onyx\IDENTITY.md` | Mall | ARCHIVE | Tom. | Lämna. | Låg |
| `clawd-onyx\USER.md` | Mall | ARCHIVE | Tom. | Lämna. | Låg |
| `clawd-onyx\TOOLS.md` | Mall | ARCHIVE | Tom exempel. | Lämna. | Låg |
| `clawd-onyx\HEARTBEAT.md` | Mall | ARCHIVE | Kort. | Lämna. | Låg |
| `.openclaw\workspace\AGENTS.md` | Default | ARCHIVE | Nästan tom. Inte configens workspace. | Lämna. | Låg |

Inga av de här är `DELETE_CANDIDATE` i den här omgången. Borttagning kräver ett eget ja.

## Azom-repo

| Fil | Syfte | Status | Problem | Rekommenderad åtgärd | Prioritet |
|-----|-------|--------|---------|----------------------|-----------|
| `SOUL.md` | AzomOps-röst | UPDATE | Identiteten är rätt. En rad var OpenRouter-tung. Saknade minneshygien. | Behåll identitet och HITL. Peka på routern. | Hög |
| `Agents.md` | Kodagent | UPDATE | 8 631 tecken, under 20 000. Saknade router, drift, uppgradering, git, minne. | Lägg till korta sektioner med länkar. | Hög |
| `skills\ecom-ops\SKILL.md` | Skillkort | UPDATE | Exempel kunde läsas som live-körning. | Peka på `--mock` och de nya skillsen. | Medel |
| `docs\TELEGRAM_OPENCLAW.md` | Telegram | UPDATE | Diagrammet sa OpenRouter-phrasing. | Säg modellrouter, OpenRouter som fallback. | Medel |
| `docs\MESSENGER_OPENCLAW.md` | Messenger | KEEP | HITL och fail-closed stämmer med CURRENT_STATE. | Oförändrad. | Låg |
| `docs\MODEL_ROUTER.md` | Router | KEEP | Stämmer med kodens ordning. | Oförändrad. | Låg |
| `docs\runbooks\model-oauth.md` | OAuth | UPDATE | Saknade en samlad token-safety-lista. | Lägg till avsnitt 10. | Medel |
| `docs\CURRENT_STATE.md` | Sanning | UPDATE | Saknade pekare till nya runbooks. | Lägg pekare. Ändra inte version eller soak. | Låg |
| `docs\CASES.md` | Ärenden | KEEP | FU9 är inte inkopplad. | Oförändrad. | Låg |
| `docs\PILOT_OPS.md` | Soak | KEEP | Soak är mänskligt. | Oförändrad. | Låg |
| `docs\runbooks\cases-db-corrupt.md` | Incident | KEEP | Oförändrad den här omgången. | Oförändrad. | Låg |
| `docs\runbooks\dashboard-rate-limited.md` | Incident | KEEP | Oförändrad. | Oförändrad. | Låg |
| `docs\runbooks\gmail-oauth-revoked.md` | Incident | KEEP | Egen OAuth-klient, inte modellroutern. | Oförändrad. | Låg |
| `docs\runbooks\mail-poll-stuck.md` | Incident | KEEP | Oförändrad. | Oförändrad. | Låg |
| `docs\runbooks\openrouter-budget-exhausted.md` | Budget | KEEP | Cap gäller OpenRouter. | Oförändrad. | Låg |
| `docs\runbooks\woo-webhook-disabled.md` | Woo | KEEP | Oförändrad. | Oförändrad. | Låg |
| `docs\memory-hygiene.md` | Minne | MISSING | Saknades. | Skapa. | Hög |
| `docs\memory-cleanup-proposals.md` | Förslag | MISSING | Saknades. | Skapa. Radera inget. | Hög |
| `docs\runbooks\openclaw-upgrade.md` | Uppgradering | MISSING | Saknades. | Skapa. | Hög |
| `docs\runbooks\gateway-recovery.md` | Gateway | MISSING | Saknades. | Skapa. | Hög |
| `docs\runbooks\secret-leak-response.md` | Läcka | MISSING | Saknades. | Skapa. | Hög |
| `skills\openclaw-upgrade\SKILL.md` | Skill | MISSING | Saknades. | Skapa. | Hög |
| `skills\model-router-health\SKILL.md` | Skill | MISSING | Saknades. | Skapa. | Hög |
| `skills\oauth-incident\SKILL.md` | Skill | MISSING | Saknades. | Skapa. | Hög |
| `skills\azom-safe-ops\SKILL.md` | Skill | MISSING | Saknades. | Skapa. | Hög |

Övriga levande docs i `docs\` (install, Woo, marketing, developer guide, CLI) lämnas. De motsäger inte den här uppgraderingen och är inte OpenClaw-klientens identitetsfiler.

## Sånt inventeringen letade efter

| Fynd | Var |
|------|-----|
| Föråldrat modellval | Katalog 2026-06-28. Config är källan. |
| Föråldrad OAuth-text | Azom-runbooken hade ägarskap men ingen samlad token-lista. |
| OpenRouter som enda väg | Telegram-diagrammet. Lokala TOOLS listar tre leverantörer, inte bara OpenRouter. |
| Dublett SOUL/AGENTS | Azom SOUL behåller HITL. Routerdetaljer pekar på MODEL_ROUTER. |
| För lång AGENTS | clawd trunkeras vid 3 500. Azom-filen är under 20 000. |
| Hemligheter i Markdown | Inga live-värden i styrfilerna. |
| Mejl eller order utan godkännande | Azom SOUL förbjuder det. clawd SOUL gjorde det inte uttryckligen. |
| Saknad verifiering | Nya skills får verify-steg. |
| Föråldrad sökväg | `~/.clawdbot/openclaw.json` i TOOLS. |
| Destruktiva kommandon | disk-stadare kräver preview och JA. SSH-skillen får ett nyckelstopp. |
