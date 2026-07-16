# Support-time baseline capture (thin track)

**Purpose:** Make the AGENTS.md goal (“50% mindre support-tid”) measurable without blocking product work.  
**Owner:** User + Jonatan (async OK). Oscar does not invent hours.  
**Related:** [`DEVELOPMENT_PLAN_FINISH.md`](../DEVELOPMENT_PLAN_FINISH.md) FU3 · overview locked decisions.

## Instructions

1. Pick a **start date** (when measurement begins).
2. Prefer one primary metric:
   - **A.** Hours/week Jonatan spends on mail support (self-report), or  
   - **B.** Proxy: `median(time_to_approve_sec) × cases_approved_per_week` from telemetry when enough events exist.
3. Note source (conversation, spreadsheet, telemetry export).
4. Re-measure after 2–4 weeks of suggest-approve + regenerate in live use.

## Capture table

| Field | Value |
|-------|--------|
| start_date | 2026-07-16 (tooling ready; live number pending) |
| method | telemetry_proxy (prefer) / hours_per_week when Jonatan reachable |
| baseline_hours_per_week | _blocked_on: Jonatan contact_ |
| baseline_median_time_to_approve_sec | Run `python -m ecom_ops kpis --days 7` after first live approve week |
| baseline_cases_per_week | _TBD from kpis n_case_approved × scale_ |
| source | telemetry.jsonl via `ecom_ops.kpis.support_kpis_last_days` / CLI `kpis` |
| notes | Tooling ready (`python -m ecom_ops kpis --days 7`, dashboard KPI card). Fill numbers after live soak; do not invent hours. |

## Follow-ups (append)

| date | hours or proxy | notes |
|------|----------------|-------|
| | | |

## Cadence log (weekly sync samples)

| week of | Jonatan friction note | Oscar ops note | decide suggest/auto? |
|---------|----------------------|----------------|----------------------|
| | | | |
