# Automatisk installation (Ubuntu 26 / 24)

One-shot install som tar en tom VPS till **färdig, körande Azom-agent**.

## Snabbast (på ny server)

```bash
# Som root
curl -fsSL https://raw.githubusercontent.com/idealinvestse/customer-agent-template-azom/main/bin/install-ubuntu26.sh \
  | sudo bash
```

Eller via wrapper efter clone:

```bash
git clone https://github.com/idealinvestse/customer-agent-template-azom.git /tmp/azom
sudo bash /tmp/azom/bin/install.sh
```

## Vad scriptet gör (ordning)

1. Apt-update + systempaket (Python 3, venv, git, ufw, fail2ban, jq, …)
2. Docker Engine (valfritt, kan hoppas över med `--no-docker`)
3. Systemanvändare `azom` + kataloger `/opt/azom-agent`, `/var/lib/azom`, `/var/log/azom`
4. Clone eller sync av projektet
5. `python3 -m venv` + `pip install -r requirements.txt` + `pip install -e .`
6. Skapar `.env` från `.env.example`, sätter prod-paths, **genererar** dashboard-lösenord
7. Installerar systemd units (`azom-dashboard`, `azom-bot`, `azom-daily-brief.timer`, `azom-cases-poll.timer`)
8. UFW: endast SSH öppet (8080 stängd — använd reverse proxy)
9. Logrotate + unattended-upgrades + fail2ban
10. Smoke-tester (order-status, support, ssh, mail i mock)
11. Startar tjänster (bot endast om `TELEGRAM_BOT_TOKEN` är satt)

## Flaggor

| Flagga | Effekt |
|--------|--------|
| `--repo URL` | Git remote (default: idealinvestse/customer-agent-template-azom) |
| `--branch NAME` | Branch (default: `main`) |
| `--app-dir PATH` | Installationskatalog (default: `/opt/azom-agent`) |
| `--user NAME` | Serviceanvändare (default: `azom`) |
| `--ssh-port N` | UFW SSH-port (default: 22) |
| `--mock` | Sätter `AZOM_USE_MOCK=1` |
| `--no-docker` | Hoppa över Docker |
| `--no-ufw` | Hoppa över brandvägg |
| `--no-start` | Installera men starta inte services |
| `--no-smoke` | Hoppa över smoke-tester |

## Exempel

```bash
# Produktion, egen branch, utan Docker
sudo bash bin/install-ubuntu26.sh \
  --repo https://github.com/idealinvestse/customer-agent-template-azom.git \
  --branch main \
  --no-docker

# Endast install (CI / image-build)
sudo bash bin/install-ubuntu26.sh --no-start --mock --no-ufw

# Med secrets via env
sudo TELEGRAM_BOT_TOKEN=123:abc \
     DASHBOARD_PASSWORD='s3cret' \
     WOO_BASE_URL=https://azom.se \
     bash bin/install-ubuntu26.sh
```

## Efter install

```bash
# Lösenord (root only)
sudo cat /root/azom-install-credentials.txt

# Editera övriga secrets
sudo nano /opt/azom-agent/.env

# Om du la till Telegram-token i efterhand
sudo systemctl enable --now azom-bot

# Health
curl http://127.0.0.1:8080/health
curl -u jonatan:PASSWORD http://127.0.0.1:8080/
```

## Idempotent

Scriptet kan köras om säkert:

- apt packages / docker: skippas om redan installerat
- git: `pull --ff-only`
- venv: återskapas/uppdateras
- `.env`: fyller bara tomma secrets; behåller befintliga
- systemd: kopieras om + `daemon-reload` + restart

## Ubuntu-versioner

| Version | Status |
|---------|--------|
| **26.x** | Primär target |
| **24.04 LTS** | Fullt stödd (samma script) |
| 22.04 | Best effort |

Wrapper: `bin/install.sh` detekterar distro och anropar `install-ubuntu26.sh`.

## Relaterat

- Manuell bootstrap (äldre): `bin/bootstrap-ubuntu24.sh`
- Hetzner sizing: `docs/DEPLOY_UBUNTU24_HETZNER.md`
- systemd units: `infrastructure/systemd/`
