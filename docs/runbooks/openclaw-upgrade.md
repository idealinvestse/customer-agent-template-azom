# Runbook: uppgradera OpenClaw

**Purpose:** Uppgradera den lokala OpenClaw-klienten utan att tappa config, workspace eller hemligheter.  
**Audience:** Oscar, eller den som äger den här Windows-klienten.  
**Read this first:** [`../CURRENT_STATE.md`](../CURRENT_STATE.md), [`../PILOT_OPS.md`](../PILOT_OPS.md). Den här runbooken driftsätter inte Azom.

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Klient** | `openclaw` installerat globalt med npm på den här datorn. |
| **State** | `C:\Users\Django\.openclaw` när `OPENCLAW_STATE_DIR` inte är satt. |
| **Workspace** | Sökvägen i `agents.defaults.workspace`. På den här klienten: `C:\Users\Django\clawd`. |
| **Gateway** | Processen som tar emot kanaler. Här är läget `local`, bind loopback, port `18789`. |

## Gör / gör inte

**Gör:**

1. Ta backup före paketbyte.
2. Stoppa lokal nod och, när läget är `local`, gateway-processen som håller paketfilerna öppna.
3. Installera en pinad version: `npm install -g openclaw@2026.11.6`.
4. Kör `openclaw --version` och `openclaw doctor --lint`.
5. Starta gateway och nod igen när läget är `local`. Noden ska peka på `127.0.0.1`.

**Gör inte:**

- Köra `openclaw doctor --fix` utan att ha läst vad den vill skriva. Den kan ändra modellkostnader och config.
- Aktivera den lokala Gateway-uppgiften när config säger `gateway.mode=remote`.
- Lämna den lokala uppgiften avstängd när läget är `local`. Den ska vara igång efter uppgraderingen.
- Driftsätta Azom, pusha git, eller skicka kundmejl.
- Läsa eller kopiera Grok Builds `auth.json`.
- Skriva tokens i rapporten.

## 1. Hitta installationen

```powershell
openclaw --version
Get-Command openclaw
node --version
# expect: global npm-shim under %AppData%\Roaming\npm
# Node 24 LTS; 24.22.3 was the verified patch on 2026-11-22.
```

Kontrollera också `gateway.mode` i state-configen utan att skriva ut tokenfält. På den här datorn är läget `local` och uppgiften `OpenClaw Gateway` ska vara igång.

## 2. Backup

```powershell
$BACKUP_DIR = Join-Path $env:USERPROFILE ("openclaw-backup-" + (Get-Date -Format "yyyy-MM-dd-HHmmss"))
New-Item -ItemType Directory -Force -Path $BACKUP_DIR | Out-Null
openclaw --version > "$BACKUP_DIR\version-before.txt"
openclaw doctor --lint > "$BACKUP_DIR\doctor-before.txt" 2>&1
openclaw gateway status > "$BACKUP_DIR\gateway-before.txt" 2>&1
tar -czf "$BACKUP_DIR\openclaw-agent-state.tar.gz" --exclude=node_modules --exclude='*/node_modules' --exclude=npm --exclude=cache --exclude='*/cache' --exclude=logs --exclude='*/logs' --exclude=tmp --exclude='*/tmp' --exclude=traces --exclude='*/traces' --exclude=backups --exclude='*/backups' -C "$env:USERPROFILE\.openclaw" openclaw.json .env gateway.token gateway.remote.json bot_users.yaml sites.yaml gateway.cmd gateway.vbs node.cmd node.vbs hemmalasse-node-bootstrap.ps1 agents credentials devices identity state plugin-skills workspace
tar --exclude=node_modules --exclude='*/node_modules' --exclude=.git --exclude='*/.git' --exclude=npm --exclude=cache --exclude='*/cache' --exclude=logs --exclude='*/logs' --exclude=tmp --exclude='*/tmp' -czf "$BACKUP_DIR\agent-workspace.tar.gz" -C $env:USERPROFILE "clawd"
```

State-arkivet innehåller agenternas SQLite-databaser, sessioner och credentials eftersom de ligger under `agents`, `state` och `credentials`. Kommandona lämnar npm/Node-bibliotek, `node_modules`, installerade pluginpaket, cache, loggar, temporära filer, traces, gamla backup-arkiv och Git-data utanför. Verifiera arkivlistan med `tar -tzf` och spara backupen lokalt; den innehåller fortfarande konfiguration och agentdata.

## 3. Stoppa lokal nod och gateway

Stoppa `node run`-processen från `.openclaw\node.cmd` och den lokala gateway-processen, så att npm kan byta paketfiler.

Starta båda igen när paketbytet är klart och läget är `local`. Noden ska ha `--host 127.0.0.1` i `node.cmd`. Starta inte gateway om läget är `remote`.

## 4. Uppgradera

```powershell
nvm install 24.22.3 64
nvm use 24.22.3
npm install -g openclaw@2026.11.6
node --version
openclaw --version
# expect: Node v24.22.3 or newer in the 24 LTS line; OpenClaw 2026.11.6
```

När läget är `local`, uppdatera den hanterade tjänsten från samma nvm-installation: `openclaw gateway install --force --port 18789 --runtime node --runtime-path "$env:NVM_HOME\nodejs\node.exe"`. Gateway-installationen startar tjänsten. Ett vanligt medium-token från samma användare får `Access is denied` mot uppgiftens XML i `System32\Tasks`. Kör inte kommandot som en annan användare. Ägaren uppdaterar definitionen med sitt upphöjda token.

## 5. Doctor och status

```powershell
openclaw doctor --lint
openclaw gateway status
openclaw health
```

`--lint` är skrivskyddad. I `local` ska `health` svara mot `127.0.0.1:18789`. Timeout mot en fjärr-gateway är ett värdproblem. Byt inte läge för att tysta den.

## 6. Verifiera

- Version är `2026.11.6`.
- Workspace-filerna `SOUL.md` och `AGENTS.md` finns kvar.
- Telegram, Messenger och dashboard markeras BLOCKED om gateway-värden är offline.
- Modelltest utan fallback körs bara efter uttryckligt ja. Annars BLOCKED.
- Inga tokens i den skrivna rapporten.

## 7. Rollback

```powershell
npm install -g openclaw@2026.6.10
openclaw --version
```

Återställ inte state-arkivet ovanpå en nyare config om du inte först har jämfört filerna. Dokumentera läget i stället för att packa upp blint.

Azom på `/opt/azom-agent` uppgraderas inte av de här kommandona.
