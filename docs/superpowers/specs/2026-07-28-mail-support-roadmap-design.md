# Design: Mail & kundsupport — vidareutvecklingsroadmap

**Date:** 2026-07-28  
**Status:** approved  
**Approach:** Sequenced holistics (ops → mail robustness → safe automation → re-evaluate)  
**Supersedes for sequencing:** residual human-DoD in `docs/DEVELOPMENT_PLAN_FINISH.md`  
**Does not replace:** Cases 2.0 (`2026-07-11-cases-v2-design.md`) or Path B plan (U1–U6 shipped)

## Locked decisions

| Decision | Choice |
|----------|--------|
| Primary sequencing | Fas 0 drift/mätning → Fas 1 mail-robusthet → Fas 2 triage-friktion → Fas 3 FU9 auto-send (gated) → Fas 4 omvärdering |
| Parallelism | Fas 2 may start lightly after Fas 0 H1 (live soak executed); Fas 1 P0 may start after H1; Fas 3 blocked until Fas 0 H1–H3 green |
| Core architecture | Keep `MailTransport` → `MailClient`/`MailService` → `CaseService.poll` / `approve_and_send`; no rewrite |
| Human approve default | Unchanged — production send only via explicit approve (`CASE_REPLY`) |
| Auto-send | Rails only until all 8 FU9 preconditions true; repo default remains `auto_send_enabled: false` |
| Outlook/Graph dashboard OAuth | Deferred unless live soak proves need; env + probe parity first |
| Per-mailbox credentials | In scope for Fas 1 (P1) via `env_prefix` on `MailboxConfig` |
| IMAP IDLE / FAQ/KB / V3 / GA4 / default-on auto-send | Explicit non-goals |

## Problem frame

Path B + Cases 2.0 code DoD is green. Remaining risk is **unproven live ops** (soak, baseline, classify precision), **mail credential fragility** (OAuth refresh not persisted; shared mailbox secrets), and **premature automation**. Goal: reduce Jonatan support friction measurably while keeping Oscar’s kill-switches and human-in-the-loop intact.

## Current state (evidence)

| Layer | Status |
|-------|--------|
| Mail providers (gmail/outlook/graph/imap/pop3) | Shipped; Gmail dashboard OAuth only |
| Poll → ingest → classify → draft → suggest → approve → send | Shipped |
| Auto-send rails + kill-switch | Shipped, **not wired** |
| FU1–FU5 (regenerate, baseline doc, brief, budget warn) | Shipped |
| Live soak executed / baseline filled / weekly cadence | Open (human) |
| OAuth access token persist after SMTP/IMAP refresh | Gap (`SmtpImapTransport._ensure_oauth_token` updates memory only) |
| Per-mailbox `env_prefix` | Commented future in `config/mailboxes.yaml` |

## Architecture (unchanged core)

```text
Triggers: azom-cases-poll.timer | CLI cases poll | POST /cases/poll
  → CaseService.poll → enabled_mailboxes() → MailClient per mailbox
  → SupportService.handle (classify/draft/suggest)
  → CaseStore (cases.db)
  → Human: Dashboard / Telegram / CLI approve_and_send
  → MailService.send (threading headers)

Auto-send: should_auto_send() exists; poll MUST NOT call sender until Fas 3 gate.
```

## Phases

### Fas 0 — Drift & mätning (now)

| ID | Work | Owner | Exit gate |
|----|------|-------|-----------|
| H1 | Execute `docs/solutions/2026-07-16-live-soak-checklist.md` on prod | Oscar + Jonatan | Checklist signed |
| H2 | Fill baseline in `docs/ideation/baseline-capture.md` (hours/week **or** median `time_to_approve_sec` × volume) | Jonatan / Oscar | Number recorded |
| H3 | Classify quality on live samples; tune `cases_ai.yaml` thresholds only — do not widen allowlist early | Oscar + agent | 0 false-positive suggest on never-list stick sample |
| H4 | Weekly 15–30 min sync × 3 | Both | Cadence without process chaos |

**Code in Fas 0:** only fixture/threshold changes justified by H3 data. No auto-send wire. No new OAuth provider UI.

### Fas 1 — Mail-robusthet (after H1)

| Prio | Item | Notes |
|------|------|-------|
| P0 | Persist refreshed OAuth access token (Gmail store; generic refresh path) | After `_ensure_oauth_token` refresh, write back via `GmailOAuthStore.save_tokens` when provider=gmail |
| P0 | Poll PARTIAL vs ALL-fail clarity in dashboard/brief + runbook links | Close gaps found in soak |
| P1 | Per-mailbox credentials via `MailboxConfig.env_prefix` | e.g. `MAIL_SE_USERNAME` when prefix `MAIL_SE_` |
| P1 | Outlook/Graph env matrix docs + `probe_mail` parity | Dashboard OAuth only if soak demands |
| P2 | `MailService.reply` full In-Reply-To/References parity with approve path | CLI consistency |

### Fas 2 — Triage-friktion (after H1; may overlap Fas 1)

- Dashboard draft diff after regenerate
- Regenerate throttle (budget protection)
- Bulk triage polish driven by soak friction notes
- Messenger: keep approve + dashboard deep-link; full triage stays on dashboard

### Fas 3 — Säker automation (after H1–H3 + FU9 doc)

Wire **one** post-ingest call site per `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`:

1. Sprint A+B green in prod  
2. Live soak done; ≥2 weeks human approve without serious bad send  
3. Suggest precision high on `order_status` (0 FP on never-list)  
4. Oscar **written** enable for bounded window  
5. Config overlay in data dir — not blind repo flip  
6. Single call site, eligible cases only  
7. Telemetry `case_auto_sent` + daily cap + conf ≥ 0.92 + `order_id` + `order_status` only  
8. Rollback: `auto_send_enabled: false` + `AZOM_AUTO_SEND_KILL=1` within 1 minute  

Repo default stays `auto_send_enabled: false`.

### Fas 4 — Omvärdering

With baseline + ≥2 weeks approve KPIs (and optional auto-send trial data), decide: engagement/GA4, V3, wider auto-send — **not before**.

## Non-goals (entire roadmap)

- V3 multi-tenant  
- GA4 / engagement program  
- FAQ / knowledge base  
- IMAP IDLE (keep 5-min timer)  
- Default-on auto-send  
- Silent customer send from chat LLM  
- SSO  

## Success metrics

| Type | Metric |
|------|--------|
| Soft | Jonatan: lower friction on ★ suggest-approve cases |
| Hard | `time_to_approve_sec` / `draft_edit_distance` visible via `ecom_ops kpis` and `/brief` |
| Ops | Soak green; poll-age on `/health`; zero unintentional customer mail |
| Automation | Zero auto-send until all eight FU9 preconditions true |

## Surfaces touched (by phase)

| Phase | Primary paths |
|-------|----------------|
| 0 | `docs/solutions/2026-07-16-live-soak-checklist.md`, `docs/ideation/baseline-capture.md`, `config/cases_ai.yaml`, classify fixtures |
| 1 | `skills/ecom_ops/integrations/mail_providers/smtp_imap.py`, `skills/ecom_ops/oauth/gmail.py`, `skills/ecom_ops/cases/mailboxes.py`, `skills/ecom_ops/integrations/mail.py`, dashboard status/brief, `.env.example` |
| 2 | `infrastructure/dashboard/templates/case_detail.html`, `cases/service.py`, bot regenerate throttle |
| 3 | `skills/ecom_ops/cases/service.py`, `cases/auto_send.py`, tests asserting wire + kill-switch |

## Related docs

- `docs/CASES.md`  
- `docs/DEVELOPMENT_PLAN_FINISH.md`  
- `docs/solutions/2026-07-16-fu9-auto-send-preconditions.md`  
- `docs/solutions/2026-07-16-live-soak-checklist.md`  
- `docs/superpowers/plans/2026-07-28-001-mail-support-vidareutveckling-plan.md`  
