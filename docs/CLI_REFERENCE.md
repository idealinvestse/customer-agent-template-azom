# CLI reference — `python -m ecom_ops`

**Purpose:** Complete command reference for the ecom-ops CLI, with flags and expected behavior.  
**Audience:** Developers and coding agents.  
**Read this first:** [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md), [`CURRENT_STATE.md`](CURRENT_STATE.md).

**What you get:** Every operator command in one place: cases, mail, marketing, WordPress site ops, KPIs, and the launcher. Flags below are the ones `skills/ecom_ops/cli.py` actually accepts. The product tour is [`FUNKTIONER.md`](FUNKTIONER.md).

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
| `--actor` | profile `operator` (Azom: `agent`) | Must exist in `config/rbac.yaml`. Azom: `jonatan` \| `oscar` \| `agent` |
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
# expect: version payload for package 3.0.0
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
python -m ecom_ops --mock product-desc --product-id 42 --language sv --research
python -m ecom_ops --mock product-desc --product-id 42 --language sv --research-brief res-000000000001
python -m ecom_ops --mock product-desc --name "Widget" --features "a,b" --language nb --publish
# flags: --product-id, --name, --features, --language (default sv), --publish, --research-brief, --research
# note: --research runs product research before generating copy and refuses --publish
```

### `support`

Classify a support message and produce a draft (does not send customer mail).

```bash
python -m ecom_ops --mock support --message "Var är order 1001?"
# optional: --email, --customer-name, --language (default sv)
```

### `ssh` / `ssh-health`

Mock accepts only exact allowlisted health commands. Live SSH is disabled unless `AZOM_SSH_READ_ENABLED=1` is set in the process environment and a dedicated non-root `*-ssh-reader`, OpenSSH key, and explicit pinned `SSH_KNOWN_HOSTS` file are configured. The runner disables SSH config, agent, password authentication, forwarding, and TTY; it refuses root, unpinned hosts, and every command outside the fixed allowlist. The account must have no sudo or Docker group access. Configure a server-side command wrapper with the same allowlist before enabling it, and verify the host key fingerprint out of band before adding it to `known_hosts`.

The `known_hosts` file must contain only exact, unhashed entries for the configured host: `host key-type public-key` on port 22, or `[host]:port key-type public-key` on another port. Do not trust an unverified `ssh-keyscan` result.

```bash
python -m ecom_ops --mock ssh --command "uptime"
python -m ecom_ops --mock ssh-health
# optional: --host
```

### `ops`

Runtime health snapshot and disk housekeeping (`skills/ecom_ops/runtime_health.py`, `skills/ecom_ops/ops_gc.py`):

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops ops status [--json]
# expect: poller lease status, Telegram/Messenger queue counts, circuit breakers, ChatGPT OAuth TTL (no tokens), disk usage, and recent sanitized errors

python -m ecom_ops ops gc --dry-run [--grace-hours 24] [--json]
# expect: dry-run counts for expired wr-* workspaces, untracked pv-* dirs, old inbox media, expired proposals, and log rotation; mutates nothing

python -m ecom_ops --actor oscar ops gc [--grace-hours 24] [--json]
# Oscar ADMIN: executes housekeeping (cleans expired wr-* dirs past grace period, untracked pv-*, old telegram/messenger inbox media, expired bot_proposals/wp_review_pins, expired/LRU web_search_cache.sqlite3 rows, expired non-pinned research_artifacts/res-*, and gzip-rotates telemetry.jsonl / audit.jsonl)
```

Live execution of `ops gc` requires `--actor oscar` (`full_admin`). `--dry-run` and `ops status` are read-only. Active (non-expired) `wr-*` workspaces are never removed.

### Quality / KPI helpers

| Command | Flags | Purpose |
|---------|-------|---------|
| `kpis` | `--days` (default 7), `--baseline PATH` | Case / approve KPI dump; optional human baseline compare (deltas only) |
| `soak-preflight` | (none) | Read-only A1 pre-flight checks; never marks soak complete |
| `classify-eval` | `--fixtures DIR` (default `tests/fixtures/support_classify`) | Fixture accuracy check |
| `draft-eval` | `--dir DIR` (default `tests/fixtures/draft_quality`) | Draft quality fixtures |
| `drift-check` | `--days` (default 7) | Ops drift signals |
| `trends` | `--days` (default 30) | Longer-window trends |

```bash
python -m ecom_ops kpis --days 7
python -m ecom_ops kpis --days 7 --baseline config/baseline.example.yaml
python -m ecom_ops soak-preflight
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
# Oscar ADMIN: latest-per-case FU9 shadow trail + suggest-vs-outcome cross-tab

python -m ecom_ops --actor oscar cases calibration-export [--days 30]
# Oscar ADMIN: redacted samples (no subject/body/from/draft)

python -m ecom_ops --actor oscar cases calibration-report [--days 30]
# Oscar ADMIN: classifier flags vs human outcomes; does not mark calibration complete

python -m ecom_ops --mock cases close --id <uuid> [--reason "..."]
# close without customer reply

python -m ecom_ops --actor oscar cases retention-purge [--days 90] [--redact] [--dry-run]
# Oscar ADMIN — GDPR: delete or redact old closed cases (and unlink case:<uuid> target_ref from research_jobs.sqlite3)

python -m ecom_ops --mock cases research --id <uuid> [--query "..."] [--depth quick|standard] [--wait]
# Run or enqueue PII-firewalled web research linked to case:<uuid> (never sends customer mail)
```

Global `--null-send` (or `AZOM_NULL_SEND=1`) activates the null-send profile. `status` always prints `null_send=on|off`.  
`shadow-report`, `calibration-export`, `calibration-report`, and `retention-purge` require `Permission.ADMIN` (use `--actor oscar`).

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
python -m ecom_ops --mock marketing research --topic "Google Ads Consent Mode v2 SE" [--depth quick|standard|deep] [--wait]
```

### `wp`

WordPress site health and local mock backups (Phase 1–2). See [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md).

```bash
python -m ecom_ops --mock --actor jonatan wp sites
python -m ecom_ops --mock --actor jonatan wp status
python -m ecom_ops --mock --actor jonatan wp show --id mock-staging
python -m ecom_ops --mock --actor agent wp check --id mock-staging
python -m ecom_ops --mock --actor oscar wp check --id mock-shop
python -m ecom_ops --mock --actor oscar wp check --due
python -m ecom_ops --mock --actor oscar wp backup --id mock-shop
python -m ecom_ops --mock --actor jonatan wp backups --id mock-shop
python -m ecom_ops --mock --actor agent wp restore-drill --backup <backup-id> --target mock-staging
python -m ecom_ops --mock --actor jonatan wp files --id mock-staging --kind theme [--slug twentytwentyfive]
python -m ecom_ops --mock --actor agent wp file-propose --id mock-staging --kind theme --slug twentytwentyfive --path style.css --from ./edit.css
python -m ecom_ops --mock --actor agent wp file-apply --id mock-staging --plan fe-aaaaaaaaaaaa --from ./edit.css
python -m ecom_ops --mock --actor jonatan wp changes --id mock-staging [--limit 20] [--category product] [--status applied|rolled_back] [--batch seob-aaaaaaaaaaaa] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan wp change-show --id mock-staging --change chg-aaaaaaaaaaaa
python -m ecom_ops --mock --actor oscar wp rollback --id mock-staging --change chg-aaaaaaaaaaaa [--execute --expected-sha256 <64-hex>]
python -m ecom_ops --mock --actor oscar wp rollback-batch --id mock-staging --batch seob-aaaaaaaaaaaa [--execute [--confirm-batch-id seob-aaaaaaaaaaaa]]
python -m ecom_ops --mock --actor jonatan wp bridge-health --id mock-staging
python -m ecom_ops --mock --actor oscar wp cache-flush --id mock-staging
python -m ecom_ops --mock --actor oscar wp bridge-maintenance --id mock-staging --enabled 1|0|on|off [--duration 300]
python -m ecom_ops --mock --actor jonatan wp bridge-translations --id mock-staging [--post-id 123]
python -m ecom_ops --mock --actor oscar wp deploy-live --id mock-staging --plan wr-aaaaaaaaaaaa --base-sha256 <64-hex> --work-sha256 <64-hex> [--transport auto|bridge|sftp_deploy]
python -m ecom_ops --mock --actor jonatan wp plugins-inventory --id mock-staging [--wporg]
python -m ecom_ops --mock --actor jonatan wp plugins-mu --id mock-staging
python -m ecom_ops --mock --actor jonatan wp plugins-plans --id mock-staging [--table | --format json|table]
python -m ecom_ops --mock --actor oscar wp plugins-plan-update --id mock-staging [--mode policy|single|selected|all] [--policy non_critical_only|security_only|patch_only|minor_safe] [--slugs a,b] [--exclude c] [--include-critical]
python -m ecom_ops --mock --actor oscar wp plugins-update --id mock-staging --plan plu-aaaaaaaaaaaa --expected-sha256 <64-hex> [--allow-critical]
python -m ecom_ops --mock --actor jonatan wp plugins-autoupdate --id mock-staging [--show | --drift]
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --plan [--preset non_critical_only|security_only|disable_all|custom] [--changes-json '[...]'] [--allow-critical]
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --execute --plan pau-aaaaaaaaaaaa --expected-sha256 <64-hex>
python -m ecom_ops --mock --actor oscar wp plugins-diagnostics --id mock-staging [--hooks init,plugins_loaded] [--log-lines 80]
python -m ecom_ops --mock --actor oscar wp plugins-quarantine --id mock-staging --slug broken-plugin [--kind plugin|mu_plugin]
python -m ecom_ops --mock --actor oscar wp plugins-unquarantine --id mock-staging --slug broken-plugin [--quarantine-name <name>] [--kind plugin|mu_plugin]
python -m ecom_ops --mock --actor oscar wp plugins-recovery --id mock-staging --slug broken-plugin --action rollback_snapshot|deactivate|quarantine_sftp|restore_from_quarantine [--change chg-aaaaaaaaaaaa]
python -m ecom_ops --mock --actor oscar wp plugins-recovery --id mock-staging --execute --plan plr-aaaaaaaaaaaa --expected-sha256 <64-hex>
```

Telegram's WordPress conversation uses the same registered site ids. Use `/wp sites` to list sites visible under `WP_OPS_READ`, `/wp use <site-id>` to set the current conversation site, and `/wp clear` to clear it. Natural-language requests can use the named site-status, backup-status, security-findings, open-operations, and plan-preview tools. Each turn is limited to 3 WordPress tool calls, 30 seconds of tool execution, 4 KB per result, and 10 KB combined. Plan previews are stored with a digest for 12 hours but remain non-executable and cannot be approved while the update and restore adapters are unavailable.

Staging checks, staging backups, and staging drills need `WP_OPS_OPERATE`. Production checks and production mock backups need `WP_OPS_ADMIN`. `wp restore` is not a command. A verified manifest stays `green: false` with `encryption: unconfigured` and `offsite: false`.

`wp files` lists themes, plugins, or the editable files in one component. `file-propose` and `file-apply` read the new text from `--from`. There is no `--body` and no new file command in this pass. A staging edit needs `WP_OPS_OPERATE`. A production edit needs `WP_OPS_ADMIN` and `CODE_EDIT`. The JSON does not include the file body. `AZOM_USE_MOCK=1` keeps the edit on the local mock tree even if `AZOM_WP_FILES_SSH=1`. Live SFTP still refuses `write_bytes`, so `file-apply` cannot change a customer site. Chat diff plans and the dashboard review page are separate from these two commands. The production review POST returns `BackupNotVerified`.

The document root and SSH login are set in the dashboard at `/wp/<id>/ssh`, not by a CLI flag. Oscar can save them. `wp show` and `wp sites` do not include the local identity path or the public host key. The private key is not printed.

#### WordPress ops Do / Do not

**Do:**

- Use the synthetic `example.test` sites unless the owner names a pilot.
- Read `secret_ref` as an environment variable name.

**Do not:**

- Point `wp check` at a customer site. The health package has no live HTTP client.
- Pass a shell command to `wp file-propose`. The edit path is site, kind, slug, and one relative file.
- Treat an unknown backup, a verified local mock manifest, or a single failed probe as a green, healthy site.

`status` includes `ga4=on|off`, `ads=on|off`, and `web_search` readiness. Soft path: `bash bin/mock-marketing-azom.sh`.

### `search`, `fetch-url`, and `research`

Fail-closed mock-first web search, public HTTPS extraction, and async Deep Research jobs (`skills/ecom_ops/research/`, [`WEB_SEARCH_RESEARCH.md`](WEB_SEARCH_RESEARCH.md)):

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock search --query "WooCommerce HPOS hooks" [--profile deep_general] [--locale sv] [--freshness-days 30] [--max-results 5]
python -m ecom_ops --mock fetch-url --url "https://developer.woocommerce.com/docs/hpos-overview/" [--profile deep_general] [--max-chars 5000]
python -m ecom_ops --mock research start --topic "Nordisk CarPlay marknadsanalys" [--profile deep_general] [--depth quick|standard|deep] [--market SE|NO|DK] [--target-ref <ref>] [--wait]
python -m ecom_ops --mock research list [--status STATUS] [--limit 20]
python -m ecom_ops --mock research show --id <res-id> [--section full|summary|sources]
python -m ecom_ops --mock research status --id <res-id>
python -m ecom_ops --mock research steer --id <res-id> --note "..."
python -m ecom_ops --mock research cancel --id <res-id>
python -m ecom_ops --mock research profiles [--id <profile-id>] [--recommend "<goal>" --ambition quick|standard|deep]
# expect: JSON with ok: true, provenance: "mock" (in mock mode), and res-<12hex> identifiers
```

### `seo`

Fail-closed mock-first WordPress/WooCommerce SEO Studio (`skills/ecom_ops/seo/`, [`SEO_STUDIO.md`](SEO_STUDIO.md)):

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop [--scope site|technical|page|product|media|links|cannibalization|gsc|parity|deep_crawl] [--path /] [--market no|se|dk] [--target-id ID] [--limit 50]
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop --scope media
python -m ecom_ops --mock --actor jonatan seo audit --site mock-shop --scope cannibalization
python -m ecom_ops --mock --actor jonatan seo audits [--site mock-shop] [--limit 20] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo fixes [--site mock-shop] [--status pending|applied] [--limit 30] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo batches [--site mock-shop] [--limit 20] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo batch-propose --site mock-shop --audit seoa-aaaaaaaaaaaa [--scope all|media_alt|seo_meta] [--limit 20]
python -m ecom_ops --mock --actor oscar seo batch-apply --site mock-shop --batch seob-aaaaaaaaaaaa --expected-sha256 <64-hex>
python -m ecom_ops --mock --actor jonatan seo gsc [--site mock-shop] [--days 28] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo parity [--site mock-shop] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo diff [--site mock-shop] [--latest seoa-...] [--previous seoa-...]
python -m ecom_ops --mock --actor jonatan seo digest [--site mock-shop] [--days 7] [--markdown]
python -m ecom_ops --mock --actor oscar seo fix-propose --site mock-shop --fix-type seo_meta --target-type product|page|post|media|category|site --target-id 501 --json-fields '{"meta_title":"CarPlay Volvo | Azom"}' [--summary "..."] [--market no|se|dk]
python -m ecom_ops --mock --actor oscar seo fix-apply --site mock-shop --plan seom-aaaaaaaaaaaa --expected-sha256 <64-hex>
python -m ecom_ops --mock --actor jonatan seo strategy --site mock-shop --title "Nordisk CarPlay SEO" [--market no|se|dk] [--topics "CarPlay,DAB+"] [--goals "..."] [--research-brief res-...]
python -m ecom_ops --mock --actor jonatan seo campaign --site mock-shop --name "CarPlay Volvo" [--market no|se|dk] [--strategy-id seos-...] [--keywords "carplay volvo"] [--urls "/produkt/carplay"] [--campaign-type commercial|seasonal|product_launch|content_hub|technical_sprint|category|informational|technical] [--objective "..."] [--research-brief res-...]
python -m ecom_ops --mock --actor jonatan seo link-pack --site mock-shop --name "NO CarPlay Q2" [--market no|se|dk] [--campaign-id seoc-...] [--budget 15000] [--links 6] [--targets-json '[{"target_url":"/","keyword":"carplay","anchor_type":"brand","anchor_text":"Azom"}]'] [--notes "..."] [--export md|json|csv]
python -m ecom_ops --mock --actor jonatan seo link-packs [--site mock-shop] [--limit 20] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo link-pack-edit --id seol-aaaaaaaaaaaa [--name "..."] [--budget 15000] [--targets-json '[...]'] [--notes "..."]
python -m ecom_ops --mock --actor jonatan seo link-pack-verify --id seol-aaaaaaaaaaaa [--table | --format json|table]
python -m ecom_ops --mock --actor oscar seo link-pack-status --id seol-aaaaaaaaaaaa --status draft|ready_for_review|approved_for_manual_order|ordered_external|delivered|cancelled [--note "..."]
python -m ecom_ops --mock --actor jonatan seo vendors --market no --budget 15000 --links 6 [--goal organic_growth] [--no-web-research] [--table | --format json|table]
python -m ecom_ops --mock --actor jonatan seo autopilot --site mock-shop --topic "Trådlös CarPlay Volvo & BMW" --market no --budget 15000 --links 6 [--no-web-research] [--with-batch] [--table | --format json|table]
# note: live seo fix-apply / batch-apply requires AZOM_USE_MOCK=0, AZOM_SEO_WRITE_ENABLED=1, AZOM_SEO_KILL unset, and Oscar WP_OPS_ADMIN; seo autopilot & seo vendors are strictly HITL (never auto-buy links or auto-apply WP edits)
```

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
| `AZOM_WEB_SEARCH_ENABLED=1` | Live web search egress gate (process env only; requires `AZOM_USE_MOCK=0`) |
| `AZOM_WEB_FETCH_ENABLED=1` | Live web fetch egress gate (process env only; requires `AZOM_USE_MOCK=0`) |
| `AZOM_RESEARCH_ENABLED=1` | Live async Deep Research gate (process env only; requires `AZOM_USE_MOCK=0`) |
| `AZOM_WEB_SEARCH_KILL=1` / `AZOM_RESEARCH_KILL=1` | Emergency kill-switches forcing search/research off (process env only) |
| `AZOM_SEO_WRITE_ENABLED=1` | Live SEO Studio write gate for approved `seom-*` / `seob-*` meta/media fixes (process env only; requires `AZOM_USE_MOCK=0`) |
| `AZOM_SEO_KILL=1` | Emergency kill-switch halting all SEO Studio writes and SEO rollbacks (process env only) |
| `AZOM_GSC_ENABLED=1` | Live read-only Google Search Console (`webmasters.readonly`) gate (process env only; requires `AZOM_USE_MOCK=0`) |
| `AZOM_GSC_KILL=1` | Emergency kill-switch forcing Google Search Console connector off (process env only) |

## Related

- Product tour (Swedish): [`FUNKTIONER.md`](FUNKTIONER.md)
- Cases ops (Swedish): [`CASES.md`](CASES.md)
- Mail setup (Swedish): [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md)
- Marketing Google: [`MARKETING_GOOGLE.md`](MARKETING_GOOGLE.md)
- Web search & research: [`WEB_SEARCH_RESEARCH.md`](WEB_SEARCH_RESEARCH.md)
- System map: [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md)

## System Launcher (`ecom_ops.launcher`)

Preflight check (Python, config, `.env`, OAuth, model router, circuit breaker, dashboard ports, Telegram/Messenger allowlists) and interactive/headless launcher:

```bash
# Bash (Ubuntu/Linux/macOS) or PowerShell (Windows)
./bin/launch.sh
.\bin\launch.ps1

# Direct CLI
python -m ecom_ops.launcher --check [--mode mock|live] [--json]
python -m ecom_ops.launcher --init-env
python -m ecom_ops.launcher --target all|dashboard|bot [--dry-run]
```
