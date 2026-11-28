# Deploy: Ubuntu 26.04 / 24.04 LTS på Hetzner VPS

**Purpose:** Sizing, nätverk, reverse proxy och deploy-steg för Azom på Hetzner (Ubuntu 26.04 / 24.04).  
**Audience:** Oscar / ops.  
**Read this first:** [`AUTO_INSTALL.md`](AUTO_INSTALL.md), [`runbooks/ubuntu26-hetzner-deploy.md`](runbooks/ubuntu26-hetzner-deploy.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md), [`PILOT_OPS.md`](PILOT_OPS.md).

**I korthet:** Piloten körs på en Hetzner VPS med Ubuntu 26.04/24.04 (`dashboard.azom.no`, `95.217.216.3`). Installationsskriptet sätter upp venv, systemd, Caddy HTTPS och UFW på några minuter. Live-soak och FU9 auto-send kräver separata manuella godkännanden av Oscar och Jonatan.

## Rekommenderad VPS-storlek

| Scenario | Hetzner typ | Spec | ~pris/mån | När |
|----------|-------------|------|-----------|-----|
| **Pilot (rekommenderad start)** | **CX22** eller **CPX21** | 2 vCPU, **4 GB RAM**, 40 GB NVMe | ~€4–7 | En kund-agent, dashboard, bot, mail, cron, Caddy |
| Budget / minimal | CX22 shared / CX11 om tillgänglig | 2 vCPU, 2 GB RAM | ~€3–4 | Endast CLI + cron, ingen bot |
| Headroom (fler integrationer / headless Chrome senare) | CPX31 | 4 vCPU, 8 GB RAM | ~€12–15 | Browser-automation, fler workers |

### Varför 4 GB (inte 2 GB)?

V1/V3-workloads (CLI, Flask/Waitress dashboard, Telegram long-poll, mail IMAP/SMTP, Caddy) är lätta, men Ubuntu 26 + journald + pip/apt-uppdateringar behöver marginal:

| Process | RAM ungefär |
|---------|-------------|
| OS + systemd + Caddy | 450–750 MB |
| Dashboard (Waitress/Flask) | 60–120 MB |
| Telegram bot | 40–80 MB |
| Docker (om används) | 100–300 MB |
| Spikes (pip, compose build, apt) | +500 MB |

**2 GB fungerar** i bare-metal (venv + systemd utan Docker), men blir trångt vid image-builds.  
**4 GB (CX22/CPX21) är sweet spot** för single-tenant pilot med marginal.

### Region & image

- **Location:** Falkenstein (`fsn1`), Nuremberg (`nbg1`) eller Helsingfors (`hel1`) — låg latens till SE/NO/DK
- **Image:** Ubuntu 26.04 (primär) eller Ubuntu 24.04 LTS
- **Network:** IPv4 + IPv6, DNS A-record `dashboard.azom.no -> 95.217.216.3`, firewall i Hetzner Cloud + UFW
- **Disk:** 40 GB räcker (logs roteras; SQLite-backuper gallras efter 30 dagar)

---

## Snabbstart (one-shot auto-install med Caddy HTTPS)

```bash
# cwd: på Hetzner VPS som root
# env: sätter AZOM_USE_MOCK=0 i /opt/azom-agent/.env
ssh root@95.217.216.3

git clone https://github.com/idealinvestse/customer-agent-template-azom.git /tmp/azom
sudo bash /tmp/azom/bin/install-ubuntu26.sh \
  --domain dashboard.azom.no \
  --proxy caddy \
  --no-docker
# expect: exit 0, Caddy lyssnar på :80/:443 med giltigt Let's Encrypt-certifikat för dashboard.azom.no
```

Efter installation:

```bash
# Visa genererade lösenord och webhook/OAuth-URL:er (root only)
sudo cat /root/azom-install-credentials.txt

# Fyll i övriga secrets (WOO_*, MAIL_*, TELEGRAM_*, MESSENGER_*, OPENROUTER_API_KEY)
sudo nano /opt/azom-agent/.env
```

Fulla flaggor och detaljer: [`docs/AUTO_INSTALL.md`](AUTO_INSTALL.md) och [`docs/runbooks/ubuntu26-hetzner-deploy.md`](runbooks/ubuntu26-hetzner-deploy.md).

### Reverse proxy (Caddy standard / Nginx alternativ)

Dashboard binder alltid till **`127.0.0.1:8080`** och exponeras **aldrig** direkt i UFW.

- **Caddy (rekommenderad, automatisk TLS):** mall finns i `infrastructure/caddy/Caddyfile`. Konfigureras automatiskt av `bin/install-ubuntu26.sh --domain dashboard.azom.no --proxy caddy`. Blockerar `/metrics` med `403` och skickar `X-Forwarded-Proto`, `X-Forwarded-For`, `X-Forwarded-Host` och `X-Hub-Signature-256` till `127.0.0.1:8080`.
- **Nginx (alternativ):** mall finns i `infrastructure/nginx/dashboard.azom.no.conf`. Kräver Certbot TLS-certifikat under `/etc/letsencrypt/live/dashboard.azom.no/`.
- **ProxyFix:** `TRUSTED_PROXY_COUNT=1` i `.env` aktiverar `ProxyFix` (`x_for`, `x_proto`, `x_host`, `x_port`, `x_prefix`) i `infrastructure/dashboard/app.py`.

**Webhook- och OAuth-URL:er för `dashboard.azom.no`:**

- Meta Messenger webhook: `https://dashboard.azom.no/webhooks/messenger` (`messages` + `messaging_postbacks`)
- WooCommerce webhook: `https://dashboard.azom.no/webhooks/woo`
- Gmail OAuth callback: `https://dashboard.azom.no/oauth/gmail/callback`
- Google Marketing OAuth callback: `https://dashboard.azom.no/oauth/google/callback`
- ChatGPT SIWC OAuth callback: `https://dashboard.azom.no/oauth/openai/callback`

---

## Alternativ: Docker Compose (prod)

```bash
# cwd: /opt/azom-agent
cd /opt/azom-agent
cp .env.example .env   # fyll secrets, AZOM_USE_MOCK=0
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

- Port **`127.0.0.1:8080`** only
- Image tag: `azom-agent:3.0`
- Resource limits: dashboard 256 MB, bot 128 MB
- Data volume: `azom-data` → container path **`/app/.azom-data`** (inte `/var/lib/azom`)
- Detaljer: [`DOCKER_CONFIG_OVERLAY.md`](DOCKER_CONFIG_OVERLAY.md)

---

## Filsökvägar på servern (systemd vs Docker)

| Path | Innehåll |
|------|----------|
| `/opt/azom-agent` | Kod + `.venv` + `.env` (systemd) |
| `/var/lib/azom` | `AZOM_DATA_DIR` för systemd: `cases.db`, `telegram_jobs.sqlite3`, `messenger_jobs.sqlite3`, `messenger_proposals.sqlite3`, `research_jobs.sqlite3`, `wp_change_ledger.sqlite3`, `seo_studio.sqlite3`, `shop_intel.sqlite3`, `oauth/`, `backups/` |
| `/app/.azom-data` | `AZOM_DATA_DIR` inuti Docker-containern (compose-volym) |
| `/var/log/azom` | Loggar (systemd) |
| `/etc/systemd/system/azom-*.{service,timer}` | 12 installerade enheter och timers |
| `/etc/caddy/Caddyfile` | Caddy HTTPS reverse proxy-konfiguration |

Basic Auth: användarnamn är alltid `jonatan` / `oscar`; lösen via `DASHBOARD_PASSWORD*` / `DASHBOARD_OSCAR_PASSWORD*`.

---

## Säkerhet (checklista)

- [ ] `ufw` tillåter endast SSH, `80/tcp` och `443/tcp` (port `8080` stängd utåt)
- [ ] Dashboard **inte** publikt på `0.0.0.0` utan TLS; `TRUSTED_PROXY_COUNT=1` i `.env`
- [ ] `.env` och `/root/azom-install-credentials.txt` har mode `600`, `.env` ägd av `azom`
- [ ] `DASHBOARD_PASSWORD`, `DASHBOARD_OSCAR_PASSWORD` och `DASHBOARD_SECRET_KEY` satta
- [ ] `AZOM_USE_MOCK=0` i produktion (`AZOM_NULL_SEND` inte default-on i systemd)
- [ ] `https://dashboard.azom.no/metrics` returnerar `403` externt
- [ ] `MESSENGER_ALLOWED_PSIDS` + `MESSENGER_ACTOR_MAP` samt `TELEGRAM_ALLOWED_CHAT_IDS` + `TELEGRAM_ACTOR_MAP` satta (prod fail-closed)
- [ ] SSH key-only till VPS, fail2ban aktiv
- [ ] Secrets aldrig i git

---

## Drift och verifiering

```bash
# Systemkontroll & hälsokontroll
sudo /opt/azom-agent/bin/systemcheck.sh --domain dashboard.azom.no --expected-ip 95.217.216.3 --check-network
sudo /opt/azom-agent/bin/healthcheck.sh --check-public

# Status & timers
systemctl status azom-dashboard caddy fail2ban
systemctl list-timers 'azom-*' --no-pager

# Logs
journalctl -u azom-dashboard -f
journalctl -u caddy -f

# Manuell backup
sudo -u azom /opt/azom-agent/bin/backup-azom-data.sh
```

Timers som installerats: `azom-cases-poll`, `azom-daily-brief`, `azom-backup`, `azom-retention-purge`, `azom-shop-intel`.

## Relaterat

- Runbook: [`runbooks/ubuntu26-hetzner-deploy.md`](runbooks/ubuntu26-hetzner-deploy.md)
- One-shot: [`AUTO_INSTALL.md`](AUTO_INSTALL.md)
- Pilot / soak: [`PILOT_OPS.md`](PILOT_OPS.md)
- Messenger: [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md)
