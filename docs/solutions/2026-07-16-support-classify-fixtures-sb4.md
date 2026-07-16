# Support classify fixtures (SB4) — 2026-07-16

## What

Checked-in anonymized samples under `tests/fixtures/support_classify/` drive:

- keyword `classify_message` category
- order_id extraction
- `is_suggest_approve_eligible` with hypothetical LLM confidence

## Current rails (unchanged by this note)

From `config/cases_ai.yaml`:

- suggest categories: `order_status`, `shipping`
- min confidence: **0.8**
- require order_id: **true**
- never: `abuse`, `return`, `billing`

Keyword-only path uses confidence **0.65** → never suggest without LLM lift.

## Live calibration

Do **not** lower thresholds without:

1. Export ≥20 anonymized live samples  
2. Score confusion on return/billing/abuse FP  
3. Update fixtures + this note  

No live threshold change in this commit.

## Related

- Plan: `docs/superpowers/plans/2026-07-16-001-sprint-a-approve-flow-and-measure-plan.md` SB4  
- Tests: `tests/test_support_classify_fixtures.py`
