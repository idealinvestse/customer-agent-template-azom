# AzomOps Runbooks

Operational runbooks for common incidents. Each runbook has: trigger symptoms,
diagnosis commands, fix steps, and verification.

| Runbook | Scenario |
|---------|----------|
| [woo-webhook-disabled.md](woo-webhook-disabled.md) | Woo stänger av webhook efter 5 misslyckade leveranser |
| [openrouter-budget-exhausted.md](openrouter-budget-exhausted.md) | OpenRouter-budget slut — LLM-anrop skippas |
| [mail-poll-stuck.md](mail-poll-stuck.md) | Cases-poll fastnar (credentials expired / IMAP nere) |
| [gmail-oauth-revoked.md](gmail-oauth-revoked.md) | Gmail OAuth refresh-token revoked |
| [cases-db-corrupt.md](cases-db-corrupt.md) | cases.db korrupt / SQLite disk-I/O fel |
| [dashboard-rate-limited.md](dashboard-rate-limited.md) | Dashboard login rate-limited (429) |
