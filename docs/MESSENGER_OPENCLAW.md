# Meta Messenger — OpenClaw hybrid (daily driver)

**Purpose:** Beskriva Messenger som Jonatans primära ops-chat: webhook, fail-closed PSID, HITL och handoff till dashboard.  
**Audience:** Jonatan, Oscar, coding agents.  
**Read this first:** [`SOUL.md`](../SOUL.md), [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md), [`CASES.md`](CASES.md), [`PILOT_OPS.md`](PILOT_OPS.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Daily driver** | Messenger — primär yta för triage/approve. |
| **PSID** | Page-Scoped ID — Meta-användar-id mot sidan. |
| **Fail-closed** | Tom `MESSENGER_ALLOWED_PSIDS` eller `MESSENGER_ACTOR_MAP` i live (`AZOM_USE_MOCK=0`) nekar. |
| **Postback** | Knapptryck (t.ex. Godkänn & skicka) som går via webhook — explicit path. |
| **Dry-run outbound** | Utan page token loggas payload; i live blockeras mutationer. |

**Kod:** samma `BotHandler` som Telegram under `skills/ecom_ops/bot/` + dashboard webhook-route.  
**Webhook:** `GET|POST /webhooks/messenger` på dashboard-hosten (`azom-dashboard.service`).  
**Ingen** egen `azom-messenger.service` — till skillnad från Telegram (`azom-bot.service`).

---

## Ytor (lager)

| Yta | Roll |
|-----|------|
| **Messenger** | Daily driver — list/show/approve/nästa, order lookup, recovery |
| **Dashboard** | Full power — edit draft, orderpanel, poll, bulk close, settings |
| **Telegram** | Backup chat — samma hjärna |

Handoff när Messenger inte räcker: svar inkluderar **Öppna i dashboard**  
(`AZOM_DASHBOARD_PUBLIC_URL` + `/cases/{id}?from=messenger`).

---

## Environment

```bash
MESSENGER_PAGE_ACCESS_TOKEN=...
MESSENGER_APP_SECRET=...
MESSENGER_VERIFY_TOKEN=...
# Krävs när AZOM_USE_MOCK=0 (tom = fail-closed)
MESSENGER_ALLOWED_PSIDS=psid1,psid2
MESSENGER_ACTOR_MAP=psid1:jonatan,psid2:oscar
# MESSENGER_FAIL_CLOSED=1   # också implied när AZOM_USE_MOCK=0
AZOM_DASHBOARD_PUBLIC_URL=https://agent.azom.se
# ingen trailing slash
```

### Bra / dåligt exempel (prod)

**Bra:**

```bash
MESSENGER_ALLOWED_PSIDS=1000000000000000
MESSENGER_ACTOR_MAP=1000000000000000:jonatan
AZOM_DASHBOARD_PUBLIC_URL=https://agent.azom.se
```

**Dåligt:** Tom allowlist i live — alla nekas (fail-closed) eller fel uppsättning om någon tror att “öppen” är default. Default i live är **stängd**.

---

## Webhook

- URL: `https://<host>/webhooks/messenger`
- **Ingen** dashboard Basic Auth på webhooken.
- Skydd: Meta verify-token (GET) + HMAC med `MESSENGER_APP_SECRET` (POST).
- Prenumerera i Meta: `messages`, `messaging_postbacks`.
- Dedup: per Messenger `mid`.
- Svar: HTTP **200** efter per-event-hantering så Meta inte retry:ar hela batchen i onödan.

### Verifiera lokalt / på host

```bash
curl -sS "http://127.0.0.1:8080/health" | head
# expect: innehålla messenger-relaterade readiness-fält när konfigurerat
# prod: messenger.send_enabled: true när page token + live är OK
```

---

## Local / mock

Med `AZOM_USE_MOCK=1` fungerar tom allowlist/actor-map (dev default → jonatan).

Utan `MESSENGER_PAGE_ACCESS_TOKEN`:

| Läge | Beteende |
|------|----------|
| Outbound Graph Send | Dry-run (loggar payload; varning i footer) |
| Live `AZOM_USE_MOCK=0` | **Mutationer blockeras** (approve / close / order write / product write). Read-only (`/cases list\|show`, `/order`) fungerar |
| Mock | Approve-postbacks körs (mail via mock) så tester täcker HITL |

---

## HITL (samma som man Telegram)

**Gör:**

- Skicka via postback **Godkänn & skicka** / `/cases approve` / dashboard / CLI `cases reply`.
- Använd regenerate — den skickar aldrig.

**Gör inte:**

- Tro att NL “godkänn abcdef01” skickar mail — det är bara confirm UX.
- Köra prod utan PSID-allowlist och actor map.

Slash/flows speglar Telegram-katalogen: `/help` `/cases` `/order` `/brief` `/status` `/marketing` …
`/marketing` är **read-only** snapshot — Ads-mutates endast via dashboard/CLI approve.
Se [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) för full kommando-tabell och free-text prefetch.  
Marketing: [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md).

---

## Typiskt Jonatan-flöde

1. `/cases` — se kö och ★-antal  
2. Visa ett ★-ärende — kontrollera order/market  
3. Godkänn & skicka (postback) — case → `replied`  
4. Godkänn & nästa om fler finns  
5. Vid behov: deep link till dashboard för draft-edit  

Full soak-checklista: [`PILOT_OPS.md`](PILOT_OPS.md).

---

## Relaterat

- Cases: [`CASES.md`](CASES.md)
- Telegram backup: [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md)
- Systemkarta: [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md)
