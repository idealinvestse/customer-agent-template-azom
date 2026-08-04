---
title: Shadow Live Ledger - Plan
date: 2026-08-04
deepened: 2026-08-04
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
topic: shadow-live-ledger
origin: docs/plans/2026-08-04-001-feat-shadow-live-ledger-plan.md
---

# Shadow Live Ledger - Plan

## Goal Capsule

**Objective:** Ship a null-send runtime profile that runs the normal cases poll, records FU9 would-have decisions without any customer send path, shows Jonatan a per-case shadow hint, and gives Oscar a CLI summary of the trail.

**Product authority:** This plan owns the shadow profile + observation + both operator surfaces. Countersign / A1 signature ritual, wiring FU9 to actually send, and a separate sidecar cron are not active scope.

**Open blockers:** None. Live mailbox credentials remain Oscar-owned for the live-read rung; mock-capable shadow must work without them.

**Product Contract preservation:** unchanged meaning for R1–R12 / F1–F3; AE6 added (null-send + auto_send_enabled). Deepened 2026-08-04: System-Wide Impact, KTD1–KTD5/KTD6, U1–U5, Risks.

## Product Contract

### Summary

Introduce a **null-send profile**: cases poll (and related draft/suggest work) can run with outbound customer mail **absent by construction**, while FU9 eligibility is always evaluated and written as a shadow observation. Jonatan sees the observation on the case; Oscar reads a CLI report. Success for v1 is a trustworthy trail, not rate dashboards or soak countersignatures.

### Problem Frame

FU9 rails and A1 live soak are parked because there is no safe way to accumulate “what would we have sent?” evidence without a full live send session. Today the workaround is to do nothing. Soft-soak and classify-eval do not answer the FU9 counterfactual. The cost is a frozen auto-send debate and no progressive trust ladder between mock and live send.

### Key Decisions

- **Null-send profile over observe-in-default-poll or sidecar** — structural absence of send beats a flag or a second pipeline. (session-settled: user-directed — chosen over observe-inside-default-poll and sidecar: strongest safety story for a worthwhile process)
- **Both Oscar and Jonatan as first users** — evidence summary + per-case hint. (session-settled: user-directed — chosen over Oscar-only or Jonatan-only)
- **Visibility-first success** — trustworthy per-case trail + summary; aggregate rates deferred. (session-settled: user-directed — chosen over rate targets or Jonatan-flag-first bar)
- **No countersign ritual in v1** — badge + CLI report only. (session-settled: user-directed — chosen over countersign bundle MVP)
- **Mock-capable ladder first** — profile runs under mock for soft-soak immediately; live-read is the same profile with live transports, still null-send. (session-settled: user-approved — agent chose after user asked to decide for a worthwhile end-to-end process)
- **Observe during poll inside the profile** — shadow evaluation runs as part of poll while the profile is active, not as a separate pass. Governs R3, R4.

### Actors

- **Jonatan** — works the case queue; needs a clear shadow hint without new send powers.
- **Oscar** — reads the shadow report; may enable live-read env for the profile; never gets silent send from this feature.
- **Agent / poll operator** — runs poll under the null-send profile (mock soak or live-read).
- **System** — evaluates FU9 rails, writes observations, refuses any customer send in this profile.

### Requirements

**Profile and safety**

- R1. A named null-send runtime profile exists such that, while it is active, no customer-facing case reply or other customer mail can be sent — including if `auto_send_enabled` is true, approve UI is misused, or a future poll wiring mistake occurs.
- R2. Activating the profile must be intentional and visible in status/CLI output so operators know they are in null-send (not silent half-mock).
- R3. While the profile is active, cases poll still performs ingest → classify → draft → suggest-approve evaluation as today (mock or live reads per env).

**Shadow observation**

- R4. For each case that reaches draft/suggest evaluation under the profile, the system records a shadow observation: whether FU9 rails would have allowed auto-send, and if denied, the primary deny reason (enabled flag, kill-switch, category, confidence, order_id, escalated, daily cap, or equivalent).
- R5. Observations must be durable and queryable after the poll run (same operational durability class as existing telemetry), without storing unnecessary PII beyond what case telemetry already retains.
- R6. Evaluating or logging a shadow observation must never call a mail send API and must never set case state to a terminal “sent” outcome.

**Jonatan surface**

- R7. Open cases that have a shadow observation show a clear, non-alarming hint in the case UI (dashboard at minimum) distinguishing would-have-sent vs denied (+ short reason).
- R8. The hint must not look like an approve button or imply that mail was sent.

**Oscar surface**

- R9. A CLI command (or clear `kpis`/cases subcommand) summarizes recent shadow observations: counts of would-have-sent vs denied, deny-reason breakdown, and enough case identifiers to spot-check the trail.
- R10. v1 success is that Oscar trusts the trail exists and is complete for the runs performed — not that rates meet a threshold.

**Ladder**

- R11. Mock mode must be able to exercise the full null-send + observe + badge + report loop without live credentials.
- R12. The same profile must support a later live-read rung (real mail/Woo read, still null-send) without changing the product contract — only env/credentials.

### Key Flows

**F1. Soft-soak shadow run (mock)**

1. Operator starts null-send profile with mock transports.
2. Poll ingests/fixtures as today; drafts and suggest-approve run.
3. System writes shadow observation per R4–R6.
4. Jonatan opens a case and sees the hint (R7–R8).
5. Oscar runs the summary command (R9) and sees the trail.

**F2. Live-read shadow run (later rung)**

1. Oscar enables live read credentials with null-send profile still active.
2. Same as F1 with live inbox/Woo reads.
3. Any attempt to send customer mail fails closed with a clear error.

**F3. Approve under null-send**

1. Jonatan attempts approve-and-send while the profile is active.
2. Send is refused; case draft and shadow observation remain; operator is told why (null-send profile).

### Acceptance Examples

- AE1. Covers R1, R6. Under null-send, calling the human approve-and-send path does not deliver customer mail and does not record a successful `mail_send` / case-replied-as-sent outcome.
- AE2. Covers R4, R9. After a mock poll that produces N drafted cases, the summary reports N shadow observations and each case id can be traced to would-have-sent or a deny reason.
- AE3. Covers R7, R8. A case with deny reason “missing order_id” shows that reason in the UI without offering a one-click send.
- AE4. Covers R11. `bin/mock-soak-azom.sh` (or a documented one-flag variant) can run the shadow loop end-to-end in CI/dev without live secrets.
- AE5. Covers R3, R12. Switching only env from mock to live-read keeps null-send and continues writing observations; no code path treats that switch as permission to send.
- AE6. Covers R1, R6. Under null-send with `auto_send_enabled: true` and all other rails passing, `shadow_eligible=true` is recorded, but `MailService` still refuses and no customer mail is sent.

### Scope Boundaries

**In scope**

- Null-send profile, poll-time shadow observations, Jonatan case hint, Oscar CLI summary, mock-capable path, documented live-read rung.

**Deferred**

- A1 countersign / signature bundle that closes soak in `PILOT_OPS.md`
- Aggregate false-suggest rate KPIs and dashboards
- Sidecar shadow cron outside poll
- Content-bound approval tokens, reversible outbound hold, provenance/cite-or-abstain gates (separate ideation survivors)
- Messenger/Telegram shadow badge (dashboard first; bots later if needed)

**Non-goals**

- Wiring FU9 auto-send into poll
- Default-on auto-send
- FAQ/KB, V3 multi-tenant, GA4
- Recreating `docs/ideation/` / `docs/solutions/` / `docs/superpowers/`

### Assumptions

- A1. No existing cases “shadow” / null-send profile is already shipped (absence treated as true for planning).
- A2. FU9 deny reasons from current rails are sufficient for v1 observation fields; new predicates are out of scope unless required to record a reason.
- A3. Dashboard case detail is an acceptable first Jonatan surface; bot surfaces can wait.

### Outstanding Questions

**Deferred to Planning** — resolved in KTD1–KTD4 (no remaining Resolve Before Planning items).

### Risks

- Operators confuse null-send with “safe to enable auto_send_enabled” — mitigate via R2 visible status, AE6, and docs in `CASES.md` / `PILOT_OPS.md`.
- Live-read rung delayed forever — mitigate by making mock path the definition of done for v1 (R11) so value is not hostage to prod access.
- Callers that use `MailClient` / `client_from_env` directly bypass MailService — residual; mitigate with U1 source-grep regression + document that new outbound mail must go through MailService.
- Shadow telemetry PII — constrain `case_shadow_decision` meta to allowlisted fields only (see KTD6).
- Boolean-default migration inventing fake denies — nullable columns only (see KTD3).
- Null-send later disabled clearing history — never clear prior shadow fields when profile is off.

### Success Criteria

- Soft-soak (or equivalent) produces a complete shadow trail Oscar can read without inventing numbers.
- Jonatan can see per-case would-have vs deny during normal approve work.
- No customer mail is sendable under the profile in tests covering AE1.
- Team can schedule the live-read rung as env work, not a product redesign.

## Planning Contract

### Key Technical Decisions

- KTD1. **Activate via `AZOM_NULL_SEND=1` and CLI `--null-send`** — mirrors `--mock` / `AZOM_USE_MOCK`. Unset/`0`/`false` = off (existing send behavior). Status/CLI **always** print `null_send=on|off` (never omit). Document default-off in `docs/CASES.md`. Governs R1, R2.
- KTD2. **Fail closed at a shared MailService dispatch used by both `send` and `reply`** — `MailService.reply()` must not bypass the guard (it does not call `send` today). `approve_and_send` checks null-send **before** `claim_for_send` for a clear ops error without claim/release churn. Equal-risk entry points: CLI `mail send`/`mail reply`, CLI `cases reply`, dashboard approve, Messenger/Telegram approve. Governs R1, AE1, AE6, F3. (session-settled deepening: user-approved — accepted architecture+security findings over send-only guard)
- KTD3. **Dual durable record: nullable case columns + telemetry** — `shadow_eligible INTEGER NULL`, `shadow_deny_reason TEXT NULL` (no `NOT NULL DEFAULT 0`; `NULL` = not observed). No backfill. Emit `case_shadow_decision` telemetry. Under null-send, also recompute on `regenerate_draft`. When null-send is off: do not write new observations and **do not clear** existing fields. Serializers preserve `None` vs `false`. Governs R4, R5, R7, R9. (session-settled deepening: user-approved — accepted data-integrity findings)
- KTD4. **`explain_auto_send(...)` returns (eligible, reason)** — keep `should_auto_send` as bool wrapper; explainer returns first deny code (`auto_send_disabled`, `kill_switch`, `escalated`, `never_suggest_category`, `category_not_allowed`, `low_confidence`, `missing_order_id`, `daily_cap`, or `eligible`). Record under null-send after draft/suggest in `_ingest_message` (create + threaded append) — never sends. Governs R4, R6.
- KTD5. **Refuse-under-null-send is a distinct telemetry event** — `case_reply_blocked_null_send` (or equivalent) so AE1 is auditable and not confused with RBAC deny. Governs AE1, F3.
- KTD6. **Shadow telemetry meta allowlist** — only `case_id`, eligibility bool, deny/reason code, coarse tags (e.g. mailbox_id); never subject, body, from/to, or draft text. U3 report counts **latest-per-case** for column-aligned totals (raw events remain for history). Governs R5, R9, R10.

### System-Wide Impact

| Surface | Path | Null-send behavior | Owning tests |
|---|---|---|---|
| CLI `mail send` | `MailService.send` | refuse | U1 |
| CLI `mail reply` | `MailService.reply` → shared dispatch | refuse | U1 |
| CLI `cases reply` | `approve_and_send` | refuse before claim; KTD5 event | U1 |
| Dashboard approve POST | `approve_and_send` | refuse; flash not “Skickat” | U1 + U4 |
| Messenger/Telegram approve | `approve_and_send` | refuse; ops message | U1 |
| Poll ingest | `_ingest_message` | observe only; never mail | U2 + `tests/test_auto_send_rails.py` |
| Regenerate draft | `regenerate_draft` | recompute shadow under null-send | U2 |
| Status / CLI banner | status | always `null_send=on\|off` | U1 |
| Direct `MailClient` | bypass risk | residual — grep CI + docs | U1 + source assertion |

### Technical Design

```
AZOM_NULL_SEND=1 ──► MailService shared dispatch (send+reply) → fail (no client call)
                 └► approve_and_send → refuse before claim_for_send
                 └► _ingest_message / regenerate_draft → explain_auto_send → NULLABLE columns + telemetry
                 └► status banner → null_send=on|off (always)
                 └► dashboard case_detail/list → shadow badge (not approve CTA)
                 └► cases shadow-report → latest-per-case + allowlisted meta
```

Directional only — implementers choose exact exception / `MailSendResult` shape to match existing patterns.

### Patterns to Follow

- Fail-closed rails: `skills/ecom_ops/cases/auto_send.py` (`should_auto_send` deny-by-default).
- Case column migration: `skills/ecom_ops/cases/store.py` `suggest_approve` ALTER + dataclass field.
- Telemetry: `skills/ecom_ops/telemetry.py` `record(action=..., case_id=..., meta=...)`.
- CLI nesting: `skills/ecom_ops/cli.py` `cases` subcommands + `--mock` env set.
- Dashboard badges: `az-badge` in `infrastructure/dashboard/templates/case_detail.html` / `cases.html`.
- Poll must remain send-free: `tests/test_auto_send_rails.py` source assertions.

### Sequencing

1. U1 null-send profile + MailService guard (safety first)
2. U2 explain_auto_send + poll observation + store/telemetry
3. U3 CLI shadow-report + status visibility
4. U4 dashboard badge
5. U5 mock-soak + living docs

### Assumptions (planning)

- PA1. Extending SQLite case schema is acceptable (already done for `suggest_approve`).
- PA2. `auto_send_enabled: false` will make most shadow results deny with `auto_send_disabled` until Oscar enables experiment flags for observation testing — that is still a valid trail (visibility-first).
- PA3. CLI `mail send` and `mail reply` under null-send must fail (KTD2); operators use mock transports for non-customer tests.

## Implementation Units

### U1. Null-send profile and send guard

**Goal:** Make customer mail impossible while the profile is active, and make the profile visible.

**Requirements:** R1, R2; AE1, AE6, F3

**Files:**
- Modify: `skills/ecom_ops/actions/mail.py` (shared dispatch for `send` + `reply`)
- Modify: `skills/ecom_ops/cases/service.py` (`approve_and_send` before `claim_for_send`)
- Modify: `skills/ecom_ops/cli.py` (global `--null-send`, status output)
- Create: `skills/ecom_ops/runtime_profile.py` (or small helper for `null_send_active()`)
- Test: `tests/test_null_send_profile.py`

**Approach:**
- `null_send_active()` true when `AZOM_NULL_SEND` in `{1,true,yes}` or CLI sets it; unset/`0`/`false` = off.
- Shared MailService chokepoint refuses before any `client.send` / `client.reply`; no successful `mail_send` telemetry.
- `approve_and_send` refuses before `claim_for_send` + KTD5 telemetry.
- Status always includes `null_send=on|off`.
- Optional cheap source assertion: new `.client.send(` / `.client.reply(` outside `mail.py` fails CI (mirror `test_auto_send_rails`).

**Test scenarios:**
- Null-send off: existing mock send/reply unchanged.
- Null-send on: `MailService.send` does not call client send.
- Null-send on: `MailService.reply` (CLI `mail reply`) does not call client send/reply.
- Null-send on: `approve_and_send` errors without calling `claim_for_send` / without successful send telemetry.
- Dashboard / bot approve paths that call `approve_and_send` refuse (unit or integration with existing harnesses).
- Status output always includes `null_send=on` or `null_send=off`.
- `--null-send` sets env for the process.
- Telemetry refusal event contains no subject/body/from/to (KTD6).

**Verification:** pytest; no network.

---

### U2. Shadow decision at poll

**Goal:** Under null-send, every polled case that gets draft/suggest evaluation records would-have FU9 decision.

**Requirements:** R3, R4, R5, R6; AE2, AE6

**Files:**
- Modify: `skills/ecom_ops/cases/auto_send.py` (`explain_auto_send`)
- Modify: `skills/ecom_ops/cases/service.py` (`_ingest_message` create + threaded append; `regenerate_draft` / `_patch_case_after_regen`)
- Modify: `skills/ecom_ops/cases/store.py` (nullable columns via `_migrate_*` guarded ALTER; `Case` / `_row_to_case` / `to_dict`)
- Modify: `skills/ecom_ops/telemetry.py` only if action allowlist exists
- Test: `tests/test_shadow_decision.py`
- Modify: `tests/test_auto_send_rails.py` (poll still must not wire real auto-send)

**Approach:**
- `explain_auto_send` with ordered deny codes (KTD4); `should_auto_send` delegates.
- After suggest is set in `_ingest_message` (create and `append_inbound`), if null-send active → write nullable shadow columns + allowlisted `case_shadow_decision`.
- Same recompute under null-send in `regenerate_draft` path.
- Null-send off: no new writes; leave existing values untouched.
- Never call send; keep poll source-level assertions green.

**Test scenarios:**
- Null-send + missing order_id → `shadow_eligible=false`, reason `missing_order_id`, telemetry present.
- AE6: null-send + `auto_send_enabled=true` + rails pass → `shadow_eligible=true` and send still blocked.
- Regenerate under null-send updates columns and emits another telemetry event.
- Null-send off after prior observation → existing columns unchanged; no new telemetry.
- Pre-migration DB loads with shadow fields `None`.
- `to_dict` preserves `None` vs `false`.
- Poll source still contains no live auto-send dispatch.

**Verification:** pytest; mock poll fixtures.

---

### U3. Oscar CLI shadow report

**Goal:** Summarize the trail for Oscar.

**Requirements:** R9, R10; AE2

**Files:**
- Modify: `skills/ecom_ops/cli.py` (`cases shadow-report` or top-level)
- Create or modify: `skills/ecom_ops/cases/shadow_report.py` (or extend `kpis.py`)
- Test: `tests/test_shadow_report.py`
- Modify: `docs/CLI_REFERENCE.md`

**Approach:**
- Print eligible vs denied, deny-reason breakdown, recent case ids (`--days` default 7).
- Prefer case-store latest fields for counts; telemetry for history — latest-per-case for headline totals (KTD6).
- Warn (do not refuse) if run without null-send on live — trail may be incomplete; soft-soak uses null-send.
- Empty → friendly message; exit 0.

**Test scenarios:**
- Empty ledger → friendly empty message.
- Mixed eligible/denied → correct latest-per-case counts.
- Two telemetry events for one case after regenerate → headline count is 1 case.
- Meta fixtures with forbidden PII fields are not required for the report (allowlist enforced at write time in U2).

**Verification:** pytest capturing stdout / return structure.

---

### U4. Jonatan dashboard shadow hint

**Goal:** Show shadow state on case list/detail without looking like approve/send.

**Requirements:** R7, R8; AE3

**Files:**
- Modify: `infrastructure/dashboard/app.py` (pass shadow fields on case detail/list)
- Modify: `infrastructure/dashboard/templates/case_detail.html`
- Modify: `infrastructure/dashboard/templates/cases.html`
- Test: `tests/test_dashboard.py` (reuse `_load_dashboard_app`, `_auth_headers`; patterns from `test_case_detail_shows_draft_diff`, `test_case_detail_approve_guard`)

**Approach:**
- Badge e.g. `Skugga: nej (saknar order_id)` / `Skugga: skulle skickats` via `az-badge`, muted — never approve CTA styling.
- `NULL` shadow → no badge; `false` → deny reason; `true` → would-have label.
- Approve UI unchanged; null-send block from U1.

**Test scenarios:**
- Deny reason renders in case detail HTML.
- Eligible true renders distinct non-send label.
- Unevaluated (`None`) shows no shadow badge.
- Approve under null-send does not flash success “Skickat” (if covered in same harness).

**Verification:** pytest via `tests/test_dashboard.py` helpers.

---

### U5. Mock-soak wiring and living docs

**Goal:** One command path exercises the loop; docs match behavior.

**Requirements:** R2, R11, R12; AE4, AE5

**Files:**
- Modify: `bin/mock-soak-azom.sh` (export `AZOM_NULL_SEND=1`, run shadow-report)
- Modify: `docs/CASES.md` (null-send / shadow section; FU9 still not wired)
- Modify: `docs/PILOT_OPS.md` (optional soft-soak shadow note; do not mark A1 complete)
- Modify: `docs/CLI_REFERENCE.md` / `docs/CURRENT_STATE.md` as needed for accuracy
- Test: smoke via mock-soak script dry logic or document-only if script is bash-only — prefer a pytest that asserts env contract helpers

**Approach:**
- Soft-soak sets null-send + mock; ends with shadow-report.
- Docs state live-read rung = same flags with `AZOM_USE_MOCK=0` and credentials; still null-send.
- Explicitly: agents must not mark A1 soak done.

**Test scenarios:**
- Helper/docs assert null-send + mock combination is the supported soft path.
- CURRENT_STATE / CASES do not claim FU9 wired or soak complete.

**Verification:** pytest for helpers; manual/doc review for living docs.

## Verification Contract

- `pytest tests/test_null_send_profile.py tests/test_shadow_decision.py tests/test_shadow_report.py tests/test_auto_send_rails.py` (plus dashboard test path from U4)
- Keep CI ruff + coverage gate (≥65%) green; do not weaken tests to pass.
- Confirm poll still has no auto-send send wiring (`test_auto_send_rails` source checks).
- Prefer test-first for U1–U3 safety/observation units.

## Definition of Done

- All U1–U5 scenarios pass locally under mock (including AE6 and `mail reply` block).
- `bin/mock-soak-azom.sh` (updated) produces a non-empty or honestly-empty shadow report after poll without live secrets.
- Under null-send, no successful customer mail from approve, CLI `mail send`, or CLI `mail reply`.
- Living docs updated; FU9 remains not wired; A1 not marked complete.
- Product Contract R1–R12 + AE6 satisfied without expanding into deferred scope.

## Sources & Research

- Requirements origin: this file’s Product Contract (ce-brainstorm).
- Code grounding: `skills/ecom_ops/cases/auto_send.py`, `cases/service.py` `_ingest_message` / `approve_and_send` / `regenerate_draft`, `actions/mail.py` (`send` + `reply`), `telemetry.py`, `cli.py`, `cases/store.py` migrate helpers, dashboard templates + `tests/test_dashboard.py`, `tests/test_auto_send_rails.py`.
- Deepening (2026-08-04): architecture, security, repo-patterns, data-integrity agents — all findings accepted interactively.
- Ideation seed: CE scratch `ce-ideate/db93834d`; brainstorm grounding `ce-brainstorm/4ab14d26/grounding.md`.
