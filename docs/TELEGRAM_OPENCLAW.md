# Telegram bot — OpenClaw hybrid

Azom’s Telegram surface mirrors **OpenClaw / datalasse-style** slash dialogue and adds domain flows for order lookup and cases approve.

**Code:** `skills/ecom_ops/bot/`  
**Identity:** root [`SOUL.md`](../SOUL.md) (aligned with `chat_agent.SYSTEM_PROMPT`)  
**Entry:** `python -m ecom_ops.bot` · `./bin/dedicated-bot.sh` · systemd `azom-bot.service`

---

## Design

```text
Incoming update
    │
    ├─ allowlist (TELEGRAM_ALLOWED_CHAT_IDS)?
    ├─ slash → openclaw_commands.dispatch_openclaw_command
    │            (/cases approve → real send via CaseService)
    ├─ mid-flow multi-step (order_lookup …)
    └─ free text → chat_agent.run_chat
                     intent heuristics
                     read-only tool prefetch (order / cases / ops)
                     OpenRouter phrasing (optional)
                     BotReply + optional approve / triage keyboards
```

**Invariant:** free-text and NL “godkänn &lt;id&gt;” never send mail. Send requires slash approve, inline button wired to the same path, dashboard, or CLI.

---

## Environment

```bash
TELEGRAM_BOT_TOKEN=...
# Strongly recommended in production:
TELEGRAM_ALLOWED_CHAT_IDS=111111111,222222222
# chat_id → actor (jonatan|oscar|agent); unmapped → jonatan
TELEGRAM_ACTOR_MAP=111111111:jonatan,222222222:oscar
```

On start the bot registers `TELEGRAM_MENU_COMMANDS` via Telegram `setMyCommands`.

---

## Slash catalog (OpenClaw-compatible + Azom)

| Command | Description |
|---------|-------------|
| `/help` | Short intro (also `/start`) |
| `/commands` | Full catalog |
| `/status` | Version, mock/live, customer, OpenRouter spend, Gmail, session knobs |
| `/whoami` (`/id`) | chat_id + resolved actor |
| `/new [model]` | Clear dialog; optional model pin |
| `/reset` · `/reset soft` | Full reset or keep session settings |
| `/stop` (`/cancel`) | Abort in-progress flow; keep history soft |
| `/tools` · `/tools verbose` | Chat tools vs slash/CLI tools |
| `/tasks` | Open cases + open escalations count |
| `/usage` · `/usage cost\|off` | Cost / footer mode |
| `/model` · `/model <name>\|default` | Session model pin for LLM chat |
| `/verbose` · `/think` | Session style knobs |
| `/skill` | ecom-ops skill summary |
| `/context` | Flow, turns, tool_digest, session keys |
| `/health` | SSH health checks (actor-scoped) |
| `/brief` | Customer + cost brief |
| `/order [id]` | Order status (read-only; multi-step if bare) |
| `/cases` | list · show · approve · close · help |

### `/cases` subcommands

```text
/cases                  # open + escalated queue (escalated → high → ★suggest → newest)
/cases show <id8>       # detail + draft + approve keyboard
/cases approve <id8>    # send draft (RBAC CASE_REPLY / admin)
/cases regenerate <id8> # new draft from inbound (never sends)
/cases close <id8>      # close without reply
/cases help
```

Suggest-approve rows show `★föreslå` (+ confidence when present).

---

## Hybrid free-text tools (read-only)

Prefetch heuristics in `chat_agent.py` may run:

| Tool | When |
|------|------|
| `lookup_order` | Order number / status intent |
| `list_cases` / `show_case` | Ärende / triage / id8 |
| suggest filter | “föreslagna”, “vad kan jag godkänna”, ★ |
| `ops_snapshot` | status / budget / brief intent |
| approve **confirm-only** | NL “godkänn abcdef01” → UX, no send |
| soft escalate hint | abuse-ish or assistant mentions Oscar |

Session stores `messages` (clamped) + `tool_digest` for multi-turn continuity (`/context`).

---

## Actors & RBAC

`resolve_telegram_actor(chat_id)` drives approve/close/health.  
Jonatan: approve case replies. Oscar: full admin. Unmapped chat: jonatan (still must pass allowlist if set).

---

## Conversation store

`ConversationStore` under `AZOM_DATA_DIR` (default `.azom-data`): per-chat flow, slots, messages, session (`model`, `verbose`, `think`, `usage`), tool_digest.

---

## Failure modes

| Condition | Behavior |
|-----------|----------|
| No `OPENROUTER_API_KEY` | Tools still run; LLM text falls back to fixed Swedish help |
| Budget at cap | Same — tools without LLM phrasing |
| Not in allowlist | Short denial |
| Unknown slash | “Okänt kommando … /commands · /help” |

---

## Local run

```bash
export AZOM_USE_MOCK=1
export TELEGRAM_BOT_TOKEN=...   # optional for pure unit tests
python -m ecom_ops.bot
```

Tests: `tests/test_telegram_state.py`, `test_telegram_actors.py`, `test_chat_agent.py`, `test_suggest_triage_ux.py`, `test_cases_v2.py`.
