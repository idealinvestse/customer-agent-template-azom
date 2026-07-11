# Prod-path: mail threading, poll escalation, LLM drafts, auth, actors, KPI

## Problem

Case approve/send lacked `In-Reply-To`/`References`. Mailbox poll failures were easy to miss. Support drafts were template-only. Dashboard used unsalted SHA-256 and no CSRF. Telegram hard-coded `actor=jonatan`. Support-loop time was unmeasured.

## Solution

1. Thread headers on approve/send (SMTP / Graph / mock).
2. Poll mailbox errors escalate.
3. `ecom_ops.llm` OpenRouter drafts with `openrouter_cap` + template fallback.
4. Dashboard: Werkzeug password hashes, CSRF (`_csrf` / `X-CSRF-Token`), mock passwords only when `AZOM_USE_MOCK=1`, `DASHBOARD_SECRET_KEY` for sessions.
5. `TELEGRAM_ACTOR_MAP` → actor for approve/close/health/whoami.
6. Telemetry KPIs: `time_to_approve_sec`, `draft_edit_distance`, `time_to_first_edit_sec`.

## Prevention

- Tests: `test_bugfixes_prod_paths`, `test_llm_support_drafts`, `test_dashboard_auth`, `test_telegram_actors`, `test_case_kpis`.
