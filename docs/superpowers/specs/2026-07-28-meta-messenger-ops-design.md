# Design: Meta Messenger daily driver + dashboard handoff

**Date:** 2026-07-28  
**Status:** implemented (local)  
**Branch:** `feat/meta-messenger-ops`  
**Approach:** Thin Messenger webhook adapter over shared `BotHandler`; dashboard is system of record with deep links; Telegram remains backup chat.

## Problem

Jonatan works in Messenger day-to-day. Telegram alone is unfamiliar. The dashboard already has full support power but is not linked from chat. We need a seamless daily driver that hands off to the right web view when chat is not enough.

## Goals

1. Operator-only Messenger channel (Jonatan/Oscar via Standard Access + allowlist/actor map).
2. Same chat brain as Telegram (slash, NL, approve postbacks, recovery).
3. Deep links (`AZOM_DASHBOARD_PUBLIC_URL`) to `/cases/{id}`, queue, settings, etc.
4. Dashboard remains full-power companion (`?from=messenger` banner; clearer PARTIAL poll).
5. Telegram backup unchanged.

## Non-goals

- Customer Messenger inbox, Instagram, WhatsApp
- Auto-send, SPA rewrite, cross-channel sticky session

## Architecture

```
Messenger webhook ──┐
Telegram long-poll ─┼──► BotHandler ──► CaseService / chat_agent
Dashboard UI ───────┘         ▲
                              └── deep links (web_url / text foot)
```

## Key modules (to implement)

- `skills/ecom_ops/bot/dashboard_links.py` — public URL builder
- `skills/ecom_ops/bot/reply.py` — `ActionButton` / `ActionMarkup` (postback | url)
- `skills/ecom_ops/bot/actors.py` — `resolve_channel_actor(channel, peer_id)`
- `skills/ecom_ops/bot/messenger_adapter.py` — HMAC, parse, Send API render
- Dashboard `GET|POST /webhooks/messenger`
- Case/approve replies: URL button "Öppna i dashboard"
- `cases_poll` PARTIAL flash; `?from=messenger` banner on case/cases

## HITL

NL approve never sends. Postback `/cases approve` and dashboard approve only.

## Local env (dev)

```bash
AZOM_DASHBOARD_PUBLIC_URL=http://127.0.0.1:8080
MESSENGER_VERIFY_TOKEN=dev-verify
MESSENGER_APP_SECRET=dev-secret
MESSENGER_PAGE_ACCESS_TOKEN=  # optional for send; mock path in tests
MESSENGER_ALLOWED_PSIDS=
MESSENGER_ACTOR_MAP=
```

Without live Meta tokens, unit tests use signed fixtures; webhook verify GET works locally.
