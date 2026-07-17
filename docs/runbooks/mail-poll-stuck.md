# Runbook: Mail poll stuck / credentials expired

## Symptom
- Inga nya cases skapas från mail (kö står stilla)
- `azom-cases-poll.timer` kör men `IngestResult.errors > 0`
- Dashboard probe visar mail = error

## Diagnos
```bash
# 1. Kolla senaste poll-resultat
journalctl -u azom-cases-poll -n 50

# 2. Kör poll manuellt
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops cases poll

# 3. Kolla mail-probe
sudo -u azom /opt/azom-agent/.venv/bin/python -c "
from infrastructure.dashboard.secret_probes import probe_mail
print(probe_mail().to_dict())
" 2>&1 || true
# (Kör från /opt/azom-agent)

# 4. Verifiera credentials
sudo -u azom cat /opt/azom-agent/.env | grep -E 'MAIL_|IMAP_|SMTP_'
```

## Fix
1. **Om IMAP/SMTP credentials expired:**
   ```bash
   sudo nano /opt/azom-agent/.env
   # Uppdatera MAIL_PASSWORD / SMTP_PASSWORD / IMAP-host
   sudo systemctl restart azom-dashboard
   ```
2. **Om Gmail OAuth-token expired:**
   - Se [gmail-oauth-revoked.md](gmail-oauth-revoked.md)
3. **Om IMAP-server är nere:** vänta + övervaka; eskalera till Oscar om > 30 min.
4. **Om mailbox-config är fel:** kolla `config/mailboxes.yaml` — rätt host/port/ssl.

## Verifiering
```bash
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops cases poll
# Förväntat: ok=true, created>0 (eller skipped om inga nya mail)

sudo systemctl restart azom-cases-poll.timer
```
