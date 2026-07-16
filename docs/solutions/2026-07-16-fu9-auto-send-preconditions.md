# FU9 Auto-send — preconditions (rails only, no wire)

**Status:** **Not wired.** `should_auto_send` / day counter exist; poll does **not** call outbound send.  
**Default:** `config/cases_ai.yaml` → `auto_send_enabled: false`. Kill-switch: `AZOM_AUTO_SEND_KILL=1`.

## Do not enable until ALL are true

1. Sprint A+B green in prod (order panel, approve&next, extract, fixtures).  
2. Live soak (H2) completed; ≥2 weeks human approve without serious bad send.  
3. Suggest precision high on `order_status` live sample (0 FP on never-list).  
4. Oscar **written** enable for a bounded experiment window.  
5. Config overlay only (data dir), not blind repo flip.  
6. Wire **one** call site post-ingest eligible only (not broad poll send).  
7. Telemetry `case_auto_sent` + daily cap + conf ≥ 0.92 + order_id + `order_status` only.  
8. Rollback: set `auto_send_enabled: false` + `AZOM_AUTO_SEND_KILL=1` within 1 minute of incident.

## This commit

No sender wiring. Docs only — intentional gate.

## Related

- `skills/ecom_ops/cases/auto_send.py`  
- `docs/DEVELOPMENT_PLAN_FINISH.md` Fas 3  
- Soak: `docs/solutions/2026-07-16-live-soak-checklist.md`
