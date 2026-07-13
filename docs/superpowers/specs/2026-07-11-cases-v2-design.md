# Design: Cases 2.0 — ärendehantering

**Date:** 2026-07-11  
**Status:** implemented  
**Supersedes:** `2026-07-11-email-case-management-design.md` (MVP)

## Locked decisions

| Decision | Choice |
|----------|--------|
| Auto-send | No by default — human approve still required; Path B later added **disabled** rails only (`config/cases_ai.yaml`, kill-switch) |
| FAQ/KB | Out of scope |
| IMAP IDLE | Out of scope — systemd timer 5 min |
| Statuses | `open` \| `escalated` \| `replied` \| `closed` |
| Threading | In-Reply-To / References / from+normalized subject |
| Mark read | After successful ingest (best-effort) |
| Order drafts | Enrich with Woo read-only when `order_id` present |
| Telegram | `/cases` list · show · approve · close |
| Per-mailbox secrets | Out of scope (shared env) |

## Schema delta

`cases`: `escalation_id`, `priority`, `assignee`  
`case_messages`: `in_reply_to`, `references_header`  
Migrations via `ALTER TABLE` in `CaseStore._init_schema`.

## Surfaces

- CLI: `cases poll|list|show|reply|draft|close`
- Dashboard: queue filters, age, save draft, RBAC close
- Telegram OpenClaw commands
- systemd: `azom-cases-poll.timer`

## Non-goals

Default-on auto-send, FAQ/KB, IMAP IDLE, multi-tenant, per-mailbox OAuth, SLA automation.

> **Path B note:** Suggest-approve + auto-send *rails* (default off) shipped on `main` without changing the human-approve production default. See `docs/CASES.md` and `docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md`.
