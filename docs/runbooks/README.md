# AzomOps Runbooks

**Purpose:** Incident-procedurer (symptom → diagnos → fix → verify).  
**Audience:** Oscar / ops.  
**Read this first:** [`../PILOT_OPS.md`](../PILOT_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

Varje runbook följer samma form: trigger → diagnoskommandon → fix → verifiering.

| Runbook | Scenario |
|---------|----------|
| [woo-webhook-disabled.md](woo-webhook-disabled.md) | Woo stänger av webhook efter upprepade misslyckade leveranser |
| [openrouter-budget-exhausted.md](openrouter-budget-exhausted.md) | OpenRouter-budget slut — OpenRouter-anrop skippas |
| [model-oauth.md](model-oauth.md) | Modellroute, ChatGPT OAuth, xAI, backup av refresh-token |
| [openclaw-upgrade.md](openclaw-upgrade.md) | Lokal OpenClaw-klient: backup, uppgradering, verifiering, rollback |
| [gateway-recovery.md](gateway-recovery.md) | Gateway startar inte, port, config, plugin, databaslås |
| [secret-leak-response.md](secret-leak-response.md) | Token eller nyckel har synts i logg, chat eller git |
| [mail-poll-stuck.md](mail-poll-stuck.md) | Cases-poll fastnar (credentials / IMAP) |
| [gmail-oauth-revoked.md](gmail-oauth-revoked.md) | Gmail OAuth refresh-token revoked |
| [cases-db-corrupt.md](cases-db-corrupt.md) | cases.db korrupt / SQLite disk-I/O |
| [dashboard-rate-limited.md](dashboard-rate-limited.md) | Dashboard login rate-limited (429) |
| [wp-ops-backup.md](wp-ops-backup.md) | Lokal mock-backup och staging-drill. Ingen produktionsåterställning |
| [wp-ops-files.md](wp-ops-files.md) | Typad redigering av en befintlig tema- eller pluginfil. Inget fritt skal |

**Gör inte:** Markera live soak eller FU9 som klart från en runbook-fix — det ägs av Oscar + Jonatan via [`PILOT_OPS.md`](../PILOT_OPS.md) / [`CASES.md`](../CASES.md).
