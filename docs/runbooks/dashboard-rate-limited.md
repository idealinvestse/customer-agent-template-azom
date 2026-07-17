# Runbook: Dashboard login rate-limited (429)

## Symptom
- Dashboard returnerar HTTP 429 "Too many failed login attempts"
- Användare kan inte logga in även med rätt lösenord

## Diagnos
```bash
# Kolla om det är legitimt (brute-force) eller en bugg
journalctl -u azom-dashboard -n 200 | grep -i "429\|auth\|login"
```

## Fix
1. **Vänta 5 minuter** (rate-limit-fönstret är 300s) — fönstret löper ut automatiskt.
2. **Om akut (Oscar behöver in nu):** starta om dashboarden (nollställer in-memory räknare)
   ```bash
   sudo systemctl restart azom-dashboard
   ```
3. **Om återkommande:** kontrollera om en monitor/healthcheck skickar fel credentials
   ```bash
   # Kolla om UFW/proxy gör auth-probes
   grep -r "DASHBOARD_PASSWORD" /opt/azom-agent/.env
   ```

## Förebyggande
- Säkerställ att healthchecks inte skickar Basic Auth med fel lösenord
- Överväg att höja `_LOGIN_MAX_FAILURES` om legitim trafik triggar gränsen
