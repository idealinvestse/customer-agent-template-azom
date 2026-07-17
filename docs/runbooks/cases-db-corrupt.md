# Runbook: cases.db corrupt / SQLite I/O error

## Symptom
- `sqlite3.DatabaseError: database disk image is malformed`
- `sqlite3.OperationalError: disk I/O error`
- Cases-poll kraschar; dashboard `/cases` returnerar 500

## Diagnos
```bash
# 1. Kolla DB-integritet
sudo -u azom sqlite3 /var/lib/azom/cases.db "PRAGMA integrity_check;"
# Friskt: "ok" — annars felbeskrivning

# 2. Kolla diskutrymme
df -h /var/lib/azom

# 3. Kolla fil-permissioner
ls -la /var/lib/azom/cases.db
```

## Fix
1. **Om disk full:**
   ```bash
   # Rensa gamla backups + telemetry
   sudo -u azom /opt/azom-agent/.venv/bin/python -c "
   from ecom_ops.telemetry import rotate_telemetry
   print(rotate_telemetry(retention_days=30))
   "
   sudo find /var/lib/azom/backups -mtime +30 -exec rm -rf {} +
   ```
2. **Om DB korrupt (restore från backup):**
   ```bash
   # Stoppa poll
   sudo systemctl stop azom-cases-poll.timer

   # Hitta senaste backup
   LATEST=$(ls -td /var/lib/azom/backups/20* | head -1)
   echo "Restoring from: $LATEST"

   # Backupa korrupt DB först
   cp /var/lib/azom/cases.db /var/lib/azom/cases.db.corrupt.$(date +%s)

   # Restore
   cp "$LATEST/cases.db" /var/lib/azom/cases.db
   chown azom:azom /var/lib/azom/cases.db

   # Verifiera
   sudo -u azom sqlite3 /var/lib/azom/cases.db "PRAGMA integrity_check;"

   sudo systemctl start azom-cases-poll.timer
   ```
3. **Om permission-fel:**
   ```bash
   chown azom:azom /var/lib/azom/cases.db
   chmod 600 /var/lib/azom/cases.db
   ```

## Verifiering
```bash
sudo -u azom sqlite3 /var/lib/azom/cases.db "SELECT COUNT(*) FROM cases;"
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops cases list --status open
```
