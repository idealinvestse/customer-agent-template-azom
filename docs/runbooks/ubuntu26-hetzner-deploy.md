# Runbook: Deploy på Ubuntu 26.04 (Hetzner + Caddy HTTPS)

**Purpose:** Steg-för-steg-guide för att driftsätta, verifiera och felsöka Azom Ops Agent på Hetzner VPS med Ubuntu 26.04 och Caddy HTTPS (`dashboard.azom.no`).  
**Audience:** Oscar / ops.  
**Read this first:** [`../AUTO_INSTALL.md`](../AUTO_INSTALL.md), [`../DEPLOY_UBUNTU24_HETZNER.md`](../DEPLOY_UBUNTU24_HETZNER.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Systemd-sökvägar** | Kod i `/opt/azom-agent`, data (`AZOM_DATA_DIR`) i `/var/lib/azom`, loggar i `/var/log/azom`. |
| **Docker-sökvägar** | Data i `/app/.azom-data` (gäller endast Compose, inte bare-metal systemd). |
| **Caddy** | Reverse proxy med automatisk Let's Encrypt TLS på port 80/443 som skickar trafik till `127.0.0.1:8080`. |
| **Fail-closed** | Tom `TELEGRAM_ALLOWED_CHAT_IDS` / `MESSENGER_ALLOWED_PSIDS` eller actor-map nekar all åtkomst i `AZOM_USE_MOCK=0`. |

## Gör / Gör inte

**Gör:**
- Verifiera DNS A-record (`dashboard.azom.no` → serverns publika IP, t.ex. `95.217.216.3`) innan Caddy startas.
- Håll port `8080` stängd i UFW och exponera bara `22/tcp`, `80/tcp` och `443/tcp`.
- Låt `AZOM_USE_MOCK=0` gälla i produktion och verifiera att `/metrics` returnerar `403` publikt.

**Gör inte:**
- Logga, posta eller committa lösenord, `DASHBOARD_SECRET_KEY`, `.env` eller `/root/azom-install-credentials.txt`.
- Aktivera `AZOM_NULL_SEND=1` som default i `/etc/systemd/system/azom-*.service`.
- Aktivera NO/DK-brevlådor eller FU9 auto-send utan skriftligt beslut och slutförd manuell A1 live-soak ([`../PILOT_OPS.md`](../PILOT_OPS.md)).

## 1. Förkontroll av DNS och VPS

```bash
# cwd: lokal arbetsstation eller VPS
getent ahosts dashboard.azom.no
# expect: 95.217.216.3 (serverns publika IPv4)
```

Kontrollera på ny Hetzner-server att `root`-lösenordet inte kräver interaktivt lösenordsbyte innan automatisk SSH-deploy körs.

## 2. Installation (one-shot med Caddy HTTPS)

```bash
# cwd: /tmp/azom-release (eller klonat repo på servern) som root
# env: skapar /opt/azom-agent/.env med AZOM_USE_MOCK=0
sudo bash bin/install-ubuntu26.sh \
  --domain dashboard.azom.no \
  --proxy caddy \
  --no-docker
# expect: exit 0, 12 systemd-enheter installerade, Caddy validerad, UFW aktiv med SSH+80+443
```

Vad skriptet konfigurerar automatiskt:
- Paket: `sqlite3`, `libffi-dev`, `libssl-dev`, `dnsutils`/`bind9-dnsutils`, `python3-venv`, `ufw`, `fail2ban`, `caddy`.
- `.env` (mode `600`, ägd av `azom`): `DASHBOARD_PASSWORD`, `DASHBOARD_OSCAR_PASSWORD`, `DASHBOARD_SECRET_KEY`, `AZOM_DASHBOARD_PUBLIC_URL=https://dashboard.azom.no`, `TRUSTED_PROXY_COUNT=1`, `MAIL_OAUTH_REDIRECT_URI`, `GOOGLE_OAUTH_REDIRECT_URI`.
- Reverse proxy: `/etc/caddy/Caddyfile` (blockerar `/metrics` med `403`).
- Timers: `azom-cases-poll.timer`, `azom-daily-brief.timer`, `azom-backup.timer`, `azom-retention-purge.timer`, `azom-shop-intel.timer`.

## 3. Verifiering efter deploy

```bash
# cwd: /opt/azom-agent (som root)
systemctl is-active azom-dashboard caddy fail2ban
# expect: active för samtliga tre

systemctl list-timers 'azom-*' --no-pager
# expect: 5 aktiva timers (cases-poll, daily-brief, backup, retention-purge, shop-intel)

ufw status
# expect: 22/tcp, 80/tcp, 443/tcp ALLOW; 8080/tcp ej listad

curl -fsS http://127.0.0.1:8080/live
curl -fsS https://dashboard.azom.no/health
# expect: {"ok": true, ...} med giltigt HTTPS-certifikat

curl -sS -o /dev/null -w '%{http_code}\n' https://dashboard.azom.no/metrics
# expect: 403

/opt/azom-agent/bin/systemcheck.sh --domain dashboard.azom.no --expected-ip 95.217.216.3 --check-network
/opt/azom-agent/bin/healthcheck.sh --check-public
# expect: exit 0 (eller tydlig lista på saknade live-secrets innan onboarding är klar)
```

## 4. Onboarding efter installation (Oscar)

1. Läs inloggning och callback-URL:er:
   ```bash
   sudo cat /root/azom-install-credentials.txt
   ```
2. Lägg in live-nycklar i `/opt/azom-agent/.env` (`WOO_*`, `MAIL_*`, `TELEGRAM_*`, `MESSENGER_*`, `OPENROUTER_API_KEY`).
3. Konfigurera externa webhooks och OAuth callbacks:
   - Meta Messenger: `https://dashboard.azom.no/webhooks/messenger`
   - WooCommerce: `https://dashboard.azom.no/webhooks/woo`
   - Gmail OAuth: `https://dashboard.azom.no/oauth/gmail/callback`
   - Google Marketing OAuth: `https://dashboard.azom.no/oauth/google/callback`
   - ChatGPT SIWC OAuth: `https://dashboard.azom.no/oauth/openai/callback`
4. Starta om dashboard och aktivera Telegram-bot efter `.env`-ändring:
   ```bash
   sudo systemctl restart azom-dashboard
   sudo systemctl enable --now azom-bot
   ```

## 5. Felsökning

- **Dashboard startar inte:** `journalctl -u azom-dashboard -n 80 --no-pager`. Kontrollera behörigheter på `/var/lib/azom` och `/opt/azom-agent/.env` (`azom:azom`, `600`).
- **Caddy TLS misslyckas:** Kontrollera att port `80` och `443` är öppna både i UFW och i Hetzner Cloud Firewall, och att `getent ahosts dashboard.azom.no` pekar på serverns IP. Kör `caddy validate --config /etc/caddy/Caddyfile` och `journalctl -u caddy -n 50`.
- **OAuth / CSRF-problem bakom proxy:** Kontrollera att `TRUSTED_PROXY_COUNT=1` och `AZOM_DASHBOARD_PUBLIC_URL=https://dashboard.azom.no` finns i `/opt/azom-agent/.env`.
- **Backup:** Testa manuellt med `sudo -u azom /opt/azom-agent/bin/backup-azom-data.sh` och kontrollera `manifest.txt` under `/var/lib/azom/backups/<timestamp>/`.
