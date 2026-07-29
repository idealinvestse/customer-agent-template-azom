# Design: Azom.no Support vNext (Approach A)

**Date:** 2026-07-29  
**Status:** approved  
**Approach:** Config + templates + keywords + fixtures + draft-eval fix  
**Aligns:** `docs/DEVELOPMENT_PLAN_FINISH.md` FU7 (classify/draft quality); not FU9  
**Does not replace:** Cases 2.0, Path B rails, mail-roadmap Fas 0–1 mail hardening

## Locked decisions

| Decision | Choice |
|----------|--------|
| Scope | prompts 1.1, `draft_reply` nb/da bodies, NO keywords, fixtures, draft-eval API fix |
| Mailboxes | Keep `support_no` stub; add `info_no` (`info@azom.no`, `nb`, `enabled: false`) |
| Suggest rails | Unchanged (`cases_ai.yaml`) |
| Auto-send | Remains `false`; never wire in this work |
| Live NO poll | Not enabled in repo until Oscar + credentials |

## Problem

Path B is green for SE. Norwegian mailbox stubs exist but template drafts for `language=nb` still emit Swedish bodies; prompts lack bilmultimedia / angrerett guidance; classify fixtures are SV-only; `draft-eval` crashes (`order_id=` invalid on `SupportService.handle`).

## Architecture (unchanged send path)

```text
mailboxes.yaml (language=nb) → CaseService._ingest_message
  → SupportService.handle(language=mb.language)
  → hybrid_classify + draft (LLM or draft_reply)
  → cases.db → human approve_and_send only
```

## Requirements

1. `draft_reply` uses true bokmål/dansk category bodies (not SV-when-not-EN).
2. Keyword classify covers NO return/abuse/shipping/order cues; no new categories.
3. `prompts.yaml` classify+draft version `1.1` — bilmultimedia + language-aware + no legal/refund invention.
4. NO classify + draft_quality fixtures; classify-eval and draft-eval green.
5. `info_no` stub + enable-gate docs; SOUL one-liner for draft language.
6. Zero changes to poll→send; auto-send stays off.

## Non-goals

env_prefix, OAuth persist, FU9 wire, FAQ/KB, IMAP IDLE, V3, suggest allowlist widening, `enabled: true` for NO mailboxes.

## Rollback

Revert prompts to 1.0; restore prior `draft_reply`/keywords; fixtures are additive and can be deleted.
