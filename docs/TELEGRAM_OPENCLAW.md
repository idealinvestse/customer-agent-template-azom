# Telegram-bot — OpenClaw hybrid (backup-yta)

**Purpose:** Beskriva Telegram som backup-ops-chat: slash-katalog, hybrid free-text, HITL och fail-closed actors.  
**Audience:** Jonatan, Oscar, coding agents.  
**Read this first:** [`SOUL.md`](../SOUL.md), [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) (daily driver), [`CASES.md`](CASES.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Backup chat** | Telegram — samma `BotHandler` som Messenger, men inte primär yta. |
| **Fail-closed map** | När `TELEGRAM_ACTOR_MAP` är icke-tom nekas unmapped chat_id. |
| **Confirm UX only** | NL “godkänn …” visar bekräftelse — skickar **inte** mail. |
| **tool_digest** | Kort sammanfattning av senast körda read-only tools för följdfrågor. |

**Kod:** `skills/ecom_ops/bot/`  
**Identitet:** [`SOUL.md`](../SOUL.md)  
**Entry:** `python -m ecom_ops.bot` · `./bin/dedicated-bot.sh` · systemd `azom-bot.service`

---

## Design

```text
Incoming update
    │
    ├─ allowlist (TELEGRAM_ALLOWED_CHAT_IDS)?
    ├─ actor map (TELEGRAM_ACTOR_MAP) — non-empty ⇒ unmapped denied
    ├─ slash → openclaw_commands.dispatch_openclaw_command
    │            (/cases approve → real send via CaseService)
    ├─ mid-flow multi-step (order_lookup …)
    └─ free text → chat_agent.run_chat
                     intent heuristics
                     read-only tool prefetch (order / cases / ops)
                     OpenRouter phrasing (optional)
                     BotReply + optional approve / triage keyboards
```

**Invariant:** free-text och NL “godkänn &lt;id&gt;” skickar aldrig mail. Send kräver slash approve, inline-knapp kopplad till samma path, dashboard eller CLI.

---

## Environment

```bash
TELEGRAM_BOT_TOKEN=...
# Krävs i prod (AZOM_USE_MOCK=0):
TELEGRAM_ALLOWED_CHAT_IDS=111111111,222222222
# chat_id → actor; non-empty map → unmapped denied
TELEGRAM_ACTOR_MAP=111111111:jonatan,222222222:oscar
# TELEGRAM_FAIL_CLOSED=1
```

Vid start registrerar boten `TELEGRAM_MENU_COMMANDS` via Telegram `setMyCommands`.

### Bra / dåligt exempel

**Bra (prod):**

```bash
TELEGRAM_ALLOWED_CHAT_IDS=111111111
TELEGRAM_ACTOR_MAP=111111111:jonatan
```

**Dåligt (prod):** Tom allowlist eller tom map medan live — riskerar fel åtkomstmodell. I live ska map/allowlist vara satta enligt Oscars policy.

---

## Slash-katalog (OpenClaw-kompatibel + Azom)

| Kommando | Beskrivning |
|----------|-------------|
| `/help` | Kort intro (även `/start`) |
| `/commands` | Full katalog |
| `/status` | Version, mock/live, customer, OpenRouter spend, Gmail, session |
| `/whoami` (`/id`) | chat_id + resolved actor |
| `/new [model]` | Rensa dialog; valfri model pin |
| `/reset` · `/reset soft` | Full reset eller behåll session settings |
| `/stop` (`/cancel`) | Avbryt pågående flow; behåll historik soft |
| `/tools` · `/tools verbose` | Chat tools vs slash/CLI tools |
| `/tasks` | Open cases + open escalations count |
| `/usage` · `/usage cost\|off` | Cost / footer mode |
| `/model` · `/model <name>\|default` | Session model pin |
| `/verbose` · `/think` | Session style knobs |
| `/skill` | ecom-ops skill summary |
| `/context` | Flow, turns, tool_digest, session keys |
| `/health` | SSH health (actor-scoped) |
| `/brief` | Customer + cost brief |
| `/order [id]` | Order status read-only; multi-step om bart |
| `/cases` | list · show · approve · regenerate · close · help |

### `/cases` subcommands

```text
/cases                  # open + escalated (escalated → high → ★suggest → newest)
/cases show <id8>       # detail + draft + approve keyboard
/cases approve <id8>    # send draft (RBAC CASE_REPLY / admin)
/cases regenerate <id8> # new draft from inbound (never sends)
/cases close <id8>      # close without reply
/cases help
```

Suggest-approve rader visar `★föreslå` (+ confidence när den finns).

---

## Hybrid free-text

Thread state under `AZOM_DATA_DIR/telegram_state.json`:

- **TTL:** 24h idle (refreshas vid aktivitet)
- **History:** senaste ~40 turns
- **Sticky:** `session.last_order_id`, `session.last_case_id8`
- **tool_digest** för följdfrågor (“och frakten?”, “samma order”)

Prefetch (`chat_agent.gather_tool_results`):

| Tool | När |
|------|-----|
| `lookup_order` | Order-id eller sticky follow-up |
| `list_cases` / `show_case` | Ärende / triage / id8 / sticky case |
| suggest filter | “föreslagna”, ★ |
| `ops_snapshot` / capabilities | status / budget / “vad kan du” |
| approve **confirm-only** | NL “godkänn id8” → UX, no send |
| `propose_order_status` | → **confirm button** (inte silent) |
| `propose_product_desc` | → **confirm button** |
| `propose_regenerate` | → **confirm button** (skickar aldrig) |

### Site write rails

| Action | Callback | RBAC |
|--------|----------|------|
| Order status | `order:set:{id}:{status}` | `ORDER_STATUS_UPDATE` (operator/Oscar) |
| Product desc | `product:desc:{id}:{0\|1}` | `PRODUCT_DESC_WRITE` |
| Regen draft | `cases:regen:{id8}` | `CASE_REPLY` (Jonatan OK) |

Jonatan kan approve:a cases men **inte** skriva Woo order/product om hen inte är mappad till `agent`/`oscar`.

---

## Actors & RBAC

`resolve_telegram_actor(chat_id)` styr approve/close/health.

| Map-läge | Beteende |
|----------|----------|
| Tom map (dev/mock) | unmapped → `jonatan` |
| Icke-tom map (prod) | unmapped → **denied** (fail-closed) |

Allowlist gäller separat om den är satt.

---

## Failure modes

| Condition | Behavior |
|-----------|----------|
| Ingen `OPENROUTER_API_KEY` | Tools körs; annars prior_digest/sticky eller fast svensk hjälp |
| Budget at cap | Samma — tools utan LLM-phrasing |
| Inte i allowlist | Denial + hint (Oscar + `TELEGRAM_ALLOWED_CHAT_IDS`) |
| Approve fail | Svenskt fel + Regenerera / Visa / Lista |
| Tom case-kö | Hint: nästa poll (~5 min) eller dashboard `/cases` |
| Okänt slash | “Okänt kommando … /commands · /help” |

---

## Local run

```bash
# cwd: repo-root
export AZOM_USE_MOCK=1
export TELEGRAM_BOT_TOKEN=...   # optional for pure unit tests
python -m ecom_ops.bot
```

Tester: `tests/test_telegram_state.py`, `test_telegram_actors.py`, `test_chat_agent.py`, `test_suggest_triage_ux.py`, `test_cases_v2.py`.

## Relaterat

- Messenger (primär): [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md)
- Cases: [`CASES.md`](CASES.md)
- Pilot: [`PILOT_OPS.md`](PILOT_OPS.md)
