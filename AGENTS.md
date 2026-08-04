# AGENTS.md — Azom customer agent

**Purpose:** Always-on operating notes for coding agents working in this repository.  
**Audience:** Coding agents (primary). Humans may skim.  
**Read this first:** [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md), [`docs/DOC_STYLE.md`](docs/DOC_STYLE.md), then the living doc for the surface you touch.

## Read order (do this before coding)

1. [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) — version, shipped, blockers, non-goals  
2. [`SOUL.md`](SOUL.md) — voice + hard HITL constraints (Swedish customer/ops voice)  
3. [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) — architecture  
4. Surface docs:
   - Cases / FU9: [`docs/CASES.md`](docs/CASES.md) (Swedish)
   - Pilot / soak: [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) (Swedish)
   - Dev/tests: [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md)
   - CLI: [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md)
   - Mail: [`docs/MAIL_PROVIDERS.md`](docs/MAIL_PROVIDERS.md)
   - Woo/WP: [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md)
   - Marketing Google (Ads+GA4): [`docs/MARKETING_GOOGLE.md`](docs/MARKETING_GOOGLE.md)
   - Messenger: [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md)
   - Telegram: [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md)
5. Skill card: [`skills/ecom-ops/SKILL.md`](skills/ecom-ops/SKILL.md)

**Source of truth order:** `CURRENT_STATE` > this file > code/tests > chat memory.

## Budget and roles

- OpenRouter budget: **USD 100** (`config/limits.yaml`)
- **Jonatan:** `viewer` (+ mail/SSH read, non-secret settings, **CASE_REPLY**, **MARKETING_READ** + **MARKETING_SUGGEST**)
- **Oscar:** `full_admin` + escalation target (critical + code_edit + secrets UI + experiment flags + **MARKETING_MUTATE** + `shadow-report` / `retention-purge`)
- **Agent automation:** `operator` (order/product/support, **MAIL_SEND**+**MAIL_READ**, **CASE_REPLY**, SSH read, cases poll, **MARKETING_READ**)

## Goals

- 3 months: ~50% less support time + high engagement (needs baseline + soak — human-owned)
- Onboarding: Telegram bot + password-protected web dashboard — **shipped** (v2)

## Runtime target

- **Package version:** **2.0.0** (`pyproject.toml` + `ecom_ops.__version__` — keep in sync; capability tracks V2.1–V2.3 are not package bumps)
- **OS:** Ubuntu 26.x (primary) / 24.04 LTS
- **Host:** Hetzner Cloud — **CX22 / CPX21** (2 vCPU, 4 GB RAM)
- **Auto-install:** `sudo bash bin/install.sh` or `bin/install-ubuntu26.sh`
- Docs: [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md), [`docs/AUTO_INSTALL.md`](docs/AUTO_INSTALL.md), [`docs/DEPLOY_UBUNTU24_HETZNER.md`](docs/DEPLOY_UBUNTU24_HETZNER.md), [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md)

## Identity (OpenClaw)

- **SOUL:** [`SOUL.md`](SOUL.md) — Swedish ops voice, human-in-the-loop, no silent send, order truth via tools
- **Skill card:** [`skills/ecom-ops/SKILL.md`](skills/ecom-ops/SKILL.md)
- Telegram (backup): [`docs/TELEGRAM_OPENCLAW.md`](docs/TELEGRAM_OPENCLAW.md)
- Meta Messenger (daily driver): [`docs/MESSENGER_OPENCLAW.md`](docs/MESSENGER_OPENCLAW.md) — fail-closed PSID allowlist/map in prod

## Hard constraints (never violate)

**Do:**

- Keep case customer mail on explicit approve paths only.
- Use mock mode (`AZOM_USE_MOCK=1` / `--mock`) for local work unless Oscar authorized live.
- Escalate critical / code_edit / secrets / unsafe SSH to Oscar.
- Update living docs when behavior or CLI changes ([`docs/DOC_STYLE.md`](docs/DOC_STYLE.md)).

**Do not:**

- Silent-send customer mail from free-text / NL “godkänn”.
- Wire auto-send into poll (FU9) without Oscar **written** enable + all gates in [`docs/CASES.md`](docs/CASES.md).
- Mark live soak complete — only Oscar + Jonatan can ([`docs/PILOT_OPS.md`](docs/PILOT_OPS.md)).
- Enable NO/DK mailboxes without Oscar + credentials.
- Commit secrets (`.env`, OAuth tokens, `secrets.env`).
- Weaken tests to pass CI.
- Recreate deleted history folders (`docs/superpowers/`, `docs/solutions/`, `docs/ideation/`).
- Default-on `AZOM_NULL_SEND` in systemd (Oscar sets it in `.env` for soft-soak only).

## V1 core (still in v2)

- order-status, product-desc, support, SSH, mail via `skills/ecom_ops`
- Mail providers: gmail, outlook, exchange_graph, generic_imap, generic_pop3
- CLI: `python -m ecom_ops` · `./bin/ecom-automation.sh`
- Tests: `pytest` (CI: ruff + cov ≥ 65%)

## Shipped capability tracks (summary)

Detail: [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md).

- **V2.0:** Dashboard, Gmail OAuth, Telegram + Messenger, install/Docker, status/smoke CLI
- **Cases 2.0 + Path B:** suggest-approve; auto-send rails **default off** + `AZOM_AUTO_SEND_KILL`; **not wired**
- **Path B2:** richer return/billing drafts; never ★
- **Shadow Live Ledger:** null-send (`AZOM_NULL_SEND` / `--null-send`) + `cases shadow-report` (Oscar); mock soak via `bin/mock-soak-azom.sh`
- **V2.1:** Woo/WP capacity — [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md)
- **V2.2:** live `probe_mail`, mail env matrix, bulk close
- **V2.3:** robustness (thread reopen, OAuth expiry, probe fail-closed); **ops next = Oscar A1 live soak**
- **Marketing Google (Ads+GA4):** mock-first ledger + HITL rails — [`docs/MARKETING_GOOGLE.md`](docs/MARKETING_GOOGLE.md); live APIs still stubbed

## Cases quick CLI

```bash
python -m ecom_ops --mock cases poll
python -m ecom_ops --mock --null-send cases poll
python -m ecom_ops --mock cases list --status open,escalated
python -m ecom_ops --mock cases draft --id <uuid> --body "..."
python -m ecom_ops --mock cases reply --id <uuid>
python -m ecom_ops --mock cases close --id <uuid>
python -m ecom_ops --actor oscar cases shadow-report --days 7
./bin/cases-poll.sh
```

Full reference: [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) · operator guide: [`docs/CASES.md`](docs/CASES.md)

## Mail CLI

```bash
python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
python -m ecom_ops --mock mail fetch
python -m ecom_ops status
```

## Marketing CLI (mock)

```bash
python -m ecom_ops --mock marketing digest --days 7
python -m ecom_ops --mock --actor jonatan marketing suggests build
bash bin/mock-marketing-azom.sh
```

## Telegram / Messenger env (prod)

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=...          # empty in live = fail-closed (deny all)
TELEGRAM_ACTOR_MAP=chat:jonatan,...    # empty in live = deny; unmapped denied when map set
MESSENGER_PAGE_ACCESS_TOKEN=...
MESSENGER_APP_SECRET=...
MESSENGER_VERIFY_TOKEN=...
MESSENGER_ALLOWED_PSIDS=...            # Jonatan PSID only in prod; empty = fail-closed
MESSENGER_ACTOR_MAP=psid:jonatan,...   # empty in live = deny
```

Messenger runs on the **dashboard** webhook (no separate systemd unit). Telegram uses `azom-bot.service`.

## Prod paths (Ubuntu systemd)

- Code: `/opt/azom-agent`
- Data: `/var/lib/azom`
- Logs: `/var/log/azom`
- Env: `/opt/azom-agent/.env` (`AZOM_USE_MOCK=0`)
- Docker data (compose): `/app/.azom-data` — see [`docs/DOCKER_CONFIG_OVERLAY.md`](docs/DOCKER_CONFIG_OVERLAY.md)

## Status (code vs goals)

- **Shipped:** Path B + Path B2 + Sprint A/B/C + SB5 + V2.1 + V2.2 + V2.3 + Shadow Live Ledger + Marketing Google (mock-first)
- **Ops next:** Oscar A1 live soak — [`docs/PILOT_OPS.md`](docs/PILOT_OPS.md) (agents must not mark done)
- **Mock soft-soak:** `bash bin/mock-soak-azom.sh` · `python -m ecom_ops classify-eval` · `python -m ecom_ops kpis`
- **FU9 auto-send:** rails only — see [`docs/CASES.md`](docs/CASES.md) (**do not wire** without Oscar written enable + soak preconditions)
- **NO/DK mailboxes:** remain `enabled: false` until Oscar + credentials
- **Out of scope:** V3 multi-tenant, FAQ/KB, default-on auto-send, Meta/TikTok ads, default-on Ads mutate

## V2.1 Woo/WordPress (pointer)

See [`docs/WOO_WORDPRESS.md`](docs/WOO_WORDPRESS.md) for shipment trackings, multi-site `domain=`, WordPress client, retries, webhooks, pagination, probes.
