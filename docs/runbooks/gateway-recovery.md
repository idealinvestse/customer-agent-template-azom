# Runbook: Gateway svarar inte

**Purpose:** Återställa OpenClaw Gateway utan att gissa sökväg eller byta läge av misstag.  
**Audience:** Oscar.  
**Read this first:** [`openclaw-upgrade.md`](openclaw-upgrade.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Local mode** | `gateway.mode=local`. På den här klienten lyssnar gateway på `127.0.0.1:18789`. |
| **Remote mode** | Klienten pratar med en gateway på en annan värd. |
| **Local task** | Schemalagd uppgift `OpenClaw Gateway` på Windows. Den ska vara igång i local mode och avstängd i remote mode. |

## Gör inte

- Aktivera den lokala uppgiften när `gateway.mode` är `remote`, om inte läget medvetet byts till `local`.
- Stäng av den lokala uppgiften när `gateway.mode` redan är `local`.
- Köra `doctor --fix` som första steg.
- Skriva ut gateway-token eller provider-nycklar från config.

## 1. Gateway startar inte

1. Kör `openclaw gateway status` och `openclaw doctor --lint`.
2. Läs bara statusraderna: runtime, bind, port, probe.
3. På den här klienten är läget `local`. Uppgiften `OpenClaw Gateway` ska vara `Running`, och `127.0.0.1:18789` ska svara. Noden `hemmalasse` ska köras med `--host 127.0.0.1`. Argumentet i `node.cmd` styr målet. Miljövariabeln `OPENCLAW_GATEWAY_HOST` räcker inte.
4. Om läget fortfarande är `remote` och fjärrvärden är offline: starta den värden. Byt inte till lokal gateway utan ett uttryckligt beslut.

## 2. Portkonflikt

1. Se vilken process som lyssnar på gateway-porten. Förväntad port i config är `18789`.
2. Om en annan process äger porten: stoppa inte den processen förrän du vet vad den är.
3. Byt inte port i config och i tjänsteargumenten var för sig.

## 3. Configfel

1. `openclaw doctor --lint` visar felet utan att skriva om filen.
2. Jämför med `openclaw.json.last-good` och de befintliga `.bak`-filerna.
3. Återställ bara den trasiga nyckeln. Packa inte upp hela backupen ovanpå en nyare fil.

## 4. Pluginfel

1. Doctor visar plugin-fel med namn.
2. Stäng av det pluginet i config bara om felet hindrar start och pluginet inte är telegram eller den lokala modellvägen du behöver.
3. Installera inte om alla plugins i ett svep.

## 5. Databaslås

1. Symptom: sqlite `database is locked` i gateway- eller nodloggen.
2. Stoppa nod och gateway kontrollerat. Kopiera `openclaw.sqlite` tillsammans med `-wal` och `-shm`.
3. Starta en process i taget. Radera inte `-wal` medan en process kan ha skrivningar.

## 6. Rollback

1. Pinna tillbaka klienten med `npm install -g openclaw@2026.6.10` om det nya paketet inte startar.
2. Lämna state-katalogen orörd om doctor inte har skrivit config.
3. Om config hann skrivas: återställ från backupens `openclaw-config.tar.gz` till en kopia, jämför, och be Oscar godkänna bytet.
