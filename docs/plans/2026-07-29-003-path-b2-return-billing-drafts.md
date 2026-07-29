---
title: "Core 3.0 Path B2 — return/billing draft quality + clearer triage"
date: 2026-07-29
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
origin: LFG autonomous ship (user: full autonomy + assumptions)
related:
  - docs/ideation/2026-07-29-azom-core-30-deepen.md
  - docs/DEVELOPMENT_PLAN_FINISH.md
  - docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md
  - AGENTS.md
  - SOUL.md
---

# Core 3.0 Path B2 — return/billing draft quality + clearer triage

> **For agentic workers:** Execute unit-by-unit with TDD. Progress is git-derived.
> **Pipeline mode:** LFG owns shipping after local verification.

## Goal Capsule

Improve Jonatan’s starting draft and triage signal for `return` and `billing` cases so manual review is faster, without widening ★ suggest-approve, wiring auto-send, enabling NO/DK mailboxes, or marking live soak done.

Authority: Core 3.0 Q2 (`docs/ideation/2026-07-29-azom-core-30-deepen.md`) + locked SOUL (human approve, no silent send).

Stop conditions: any change that sets `suggest_approve` true for return/billing/abuse; any `enabled: true` for NO/DK mailboxes; any auto-send poll wiring; any claim that FU6 soak is complete.

## Product Contract

### Problem

Return and billing drafts are thin one-liners (especially billing). Missing `order_id` does not trigger the SB5 soft-ask used for order_status/shipping. LLM draft prompt is category-agnostic. Cases stay `open` with no elevated triage signal, so Jonatan must re-read category to know they are never ★-safe.

### Requirements

- R1. Template `draft_reply()` for `return` and `billing` must ask for the operational checklist Jonatan needs (order id when missing, reason/photos for return, payment date/reference for billing) in sv/nb/da/en, without promising refunds or payment outcomes.
- R2. Soft order-number ask parity (SB6): when category is `return` or `billing` and `order_id` is absent, both template and LLM-fallback paths must include an order-number ask (same language rules as SB5).
- R3. Draft prompt `config/prompts.yaml` `draft` bumps to v1.2 with explicit return/billing guidance; still forbid invented refunds/legal outcomes.
- R4. `draft-eval` gains billing fixtures and stronger checks (`must_ask_order_id`, `must_not_promise_refund`); existing return fixtures keep passing; CI avg score gate unchanged (≥ 0.8).
- R5. Clearer triage: new return/billing cases get elevated `priority` (high) on ingest without changing `status` to `escalated` and without Oscar escalation tickets (abuse path unchanged).
- R6. Dashboard case detail shows a non-★ triage hint for return/billing (“Kräver manuell granskning”).
- R7. Suggest rails unchanged: return/billing/abuse never `suggest_approve`; `cases_ai.yaml` thresholds untouched (FU7 blocked on soak).
- R8. Docs: mark Path B2 Q2 as shipped in Core 3.0 ideation + DEVELOPMENT_PLAN_FINISH pointer.

### Actors

- Jonatan: reads richer drafts, sees triage priority/hint, still approve/sends manually.
- Oscar: unchanged escalation for abuse/critical only.
- Agent/poll: produces drafts; never silent send.

### Acceptance examples

- AE1. Mock inbound “Jag vill returnera, fel storlek” (no order id) → category `return`, draft asks for ordernummer, no refund promise, `suggest_approve=false`, `priority=high`.
- AE2. Mock inbound “Faktura 3010 betald två gånger” with order 3010 → category `billing`, draft references next review steps + payment date ask where useful, never ★, `priority=high`.
- AE3. Abuse keyword inbound still escalates to Oscar; Path B2 must not weaken that path.
- AE4. `python -m ecom_ops draft-eval` still ≥ 0.8 average with new fixtures.

### Out of scope

- FU6 live soak / baseline human fill
- FU7 threshold tuning / allowlist widen
- FU9 auto-send wire
- NO/DK mailbox `enabled: true`
- Path B2 returns as auto-escalation tickets to Oscar
- Multi-tenant / FAQ / GA4

## Planning Contract

### Key Technical Decisions

- KTD1. `session-settled:` Ship Path B2 draft+triage now rather than waiting for soak-gated FU7 — soak is Oscar-owned; drafts improve mock/pilot quality immediately. Rejected: idle until soak samples.
- KTD2. `session-settled:` Clearer escalate = elevated case `priority=high` + UI hint, not Oscar `escalated` tickets for all returns — avoids alert spam; abuse remains the only auto-Oscar path. Rejected: auto-escalate every return/billing.
- KTD3. `session-settled:` Extend SB5 soft-ask helper to return/billing (shared language block) instead of duplicating four language strings in a third place. Rejected: copy-paste a second ask block.
- KTD4. Prompt bump is additive category bullets in `draft` v1.2; keep classify prompt at 1.1. Rejected: rewrite classify for FU7 without samples.
- KTD5. `session-settled:` Do not touch `config/cases_ai.yaml` suggest allowlist/thresholds or `config/mailboxes.yaml` enabled flags.

### Assumptions

- A1. Swedish is primary; nb/da/en stay in parity for template strings.
- A2. Existing LLM guardrails against refund promises remain sufficient; new templates reinforce them.
- A3. Priority field already drives list sort (escalated → high → suggest → newest); elevating return/billing surfaces them without new schema.

### Technical design

```text
SupportService.handle
  → draft (LLM | draft_reply)
  → SB6 soft-ask if return|billing|order_status|shipping and no order_id
  → suggest rails (unchanged)
CaseService._ingest_message
  → if category in {return, billing} and not abuse: priority = high
Dashboard case_detail
  → triage hint when category in {return, billing}
```

### Sequencing

U1 → U2 → U3 → U4 → U5 (docs last). U1–U3 are code-critical; U4 is thin UI; U5 is docs.

## Implementation Units

### U1. Richer return/billing templates + shared soft-ask (SB6)

**Problem:** Thin bodies; return/billing skip soft-ask when `order_id` missing.

| | |
|--|--|
| **Behavior** | Expand `draft_reply()` RETURN/BILLING bodies (sv/nb/da/en) with checklist language; no refund/payment promises. Extract soft-ask append into a small helper used for order_status, shipping, return, and billing when `order_id` is absent. |
| **Tests** | `tests/test_support.py` — return/billing bodies mention ordernummer when missing; never refund promise; suggest false on handle. |
| **Files** | `skills/ecom_ops/actions/support.py`, `tests/test_support.py` |

**Done when:** template + handle soft-ask cover return/billing; abuse path unchanged.

### U2. Draft prompt v1.2 category guidance

| | |
|--|--|
| **Behavior** | Bump `draft.version` to `1.2`; add short bullets for return (checklist, angrerett as process only) and billing (ask order/payment refs, no outcome promises). Sync Python builtin fallback in `prompts.py` if present. |
| **Tests** | Existing prompt load tests / `tests/test_llm_support_drafts.py` still pass; assert version 1.2 when loading draft prompt. |
| **Files** | `config/prompts.yaml`, `skills/ecom_ops/prompts.py`, relevant tests |

**Done when:** `get_prompt("draft")` returns version `1.2` with return/billing guidance text.

### U3. Draft-eval fixtures + checks

| | |
|--|--|
| **Behavior** | Add `billing_sv.json`, `billing_nb.json`, `return_missing_oid_sv.json` (and optional billing missing oid). Extend `_check_draft` for `must_ask_order_id` when fixture sets it. Keep `must_not_promise_refund`. |
| **Tests** | `tests/test_draft_eval.py`; run `python -m ecom_ops draft-eval`. |
| **Files** | `tests/fixtures/draft_quality/*`, `skills/ecom_ops/draft_eval.py`, `tests/test_draft_eval.py` |

**Done when:** new fixtures load; avg score ≥ 0.8.

### U4. Triage priority + dashboard hint

| | |
|--|--|
| **Behavior** | On create (and threaded update when category is return/billing), set `priority="high"` unless already escalated/abuse. Case detail template shows muted hint that ★ is not available / manual review required. |
| **Tests** | `tests/test_cases_v2.py` or focused case ingest test; dashboard template test if present. |
| **Files** | `skills/ecom_ops/cases/service.py`, `infrastructure/dashboard/templates/case_detail.html`, tests |

**Done when:** mock return case has `priority=high`, `status=open`, `suggest_approve=false`, hint visible in template context/HTML.

### U5. Docs status sync

| | |
|--|--|
| **Behavior** | Update Core 3.0 ideation Q2 / Path B2 row to shipped (code); note FU6/FU7 still open. Brief pointer in `DEVELOPMENT_PLAN_FINISH.md` §16. |
| **Tests** | None (docs). |
| **Files** | `docs/ideation/2026-07-29-azom-core-30-deepen.md`, `docs/DEVELOPMENT_PLAN_FINISH.md` |

## Verification Contract

- Targeted: `pytest tests/test_support.py tests/test_draft_eval.py tests/test_cases_v2.py tests/test_llm_support_drafts.py -q`
- Draft eval: `python -m ecom_ops draft-eval`
- Broader: `pytest -q` (CI also ruff + cov ≥ 65%)
- Manual sanity: `--mock` support/cases path for a return without order id
- Execution direction: test-first for U1–U4 behavior units

## Definition of Done

- All U1–U5 complete on a feature branch
- Return/billing never ★; abuse escalation intact
- Soft-ask present when order_id missing for return/billing
- `draft-eval` ≥ 0.8 with new fixtures
- Docs reflect Path B2 code shipped; soak still Oscar-owned
- No mailbox enable / no auto-send wire / no `cases_ai.yaml` threshold edits

## Appendix

### Settled-decisions brief (LFG intake)

- Direction: Autonomous Core 3.0 Fas 3 code slice (Path B2), ship to PR.
- Decision: Path B2 drafts now / not wait for soak — provenance `user-directed` (full autonomy) — rejected wait-idle — reason soak is human-gated.
- Decision: priority+UI triage not Oscar auto-escalate — provenance `user-directed` (assumption) — rejected ticket spam — reason abuse path must stay distinct.
- Decision: no auto-send / no NO-DK enable — provenance `user-approved` (AGENTS/Core 3.0) — rejected silent enable.
- Open: FU6 soak, FU7 tune, FU9 wire (explicitly deferred).
- Report conflicts if research invalidates any of the above.

### Research breadcrumbs

- `skills/ecom_ops/actions/support.py` — `draft_reply`, SB5 soft-ask block ~553–584
- `skills/ecom_ops/cases/suggest.py` — never-list includes return/billing
- `config/prompts.yaml` — draft 1.1
- `tests/fixtures/draft_quality/` — return_sv/nb exist; no billing fixtures
- Explore: Path B2 not built; regenerate/budget/brief/classify-eval already shipped
