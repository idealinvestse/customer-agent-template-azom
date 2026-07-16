---
title: "Sprint A → B: approve-flow friction + measure, then suggest coverage"
date: 2026-07-16
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
origin: critical review 2026-07-16 (capacity & usability)
related:
  - docs/DEVELOPMENT_PLAN_FINISH.md
  - docs/ideation/baseline-capture.md
  - docs/superpowers/plans/2026-07-11-001-feat-cases-ai-quality-path-b-plan.md
  - docs/SYSTEM_OVERVIEW.md
  - AGENTS.md
---

# Sprint A → B — Approve friction · Measure · Suggest coverage

> **For agentic workers:** Execute **unit-by-unit with TDD**. Progress is git-derived, not checkboxes alone.  
> **Sprint B starts automatically** when Sprint A exit gates are green — do **not** stop for a new planning round unless a gate fails or scope expands.

**Goal:** Cut Jonatan’s time per approve/send (Sprint A), make the 50 % story measurable the same sprint, then **without pause** raise share of ★ suggest-approve cases (Sprint B).

**Not goals:** auto-send wire, V3, GA4, IMAP IDLE, FAQ/KB, default-on anything outbound without human confirm.

---

## 0. Continuity model (auto-handoff A → B)

```text
                    Sprint A exit green?
                           │
              ┌────────────┴────────────┐
              │ YES                     │ NO
              ▼                         ▼
     Start SA→SB handoff           Fix blockers
     immediately (same PR          (max 1 follow-up
     train / next commit)          unit), then re-check
              │
              ▼
           Sprint B units SB1…
```

### Sprint A exit gates (all required)

| # | Gate | How to verify |
|---|------|----------------|
| G1 | Approve path fewer clicks | `Nästa` / `Godkänn & nästa` works in dashboard; filter preserved for open queue |
| G2 | Order facts visible without re-reading draft | Order panel on case detail when `order_id` present |
| G3 | ★ visibility | Nav or overview shows `suggest_count`; list filter `?suggest=1` still works |
| G4 | Telegram brief parity | `/brief` includes open / escalated / ★ / readiness / budget warn (same fields as shell brief subset) |
| G5 | Measure surface | CLI (or `status`/`brief` section) prints last-7d: n_approve, median `time_to_approve_sec`, mean `draft_edit_distance`; baseline doc has filled *proxy row or explicit blocked-on-contact* |
| G6 | Tests green | `pytest` + ruff; targeted tests for each SA unit |

**When G1–G6 hold:** open Sprint B with unit **SB1** in the **same workstream** (new branch OK, but no “waiting for replan”).

**Hard stop / replan only if:** live suggest precision crisis (many bad ★), mail/poll P0 outage, or Oscar blocks further AI spend.

---

## 1. Sprint map

| Sprint | Theme | Primary outcome | Maps finish-plan |
|--------|--------|-----------------|------------------|
| **A** | Friction + measure | Faster approve; KPI visible | FU8 (forced picks) + residual measure |
| **B** | Suggest coverage + quality | More safe ★; richer order context | FU7 + capacity gaps |
| **C** (later) | Ops hardening | Partial poll fail, auth defaults | finish F1.3 / backlog B/C |
| **D** (gated) | Auto-send experiment | Only if B green + Oscar | FU9 |

Horizon: Sprint A ≈ **1.5–3 dev-days**. Sprint B ≈ **3–6 dev-days** (depends on live sample access).

---

## 2. Sprint A — units (execute in order)

### SA1 · Order panel on case detail (P0)

**Problem:** Order truth is only inside draft text (`[Order …]`). Operator re-reads the whole draft to check status/tracking.

| | |
|--|--|
| **Behavior** | On `GET /cases/<id>`, if `case.order_id`: resolve order context (mock-aware) and render a dedicated **Order**-card above or beside draft (status, total, line items, tracking if present). Missing Woo → muted “Kunde inte hämta order”. |
| **API** | Prefer `resolve_order_context` / `format_order_context_block`; optional structured dict helper for templates if text parse is fragile. |
| **RBAC** | Same as case detail (auth user with case access). |
| **Never** | Invent tracking; never write Woo from panel. |
| **Tests** | Template/context contains order card when mock order 1001; empty when no `order_id`. |
| **Files** | `infrastructure/dashboard/app.py`, `templates/case_detail.html`, maybe `order_context.py`, `tests/test_dashboard_*.py` or extend existing dashboard tests |

**Done when:** mock case with order_id shows panel fields without scrolling the draft.

---

### SA2 · “Godkänn & nästa” + “Nästa i kö” (P0)

**Problem:** After send/close, operator returns to list and re-clicks.

| | |
|--|--|
| **Behavior** | After successful `reply` or `close`, optional redirect to **next** active case in current queue semantics: default `status=open,escalated`, prefer same mailbox/category/suggest filters from query/form if present. Button **Nästa** on open cases (no send). Primary button can be **Godkänn och skicka & nästa** (still requires confirm dialog). If no next → back to list with msg. |
| **Service** | Small helper: `CaseStore`/`CaseService.next_case_id(after_id, *, status, mailbox, category, suggest_only)` using same sort as list (escalated → high → suggest → newest). |
| **Tests** | Two open cases → approve first → lands on second id. |
| **Files** | `cases/store.py` or `service.py`, `app.py`, `case_detail.html`, tests |

**Done when:** approve path is list → detail → send → **auto next** without re-opening list (except empty queue).

---

### SA3 · Suggest count in ops counts + overview (P0)

**Problem:** Nav/overview shows open/escalated but not how many low-friction ★ cases await.

| | |
|--|--|
| **Behavior** | `_ops_counts()` adds `suggest_cases` (count active with `suggest_approve=1`). Nav badge and/or overview card links to `/cases?status=open,escalated&suggest=1`. |
| **Store** | `count_suggest_approve(status=…)` or filter count if cheap. |
| **Tests** | Fixture cases with/without flag → count matches. |
| **Files** | `app.py`, `base.html` / `index.html`, `cases/store.py` if needed |

**Done when:** operator sees ★ count without opening cases list filter first.

---

### SA4 · Telegram `/brief` = shell brief subset (P0)

**Problem:** `bin/daily-brief-azom.sh` has cases + readiness + budget; `cmd_brief` only customer + cost.

| | |
|--|--|
| **Behavior** | `/brief` prints: customer/domains; `cases.open`, `escalated`, `suggest_approve`, `queue_total`; readiness stale/ok + last poll age if known; budget used/cap + near_cap line; optional top-1 stuck id8. Reuse `budget_status`, `readiness_from_last_poll`, `CaseService.list_open` (cap 50). No secrets. |
| **Parity** | Align field names with brief JSON keys where practical so agents/docs stay one vocabulary. |
| **Tests** | Mock list_open + budget → string contains suggest count and budget message when near cap. |
| **Files** | `bot/openclaw_commands.py`, tests under `tests/test_*brief*` or openclaw tests |

**Done when:** `/brief` answers “hur ser kön ut?” without dashboard.

---

### SA5 · Last-7d support KPI + baseline surface (P0 measure)

**Problem:** `time_to_approve_sec` / `draft_edit_distance` are written but never aggregated for operators.

| | |
|--|--|
| **CLI** | `python -m ecom_ops kpis` (or `status --kpis`) → last 7d from telemetry JSONL: `n_case_approved` (or equivalent action name used today), median `time_to_approve_sec`, mean `draft_edit_distance`, n with suggest if meta has it. |
| **Optional thin UI** | One line on dashboard overview or in `/brief` under “KPI 7d”. |
| **Baseline** | Update `docs/ideation/baseline-capture.md`: either proxy numbers from first live week **or** row `blocked_on: Jonatan contact` with date — never invent hours. |
| **Tests** | Synthetic telemetry lines → correct median/mean. |
| **Files** | `telemetry.py` helpers, `cli.py`, maybe `budget.py`-style module `kpis.py`, baseline md, tests |

**Done when:** one command prints 7d KPI; baseline doc not all blank (number **or** explicit blocked note).

---

### SA6 (optional if time) · Sticky regenerate / list quick actions (P2)

Only if SA1–SA5 done and day left:

- Telegram regenerate uses sticky last case id when id omitted.
- Dashboard list: “Öppna ★” as default landing for Jonatan link from nav (already partially via suggest filter).

**Stop rule:** if not shipping same day, defer to SB polish — do not delay Sprint B handoff.

---

## 3. Sprint A Definition of Done

- [ ] SA1–SA5 merged (or SA6 skipped with note)
- [ ] G1–G6 exit gates green
- [ ] No silent send regressions (approve still confirm / explicit path)
- [ ] Docs: this plan status → **Sprint A shipped**; **Sprint B in progress**
- [ ] AGENTS.md / finish plan pointer updated if needed

**Immediately after:** start **SB1** (same session/branch train allowed).

---

## 4. Sprint B — units (auto-start after A)

### SB1 · Stronger order_id extraction (P0 capacity)

| | |
|--|--|
| **Behavior** | Expand `ORDER_RE` / extractors: bare 4–12 digit in subject when clear; SV/NO/DK phrases (`ordernummer`, `beställning`, `ordre`, `bestilling`); keep abuse path untouched. Prefer shared helper used by support + chat_agent (dedupe regex drift). |
| **Tests** | Fixture table of subjects/bodies → expected ids; false positives documented. |
| **Files** | `actions/support.py`, maybe `chat_agent.py`, tests |

---

### SB2 · Woo lookup by customer email (P0 capacity)

| | |
|--|--|
| **Behavior** | `WooClient.find_orders_by_email(email, *, per_page=5)` (REST `email` or search). On ingest when `order_id` missing: if single recent match → set candidate `order_id` + context; if multiple → draft note “flera ordrar, bekräfta nummer” **without** auto-suggest unless policy says so (default: **no** suggest without explicit id or single unambiguous match + high conf). |
| **Safety** | Never pick wrong order silently for suggest-approve; prefer escalate confidence down. |
| **Tests** | mock multi-order vs single-order paths. |
| **Files** | `integrations/woocommerce.py`, `support.py` / `cases/service.py` poll ingest, tests |

---

### SB3 · Richer safe order_context (P1 quality)

| | |
|--|--|
| **Fields** | Add when present in Woo raw: `date_created`, `payment_method_title`, shipping method/title, `customer_note` (truncated), billing country (not full address if PII-heavy — prefer city/country only). Keep “never invent tracking”. |
| **Dashboard** | Order panel (SA1) shows new fields automatically if using shared formatter. |
| **Tests** | formatter includes new keys when raw present. |
| **Files** | `order_context.py`, tests |

---

### SB4 · Classify / suggest calibration loop (P1 finish FU7)

| | |
|--|--|
| **Tooling** | Fixture pack `tests/fixtures/support_classify/*.json` (anonymized); optional CLI `python -m ecom_ops classify-eval --fixtures …` scoring keyword vs hybrid. |
| **Config** | Adjust `cases_ai.yaml` thresholds **only** with fixture+live sample notes (document in fixture README or solutions note). |
| **Feedback thin** | Optional dashboard/Telegram “fel kategori” telemetry action `case_classify_feedback` — store category_actual in meta (no PII body). |
| **Tests** | Regression: return/billing never suggest; abuse gate holds. |
| **Files** | `support.py`, `suggest.py`, `cases_ai.yaml`, fixtures, tests |

---

### SB5 (optional) · Soft non-★ draft for missing order (P2)

If SB1–SB2 still leave many untagged status mails: draft template that asks for order number without setting `suggest_approve`. Keeps human path but reduces blank thinking.

---

## 5. Sprint B exit gates

| # | Gate |
|---|------|
| H1 | Fixture suite green; no FP suggest on return/billing/abuse |
| H2 | Live or mock soak: ★ rate improved **or** documented why (e.g. mailbox content) |
| H3 | Order panel shows richer context when Woo returns fields |
| H4 | KPI 7d still green (no regression from extra LLM) |
| H5 | Document: threshold changes + sample notes in `docs/solutions/` or ideation |

**Then decide** (product, not auto): Sprint C ops harden vs wait-and-measure 2 weeks vs FU9 auto-send preconditions.

---

## 6. Out of scope (both sprints)

- Wiring `should_auto_send` into poll sender
- Bulk close (nice; only if both sprints early)
- TELEGRAM fail-closed actor rewrite (Sprint C unless security incident)
- Multi-tenant / GA4 / IMAP IDLE

---

## 7. Execution checklist for agents

1. Read this plan + `AGENTS.md` + `SOUL.md` (no silent send).
2. Implement **SA1** with failing test first; open PR or commit slice.
3. SA2 → SA3 → SA4 → SA5 sequentially unless independent (SA3 can parallel SA1).
4. Run `pytest` + ruff for touched areas.
5. Verify G1–G6; mark Sprint A done in plan status line.
6. **Without waiting:** start **SB1**.
7. Stop and ask human only if: prod credentials needed for SB2 live, PII in fixtures, or threshold widen disputed.

### Suggested git commit granularity

```text
feat(dashboard): order panel on case detail (SA1)
feat(dashboard): approve-and-next queue navigation (SA2)
feat(dashboard): suggest-approve count in nav (SA3)
feat(telegram): brief cases+readiness+budget (SA4)
feat(ops): last-7d case KPI CLI + baseline surface (SA5)
feat(support): stronger order id extraction (SB1)
feat(woo): find orders by email for draft context (SB2)
feat(order): richer safe order context fields (SB3)
test(support): classify fixtures + threshold note (SB4)
```

---

## 8. Traceability

| Unit | Review finding | Finish-plan / Path B |
|------|----------------|----------------------|
| SA1 | Order panel missing | FU8 polish |
| SA2 | Extra clicks after approve | FU8 polish |
| SA3 | No ★ count in overview | F3 / ops UX |
| SA4 | `/brief` thin vs shell | FU4 residual |
| SA5 | KPI not aggregated; baseline TBD | FU3 residual |
| SB1–SB2 | Suggest rarity / no email lookup | FU7 / capacity |
| SB3 | Thin order_context | Path B U2 follow-up |
| SB4 | Calibrate thresholds | FU7 |

---

## 9. Status log

| Date | Event |
|------|--------|
| 2026-07-16 | Plan authored; Sprint A not started |
| 2026-07-16 | Sprint A SA1–SA5 implemented (order panel, next, ★ count, /brief, kpis CLI) |
| 2026-07-16 | Sprint A tests green; SB1 order extract + SB2 email→Woo unique match shipped |
| 2026-07-16 | P1 review fixes committed (`660f437`); SB3 richer context + SB4 fixtures |
| 2026-07-16 | Live soak checklist + FU9 preconditions docs; Sprint C partial poll + actor fail-closed; SB5 soft ask |

**Current:** Code complete for A/B/C+SB5. **H2 live soak** still requires prod host run. FU9 not wired.

---

## 10. First command after approve

```bash
# From repo root — confirm green baseline before SA1
pytest -q --tb=no
# Then implement SA1 with TDD against mock case + order 1001
```
