# Design: Telegram dialog continuity

**Date:** 2026-07-28  
**Status:** implemented  
**Approach:** shared recovery helper + no-LLM digest continuity + approve-fail keyboards; tighten sticky follow-up; Telegram “Godkänn & nästa”. HITL/fail-closed auth unchanged.

## Problem

Happy-path continuity is strong (24h TTL, sticky order/case, tool prefetch, confirm-only writes). Empty queues, not-found, approve failures, and no-LLM chit-chat often end in dead-end text (“Misslyckades.” / generic fallback) with no next step. Dashboard already has `next_in_queue`; Telegram does not.

## Goals

1. Every operator-facing error/empty reply ends with a concrete recovery path (footer and/or keyboard).
2. No-LLM / budget / LLM-error paths reuse `prior_digest` + sticky IDs when tools did not re-fire.
3. Approve failures offer Regenerera / Visa / Lista (and after success: optional “nästa”).
4. Sticky short-message heuristic only fires on real order follow-ups (`wants_order_followup`).
5. Docs aligned: SOUL ~180 words; actor-map fail-closed when map is set.

## Non-goals

- Auto-send wiring (FU9)
- Luckra allowlist / `TELEGRAM_ACTOR_MAP` fail-closed
- Dashboard `/interact` multi-turn session
- TTL soft-expiry UX (later wave)

## Design

### Recovery helper (`bot/recovery.py`)

- `with_recovery(text, *, hints=...)` appends a short Swedish footer (2–3 steps).
- Presets: empty queue, not-found case/order, deny allowlist/actor, approve fail, unknown callback.
- `approve_fail_keyboard(case_id8)` → Regenerera / Visa / Lista.
- `approve_success_keyboard(next_id8 | None)` → optional Godkänn & nästa + Visa nästa.

### No-LLM continuity (`chat_agent.run_chat`)

When key/budget/error and no new tool bits: if `prior_digest` or sticky IDs exist, render digest/sticky + “fråga vidare…” instead of raw `FALLBACK_*`.

### Empty queue / not-found

`tool_list_cases`, `/cases list`, show/approve miss paths append empty-queue or not-found recovery.

### Callback deny parity

`handle_callback` uses the same allowlist/actor denial text (+ recovery) as `handle()`.

### Sticky tighten

Replace `len(text) < 48` OR with `wants_order_followup(text)` only in `gather_tool_results`.

### `/context`

Print `last_order_id`, `last_case_id8`, and active `flow` explicitly.

### Godkänn & nästa

After successful `approve_and_send`, call `CaseService.next_in_queue` and attach keyboard `cases:approve_next:{id8}` / show next. Callback routes to approve of that id8 (same as approve).

## Tests

- no-LLM + `prior_digest` / sticky
- empty queue contains poll/dashboard hint
- approve-fail keyboard present
- “hej” with sticky order does **not** prefetch order; “och frakten?” still does
- approve success may include next-in-queue keyboard when queue non-empty

## Principle

Security blocks actions toward the customer; the operator dialog always gets a path forward.
