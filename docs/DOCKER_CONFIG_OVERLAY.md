# Docker config & data overlays

Azom separates **read-only config** (YAML in the image/host mount) from **writable runtime data** (`AZOM_DATA_DIR`).

## Volume layout

| Path | Compose | Purpose |
|------|---------|---------|
| `/app/config` | `:ro` in prod & dev | `sites.yaml`, `rbac.yaml`, `mailboxes.yaml`, `cases_ai.yaml`, … |
| `/app/.azom-data` | **read-write** | `secrets.env`, `runtime.env`, `cases.db`, OAuth tokens, telemetry, poll marker |
| `/app/logs` | rw (prod) | Optional log mount |

**Dev fix (P7):** `infrastructure/docker-compose.yml` mounts `azom-data` **without** `:ro` so Oscar secrets / settings overlays / cases DB can write.

## Overlay precedence

1. Process environment / `.env`
2. `AZOM_DATA_DIR/runtime.env` (mock toggle, `MAIL_PROVIDER`, …)
3. `AZOM_DATA_DIR/secrets.env` (Oscar UI secrets; chmod 600)

Loaded every request via `settings_store.apply_env_overlays()`.

## Settings UI vs Docker

- **Secrets** → always `AZOM_DATA_DIR/secrets.env` (works with `config:ro`).
- **Non-secret YAML** (`sites.yaml`, `limits.yaml`, …) → written under `AZOM_CONFIG_DIR`. With `:ro` config mounts, prefer editing YAML on the host or changing compose to a writable bind for `/app/config` on single-tenant VPS installs.

## CDN assets

Dashboard templates load Tailwind + Alpine from CDN. Containers need egress for first paint unless you vendor those assets later (air-gapped).

## Health / smoke

```bash
# Readiness includes last cases-poll age (written by azom-cases-poll)
curl -s http://127.0.0.1:8080/health | jq .readiness

# Opt-in smoke (mock-safe in CI; live when AZOM_USE_MOCK=0)
AZOM_LIVE_SMOKE=1 python -m ecom_ops --mock smoke
AZOM_LIVE_SMOKE=1 AZOM_USE_MOCK=0 python -m ecom_ops smoke --live
```
