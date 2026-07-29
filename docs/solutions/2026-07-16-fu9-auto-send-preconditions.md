# FU9 Auto-send — preconditions (rails only, no wire)

**Status:** **Not wired.** `should_auto_send` / day counter exist; poll does **not** call outbound send.  
**Default:** `config/cases_ai.yaml` → `auto_send_enabled: false`. Kill-switch: `AZOM_AUTO_SEND_KILL=1`.  
**v2.3 plan:** Fas 3 is **docs + gates only** — do not implement wire in the Live-proof plan. Wire requires a **new** approved plan after all preconditions below.

## Do not enable until ALL are true

1. Sprint A+B green in prod (order panel, approve&next, extract, fixtures).  
2. Live soak (FU6 / A1) completed; ≥2 weeks human approve without serious bad send.  
3. Suggest precision high on `order_status` live sample (0 FP on never-list).  
4. Oscar **written** enable for a bounded experiment window.  
5. Config overlay only (data dir), not blind repo flip.  
6. Wire **one** call site post-ingest eligible only (not broad poll send).  
7. Telemetry `case_auto_sent` + daily cap + conf ≥ 0.92 + order_id + `order_status` only.  
8. Rollback drill practiced (see below).

## Rollback drill (practice before any wire)

Within **1 minute** of a bad auto-send incident:

1. Set overlay / data-dir config: `auto_send_enabled: false` (never rely on repo alone).  
2. Set env: `AZOM_AUTO_SEND_KILL=1`.  
3. Restart `azom-cases-poll.timer` (or equivalent poll service).  
4. Confirm with `python -m ecom_ops status` / logs: no further auto-sends.  
5. Audit last `case_auto_sent` telemetry events; escalate to Oscar.

## Gate status (2026-07-29)

| Precondition | Status |
|--------------|--------|
| 1 Sprint A+B code | ✅ in repo |
| 2 Live soak + 2 weeks clean approve | ⬜ blocked_on Oscar A1 |
| 3 Suggest precision live sample | ⬜ blocked_on A3 classify stick |
| 4 Oscar written enable | ⬜ |
| 5 Overlay-only config | ⬜ (not started) |
| 6 Single post-ingest call site | ⬜ **not wired** (intentional) |
| 7 Telemetry + caps | ✅ rails exist; unused until wire |
| 8 Rollback drill practiced | ⬜ after soak |

**Agent rule:** do not open a PR that calls send from poll until Oscar written enable + this checklist is green.

## Related

- `skills/ecom_ops/cases/auto_send.py`  
- `skills/ecom_ops/cases/service.py` — `evaluate_auto_send_eligibility` (checkpoint only)  
- `tests/test_auto_send_rails.py` — deny-by-default + `test_poll_source_does_not_call_auto_send`  
- `docs/DEVELOPMENT_PLAN_FINISH.md` Fas 3 / v2.3 Fas C  
- Soak: `docs/solutions/2026-07-16-live-soak-checklist.md`  
- v2.3 plan: `docs/superpowers/plans/2026-07-29-002-mail-kundhantering-v23-plan.md`
