# Runbook: WooCommerce webhook disabled

## Symptom
- Webhook-leveranser slutar komma (inga nya events i dashboard)
- Woo admin → WooCommerce → Settings → Advanced → Webhooks visar status "Disabled"
- Logg: `Woo webhook signature mismatch` eller inga webhook-rader alls

## Diagnos
```bash
# 1. Kolla om webhook-endpoint svarar
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/webhooks/woo
# Förväntat: 405 (GET inte tillåtet) eller 401 (POST utan signatur) — inte 404/500

# 2. Kolla att WOO_WEBHOOK_SECRET är satt
sudo -u azom cat /opt/azom-agent/.env | grep WOO_WEBHOOK_SECRET

# 3. Kolla dashboard-logg för webhook-fel
journalctl -u azom-dashboard -n 100 | grep -i webhook
```

## Fix
1. **Om secret ändrats i Woo men inte i .env:**
   ```bash
   sudo nano /opt/azom-agent/.env
   # Uppdatera WOO_WEBHOOK_SECRET=<nytt värde från Woo admin>
   sudo systemctl restart azom-dashboard
   ```
2. **Om webhook är disabled i Woo:**
   - Gå till WP admin → WooCommerce → Settings → Advanced → Webhooks
   - Redigera webhook → Status: Active → Save
3. **Om endpoint returnerar 404:** kontrollera att Flask-routen `/webhooks/woo` finns och att dashboarden körs.

## Verifiering
```bash
# Manuell test-leverans från Woo admin (Edit webhook → "Deliver log" → "Send test")
# Eller: skapa en test-order i Woo och bekräfta att event dyker upp i logg
journalctl -u azom-dashboard -f | grep -i webhook
```
