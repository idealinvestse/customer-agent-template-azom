# Design: Dashboard ops polish

**Date:** 2026-07-11  
**Status:** implemented  
**Approach:** template + small `app.py` context helpers; reuse Alpine in `base.html`; no React, no auth change.

## Problem

After the Tailwind/Alpine redesign and Cases 2.0, ops friction remained: slow `GET /` (live secret probes), thin triage chrome, unused JSON status endpoints, weak empty/confirm states.

## Goals

1. Jonatan sees open work at a glance (nav badges + sorted case queue).
2. Overview stays fast (no live probe storm on every home load).
3. Wire existing APIs into UI (`/onboarding/status`, `/oauth/gmail/status`).
4. Safer approve-send + clearer escalation/OAuth affordances.

## Non-goals

- Redesign theme again
- Jonatan running secret probes (Oscar-only)
- New case features (regenerate draft, assignee workflow)
- Charts / analytics SPA

## Design

### Shared nav counts

`_dashboard_context()` always includes cheap SQLite/jsonl counts:

- `open_cases`, `escalated_cases`, `queue_cases` (= open + escalated)
- `open_escalations`

Sidebar badges: Ärenden → `queue_cases`; Eskaleringar / Oscar eskaleringar → `open_escalations`.

### Fast overview

`GET /` uses presence-only integration summary from `secrets_status()` + `runtime_status()`. Live probes are not run on home.

Oscar `POST /oscar/secrets/test` writes `AZOM_DATA_DIR/probe_last.json`. Index may show a stale “Senaste Oscar-test” timestamp when the file exists.

### Case triage

- Default sort: escalated → priority=high → newest `created_at`
- Mailbox `<select>` from `enabled_mailboxes()`
- Status filter chips; richer empty state + poll loading
- Case detail: Alpine confirm before approve-send; assignee; escalation deep link

### Live status wiring

- Onboarding: Alpine `fetch('/onboarding/status')` + Gmail status refresh
- Nav / Oscar / onboarding: Gmail control is status-aware (Anslut vs Förnya)

### Oscar + Interact

- Oscar escalations default `?show=open`; toggle “Visa lösta”
- Interact: after draft, “Eskalera till Oscar” + link to cases (no mail send)

## Tests

- Index shows presence/runtime integration chrome without requiring live probe labels
- Nav/index expose case count keys when cases exist
- Oscar escalations default open filter
- Case detail contains approve confirm / `data-approve-guard`
- Onboarding + Gmail status endpoints remain 200 for authenticated users
