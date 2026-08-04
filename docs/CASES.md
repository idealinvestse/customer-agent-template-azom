# Cases 2.0 + Path B (AI-kvalitet)

**Purpose:** Beskriva mail→ärende→utkast→human approve→skicka så att operatörer och svagare modeller gör rätt utan att gissa.  
**Audience:** Jonatan, Oscar, coding agents.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`PILOT_OPS.md`](PILOT_OPS.md), [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Case / ärende** | En rad i `cases.db` för en kundtråd från inbox. |
| **Path B** | AI-rails: ★ suggest-approve + auto-send-rails (av som default, **inte wired**). |
| **Suggest-approve (★)** | Systemet flaggar att ärendet är säkert nog att godkänna snabbt — människa måste fortfarande bekräfta. |
| **Auto-send / FU9** | Automatiskt skicka utan human approve. **Finns inte i poll.** Rails + kill-switch finns bara. |
| **Regenerate** | Skapa nytt utkast från inbound — **skickar aldrig**. |
| **Fail-closed actors** | Tom allowlist/map i live nekar Messenger/Telegram. |

**Kod:** `skills/ecom_ops/cases/`  
**Config:** `config/mailboxes.yaml`, `config/cases_ai.yaml`  
**DB:** `$AZOM_DATA_DIR/cases.db`

---

## Statusar

| Status | Betydelse |
|--------|-----------|
| `open` | Ny / i kö |
| `escalated` | Abuse/legal/critical eller mänskligt eskalerat |
| `sending` | Transient claim under `approve_and_send` (claim/release) — syns sällan i UI |
| `replied` | Utkast godkänt och mejlat |
| `closed` | Stängt utan kundsvar |

Aktiv kö för draft/regenerate/reply: `open` och `escalated` (inte `replied` / `closed` / `sending`).

---

## Pipeline (steg för steg)

1. **Poll** — `CaseService.poll` / `azom-cases-poll.timer` / dashboard `POST /cases/poll` / CLI `cases poll`
2. **Ingest** — mailbox-meddelanden → nytt case eller tråd (In-Reply-To / References / from+subject)
3. **Mark read** — best-effort efter lyckad ingest
4. **Classify** — keyword abuse-gate + confidence (hybrid); kategorier t.ex. `order_status`, `shipping`, `return`, `billing`, `abuse`
5. **Draft** — support-service + OpenRouter när nyckel/budget tillåter; **order context** från Woo när `order_id` finns
6. **Suggest-approve** — ren eligibility (`suggest.py`) → flagga `suggest_approve` + confidence-kolumner
7. **Human approve** — dashboard, Messenger/Telegram, eller CLI `cases reply` → skicka med trådheaders
8. **Close** — valfritt utan skick

Mailbox-poll-fel skapar eskaleringsticket så ops inte missar trasig inbox. Partial errors syns i `last_case_poll.json` och påverkar `/health`.

**Trådning:** Approve/send sätter `In-Reply-To` och `References`. Replied-trådar kan återöppnas vid nytt inbound. Utgående Message-ID sparas.

---

## Config: mailboxes

```yaml
# config/mailboxes.yaml
mailboxes:
  - id: support_default
    label: Support (default)
    address: support@azom.se
    site: azom
    market: se
    language: sv
    enabled: true
    # provider: gmail
    # env_prefix: MAIL_SE_

  # azom.no — håll enabled: false tills credentials finns + Oscar OK
  - id: support_no
    address: support@azom.no
    market: no
    language: nb
    enabled: false
```

Credentials ligger i env / `secrets.env`. Se [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md).

**Enable-gate NO/DK:** Oscar lägger fungerande mail-creds → sätt `enabled: true` först då. Agents får inte aktivera själva.

---

## Config: cases AI rails (Path B)

```yaml
# config/cases_ai.yaml (defaults)
suggest_approve_categories: [order_status, shipping]
suggest_approve_min_confidence: 0.8
suggest_approve_require_order_id: true
never_suggest_categories: [abuse, return, billing]

auto_send_enabled: false
auto_send_categories: [order_status]
auto_send_min_confidence: 0.92
max_auto_sends_per_day: 10
kill_switch_env: AZOM_AUTO_SEND_KILL
```

### Suggest-approve (★)

- Badge/UX only — kräver fortfarande human confirm.
- Default: bara `order_status` / `shipping`, confidence ≥ 0.8, `order_id` krävs.
- Aldrig suggest på `abuse`, `return`, `billing`.
- Keyword-only confidence är typiskt 0.65 → kan **inte** suggesta ensam.

**Kalibrering:** Sänk inte trösklar utan ≥20 anonymiserade live-samples, confusion review, fixture-uppdatering, och noll false positives på return/billing/abuse.

### Regenerera utkast

- CLI/dashboard/Telegram regenerate **skickar aldrig**.
- Behåller tidigare draft; cooldown ca 60 sekunder i service.
- Dashboard kan visa side-by-side diff.

### Bulk

- **Bulk close** tillåtet med RBAC/CSRF i dashboard.
- **Bulk approve/send** är förbjudet / finns inte.

### Path B2 — return/billing drafts (shipped)

Return- och billing-ärenden får **rikare utkast** (templates + draft-prompt) och kan få högre priority / UI-hint för eskalering. De är fortfarande i `never_suggest_categories` — **aldrig ★** och **aldrig auto-send**.

---

## Auto-send (FU9) — inte wired

**Status:** **Not wired.** `should_auto_send` / day counter finns; poll anropar **inte** outbound send.  
**Default:** `auto_send_enabled: false`.  
**Kill-switch:** `AZOM_AUTO_SEND_KILL=1` nekar alltid.  
**Kod:** `skills/ecom_ops/cases/auto_send.py`, checkpoint `evaluate_auto_send_eligibility` i `service.py` (skickar aldrig).  
**Tester:** `tests/test_auto_send_rails.py` (inkl. att poll-källan inte anropar auto-send).

### Null-send / Shadow Live Ledger (soft-soak)

**Profil:** `AZOM_NULL_SEND=1` eller CLI `--null-send` (default **av**).  
Under profilen vägrar `MailService` all kundmail (`send` + `reply`); `approve_and_send` nekar **före** `claim_for_send`.  
Poll kör fortfarande ingest → classify → draft → suggest; under null-send skrivs `shadow_eligible` / `shadow_deny_reason` + telemetry `case_shadow_decision` (FU9 would-have, aldrig skick).  
Jonatan ser muted badge `Skugga: …` i dashboard (plus null-send-banner när profilen är aktiv); Oscar läser `python -m ecom_ops --actor oscar cases shadow-report` (**ADMIN** krävs).  
Soft-soak: `bash bin/mock-soak-azom.sh` sätter null-send + mock. Live soft-soak: Oscar sätter `AZOM_NULL_SEND=1` i `.env` + restart — se [`PILOT_OPS.md`](PILOT_OPS.md). Systemd defaultar **inte** null-send.  
**Detta är inte A1 live soak och inte FU9-wire.** `auto_send_enabled: true` under null-send kan ge `shadow_eligible=true` men mail skickas ändå inte.

### Aktivera inte förrän ALLA är sanna

1. Sprint A+B gröna i prod (orderpanel, approve&nästa, extract, fixtures).
2. Live soak (A1) klar; ≥2 veckor human approve utan allvarligt fel-skick.
3. Hög suggest-precision på `order_status` live-sample (0 FP på never-list).
4. Oscar **skriftligt** enable för begränsat experimentfönster.
5. Config overlay endast (data dir), inte blind repo-flip.
6. Wire **en** call site post-ingest eligible only (inte bred poll-send).
7. Telemetry `case_auto_sent` + daily cap + conf ≥ 0.92 + order_id + bara `order_status`.
8. Rollback-drill övad (nedan).

### Gate-status

| Precondition | Status |
|--------------|--------|
| 1 Sprint A+B code | OK i repo |
| 2 Live soak + 2 veckor clean approve | Öppen — blocked_on Oscar A1 |
| 3 Suggest precision live sample | Öppen |
| 4 Oscar written enable | Öppen |
| 5 Overlay-only config | Öppen |
| 6 Single post-ingest call site | **Inte wired** (avsiktligt) |
| 7 Telemetry + caps | Rails finns; oanvända tills wire |
| 8 Rollback drill practiced | Efter soak |

**Agent rule:** Öppna inte en PR som anropar send från poll förrän Oscar written enable + hela listan är grön.

### Rollback-drill (övning före eventuell wire)

Inom **1 minut** vid dåligt auto-send:

1. Sätt overlay/data-dir: `auto_send_enabled: false`.
2. Sätt env: `AZOM_AUTO_SEND_KILL=1`.
3. Starta om `azom-cases-poll.timer`.
4. Bekräfta med `python -m ecom_ops status` / loggar: inga fler auto-sends.
5. Auditera `case_auto_sent`; eskalera till Oscar.

---

## CLI

```bash
# cwd: repo eller /opt/azom-agent
# mock: AZOM_USE_MOCK=1 eller --mock
python -m ecom_ops --mock cases poll --limit 20
python -m ecom_ops --mock --null-send cases poll --limit 20        # shadow trail; ingen kundmail
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases show --id <uuid>
python -m ecom_ops --mock cases draft --id <uuid> --body "..."
python -m ecom_ops --mock cases regenerate --id <uuid>             # skickar aldrig
python -m ecom_ops --mock cases reply --id <uuid> [--body "..."]   # approve+send
python -m ecom_ops --mock cases close --id <uuid> [--reason "..."]
python -m ecom_ops --actor oscar cases shadow-report --days 7      # Oscar ADMIN
python -m ecom_ops --actor oscar cases retention-purge --dry-run   # Oscar ADMIN
./bin/cases-poll.sh
```

Default actor är `agent` (operator) för poll; använd `--actor jonatan` för reply som butiksägare.  
`shadow-report` och `retention-purge` kräver `--actor oscar`.

Full CLI: [`CLI_REFERENCE.md`](CLI_REFERENCE.md).

---

## Dashboard

| Path | Syfte |
|------|--------|
| `/cases` | Kö, filter, age, ★-badges, KPIs, `?suggest=1` |
| `/cases/<id>` | Draft edit/save, orderpanel, approve confirm, regenerate, close (RBAC) |
| `POST /cases/poll` | Manuell poll (auth) |
| `POST /cases/bulk-close` | Bulk close (RBAC/CSRF) — **inte** bulk approve |

Nav-badges på översikt för open/escalated. Se [`PILOT_OPS.md`](PILOT_OPS.md).

---

## Messenger / Telegram

```text
/cases
/cases show <id8>
/cases approve <id8>
/cases regenerate <id8>
/cases close <id8>
```

NL: “lista föreslagna”, “godkänn abcdef01” → **confirm UX only**, aldrig silent send.

- Messenger (daily driver): [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md)
- Telegram (backup): [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md)

---

## Schema (kort)

`CaseStore` migrerar med ALTER / versioned `_migrate`:

- Cases: status, priority, assignee, escalation_id, order_id, draft, **classify_confidence**, **classify_method**, **suggest_approve**, timestamps, …
- Messages: body, message-id, **in_reply_to**, **references_header**, …

---

## Telemetry / KPI

När tillgängligt: `time_to_approve_sec`, `draft_edit_distance`, `time_to_first_edit_sec`, plus LLM-kostnad under OpenRouter-cap.

```bash
python -m ecom_ops kpis --days 7
python -m ecom_ops classify-eval
```

---

## Gör / Gör inte

**Gör:**

- Approve via explicit path (dashboard / approve-knapp / `/cases approve` / CLI reply).
- Eskalera abuse/legal/critical till Oscar.
- Behåll `auto_send_enabled: false` tills FU9-gates + Oscar written enable.

**Gör inte:**

- Silent send från free-text / NL “godkänn”.
- Wire auto-send i poll “för att hjälpa till”.
- Sänk suggest-trösklar efter false positive utan ny kalibrering.
- Aktivera NO/DK-mailboxar utan Oscar.

---

## Icke-mål (nu)

- FAQ/KB
- IMAP IDLE (bara timer-poll)
- Multi-tenant cases
- Default-on auto-send
- Outlook browser OAuth UI
