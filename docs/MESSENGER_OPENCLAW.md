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
- Dedup: atomisk per Messenger `mid` i SQLite (`AZOM_DATA_DIR/messenger_jobs.sqlite3`).
- Hållbara förslag & callbacks: `infrastructure/dashboard/webhooks.py` delar ut en SQLite-baserad `proposal_store` (`AZOM_DATA_DIR/messenger_proposals.sqlite3`) till `BotHandler` så att `clarify:*`, `proposal:*`, `wpreview:*` samt äldre `order:set:*`, `cases:approve:*` och `product:desc:*` binds hållbart till `(actor, domain/market, target_id, expiry)` med engångskonsumtion.
- Quick Replies & bilder (paritet med Telegram):
  - `actions_to_messenger_quick_replies()` skickar upp till **13** Quick Replies (`content_type: "text"`) automatiskt när `actions` innehåller `clarify:*` eller fler än 3 val (så alla 4 `<clarify>`-val + `✍️ Skriv egen förklaring` + `✖️ Avbryt` ryms utan att klippas vid 3 knappar); `parse_webhook_payload()` tolkar `quick_reply.payload` som `kind="callback"`.
  - Inkommande bildbilagor i webhooken extraheras till `image_urls` och hämtas med anti-SSRF-skyddade `download_messenger_image()` (endast HTTPS, blockerar privata/loopback/link-local IP-adresser, verifierar PNG/JPEG/WEBP magic bytes och sparar under `AZOM_DATA_DIR/messenger_inbox_media/<peer_id>/`).
  - Utgående arbetsyteskärmdumpar (upp till 4 allowlistade PNG-bilder via `safe_workspace_attachments()`, inklusive `compare-desktop.png`) laddas upp via Graph API multipart i `send_bot_reply()` med strikt respekt för `AZOM_USE_MOCK`, `MESSENGER_SEND_ENABLED` och `AZOM_NULL_SEND`.
- Asynkron kö & UX: `POST /webhooks/messenger` lägger eventet i `MessengerJobQueue` (`WAL`, max 2 bakgrundsarbetare, återställer `running` → `pending` vid omstart) och svarar HTTP **200** direkt i drift. Bakgrundsarbetaren skickar `sender_action: typing_on` och delar svar vid ≤1950 tecken per meddelande (knappar/Quick Replies på sista chunken). (Under synkrona Flask-tester töms kön inline om inte `AZOM_MESSENGER_ASYNC_IN_TESTS=1` sätts.)
- `AZOM_TELEGRAM_MODEL_ROUTE` påverkar bara Telegram (valfri `AZOM_MESSENGER_MODEL_ROUTE` för Messenger).

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
- Tro att ett förtydligande (`clarify:pick`, `clarify:go`, `clarify:other`, `clarify:cancel`) godkänner en filändring, orderändring eller mailutskick.

### Förtydligande innan arbete (`<clarify>`)

Vid oklara ändringsförfrågningar, layoutönskemål eller beskrivningar av markerade/inringade skärmdumpar (`"ringat in på bilden"`) ställer botten en fokuserad motfråga med rekommenderade val innan arbetsytor öppnas eller ändringsförslag skapas (`skills/change-verification/SKILL.md`, max 3 rundor).

- **Knappar och Quick Replies:** För 1–3 standardåtgärder använder Messenger postback-knappar (max 3 per meddelande, och `clarify:go:`/`clarify:pick:` rankas före `clarify:other:`/`clarify:cancel:`). För `<clarify>` och menyer med fler än 3 alternativ skickas upp till **13 Quick Replies** så samtliga val (inklusive `"✍️ Skriv egen förklaring"` och `"✖️ Avbryt"`) är klickbara direkt, och alternativen visas även numrerade i texten för `1`–`4` eller fritextsvar.
- **Strikt HITL-separation:** `clarify:*` låser enbart tolkning och vägval. Själva ändringen kräver alltid efterföljande explicit godkännande (`proposal:yes:pr-...`, `action:yes`, `wpfile:stage:fe-...`, `wpreview:stage:wr-...`, Oscar-only `wpreview:push_draft:wr-...` för WP-utkast, **Godkänn & skicka** / `/cases approve`) och serverns RBAC-kontroll samt hållbar bindning i `messenger_proposals.sqlite3`.

Slash/flows speglar Telegram-katalogen: `/help` `/cases` `/order` `/brief` `/status` `/marketing` …
`/marketing` är **read-only** snapshot — Ads-mutates endast via dashboard/CLI approve.
WordPress-drift använder namngivna verktyg för en vald sajt; servern kontrollerar aktör och behörighet vid varje anrop. En tur begränsas till 3 anrop / 30 sekunder och sajtminnet gäller 12 timmar för samma aktör, kanal och konversation. Update- och restoreplaner är låsta förhandsgranskningar utan exekveringsväg. Livehälsokontroller, skanning, versionsupptäckt, uppdatering och produktions-restore är inte implementerade.

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
