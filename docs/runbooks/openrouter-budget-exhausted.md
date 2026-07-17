# Runbook: OpenRouter budget exhausted

## Symptom
- LLM-drafts/classify skippas (telemetry: `llm_budget_skip`)
- Telegram-bot svarar med "OpenRouter-budgeten är slut just nu"
- Dashboard visar budget near/at cap

## Diagnos
```bash
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops kpis --days 7
# Kolla used_usd vs cap_usd

# Kolla telemetry för budget-skip-events
sudo -u azom cat /var/lib/azom/telemetry.jsonl | grep llm_budget_skip | tail -5
```

## Fix
1. **Höj cap (tillfälligt):**
   ```bash
   sudo nano /opt/azom-agent/config/limits.yaml
   # Ändra openrouter_cap till t.ex. 150
   # Eller env: export OPENROUTER_CAP_USD=150
   sudo systemctl restart azom-dashboard azom-bot
   ```
2. **Rotera gammal telemetry (om kostnad är från gamla events):**
   ```bash
   sudo -u azom /opt/azom-agent/.venv/bin/python -c "
   from ecom_ops.telemetry import rotate_telemetry
   print(rotate_telemetry(retention_days=30))
   "
   ```
3. **Byt till billigare model** (t.ex. `openai/gpt-4o-mini` → `meta-llama/llama-3.1-8b-instruct`):
   ```bash
   # I .env:
   OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct
   ```

## Verifiering
```bash
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops status
# Budget OK + nästa LLM-anrop ska inte skippas
```
