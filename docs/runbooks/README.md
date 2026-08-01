# AzomOps Runbooks

**Purpose:** Incident-procedurer (symptom → diagnos → fix → verify).  
**Audience:** Oscar / ops.  
**Read this first:** [`../PILOT_OPS.md`](../PILOT_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

Varje runbook följer samma form: trigger → diagnoskommandon → fix → verifiering.

| Runbook | Scenario |
|---------|----------|
| [woo-webhook-disabled.md](woo-webhook-disabled.md) | Woo stänger av webhook efter upprepade misslyckade leveranser |
| [openrouter-budget-exhausted.md](openrouter-budget-exhausted.md) | OpenRouter-budget slut — LLM-anrop skippas |
| [mail-poll-stuck.md](mail-poll-stuck.md) | Cases-poll fastnar (credentials / IMAP) |
| [gmail-oauth-revoked.md](gmail-oauth-revoked.md) | Gmail OAuth refresh-token revoked |
| [cases-db-corrupt.md](cases-db-corrupt.md) | cases.db korrupt / SQLite disk-I/O |
| [dashboard-rate-limited.md](dashboard-rate-limited.md) | Dashboard login rate-limited (429) |

**Gör inte:** Markera live soak eller FU9 som klart från en runbook-fix — det ägs av Oscar + Jonatan via [`PILOT_OPS.md`](../PILOT_OPS.md) / [`CASES.md`](../CASES.md).
