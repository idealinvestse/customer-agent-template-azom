# Deploy: Ubuntu 24.04 LTS på Hetzner VPS

## Rekommenderad VPS-storlek

| Scenario | Hetzner typ | Spec | ~pris/mån | När |
|----------|-------------|------|-----------|-----|
| **Pilot (rekommenderad start)** | **CX22** eller **CPX21** | 2 vCPU, **4 GB RAM**, 40 GB NVMe | ~€4–7 | En kund-agent, dashboard, bot, mail, cron |
| Budget / minimal | CX22 shared / CX11 om tillgänglig | 2 vCPU, 2 GB RAM | ~€3–4 | Endast CLI + cron, ingen bot |
| Headroom (fler integrationer / Selenium senare) | CPX31 | 4 vCPU, 8 GB RAM | ~€12–15 | Browser-automation, fler workers |

### Varför 4 GB (inte 2 GB)?

V1-workloads (CLI, Flask dashboard, Telegram long-poll, mail IMAP/SMTP) är lätta, men Ubuntu 24 + Docker + journald + buffertar behöver luft:

| Process | RAM ungefär |
|---------|-------------|
| OS + system | 400–700 MB |
| Dashboard (Flask) | 40–80 MB |
| Telegram bot | 30–60 MB |
| Docker (om används) | 100–300 MB |
| Spikes (pip, compose build, apt) | +500 MB |

**2 GB fungerar** i bare-metal (venv + systemd utan Docker), men blir trångt vid image-builds.  
**4 GB (CX22/CPX21) är sweet spot** för single-tenant pilot med marginal.

### Region & image

- **Location:** Falkenstein (`fsn1`) eller Nuremberg (`nbg1`) — latens till SE/NO/DK ok
- **Image:** Ubuntu 24.04 LTS
- **Network:** IPv4 + IPv6, firewall i Hetzner Cloud + UFW
- **Disk:** 40 GB räcker (logs roteras; telemetry JSONL är liten)

### Skala senare

- Multi-tenant SaaS → egen VPS/container per kund eller CPX31 + orchestration
- Selenium/Chrome headless → +1–2 GB RAM (CPX31)

---

## Snabbstart (one-shot auto-install)

```bash
# På Hetzner CX22 Ubuntu 26/24 som root
ssh root@YOUR_IP

# Fully automatic — packages, clone, venv, systemd, firewall, smoke, start
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash

# Credentials (auto-generated)
sudo cat /root/azom-install-credentials.txt

# Optional: fill remaining secrets
sudo nano /opt/azom-agent/.env
```

Full flaggor och detaljer: [`docs/AUTO_INSTALL.md`](AUTO_INSTALL.md).

### Manuell väg (äldre bootstrap)

```bash
git clone <repo-url> /opt/azom-agent && cd /opt/azom-agent
sudo bash bin/bootstrap-ubuntu24.sh   # wrapper → install-ubuntu26.sh
```

Dashboard binds till **127.0.0.1:8080**. Exponera via nginx/Caddy + TLS, inte öppet i UFW.

### Reverse proxy (valfritt)

```nginx
server {
  listen 443 ssl;
  server_name agent.azom.se;
  # ssl_certificate ...;
  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
  }
}
```

---

## Alternativ: Docker Compose (prod)

```bash
cd /opt/azom-agent
cp .env.example .env   # fyll secrets, AZOM_USE_MOCK=0
docker compose -f infrastructure/docker-compose.prod.yml up -d --build
```

- Port **127.0.0.1:8080** only
- Resource limits: dashboard 256 MB, bot 128 MB
- Data volume: `azom-data`

---

## Filsökvägar på servern

| Path | Innehåll |
|------|----------|
| `/opt/azom-agent` | Kod + venv + config |
| `/var/lib/azom` | telemetry + escalations (prod data dir) |
| `/var/log/azom` | loggar |
| `/etc/systemd/system/azom-*.service` | units |

---

## Säkerhet (checklista)

- [ ] `ufw` tillåter endast SSH (ev. 443 om reverse proxy)
- [ ] Dashboard **inte** publikt på 0.0.0.0 utan TLS
- [ ] `.env` mode `600`, ägd av `azom`
- [ ] `DASHBOARD_PASSWORD` eller `DASHBOARD_PASSWORD_HASH` satt (mock-fallback av i prod)
- [ ] `AZOM_USE_MOCK=0` i produktion
- [ ] SSH key-only till VPS, fail2ban aktiv
- [ ] Secrets aldrig i git

---

## Drift

```bash
# Status
systemctl status azom-dashboard azom-bot
systemctl list-timers azom-daily-brief.timer

# Logs
journalctl -u azom-dashboard -f
journalctl -u azom-bot -f

# Manual ops
sudo -u azom bash -lc 'cd /opt/azom-agent && .venv/bin/python -m ecom_ops mail fetch'
sudo -u azom /opt/azom-agent/bin/daily-brief-azom.sh
```
