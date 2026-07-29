# Azom Core 3.0 — fördjupa Azom / azom.no (inte SaaS)

**Date:** 2026-07-29  
**Status:** locked direction (agent artifacts)  
**Customer:** Azom only (SE + NO + later DK) — **not** multi-tenant SaaS  
**Related:** [`DEVELOPMENT_PLAN_FINISH.md`](../DEVELOPMENT_PLAN_FINISH.md) §16 · mail v2.2/v2.3 · [`2026-07-29-azom-no-support-vnext-design.md`](../superpowers/specs/2026-07-29-azom-no-support-vnext-design.md)

## Locked decisions

| Decision | Choice |
|----------|--------|
| Scope | Single customer Azom; deepen support core + live azom.no |
| Multi-tenant / control hub | **Parked** — out of scope for Core 3.0 |
| Package bump | Optional later; capability first (may stay 2.x until release decision) |
| Human approve / SOUL | Unchanged — no silent send |
| Auto-send | Repo default `false`; FU9 only after hard gates |
| NO/DK enable in repo | Only after Oscar + credentials; never silent flip |

## Goals

1. Measurable support-time direction (baseline + TTA).
2. **azom.no** live in the same poll→draft→approve→send loop as SE (nb drafts already shipped).
3. Higher approve precision (FU7 live tune) without early allowlist widening.
4. Core stability: soak green, poll/mail failures visible.
5. Optional narrow FU9 only after gates.

## Non-goals

Multi-tenant, FAQ/KB, IMAP IDLE, GA4/engagement, SSO, bulk approve/send, default-on auto-send.

## Current state (2026-07-29)

| Area | Status |
|------|--------|
| SE support loop | Code DoD green |
| Mail Task 2–6 + v2.2 probe/bulk | On `main` |
| azom.no Approach A (nb drafts, fixtures, prompts 1.1) | Shipped; `support_no` / `info_no` `enabled: false` |
| FU6 live soak | Open — Oscar |
| FU7 live threshold tune | Blocked on soak samples |
| FU9 wire | Gated |
| Path B2 returns/billing drafts | Not built |

## Sequence

```text
Fas 1 Live proof (soak + baseline + cadence)
  → Fas 2 azom.no enable (credentials + Oscar OK + smoke)
  → Fas 3 Quality (FU7 tune, Path B2 drafts, soak-driven fixes)
  → Fas 4 FU9 gated wire (optional)
  → Fas 5 Re-evaluate on KPI (still no SaaS)
```

### Fas 1 — Live proof (human)

| ID | Deliverable | Owner |
|----|-------------|-------|
| C1 | Execute [`docs/solutions/2026-07-16-live-soak-checklist.md`](../solutions/2026-07-16-live-soak-checklist.md); fill outcome | Oscar + Jonatan |
| C2 | Fill [`baseline-capture.md`](baseline-capture.md) | Jonatan / Oscar |
| C3 | Weekly sync ×3 | Both |

**Exit:** soak signed; baseline number or explicit `blocked_on` note.

**Agent:** may help fixtures after samples; **must not** mark H1/soak done without Oscar.

### Fas 2 — azom.no live (ops + thin code)

| ID | Deliverable |
|----|-------------|
| N1 | Prod credentials for `support_no` / `info_no` (optional `env_prefix`) |
| N2 | Enable mailboxes only via prod overlay or explicit Oscar-approved PR |
| N3 | Smoke: poll creates nb cases; suggest rails unchanged; approve works |
| N4 | Later: same pattern for `support_dk` if volume warrants |

Code already present: `language=nb`, `woo_domain_from_market("no")`, classify/draft fixtures. Primary work is **ops enable**, not rewrite.

### Fas 3 — Core quality (after soak samples)

| ID | Deliverable |
|----|-------------|
| Q1 | FU7: 20–50 redacted samples → `classify-eval`; threshold-only changes in `config/cases_ai.yaml` |
| Q2 | Path B2: stronger return/billing drafts (never suggest/auto-send); clearer escalate |
| Q3 | Soak-driven core fixes (e.g. send retry on transient mail errors) |

Architecture stays: `MailTransport → CaseService.poll / approve_and_send`.

### Fas 4 — Narrow automation (hard gate)

All of [`docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`](../solutions/2026-07-16-fu9-auto-send-preconditions.md) must be true. Single post-ingest call site; data-dir overlay only; kill-switch; `order_status` + order_id + high conf only.

### Fas 5 — Re-evaluate

After ≥2–4 weeks KPI: stop / widen NO/DK / continue narrow auto-send / park engagement. **Not** multi-tenant.

## Hard constraints

- Human approve; no silent send (`SOUL.md`)
- Suggest ≠ send; never-list abuse/return/billing
- Order truth via Woo tools
- OpenRouter budget + soft warn
- RBAC: Jonatan `CASE_REPLY`; Oscar secrets/experiments

## Next actions

1. **Oscar:** start C1 live soak (critical path).  
2. **Agent:** this ideation landed; no FU9 wire; no NO `enabled: true` without Oscar.  
3. After soak + credentials: plan N1–N3 in an isolated worktree.  
4. After samples: Q1 FU7 TDD.

## Spec gate

Formal design spec `docs/superpowers/specs/YYYY-MM-DD-azom-core-30-design.md` only after **Fas 1 exit** (soak signed).
