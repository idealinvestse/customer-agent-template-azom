# Runbook: Gmail OAuth refresh-token revoked

## Symptom
- Mail-poll misslyckas med "invalid_grant" eller "Token has been expired or revoked"
- Gmail OAuth probe visar "missing" trots att tokens fanns tidigare
- `oauth/gmail.json` finns men refresh fungerar inte

## Diagnos
```bash
# 1. Kolla om token-fil finns
ls -la /var/lib/azom/oauth/gmail.json

# 2. Kolla OAuth-probe
sudo -u azom /opt/azom-agent/.venv/bin/python -c "
from ecom_ops.oauth.gmail import GmailOAuthStore
s = GmailOAuthStore()
print('has_tokens:', s.has_tokens())
b = s.load_tokens()
if b:
    print('email:', b.email, 'expires_at:', b.expires_at)
"

# 3. Kolla mail-poll-logg för OAuth-fel
journalctl -u azom-cases-poll -n 100 | grep -i -E 'oauth|invalid_grant|revoked'
```

## Fix
1. **Re-autentisera via dashboard:**
   - Öppna `https://<dashboard-host>/oauth/gmail/start` (Oscar)
   - Logga in med Google → godkänn scopes
   - Ny token sparas i `/var/lib/azom/oauth/gmail.json`
2. **Eller via CLI (mock-läge för test):**
   ```bash
   sudo -u azom /opt/azom-agent/.venv/bin/python -c "
   from ecom_ops.oauth.gmail import GmailOAuthStore
   GmailOAuthStore().mock_connect()
   print('Mock tokens saved')
   "
   ```
3. **Rensa gamla tokens först om de är korrupta:**
   ```bash
   sudo rm /var/lib/azom/oauth/gmail.json
   # Sen re-autentisera via dashboard
   ```
4. **Starta om tjänster:**
   ```bash
   sudo systemctl restart azom-dashboard azom-cases-poll.timer
   ```

## Verifiering
```bash
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops cases poll
# Förväntat: ok=true (mail hämtas via Gmail OAuth)
```

## Förebyggande
- Google kan revokena refresh-tokens efter 6 månaders inaktivitet
- Sätt en kalender-påminnelse att re-autentisera var 3:e månad
