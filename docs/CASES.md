# Cases 2.0 + Path B (AI quality)

Mail → support case → classify/draft → human approve → send.

**Specs:**  
- [`superpowers/specs/2026-07-11-cases-v2-design.md`](superpowers/specs/2026-07-11-cases-v2-design.md)  
- Path B plan: [`superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`](superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md)

**Code:** `skills/ecom_ops/cases/` · **Config:** `config/mailboxes.yaml`, `config/cases_ai.yaml` · **DB:** `$AZOM_DATA_DIR/cases.db`

---

## Statuses

| Status | Meaning |
|--------|---------|
| `open` | New / in queue |
| `escalated` | Abuse/legal/critical or human escalated |
| `replied` | Draft approved and mailed |
| `closed` | Closed without customer reply |

---

## Pipeline

1. **Poll** — `CaseService.poll` / `azom-cases-poll.timer` / dashboard POST `/cases/poll` / CLI `cases poll`
2. **Ingest** — mailbox messages → case or thread attachment (In-Reply-To / References / from+subject)
3. **Mark read** — best-effort after successful ingest
4. **Classify** — keyword abuse gate + confidence (hybrid path); categories e.g. `order_status`, `shipping`, `return`, `billing`, `abuse`, …
5. **Draft** — support service + OpenRouter when key/budget allow; **order context** from Woo when `order_id` present
6. **Suggest-approve** — pure eligibility (`suggest.py`) → `suggest_approve` flag + confidence columns
7. **Human approve** — dashboard, Telegram, or CLI `cases reply` → send with threading headers
8. **Close** — optional without send

Mailbox poll failures escalate (ticket) so ops does not miss broken inbox.

---

## Config: mailboxes

```yaml
# config/mailboxes.yaml
mailboxes:
  - id: support_default
    label: Support (default)
    address: support@azom.se
    site: azom
    market: se
    language: sv
    enabled: true
    # provider: gmail   # optional override of MAIL_PROVIDER
```

Credentials stay in env / `secrets.env` (shared; per-mailbox secret prefixes deferred).

---

## Config: cases AI rails (Path B)

```yaml
# config/cases_ai.yaml (defaults)
suggest_approve_categories: [order_status, shipping]
suggest_approve_min_confidence: 0.8
suggest_approve_require_order_id: true
never_suggest_categories: [abuse, return, billing]

auto_send_enabled: false          # keep false unless Oscar experiment
auto_send_categories: [order_status]
auto_send_min_confidence: 0.92
max_auto_sends_per_day: 10
kill_switch_env: AZOM_AUTO_SEND_KILL
```

**Suggest-approve:** badge/UX only — still requires human confirm.  
**Auto-send:** eligibility helper + day counter in `auto_send.py`; **not** wired as default poll sender. Env kill-switch forces off even if config enabled.

---

## CLI

```bash
python -m ecom_ops --mock cases poll --limit 20
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases show --id <uuid>
python -m ecom_ops --mock cases draft --id <uuid> --body "..."
python -m ecom_ops --mock cases regenerate --id <uuid>             # new draft, never sends
python -m ecom_ops --mock cases reply --id <uuid> [--body "..."]   # approve+send
python -m ecom_ops --mock cases close --id <uuid> [--reason "..."]
./bin/cases-poll.sh
```

Default actor for CLI is `agent` (operator) for poll; use `--actor jonatan` for reply as storefront owner.

---

## Dashboard

- `/cases` — filters, age, suggest badges, KPIs
- `/cases/<id>` — draft edit/save, order panel context, approve confirm, close (RBAC)
- Overview nav badges for open/escalated counts

---

## Telegram

```text
/cases
/cases show <id8>
/cases approve <id8>
/cases close <id8>
```

NL: “lista föreslagna”, “godkänn abcdef01” → confirm UX only. See [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md).

---

## Schema notes

Migrating `CaseStore` (ALTER / versioned `_migrate`):

- Cases: status, priority, assignee, escalation_id, order_id, draft, **classify_confidence**, **classify_method**, **suggest_approve**, timestamps, …
- Messages: body, message-id, **in_reply_to**, **references_header**, …

---

## Telemetry KPIs

Support-loop metrics (when available): `time_to_approve_sec`, `draft_edit_distance`, `time_to_first_edit_sec`, plus LLM cost under OpenRouter cap.

---

## Non-goals (current)

- FAQ/KB knowledge base  
- IMAP IDLE (timer poll only)  
- Multi-tenant cases  
- Per-mailbox OAuth secrets  
- Default-on auto-send  

Cases v2 original “auto-send: No” still holds for **production default**; Path B only adds **disabled** rails and Oscar-gated future experiments.
