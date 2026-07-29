# Design: Mail/kundhantering v2.3 — Live proof + gated auto-send

**Date:** 2026-07-29  
**Status:** approved  
**Approach:** Land v2.2 → Fas A human soak/baseline/classify → gated FU9 wire → Fas D re-evaluate  
**Aligns:** `docs/DEVELOPMENT_PLAN_FINISH.md` §16; v2.2 design; 2026-07-28 mail-support roadmap Fas 0/3/4  
**Does not replace:** Cases 2.0, Path B, v2.2 probe/bulk work

## Locked decisions

| Decision | Choice |
|----------|--------|
| Version name | **v2.3 Live proof + gated auto-send** |
| Sequencing | Land v2.2 → Fas A (H1–H4) → Fas C FU9 (hard-gated) → Fas D |
| Agent code before soak | Only land unstaged v2.2; no FU9 |
| Human approve | Unchanged |
| Auto-send | Repo `auto_send_enabled: false`; overlay-only enable after all FU9 preconditions |
| NO/DK mailboxes | Stay `enabled: false` in repo |
| Non-goals | V3, GA4, FAQ/KB, IMAP IDLE, default-on auto-send, bulk approve, Outlook browser OAuth |

## Problem

v2.2 code (live `probe_mail`, env matrix, bulk-close test, docs) is ready but not on `origin/main`. Live soak outcome is still `_TBD_`. FU9 rails exist but must not wire until soak + ≥2 weeks human approve + Oscar written enable.

## Requirements

1. **Land v2.2** on main (commit + push).
2. **Fas A:** soak signed; baseline filled; classify stick 0 FP never-list; cadence ×3.
3. **Fas C (gated):** one post-ingest auto-send call site; data-dir overlay; kill-switch; tests deny-by-default + narrow enable.
4. **Fas D:** decision log only — no V3/GA4 from this version.

## Non-goals

FAQ/KB, IMAP IDLE, V3, GA4, default-on auto-send, `enabled: true` for NO/DK in repo, premature FU9.

## Rollback

- Landed v2.2: `git revert` the land commit.  
- Fas C: overlay off + `AZOM_AUTO_SEND_KILL=1` + restart poll timer.

## Related

- Plan: `docs/superpowers/plans/2026-07-29-002-mail-kundhantering-v23-plan.md`  
- v2.2: `docs/superpowers/specs/2026-07-29-mail-kundhantering-v22-design.md`  
- FU9: `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`  
- Soak: `docs/solutions/2026-07-16-live-soak-checklist.md`
