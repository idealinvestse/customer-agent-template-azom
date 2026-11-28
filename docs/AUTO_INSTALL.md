# Automatisk installation (Ubuntu 26 / 24)

**Purpose:** One-shot install som tar en tom VPS till **färdig, körande Azom-agent** med systemd-enheter, valfri HTTPS reverse proxy (Caddy/Nginx) och brandvägg.  
**Audience:** Oscar / ops som installerar på Hetzner.  
**Read this first:** [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md), [`runbooks/ubuntu26-hetzner-deploy.md`](runbooks/ubuntu26-hetzner-deploy.md), [`CURRENT_STATE.md`](CURRENT_STATE.md), därefter [`PILOT_OPS.md`](PILOT_OPS.md) för post-install soak.

**I korthet:** Ett skript tar en tom Ubuntu-VPS till dashboard, Caddy HTTPS, Telegram-bot, mejlpoll, `shop-intel`, backup och GDPR-gallring. Live-soak är ett senare steg som bara Oscar och Jonatan bockar av.

## Ordlista

| Term | Betydelse |
|------|-----------|
| **One-shot install** | Idempotent skript (`bin/install-ubuntu26.sh`) som kan köras om utan att skriva över befintliga lösenord i `.env`. |
| **`--domain` / `--proxy`** | Ställer in publik FQDN (t.ex. `dashboard.azom.no`) och installerar Caddy (default med `--domain`) eller Nginx framför `127.0.0.1:8080`. |
| **`TRUSTED_PROXY_COUNT=1`** | Gör att Flask/Waitress (`ProxyFix`) litar på `X-Forwarded-*` från lokal reverse proxy. |
| **Fail-closed** | I live-läge (`AZOM_USE_MOCK=0`) nekar Telegram/Messenger all trafik om allowlist eller actor-map saknas. |

## Snabbast (på ny server med Caddy HTTPS)

```bash
# cwd: valfri katalog på VPS (som root)
# env: körs mot live-läge (AZOM_USE_MOCK=0)
git clone https://github.com/idealinvestse/customer-agent-template-azom.git /tmp/azom
sudo bash /tmp/azom/bin/install-ubuntu26.sh --domain dashboard.azom.no --proxy caddy --no-docker
# expect: exit 0, Caddy aktiv med Let's Encrypt HTTPS på https://dashboard.azom.no och UFW med 22/80/443 (8080 stängd)
```

Eller direkt via wrapper:

```bash
# cwd: repo-roten
sudo bash bin/install.sh --domain dashboard.azom.no --proxy caddy --no-docker
# expect: delegerar till bin/install-ubuntu26.sh
```

## Vad scriptet gör (ordning)

1. Apt-update + systempaket (`python3`, `python3-venv`, `sqlite3`, `libffi-dev`, `libssl-dev`, `dnsutils`/`bind9-dnsutils`, `ufw`, `fail2ban`, `jq`, …).
2. Docker Engine (valfritt, hoppas över med `--no-docker`; hanterar Ubuntu 26-kodnamn med automatisk fallback till `noble`).
3. Systemanvändare `azom` + kataloger `/opt/azom-agent`, `/var/lib/azom` (`AZOM_DATA_DIR`), `/var/log/azom`.
4. Clone eller `rsync` av projektet till `/opt/azom-agent`.
5. `python3 -m venv .venv` + `pip install -r requirements.txt` (inkl. `paramiko`) + `pip install -e .`.
6. Skapar `/opt/azom-agent/.env` från `.env.example`, sätter prod-sökvägar, **genererar** `DASHBOARD_PASSWORD`, `DASHBOARD_OSCAR_PASSWORD` och `DASHBOARD_SECRET_KEY` om de är tomma, och sätter `AZOM_DASHBOARD_PUBLIC_URL=https://<domain>` samt `TRUSTED_PROXY_COUNT=1` när `--domain` anges.
7. Installerar alla 12 systemd-enheter (inklusive `azom-shop-intel.service` / `.timer`) och applicerar `--mock` i enheterna om `--mock` gavs.
8. Konfigurerar reverse proxy (`caddy` eller `nginx`) från `infrastructure/caddy/Caddyfile` eller `infrastructure/nginx/dashboard.azom.no.conf`, med `/metrics` blockerat med HTTP 403.
9. UFW: öppnar SSH (`--ssh-port`), samt `80/tcp` och `443/tcp` när `--domain`/`--proxy` används. Port `8080` hålls alltid stängd utåt.
10. Logrotate + unattended-upgrades + fail2ban.
11. Smoke-tester (mock-läge) och start av tjänster (`azom-bot` endast om `TELEGRAM_BOT_TOKEN` är satt).
12. Kör `bin/systemcheck.sh` och `bin/healthcheck.sh` om de finns (icke-fatal vid `--no-start` eller saknade live-secrets före onboarding).

### Systemd-enheter som installeras (12 filer)

| Unit | Roll |
|------|------|
| `azom-dashboard.service` | Flask/Waitress dashboard på `127.0.0.1:8080` (inkl. Messenger-webhook + Woo-webhook + OAuth) |
| `azom-bot.service` | Telegram long-poll — startas bara om `TELEGRAM_BOT_TOKEN` finns |
| `azom-cases-poll.service` / `.timer` | Cases poll var 5:e minut |
| `azom-daily-brief.service` / `.timer` | Daglig brief kl 07:00 |
| `azom-backup.service` / `.timer` | Daglig online-backup av alla SQLite-databaser + state-tarball kl 03:00 |
| `azom-retention-purge.service` / `.timer` | Daglig GDPR retention purge (`--actor oscar`, 90 dagar) kl 04:00 |
| `azom-shop-intel.service` / `.timer` | Daglig Webshop Intelligence (`shop-intel schedule-run`) |

Messenger har **ingen** egen systemd-unit — den körs via `/webhooks/messenger` i `azom-dashboard.service`. `AZOM_NULL_SEND` är aldrig aktiverat som default i systemd.

## Flaggor

| Flagga | Effekt |
|--------|--------|
| `--domain FQDN` | Publik dashboard-domän (t.ex. `dashboard.azom.no`); sätter `AZOM_DASHBOARD_PUBLIC_URL` och `TRUSTED_PROXY_COUNT=1` |
| `--proxy MODE` | `caddy` \| `nginx` \| `none` (default: `caddy` om `--domain` ges, annars `none`) |
| `--repo URL` | Git remote (default: `idealinvestse/customer-agent-template-azom`) |
| `--branch NAME` | Branch (default: `main`) |
| `--app-dir PATH` | Installationskatalog (default: `/opt/azom-agent`) |
| `--user NAME` | Serviceanvändare (default: `azom`) |
| `--ssh-port N` | UFW SSH-port (default: `22`) |
| `--mock` | Sätter `AZOM_USE_MOCK=1` i `.env` och i installerade systemd-enheter |
| `--no-docker` | Hoppa över Docker |
| `--no-ufw` | Hoppa över brandvägg |
| `--no-start` | Installera men starta inte services |
| `--no-smoke` | Hoppa över smoke-tester |

## Exempel

```bash
# Produktion på Hetzner med Caddy HTTPS, utan Docker
sudo bash bin/install-ubuntu26.sh \
  --domain dashboard.azom.no \
  --proxy caddy \
  --no-docker

# Endast install (CI / image-build i mock-läge)
sudo bash bin/install-ubuntu26.sh --no-start --mock --no-ufw

# Med secrets via env
sudo TELEGRAM_BOT_TOKEN=123:abc \
     DASHBOARD_PASSWORD='s3cret' \
     WOO_BASE_URL=https://azom.se \
     bash bin/install-ubuntu26.sh --domain dashboard.azom.no --no-docker
```

## Efter install

```bash
# Lösenord och callback-URL:er (endast root, mode 600)
sudo cat /root/azom-install-credentials.txt

# Editera övriga secrets
sudo nano /opt/azom-agent/.env

# Kontrollera systemstatus och hälsa
sudo /opt/azom-agent/bin/systemcheck.sh --domain dashboard.azom.no
sudo /opt/azom-agent/bin/healthcheck.sh --check-public

# Om du la till Telegram-token i efterhand
sudo systemctl enable --now azom-bot

# Lokal och publik health
curl -fsS http://127.0.0.1:8080/health
curl -fsS https://dashboard.azom.no/health
# expect: {"ok": true, ...}
```

## Gör / Gör inte

**Gör:**
- Låt port `8080` vara stängd i UFW och exponera dashboard enbart via Caddy/Nginx på HTTPS (`443`).
- Skrapa `/metrics` enbart lokalt via `http://127.0.0.1:8080/metrics` med `METRICS_SCRAPE_TOKEN`.

**Gör inte:**
- Öppna aldrig `8080/tcp` publikt i UFW.
- Sätt aldrig `AZOM_NULL_SEND=1` som default i systemd-enheter (använd `.env` vid soft-soak).
- Markera aldrig live-soak som klar automatiskt — endast Oscar och Jonatan godkänner den.

## Idempotent

Scriptet kan köras om säkert:

- apt-paket / Docker / Caddy: skippas om redan installerat
- git: `pull --ff-only` (eller `rsync` vid lokal checkout)
- venv: återskapas/uppdateras
- `.env`: fyller bara tomma secrets; behåller befintliga lösenord och `DASHBOARD_SECRET_KEY`
- systemd: kopieras om + `daemon-reload` + restart

## Ubuntu-versioner

| Version | Status |
|---------|--------|
| **26.x** | Primär target |
| **24.04 LTS** | Fullt stödd (samma script) |
| 22.04 | Best effort |

Wrapper: `bin/install.sh` detekterar distro och anropar `install-ubuntu26.sh`.

## Relaterat

- Runbook för Hetzner + Ubuntu 26.04: [`runbooks/ubuntu26-hetzner-deploy.md`](runbooks/ubuntu26-hetzner-deploy.md)
- Manuell bootstrap (äldre): `bin/bootstrap-ubuntu24.sh`
- Hetzner sizing: [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md)
- Docker overlays: [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md)
- Post-install soak: [`PILOT_OPS.md`](PILOT_OPS.md)
- systemd units: `infrastructure/systemd/`
