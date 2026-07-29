# Mail/kundhantering v2.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v2.2 residual: live mail probes + Graph/Outlook env docs (B1), soak-gated bulk close (B2), and hard-gated FU9 wire (Fas C) without flipping repo auto-send defaults.

**Architecture:** Keep `MailTransport` → `CaseService` → human `approve_and_send`. Fix Oscar `probe_mail` so prod uses a real client `fetch(limit=1)`. Bulk close loops existing `CaseService.close`. FU9 adds one post-ingest call site behind overlay + kill-switch.

**Tech Stack:** Python 3, Flask dashboard, SQLite cases.db, pytest, systemd poll timer.

**Spec:** [`docs/superpowers/specs/2026-07-29-mail-kundhantering-v22-design.md`](../specs/2026-07-29-mail-kundhantering-v22-design.md)

## Global Constraints

- Human approve remains the production send path; `config/cases_ai.yaml` keeps `auto_send_enabled: false` in repo.
- `AZOM_AUTO_SEND_KILL=1` must always force deny.
- Never enable NO/DK mailboxes in repo (`enabled: false`).
- No FAQ/KB, IMAP IDLE, V3, GA4, bulk approve/send, Outlook browser OAuth UI.
- TDD + mock-first; Swedish UI copy.
- Fas C must not start until Fas A H1–H3 exit gates are recorded as done + Oscar written enable.

## File map

| Path | Responsibility |
|------|----------------|
| `infrastructure/dashboard/secret_probes.py` | Live `probe_mail` when not mock |
| `.env.example` | Outlook/Graph/IMAP env matrix docs |
| `tests/test_probe_mail.py` | B1 coverage |
| `infrastructure/dashboard/templates/cases.html` | B2 bulk close UI |
| `infrastructure/dashboard/app.py` | B2 bulk close route |
| `skills/ecom_ops/cases/service.py` | Fas C single auto-send site |
| `skills/ecom_ops/cases/auto_send.py` | Rails + day counter (exists) |
| `tests/test_auto_send_rails.py` | Fas C deny/enable paths |
| `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md` | Rollback drill |

---

### Task B1: Live `probe_mail` + Outlook/Graph env matrix docs

**Files:**
- Modify: `infrastructure/dashboard/secret_probes.py`
- Modify: `.env.example`
- Create: `tests/test_probe_mail.py`

**Interfaces:**
- Consumes: `AZOM_USE_MOCK`, `client_from_env(use_mock=...)`, `MailClient.fetch`
- Produces: `ProbeResult` ok/error/missing; prod never uses InMemory unless mock env

- [x] **Step 1: Write the failing tests**
- [x] **Step 2: Run tests — expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run related tests**
- [ ] **Step 5: Commit** (when user requests)

---

### Task B2: Bulk close on cases list (after A1 friction notes)

**Gate:** Fas A A1 soak executed with friction note supporting bulk close (or Oscar explicit waive).

**Files:**
- Modify: `infrastructure/dashboard/templates/cases.html`
- Modify: `infrastructure/dashboard/app.py`
- Test: `tests/test_dashboard.py` or cases dashboard suite

**Interfaces:**
- Consumes: `CaseService.close(case_id, actor=...)`
- Produces: Swedish flash summarizing closed count; **never** calls approve/send

- [x] **Step 1: Failing test** — POST bulk close with two open case ids → both `closed`; no mail send. (`tests/test_cases_v2.py::test_bulk_close_closes_open_cases_without_send`; UI already on main)

- [x] **Step 2: Implement** — checkboxes + “Stäng valda”; RBAC `CASE_REPLY`; CSRF; loop `close`. (pre-existing on main)

- [x] **Step 3:** `pytest` dashboard/cases → PASS

---

### Task Fas C: FU9 auto-send wire (gated)

**Gate checklist (all required):**

1. Fas A H1–H3 done  
2. ≥2 weeks human approve without serious bad send  
3. Oscar written enable for experiment window  
4. Kill-switch drill practiced  

**Files:**
- Modify: `skills/ecom_ops/cases/service.py`
- Modify: `skills/ecom_ops/cases/auto_send.py` (if counter integration needed)
- Modify: `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md` (rollback 5 lines)
- Test: `tests/test_auto_send_rails.py`

- [ ] **Step 1:** Keep deny-by-default test (`auto_send_enabled` false → no send from poll).

- [ ] **Step 2:** Enable-path test with overlay + mock; ineligible categories never send.

- [ ] **Step 3:** Single post-ingest call site for **new** cases only; actor with `CASE_REPLY`.

- [ ] **Step 4:** Rollback drill in FU9 doc.

- [ ] **Step 5:** `pytest tests/test_auto_send_rails.py -v` → PASS

---

### Task Fas A / D (human — no code in this plan unit)

- Fas A: execute soak checklist, baseline, classify stick, weekly cadence.  
- Fas D: after ≥2 weeks KPI, record stop/continue/park in `docs/DEVELOPMENT_PLAN_FINISH.md`.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| B1 live probe + env matrix | Task B1 |
| B2 bulk close | Task B2 |
| Fas C FU9 wire | Task Fas C |
| Fas A/D human | Documented gates |
| Non-goals | Global Constraints |

## Execution order

```text
Task B1 (now) → Task B2 (after A1 notes) → Task Fas C (after H1–H3 + Oscar enable)
Fas A human in parallel from day 0
Fas D last
```
