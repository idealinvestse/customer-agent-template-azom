# CLI reference — `python -m ecom_ops`

**Purpose:** Complete command reference for the ecom-ops CLI, with flags and expected behavior.  
**Audience:** Developers and coding agents.  
**Read this first:** [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md), [`CURRENT_STATE.md`](CURRENT_STATE.md).

## Entry points

| Entry | What it starts |
|-------|----------------|
| `python -m ecom_ops …` | Main CLI (`skills/ecom_ops/cli.py`) |
| `python -m ecom_ops.bot` | Telegram long-poll bot (separate process) |
| `./bin/ecom-automation.sh …` | Thin wrapper around CLI for automation |
| `./bin/cases-poll.sh` | One-shot cases poll |
| `./bin/dedicated-bot.sh` | Bot launcher |
| `./bin/start-dashboard.sh` | Flask dashboard (not this CLI) |

Most CLI commands print **JSON** to stdout. Exit code `0` when `ok` is true (or non-dict success); non-zero on failure.

## Global flags

```bash
python -m ecom_ops [--site SITE] [--actor ACTOR] [--mock] [--null-send] <command> ...
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--site` | `azom` | Customer / site id |
| `--actor` | `agent` | RBAC actor: `jonatan` \| `oscar` \| `agent` |
| `--mock` | off | Sets `AZOM_USE_MOCK=1` for this process (no external network) |
| `--null-send` | off | Null-send profile (refuse customer mail; shadow FU9) |
| `--version` | — | Print version and exit (argparse version action) |

**Actor guidance:**

- Poll / automation: `--actor agent` (default) is correct.
- Approve/send as storefront owner: `--actor jonatan`.
- Admin-only operations: `--actor oscar` when RBAC requires it.

## Commands

### `version`

```bash
python -m ecom_ops version
# expect: version payload for package 2.0.0
```

### `status`

```bash
python -m ecom_ops status
# expect: runtime flags, mock/live, config/data paths, readiness-oriented fields
```

### `smoke`

Opt-in integration smoke. Requires `AZOM_LIVE_SMOKE=1` **or** `--live`.

```bash
python -m ecom_ops smoke
python -m ecom_ops smoke --live
# Do not run --live against production unless Oscar authorized a live check
```

### `order-status`

Update WooCommerce order status.

```bash
python -m ecom_ops --mock order-status --order-id 1001 --status completed
# required: --order-id, --status
```

### `product-desc`

Generate (and optionally publish) a product description.

```bash
python -m ecom_ops --mock product-desc --product-id 42 --language sv
python -m ecom_ops --mock product-desc --name "Widget" --features "a,b" --language nb --publish
# flags: --product-id, --name, --features, --language (default sv), --publish
```

### `support`

Classify a support message and produce a draft (does not send customer mail).

```bash
python -m ecom_ops --mock support --message "Var är order 1001?"
# optional: --email, --customer-name, --language (default sv)
```

### `ssh` / `ssh-health`

Allowlisted SSH only; unsafe commands escalate to Oscar.

```bash
python -m ecom_ops --mock ssh --command "uptime"
python -m ecom_ops --mock ssh-health
# optional: --host
```

### Quality / KPI helpers

| Command | Flags | Purpose |
|---------|-------|---------|
| `kpis` | `--days` (default 7) | Case / approve KPI dump |
| `classify-eval` | `--fixtures DIR` (default `tests/fixtures/support_classify`) | Fixture accuracy check |
| `draft-eval` | `--dir DIR` (default `tests/fixtures/draft_quality`) | Draft quality fixtures |
| `drift-check` | `--days` (default 7) | Ops drift signals |
| `trends` | `--days` (default 30) | Longer-window trends |

```bash
python -m ecom_ops kpis --days 7
python -m ecom_ops classify-eval
python -m ecom_ops draft-eval
python -m ecom_ops drift-check --days 7
python -m ecom_ops trends --days 30
```

**Note:** Connection probes (`probe_mail`, `probe_woocommerce`, …) are **dashboard Oscar** actions (`POST /oscar/secrets/test`), not CLI subcommands.

### `mail`

```bash
python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
# optional: --cc, --html-body, --provider

python -m ecom_ops --mock mail fetch [--folder INBOX] [--limit 20] [--all] [--provider]

python -m ecom_ops --mock mail reply --to a@b.co --subject "Re: …" --body "…"
# optional: --uid, --html-body, --provider
```

`--provider` when set: `gmail` \| `outlook` \| `exchange_graph` \| `generic_imap` \| `generic_pop3`.

Thread-preserving reply behavior matters for real mail; see [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md) and [`CASES.md`](CASES.md).

### `cases`

```bash
python -m ecom_ops --mock cases poll [--limit 20]
# ingest mailboxes → create/update cases; partial errors escalate

python -m ecom_ops --mock --null-send cases poll
# same poll + FU9 shadow observations; customer mail refused while null-send on

python -m ecom_ops --mock cases list [--status open] [--limit 50]
# --status may be comma-separated, e.g. open,escalated

python -m ecom_ops --mock cases show --id <uuid>

python -m ecom_ops --mock cases draft --id <uuid> --body "..."
# saves draft; NEVER sends

python -m ecom_ops --mock cases regenerate --id <uuid>
# new draft from inbound; NEVER sends; preserves prior draft; cooldown applies in service

python -m ecom_ops --mock cases reply --id <uuid> [--body "..."]
# APPROVE AND SEND — human path; use --actor jonatan in real ops
# under --null-send / AZOM_NULL_SEND=1 this refuses before claim (no customer mail)

python -m ecom_ops --actor oscar cases shadow-report [--days 7]
# Oscar ADMIN: latest-per-case FU9 shadow trail (eligible vs denied + reason breakdown)

python -m ecom_ops --mock cases close --id <uuid> [--reason "..."]
# close without customer reply

python -m ecom_ops --actor oscar cases retention-purge [--days 90] [--redact] [--dry-run]
# Oscar ADMIN — GDPR: delete or redact old closed cases
```

Global `--null-send` (or `AZOM_NULL_SEND=1`) activates the null-send profile. `status` always prints `null_send=on|off`.  
`shadow-report` and `retention-purge` require `Permission.ADMIN` (use `--actor oscar`).

#### Cases Do / Do not

**Do:**

- Use `draft` / `regenerate` to edit content without sending.
- Use `reply` only when a human intends to send.
- Treat poll partial errors as operational failures (check escalations / `last_case_poll.json`).

**Do not:**

- Assume `regenerate` sends mail — it does not.
- Bulk-approve via CLI inventiveness — there is no bulk approve command; bulk close exists in dashboard with RBAC only.
- Wire auto-send into poll from a “helpful” CLI change — see FU9 in [`CASES.md`](CASES.md).

### `marketing`

Google Ads + GA4 (mock-first). See [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md).

```bash
python -m ecom_ops --mock marketing digest [--days 7]
python -m ecom_ops --mock marketing health
python -m ecom_ops --mock marketing waste [--days 7]
python -m ecom_ops --mock marketing pacing
python -m ecom_ops --mock marketing consistency [--days 7] [--woo-purchases N]
python -m ecom_ops --mock marketing mer [--days 7] [--woo-revenue N]
python -m ecom_ops --mock marketing snapshot
python -m ecom_ops --mock --actor jonatan marketing suggests build
python -m ecom_ops --mock marketing suggests list [--status open]
python -m ecom_ops --mock --actor jonatan marketing suggests deny --id <uuid>
python -m ecom_ops --mock --actor jonatan marketing suggests approve --id <uuid>
# approve requires kill-switch unset + ads_mutate_enabled (negatives: Jonatan; else Oscar)
python -m ecom_ops --mock --actor jonatan marketing mp-queue --name purchase --payload-json '{}'
python -m ecom_ops --mock --actor jonatan marketing merchant-queue --offer-id SKU-1 --title "…"
```

`status` includes `ga4=on|off` and `ads=on|off`. Soft path: `bash bin/mock-marketing-azom.sh`.

## Environment that changes CLI behavior

| Env | Effect |
|-----|--------|
| `AZOM_USE_MOCK=1` | Mock integrations (also set by `--mock`) |
| `AZOM_CONFIG_DIR` | Config directory (default `./config` in dev) |
| `AZOM_DATA_DIR` | Data directory for `cases.db`, oauth, secrets overlay |
| `AZOM_LIVE_SMOKE=1` | Allows `smoke` without `--live` |
| `AZOM_AUTO_SEND_KILL=1` | Forces auto-send eligibility deny (rails only today) |
| `AZOM_NULL_SEND=1` | Null-send profile (customer mail refused) |
| `AZOM_GA4_PROPERTY_IDS` | Fail-closed GA4 allowlist (empty in live = deny) |
| `AZOM_GADS_CUSTOMER_IDS` | Fail-closed Ads allowlist (empty in live = deny) |
| `AZOM_ADS_MUTATE_KILL=1` | Always deny Ads mutate / merchant write |
| `AZOM_GA_MUTATE_KILL=1` | Reserved GA-admin mutate kill (deny when set; no mutate path yet) |
| `AZOM_MP_KILL=1` | Always deny Measurement Protocol send |

## Related

- Cases ops (Swedish): [`CASES.md`](CASES.md)
- Mail setup (Swedish): [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md)
- Marketing Google: [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md)
- System map: [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md)
