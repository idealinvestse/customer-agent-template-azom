---
name: ecom-ops
description: >
  Azom WooCommerce ops (order-status, product desc, support, mail, SSH, cases, bounded WordPress reads and preview-only update/restore plans).
  V2: dashboard onboarding, Gmail OAuth, OpenClaw Messenger/Telegram hybrid,
  Cases 2.0 + Path B suggest-approve rails. Google Ads + GA4 marketing ledger
  (read → suggest → HITL mutate). Critical/code edits escalate to Oscar.
  Never silent customer mail or silent Ads mutate — human approve required.
version: "3.0.0"
---

# ecom-ops (3.0 + Path B)

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
**Model router:** `docs/MODEL_ROUTER.md`  
**Safe ops:** `skills/azom-safe-ops/SKILL.md`  
**Router health:** `skills/model-router-health/SKILL.md`  
**OAuth incident:** `skills/oauth-incident/SKILL.md`  
**OpenClaw client upgrade:** `skills/openclaw-upgrade/SKILL.md`

## Prioritized actions

| Action | Module | CLI |
|--------|--------|-----|
| order-status update | `ecom_ops.actions.order_status` | `python -m ecom_ops --mock order-status --order-id ID --status STATUS` |
| product description | `ecom_ops.actions.product_desc` | `python -m ecom_ops --mock product-desc --product-id ID --language sv` |
| customer support draft | `ecom_ops.actions.support` | `python -m ecom_ops --mock support --message "..."` |
| cases poll / approve | `ecom_ops.cases.service` | `python -m ecom_ops --mock cases poll\|list\|show\|reply\|draft\|regenerate\|close` |
| shadow report (Oscar) | `ecom_ops.cases.shadow_report` | `python -m ecom_ops --actor oscar cases shadow-report` |
| retention purge (Oscar) | `ecom_ops.cases.retention` | `python -m ecom_ops --actor oscar cases retention-purge --dry-run` |
| null-send profile | `ecom_ops.runtime_profile` | `--null-send` or `AZOM_NULL_SEND=1` |
| SSH / VPS | `ecom_ops.actions.ssh_ops` | `python -m ecom_ops --mock ssh --command "uptime"` |
| mail send/fetch | `ecom_ops.actions.mail` | `python -m ecom_ops --mock mail send\|fetch\|reply` |
| marketing Ads+GA4 | `ecom_ops.actions.marketing` | `python -m ecom_ops --mock marketing digest\|health\|consistency\|suggests\|…` |
| runtime status | CLI | `python -m ecom_ops status` · `python -m ecom_ops smoke` |

## V2 surfaces

| Surface | Entry |
|---------|--------|
| Dashboard | `./bin/start-dashboard.sh` → `/onboarding`, `/settings`, `/cases`, `/marketing`, `/wp`, `/oscar` |
| Model router | `ecom_ops.llm` facade over `ecom_ops.model_router`. Oscar: `/oscar` test, `/oauth/openai/start` |
| Gmail OAuth | `/oauth/gmail/start` → tokens in `AZOM_DATA_DIR/oauth/gmail.json` |
| Google marketing OAuth | `/oauth/google/start` → `AZOM_DATA_DIR/oauth/google_marketing.json` |
| Marketing dashboard | `/marketing` (Jonatan read + suggest HITL) |
| Messenger webhook | Dashboard `GET\|POST /webhooks/messenger` |
| Telegram bot | `python -m ecom_ops.bot` or `./bin/dedicated-bot.sh` |
| Cases timer | `./bin/cases-poll.sh` / `azom-cases-poll.timer` |
| Pilot toolkit | `python -m ecom_ops soak-preflight` · `cases calibration-report` |
| Woo webhook | `POST /webhooks/woo` (HMAC) |

## Messenger / Telegram (OpenClaw hybrid)

Slash: `/help` `/commands` `/status` `/whoami` `/new` `/reset` `/stop` `/tools` `/tasks` `/usage` `/model` `/cases` `/order` `/health` `/brief` `/marketing` `/wp sites` `/wp use <site-id>` `/wp clear`.

Free text uses tool-backed reads and proposals. It can read WooCommerce orders/products and WordPress pages, posts, site title/tagline, themes, and plugin status. Order, product, page, post, and site-text writes wait for their confirmation button; plugin on/off also requires Oscar. A free-text "yes" never authorizes a write. Customer mail only goes through an explicit approve path.

WordPress operations use the named, site-bound tools `wp_list_sites`, `wp_site_status`, `wp_backup_status`, `wp_security_findings`, `wp_prepare_update_plan`, `wp_prepare_restore_plan`, and `wp_list_open_operations`. The server checks actor and site permissions and limits a turn to 3 calls, 30 seconds, 4 KiB per result, and 10 KiB total. Site context is bound to actor, channel, and conversation for 12 hours; `/wp sites`, `/wp use <site-id>`, and `/wp clear` select or clear it. Update/restore plans are locked previews with no approval or execution path. Status, backup, and security results may be mock or unknown; live health checks, security scanning, version discovery, update execution, and production restore are not implemented.

`AZOM_TELEGRAM_MODEL_ROUTE` pins one route for Telegram free text and does not fall through on failure. Customer mail requires `/cases approve` or the approve button/postback; never from free text. Ads mutations remain blocked outside the dashboard/CLI approval rails.


Telegram runs durable per-chat jobs from a SQLite inbox with a bounded worker pool. Read turns can take up to three steer messages at safe checkpoints; approved actions queue later input and never run concurrently with another approved action for the same site. `/status`, `/jobs`, `/cancel`, and Oscar-only `/reconcile` are Telegram receiver controls. Messenger remains synchronous.

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

Site operations are separate from the WordPress content client: [`docs/WORDPRESS_OPS.md`](../../docs/WORDPRESS_OPS.md). The chat exposes named, site-bound reads for registry/status, backup status, security findings, and open operations, plus locked update/restore plan previews. The previews cannot be approved or executed. The server enforces actor/site RBAC and bounded calls (3 calls, 30 seconds, 4 KiB per result / 10 KiB total). The 12-hour site context is scoped to actor, channel, and conversation. There is no model-created shell, PHP, or WP-CLI. Security scanning, version discovery, live health checks, update execution, and production restore are not implemented; outputs must report mock or unknown states accurately.

Phase 1 is mock health. Phase 2 is a local mock backup manifest and a staging drill. A typed edit can replace one existing theme or plugin file through the dashboard/CLI path; this is not a chat tool, general shell, plugin update, or restore. Oscar sets the document root and SSH login at `/wp/<id>/ssh`. The private key is stored in the data directory and is not shown again. A normal backup excludes `ssh-keys/`. Live SSH stays off unless mock mode is off, `AZOM_WP_FILES_SSH=1`, a public host key is pinned, and an explicit non-root user is configured. `wp show` does not include the identity path. Production checks and production mock backups need `WP_OPS_ADMIN`. A production file edit also needs `CODE_EDIT`. `restore` refuses. A verified mock backup is not off-site and not green.

## Marketing Google

See `docs/MARKETING_GOOGLE.md` — mock-first Ads+GA4; live **reads** shipped; HITL mutate + kill-switches (mutate still refused).

## RBAC

- **Jonatan:** `viewer` (+ mail/SSH read, settings non-secret, **CASE_REPLY**, **MARKETING_READ**, **MARKETING_SUGGEST**, **WP_OPS_READ**)
- **Oscar:** `full_admin` (secrets UI + escalation resolve + probes + **MARKETING_MUTATE** + `shadow-report` / `retention-purge` + **WP_OPS_ADMIN**; restore still refuses)
- **Agent (automation):** `operator` (order/product/support, **MAIL_SEND**+**MAIL_READ**, **CASE_REPLY**, SSH read, case poll, **MARKETING_READ**, **WP_OPS_OPERATE** for staging health, mock backup, and staging drill)

## Escalation

Everything **critical**, **code edit**, and **non-allowlisted SSH** escalates to **Oscar**.

Tickets: `$AZOM_DATA_DIR/escalations.jsonl` (default `.azom-data/`).

## Automation

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock order-status --order-id 1001 --status completed
python -m ecom_ops --mock mail fetch
./bin/ecom-automation.sh critical "short summary"
./bin/cases-poll.sh
```

`bin/ecom-automation.sh` defaults `AZOM_USE_MOCK=1`, then sources `.env`, which can turn mock off. Prefer the `python -m ecom_ops --mock` form above.

Stop: do not send customer mail, change a live order, or run `sudo bash bin/install.sh` from this card unless Oscar has asked for that exact action.
