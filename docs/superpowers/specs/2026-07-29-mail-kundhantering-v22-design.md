# Design: Mail/kundhantering v2.2 — Ops-proof + narrow automation-ready

**Date:** 2026-07-29  
**Status:** approved  
**Approach:** Close residual of 2026-07-28 mail-support roadmap (Fas 0 → rest Fas 1/2 → gated Fas 3 → Fas 4)  
**Aligns:** `docs/DEVELOPMENT_PLAN_FINISH.md` §16; `docs/superpowers/specs/2026-07-28-mail-support-roadmap-design.md`  
**Does not replace:** Cases 2.0, Path B, azom.no Support vNext Approach A

## Locked decisions

| Decision | Choice |
|----------|--------|
| Version name | **v2.2 Ops-proof + narrow automation-ready** |
| Sequencing | Fas A ops (H1–H4) → Fas B code residual (B1 probe/docs, B2 bulk close after soak notes) → Fas C FU9 (hard-gated) → Fas D re-evaluate |
| First pure-code unit | **B1** — live `probe_mail` + Outlook/Graph env matrix docs |
| Human approve | Unchanged; production send only via explicit approve |
| Auto-send | Repo `auto_send_enabled: false`; wire only when all 8 FU9 preconditions true |
| NO/DK mailboxes | Stay `enabled: false` in repo until Oscar + credentials |
| Outlook browser OAuth | Deferred unless soak proves need |
| Non-goals | V3, GA4/engagement, FAQ/KB, IMAP IDLE, default-on auto-send, silent chat send |

## Problem

Mail Tasks 2–6 are on main (OAuth persist, env_prefix, PARTIAL UI, reply headers, draft diff + throttle). Remaining risk is **unproven live ops**, **false-green mail probes**, soak-driven triage friction (bulk close), and **premature FU9**.

## Architecture (unchanged core)

```text
Triggers: azom-cases-poll.timer | CLI | dashboard poll
  → CaseService.poll → MailClient per mailbox
  → SupportService → cases.db
  → Human approve_and_send only (until Fas C gate)

Oscar secrets: probe_mail must use real transport when AZOM_USE_MOCK=0
```

## Requirements

1. **Fas A (human):** live soak signed; baseline filled; classify stick 0 FP never-list; weekly cadence ×3.
2. **B1:** `probe_mail` in prod calls real `fetch(limit=1)`; mock mode stays network-free; `.env.example` documents provider env matrix (IMAP/Outlook/Graph).
3. **B2 (after A1 friction notes):** dashboard bulk **close** only (never bulk approve/send).
4. **Fas C:** single post-ingest auto-send call site; data-dir overlay only; kill-switch; tests for deny-by-default + narrow enable path.
5. **Fas D:** decision log only — no V3/GA4 from this version.

## Non-goals

FAQ/KB, IMAP IDLE, V3, GA4, default-on auto-send, bulk approve, `enabled: true` for NO/DK in repo, Outlook dashboard OAuth UI.

## Rollback

- B1: revert `secret_probes.probe_mail` + `.env.example` docs.  
- B2: revert cases list bulk UI + route.  
- Fas C: overlay off + `AZOM_AUTO_SEND_KILL=1` + restart poll timer.

## Related

- Plan: `docs/superpowers/plans/2026-07-29-001-mail-kundhantering-v22-plan.md`  
- FU9: `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`  
- Soak: `docs/solutions/2026-07-16-live-soak-checklist.md`
