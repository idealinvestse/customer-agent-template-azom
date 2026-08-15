# Docker — config vs data overlays

**Purpose:** Förklara skillnaden mellan read-only config och skrivbar data i Docker/prod.  
**Audience:** Ops / developers som kör compose.  
**Read this first:** [`AUTO_INSTALL.md`](AUTO_INSTALL.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md), [`CURRENT_STATE.md`](CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Config (ro)** | YAML under `AZOM_CONFIG_DIR` — mountas `:ro` i prod compose. |
| **Data (rw)** | `AZOM_DATA_DIR` — secrets, DB, OAuth, telemetry, poll-marker. |
| **Systemd vs Docker** | Systemd använder `/var/lib/azom`; Docker compose använder `/app/.azom-data`. |

Azom skiljer **read-only config** (YAML i image/host-mount) från **skrivbar runtime-data**.

## Volume-layout

| Path | Compose | Syfte |
|------|---------|--------|
| `/app/config` | `:ro` i prod & dev | `sites.yaml`, `rbac.yaml`, `mailboxes.yaml`, `cases_ai.yaml`, … |
| `/app/.azom-data` | **read-write** | `secrets.env`, `runtime.env`, `cases.db`, OAuth-tokens, telemetry, poll-marker |
| `/app/logs` | rw (prod) | Valfri logg-mount |

**Dev:** `infrastructure/docker-compose.yml` mountar data **utan** `:ro` så secrets/settings/cases.db kan skrivas.

**Image-tag (prod):** `azom-agent:3.0` i `infrastructure/docker-compose.prod.yml` (`2.0` kvar för rollback).

## Overlay-prioritet

1. Process environment / `.env` / systemd (wins for posture keys)
2. `AZOM_DATA_DIR/runtime.env` + `secrets.env` — allowlisted integration secrets only

Laddas via `ecom_ops.runtime_env.bootstrap_runtime()` (CLI, poll, bot, dashboard).  
**Aldrig** från overlay: `AZOM_USE_MOCK`, allowlists, actor maps, kill-switches, `DASHBOARD_SECRET_KEY`.

## Settings UI vs Docker

- **Secrets** → alltid `AZOM_DATA_DIR/secrets.env` (fungerar med `config:ro`).
- **Non-secret YAML** → skrivs under `AZOM_CONFIG_DIR`. Med `:ro`-mount: redigera YAML på host eller gör bind writable på single-tenant VPS.

## CDN-assets

Dashboard-templates laddar Tailwind + Alpine från CDN. Containrar behöver egress vid första paint om assets inte vendoras (air-gapped).

## Health / smoke

```bash
# cwd: host med compose up
# expect: readiness speglar last cases-poll age
curl -s http://127.0.0.1:8080/health | jq .readiness

# Opt-in smoke (mock-safe i CI; live när AZOM_USE_MOCK=0)
AZOM_LIVE_SMOKE=1 python -m ecom_ops --mock smoke
AZOM_LIVE_SMOKE=1 AZOM_USE_MOCK=0 python -m ecom_ops smoke --live
# Do not: kör --live mot prod utan Oscars OK
```

## Relaterat

- Install: [`AUTO_INSTALL.md`](AUTO_INSTALL.md)
- Deploy: [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md)
