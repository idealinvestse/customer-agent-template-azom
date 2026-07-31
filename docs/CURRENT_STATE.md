# Current state — AzomOps-Agent

**Purpose:** Single source of truth for package version, what is shipped, what is blocked on humans, and what is out of scope.  
**Audience:** Coding agents and developers. Operators may skim the “Ops next” section.  
**Read this first:** [`DOC_STYLE.md`](DOC_STYLE.md), then [`AGENTS.md`](../AGENTS.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Package version** | Python package version in `pyproject.toml` — currently **2.0.0**. Capability labels V2.1–V2.3 are tracks, not package bumps. |
| **Path B** | Cases AI quality: suggest-approve badge + auto-send **rails** (default off, not wired to poll send). |
| **Live soak (A1 / FU6)** | Human-run checklist on a real host before any auto-send discussion. |
| **FU9** | Auto-send wire into poll. **Not done.** Rails and kill-switch exist only. |
| **Fail-closed** | Empty allowlist or actor map in live mode (`AZOM_USE_MOCK=0`) denies access. |

## Source of truth order

1. This file (`docs/CURRENT_STATE.md`)
2. `AGENTS.md`
3. Code and tests
4. Chat memory (ignore if conflicting)

## Package and runtime

| Item | Value |
|------|--------|
| Package | **2.0.0** (`pyproject.toml`, `ecom_ops.__version__`) |
| Primary OS | Ubuntu 26.x (24.04 LTS supported) |
| Host sizing | Hetzner CX22 / CPX21 — 2 vCPU, 4 GB RAM |
| Prod code | `/opt/azom-agent` |
| Prod data | `/var/lib/azom` (`AZOM_DATA_DIR`) |
| Prod logs | `/var/log/azom` |
| Prod env | `/opt/azom-agent/.env` with `AZOM_USE_MOCK=0` |

## Shipped (code on main)

Treat these as done in the repository. Do not re-implement from scratch.

| Track | What shipped |
|-------|----------------|
| **V1 core** | order-status, product-desc, support classify/draft, SSH allowlist, mail providers (gmail, outlook, exchange_graph, generic_imap, generic_pop3) |
| **V2.0** | Dashboard onboarding/settings/cases/Oscar, Gmail OAuth browser consent, Telegram OpenClaw hybrid bot, Meta Messenger webhook + shared bot brain, one-shot Ubuntu install + systemd + Docker `azom-agent:2.0`, CLI `version` / `status` / `smoke` |
| **Cases 2.0 + Path B** | Poll → thread → classify → order-enriched draft → suggest-approve → human approve → threaded send; auto-send rails default off + `AZOM_AUTO_SEND_KILL` |
| **Sprint A/B/C + SB5** | Approve-flow, ★ measure, order extract/email lookup, richer context, classify fixtures, poll partial rails, Telegram/Messenger actor fail-closed, soft draft without order_id, regenerate draft (never sends) |
| **V2.1 Woo/WP** | Shipment trackings API, multi-site `domain=`, WordPress client, transport retry/session, webhooks HMAC, pagination iterators, extra Woo endpoints, system status, WP probes + secret redaction — see [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
| **V2.2 mail ops** | Live `probe_mail`, mail env matrix / per-mailbox `env_prefix`, bulk close (not bulk approve) |
| **V2.3 robustness** | Thread reopen, OAuth expiry harden, probe fail-closed; code DoD green |
| **Path B2** | Richer return/billing drafts + priority/UI escalate hints; still never suggest-approve those categories |

## Ops next (human-owned — agents must not mark done)

| Gate | Owner | Status |
|------|-------|--------|
| **A1 live soak** | Oscar (host) + Jonatan (approve sample) | **Blocked** — not executed on prod. Checklist lives in [`PILOT_OPS.md`](PILOT_OPS.md). |
| Baseline KPI / support hours | Oscar + Jonatan | Open — needed for “50% less support time” story |
| Live classify calibration (20–50 redacted samples) | Oscar + Jonatan | Open — do not lower suggest thresholds without this |
| Weekly ops cadences | Oscar / Jonatan | Open after soak |

**Agent rule:** Never mark live soak complete. Never open a PR that wires auto-send into poll until every FU9 gate in [`CASES.md`](CASES.md) is green **and** Oscar gives written enable.

## FU9 auto-send (summary)

- **Status:** Not wired. `should_auto_send` / day counter exist; poll does **not** send.
- **Default:** `config/cases_ai.yaml` → `auto_send_enabled: false`.
- **Kill-switch:** `AZOM_AUTO_SEND_KILL=1` always denies.
- Full gates and rollback: [`CASES.md`](CASES.md) section “Auto-send (FU9)”.

## Mailbox enablement

- SE support mailbox is the pilot path (human approve only).
- **NO/DK** mailboxes in `config/mailboxes.yaml` stay `enabled: false` until Oscar supplies credentials **and** authorizes enablement.
- Do not enable NO/DK in a coding task without that written authorization.

## Explicit non-goals (parked)

Do not start these unless product ownership changes:

- V3 multi-tenant SaaS
- GA4 / engagement program
- FAQ / knowledge base
- IMAP IDLE (timer poll only)
- Default-on auto-send
- Outlook browser OAuth UI (env + probe only today)
- Broad scope expansion beyond Azom single-customer deepen

## OpenRouter budget

- Cap: **USD 100** via `config/limits.yaml`.
- On budget/key miss: still serve order/cases/status via tools; skip LLM phrasing/drafts when needed.

## Roles (quick)

| Actor | Role | Typical powers |
|-------|------|----------------|
| **Jonatan** | `viewer` + `CASE_REPLY` | Read mail/SSH, non-secret settings, **approve/send case replies** |
| **Oscar** | `full_admin` | Secrets UI, probes, resolve escalations, experiment flags (auto-send) |
| **agent** | `operator` | order-status, product-desc, support draft, mail send/read, SSH health, cases poll |

## Absorption note (historical docs removed)

Facts from former `docs/superpowers/`, `docs/solutions/`, `docs/ideation/`, finish/release plans were absorbed into living docs (`CURRENT_STATE`, `CASES`, `PILOT_OPS`, `WOO_WORDPRESS`, `MAIL_PROVIDERS`, `SYSTEM_OVERVIEW`, `DEVELOPER_GUIDE`). Those historical paths no longer exist. Do not recreate them; update living docs instead.

## Related living docs

| Need | Doc |
|------|-----|
| Architecture map | [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) |
| Dev setup / tests | [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) |
| CLI details | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |
| Cases + FU9 | [`CASES.md`](CASES.md) |
| Daily pilot ops + soak | [`PILOT_OPS.md`](PILOT_OPS.md) |
| Mail setup | [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) |
| Woo/WP API surface | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |
