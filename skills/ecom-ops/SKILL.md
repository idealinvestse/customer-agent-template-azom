---
name: ecom-ops
description: >
  Azom WooCommerce ops (order-status, product desc, support, mail, SSH, cases).
  V2: dashboard onboarding, Gmail OAuth, OpenClaw Messenger/Telegram hybrid,
  Cases 2.0 + Path B suggest-approve rails. Google Ads + GA4 marketing ledger
  (read → suggest → HITL mutate). Critical/code edits escalate to Oscar.
  Never silent customer mail or silent Ads mutate — human approve required.
version: "2.0.0"
---

# ecom-ops (V2 + Path B)

**Purpose:** Skill card for agent hosts (Moss / OpenClaw-style) describing what this package can do.  
**Audience:** Coding agents and skill hosts.  
**Read this first:** repository root `AGENTS.md`, `docs/CURRENT_STATE.md`, `SOUL.md`.

**Identity / hard rules:** `SOUL.md`  
**System map:** `docs/SYSTEM_OVERVIEW.md`  
**Messenger (daily driver):** `docs/MESSENGER_OPENCLAW.md`  
**Telegram (backup):** `docs/TELEGRAM_OPENCLAW.md`  
**Cases:** `docs/CASES.md`  
**CLI:** `docs/CLI_REFERENCE.md`  
**Dev setup:** `docs/DEVELOPER_GUIDE.md`

## Prioritized actions

| Action | Module | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops order-status --order-id ID --status STATUS --mock` |
| product description | `ecom_ops.actions.product_desc` | `python -m ecom_ops product-desc --product-id ID --language sv --mock` |
| customer support draft | `ecom_ops.actions.support` | `python -m ecom_ops support --message "..." --mock` |
| cases poll / approve | `ecom_ops.cases.service` | `python -m ecom_ops cases poll\|list\|show\|reply\|draft\|regenerate\|close --mock` |
| shadow report (Oscar) | `ecom_ops.cases.shadow_report` | `python -m ecom_ops --actor oscar cases shadow-report` |
| retention purge (Oscar) | `ecom_ops.cases.retention` | `python -m ecom_ops --actor oscar cases retention-purge --dry-run` |
| null-send profile | `ecom_ops.runtime_profile` | `--null-send` or `AZOM_NULL_SEND=1` |
| SSH / VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops ssh --command "uptime" --mock` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops mail send\|fetch\|reply --mock` |
| marketing Ads+GA4 | `ecom_ops.actions.marketing` | `python -m ecom_ops marketing digest\|health\|consistency\|suggests\|… --mock` |
| runtime status | CLI | `python -m ecom_ops status` · `python -m ecom_ops smoke` |

## V2 surfaces

| Surface | Entry |
|---------|--------|
| Dashboard | `./bin/start-dashboard.sh` → `/onboarding`, `/settings`, `/cases`, `/marketing`, `/oscar` |
| Gmail OAuth | `/oauth/gmail/start` → tokens in `AZOM_DATA_DIR/oauth/gmail.json` |
| Google marketing OAuth | `/oauth/google/start` → `AZOM_DATA_DIR/oauth/google_marketing.json` |
| Marketing dashboard | `/marketing` (Jonatan read + suggest HITL) |
| Messenger webhook | Dashboard `GET\|POST /webhooks/messenger` |
| Telegram bot | `python -m ecom_ops.bot` or `./bin/dedicated-bot.sh` |
| Cases timer | `./bin/cases-poll.sh` / `azom-cases-poll.timer` |
| Woo webhook | `POST /webhooks/woo` (HMAC) |

## Messenger / Telegram (OpenClaw hybrid)

Slash: `/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` `/cases` `/order` `/health` `/brief` `/marketing` …  
Free text: read-only tool prefetch + LLM phrasing (Swedish). **Send only** via `/cases approve` or approve button/postback — never from free-text alone. **Ads mutates** only via dashboard/CLI approve — never free-text.

## Mail providers

- `gmail` — SMTP+IMAP (app password or OAuth2 XOAUTH2 / browser consent)
- `outlook` — SMTP+IMAP (app password or OAuth2 XOAUTH2); no dashboard OAuth UI
- `exchange_graph` — Microsoft Graph REST (client credentials)
- `generic_imap` / `generic_pop3` — custom hosts via env

Details: `docs/MAIL_PROVIDERS.md`.

## Cases AI rails

`config/cases_ai.yaml`: suggest-approve for `order_status`/`shipping` (default); `never_suggest_categories` includes `abuse`/`return`/`billing` (Path B2 still drafts those richer, never ★). Auto-send **off** and **not wired** into poll unless Oscar enables after FU9 gates (`AZOM_AUTO_SEND_KILL`). Null-send / Shadow Live Ledger: `AZOM_NULL_SEND` / `--null-send` (default off); Oscar `cases shadow-report`.

## Woo / WordPress

See `docs/WOO_WORDPRESS.md` — shipment trackings, multi-site `domain=`, WordPress Application Passwords, webhook HMAC, retries.

## Marketing Google

See `docs/MARKETING_GOOGLE.md` — mock-first Ads+GA4; live REST wired (Oscar OAuth/creds); HITL mutate + kill-switches.

## RBAC

- **Jonatan:** `viewer` (+ mail/SSH read, settings non-secret, **CASE_REPLY**, **MARKETING_READ**, **MARKETING_SUGGEST**)
- **Oscar:** `full_admin` (secrets UI + escalation resolve + probes + **MARKETING_MUTATE** + `shadow-report` / `retention-purge`)
- **Agent (automation):** `operator` (order/product/support, **MAIL_SEND**+**MAIL_READ**, **CASE_REPLY**, SSH read, case poll, **MARKETING_READ**)

## Escalation

Everything **critical**, **code edit**, and **non-allowlisted SSH** escalates to **Oscar**.

Tickets: `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Automation

```bash
./bin/ecom-automation.sh order-status --order-id 1001 --status completed
./bin/ecom-automation.sh mail fetch
./bin/ecom-automation.sh critical "short summary"
./bin/cases-poll.sh
sudo bash bin/install.sh   # full VPS bootstrap (Ubuntu 26/24)
```
