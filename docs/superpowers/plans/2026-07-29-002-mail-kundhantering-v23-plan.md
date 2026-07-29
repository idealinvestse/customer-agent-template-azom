# Mail/kundhantering v2.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land v2.2 on main, support Fas A human gates, and only then wire gated FU9 — never flip repo auto-send default.

**Architecture:** Unchanged mail→case→approve path. FU9 adds one post-ingest call site behind overlay + kill-switch.

**Tech Stack:** Python 3, Flask, SQLite cases.db, pytest, systemd poll timer.

**Spec:** [`docs/superpowers/specs/2026-07-29-mail-kundhantering-v23-design.md`](../specs/2026-07-29-mail-kundhantering-v23-design.md)

## Global Constraints

- `config/cases_ai.yaml` keeps `auto_send_enabled: false` in repo.
- `AZOM_AUTO_SEND_KILL=1` always forces deny.
- Never enable NO/DK mailboxes in repo.
- No FAQ/KB, IMAP IDLE, V3, GA4, bulk approve.
- Fas C must not start until Fas A H1–H3 + Oscar written enable + ≥2 weeks clean approve.

---

### Task 0: Land v2.2 (agent)

**Files (stage all):**
- `.env.example`
- `infrastructure/dashboard/secret_probes.py`
- `tests/test_probe_mail.py`
- `tests/test_cases_v2.py` (bulk close characterization)
- `docs/superpowers/specs/2026-07-29-mail-kundhantering-v22-design.md`
- `docs/superpowers/plans/2026-07-29-001-mail-kundhantering-v22-plan.md`
- `docs/DEVELOPMENT_PLAN_FINISH.md` (v2.2 + v2.3 pointers)
- Plus v2.3 spec/plan when authored in same session

- [x] **Step 1:** `pytest tests/test_probe_mail.py tests/test_cases_v2.py::test_bulk_close_closes_open_cases_without_send -q`
- [x] **Step 2:** Commit with message focusing on live probe_mail + v2.2 docs
- [x] **Step 3:** Push to `origin/main`

---

### Task A: Fas A ops proof (human)

- [ ] **A1:** Execute live soak checklist; fill outcome log (replace `_TBD_`)
- [ ] **A2:** Fill `docs/ideation/baseline-capture.md`
- [ ] **A3:** classify-eval on redacted samples; threshold-only tunes
- [ ] **A4:** Three weekly syncs

Agent may add fixtures after A3; must not mark H1 done without Oscar.

**2026-07-29 agent note:** A1–A4 remain open. Explicit blocker recorded in soak checklist outcome (`blocked_on: Oscar prod access`) and baseline notes — not a substitute for live soak.

---

### Task C: FU9 wire (gated)

**Gate:** A1–A3 + ≥2 weeks clean approve + Oscar written enable + kill-switch drill.

**Files:**
- Modify: `skills/ecom_ops/cases/service.py`
- Modify: `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`
- Test: `tests/test_auto_send_rails.py`

- [x] **Step 1:** Deny-by-default test remains green (`pytest tests/test_auto_send_rails.py` 2026-07-29)
- [ ] **Step 2:** Enable-path mock test (order_status + order_id + conf only) — only with wire plan
- [ ] **Step 3:** Single post-ingest call site; overlay only — **do not start**
- [x] **Step 4:** Rollback section in FU9 doc (2026-07-29)
- [x] **Step 5:** `pytest tests/test_auto_send_rails.py -v` → PASS (deny path; no wire)

---

### Task D: Fas 4 re-evaluate (human)

- [x] **2026-07-29 interim:** Record stop / narrow continue / park GA4 / park V3 in finish-plan execution log → **park** expansion until A1–A4 KPI; continue SE human-approve only (see finish plan Fas D row)

---

## Execution order

```text
Task 0 (land v2.2) → Task A (human) → Task C (only if gated) → Task D
```
