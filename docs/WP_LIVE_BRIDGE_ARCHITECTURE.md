# WordPress Live Bridge, Change Vault & Hybrid Deploy Architecture

**Purpose:** Architecture record for Azom's live WooCommerce/WordPress interaction, per-change backup & rollback vault (`ChangeLedger` / `SnapshotStore`), the `azom-ops-bridge` WordPress companion plugin, and the optional dedicated `*-wp-deployer` SFTP channel.  
**Audience:** Coding agents, Oscar (admin), and platform developers.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md), [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md).

## Glossary

- **Change Vault:** Local SQLite WAL ledger (`AZOM_DATA_DIR/wp_change_ledger.sqlite3`) paired with content-addressed SHA-256 pre/post JSON and file bundles under `AZOM_DATA_DIR/wp_snapshots/<site_id>/<change_id>/`.
- **`azom-ops-bridge`:** Lightweight, HMAC-SHA256-authenticated WordPress companion plugin (`wordpress-plugin/azom-ops-bridge/`, REST namespace `azom-ops/v1`).
- **`*-wp-reader` (`SshFileTransport`):** Existing read-only SSH/SFTP user (`AZOM_WP_FILES_SSH=1`). Continues to refuse `write_bytes` / `create_bytes` (`SshWriteDisabled`).
- **`*-wp-deployer` (`SshDeployTransport`):** Separate, optional hardened SSH/SFTP account (`AZOM_WP_DEPLOY_SSH=1`) stored apart in `wp_deploy_ssh.json` + `ssh-deploy-keys/<site_id>` for WSOD-proof out-of-band rollback.

## 1. Live-Site REST & Transport Audit Fixes

The pre-implementation audit uncovered and fixed the following live-production issues:

- **Cross-market Woo/WP isolation (`skills/ecom_ops/config.py`, `woocommerce.py`, `wordpress.py`):** Per-market base URLs and credentials (`WOO_BASE_URL_<DOM>`, `WOO_CONSUMER_KEY_<DOM>`, `WOO_CONSUMER_SECRET_<DOM>`, `WP_BASE_URL_<DOM>`, `WP_USERNAME_<DOM>`, `WP_APP_PASSWORD_<DOM>`); refuses cross-market fallback across `.no`, `.se`, `.dk`, and `.fi`; enforces `_PinnedHTTPSHandler` + `allow_redirects=False` on Woo/WP HTTP calls.
- **Retry safety & error hygiene (`skills/ecom_ops/integrations/_http_transport.py`, `security.py`):** `RequestsTransport` sets `trust_env=False`, `allow_redirects=False`, caps `Retry-After` at `30s` with UTC naive-date handling + jitter, retries idempotent `GET`/`HEAD` (and `POST` `429` only — never `PUT`/`PATCH`/`DELETE` or `POST` `5xx`), and scrubs Basic auth / app passwords via `redact_secrets(str)`.
- **WooCommerce REST parity (`skills/ecom_ops/integrations/woocommerce.py`, `order_context.py`):** Real Woo `system_status` parsing (`environment.wp_version` + `environment.version`), `manage_stock` allowlist in `update_product`, `custom_tracking_provider` / `tracking_provider` payload fields on `add_shipment_tracking`, clamped pagination, `/shipment-trackings` endpoint priority in `order_context.py`, PHP-serialized meta skipping, and multi-carrier tracking extraction.
- **WordPress REST hardening (`skills/ecom_ops/integrations/wordpress.py`):** `/posts/{id}/revisions` and `/pages/{id}/revisions` (`list_revisions()` with `context="edit"`), `title.get("raw")` preference, `allow_empty` & `max_content_len=131_072` rollback support, cross-host `domain=` block, and Apache `/` fallback on dotted/mixed-case plugin slugs.
- **Webhooks & Probes (`webhooks.py`, `secret_probes.py`):** Accepts unsigned WooCommerce `webhook_id=<digits>` activation pings, enforces bounded-TTL `X-WC-Webhook-Delivery-ID` replay deduplication, rejects non-dict JSON payloads, binds `probe_wordpress()` against `WORDPRESS_USERNAME`/`WORDPRESS_APP_PASSWORD`, and scrubs Basic tokens & app passwords across probe outputs and `probe_last.json` (`0o600`).

## 2. Strategic Evaluation: Extra SSH Account vs WordPress Companion Plugin

| Capability | Read-only SSH (`*-wp-reader`) | Dedicated Deploy SFTP (`*-wp-deployer`) | Companion Plugin (`azom-ops-bridge`) |
|---|---|---|---|
| Read theme/plugin/MU-plugin files | Yes (read-only, incl. `mu-plugins/`) | Overkill (separate key) | Snapshot read + hashes (`theme`/`plugin`/`mu_plugin`) |
| WP/Woo/PHP/OPcache/Multilingual health | No | No | Yes (`GET /azom-ops/v1/health`) |
| Plugin inventory, updates, MU-plugins & drop-ins | Metadata via tree walk | No | Yes (`GET /azom-ops/v1/plugins/inventory`) |
| Atomic plugin update/rollback & auto-update policy | No | No | Yes (`POST /plugins/update`, `/rollback`, `/autoupdate`, `/toggle`) |
| Plugin diagnostics (paused, `debug.log`, `$wp_filter`) | No | Out-of-band `debug.log` tail | Yes (`GET /azom-ops/v1/plugins/diagnostics`) |
| WPML / Polylang / TranslatePress inspection | No | No | Yes (`GET /azom-ops/v1/translations`) |
| WP post/page revisions + pre-snapshot | No | No | Yes (`GET /revisions`, `/snapshot`) |
| PHP syntax + automatic 5xx self-rollback | No | Post-deploy HTTP probe only | Yes (`token_get_all` + loopback probe) |
| Cache flush (WP/Woo/LiteSpeed/WPRocket) + maintenance | No | No | Yes (`POST /cache/flush`, `/maintenance`) |
| **Recover from a broken WP core / PHP fatal (WSOD)** | No (read-only) | **Yes (out-of-band SFTP quarantine & restore)** | No (PHP/REST unreachable during fatal) |

**Decision — Hybrid Architecture:** Keep `SshFileTransport` (`*-wp-reader`) strictly read-only forever, deploy normally through `azom-ops-bridge` (`AZOM_WP_BRIDGE_ENABLED=1`) for WordPress-aware telemetry, translations, caches, and atomic PHP-verified updates, and pair it with an optional out-of-band `SshDeployTransport` (`*-wp-deployer`, `AZOM_WP_DEPLOY_SSH=1`) as a WSOD-proof rescue channel.

## 3. Per-Change Snapshot Vault, Hardened Bridge & Dashboard Panel

- **`azom-ops-bridge` hardening:** Signs `METHOD\nPATH?QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`, persists anti-replay nonces in DB table `<prefix>azom_ops_nonces`, validates allowlisted IPs/CIDRs with `inet_pton` and mask-bit bounds, exempts `/azom-ops/v1/*` from third-party `rest_authentication_errors` blockers and option-backed `azom_ops_maintenance_until` maintenance mode, walks path segments to block symlinks, invalidates OPcache (`opcache_invalidate`), flushes caches before the post-write loopback probe, supports dotted/uppercase slugs, prefers Polylang before WPML, and cleans up DB/option state on `uninstall.php`.
- **Change Vault & `DeployOrchestrator`:** Every REST mutation (`product`, `order`, `page`, `page_draft`, `post`, `settings`, `plugin_toggle`) and file/deploy operation (`file_edit`, `workspace_deploy`, `cache_flush`, `maintenance`) records a SHA-256-verified before/after snapshot in `ChangeLedger` (`skills/ecom_ops/wp_ops/change_ledger.py`) with secrets redacted and per-entry digests verified in `verify_snapshot()`. `DeployOrchestrator` accepts only verified ready plans (`tests_passed` / `staged` / `production_review` with fresh evidence), rejects `draft`/`checks_incomplete`/`deployed`, captures 0-byte live files cleanly, transitions plans to `status="deployed"`, and backs up `wp_change_ledger.sqlite3` in `bin/backup-azom-data.sh`.
- **`ChangeRollbackService`:** Acquires per-site hold locks, rejects duplicate rollbacks (`AlreadyRolledBack`) and non-rollbackable `page_draft` entries, detects live drift in both mock and live modes, supports empty strings / `>8 KiB` pages / explicit `manage_stock=False`, honors market `domain` on entity rollbacks, checks SFTP overlay/key availability before preferring `SshDeployTransport` over `azom-ops-bridge`, and records linked rollback rows.
- **`AZOM_WP_DEPLOY_KILL` & Dashboard UI (`/wp/<site_id>/changes`, `<change_id>`):** Kill switch blocks `bridge_cache_flush`, `bridge_maintenance`, `DeployOrchestrator`, `SshDeployTransport`, and live `ChangeRollbackService`. Read access for `WP_OPS_READ` (Jonatan + Oscar); verify, cache flush, maintenance, `deploy-live`, and `rollback` controls restricted to Oscar under CSRF and strict CSP. Duplicate `/push-draft` returns `DraftAlreadyPushed`.

## 4. Process-Env Fail-Closed Gates

All live capabilities default to **OFF** and are listed in `PROTECTED_OVERLAY_KEYS` (`skills/ecom_ops/runtime_env.py`) so web/dashboard settings cannot enable them at runtime:

| Env var | Purpose | Default |
|---|---|---|
| `AZOM_USE_MOCK` | Offline mock mode (`1` = mock, `0` = live eligible) | `1` (mock) |
| `AZOM_WP_BRIDGE_ENABLED` | Enable live HMAC calls to `azom-ops-bridge` | `0` |
| `WP_BRIDGE_SECRET_<SITE>` | Per-site HMAC secret (>= 32 chars) matching `AZOM_OPS_BRIDGE_SECRET` | unset |
| `AZOM_WP_PLUGIN_UPDATES_ENABLED` | Enable live execution of approved `plu-*` plugin update plans | `0` |
| `AZOM_WP_AUTOUPDATE_WRITE_ENABLED` | Enable live execution of approved `pau-*` auto-update policy plans | `0` |
| `AZOM_WP_DEPLOY_SSH` | Enable `SshDeployTransport` (`*-wp-deployer`) for WSOD file restore & SFTP quarantine | `0` |
| `AZOM_WP_LIVE_DEPLOY_ENABLED` | Enable `DeployOrchestrator` live pushes (`/deploy-live`, incl. `mu_plugin_deploy`) | `0` |
| `AZOM_WP_RESTORE_ENABLED` | Enable live `ChangeRollbackService` & `plr-*` plugin recovery/quarantine execution | `0` |
| `AZOM_WP_DEPLOY_KILL` | Emergency kill-switch halting all bridge/deploy/rollback actions | unset (`0`) |
| `AZOM_WP_PLUGIN_MUTATE_KILL` | Emergency kill-switch halting plugin updates, auto-update writes, and quarantine/unquarantine | unset (`0`) |

## 5. Plugin Lifecycle, Auto-Update Policies, WSOD Quarantine & MU-Plugins (`azom-ops-bridge` v1.1.0)

- **Plugin REST routes (`includes/class-plugins.php`):** HMAC + DB-nonce verified `GET /plugins/inventory`, `POST /plugins/update` (pre-update `pbackup-<change_id>-<slug>.zip` + `Plugin_Upgrader` + OPcache/cache flush + loopback 5xx auto-revert), `POST /plugins/rollback`, `POST /plugins/autoupdate`, `POST /plugins/toggle`, and `GET /plugins/diagnostics` (paused plugins, secret/path-scrubbed `debug.log` tail <= 16 KiB, `$wp_filter` hook map, available backups). `class-snapshots.php` additionally supports `kind="mu_plugin"` scoped strictly inside `WPMU_PLUGIN_DIR` with symlink checks; `wp-content/` drop-ins remain read-only.
- **Immutable SHA-256 HITL plans (12h TTL):** `plu-<12hex>` (plugin updates with canary ordering & critical-plugin gating), `pau-<12hex>` (auto-update policies & drift reconciliation), and `plr-<12hex>` (Bridge rollback, toggle, or out-of-band `*-wp-deployer` SFTP quarantine/unquarantine). Every execution writes verified Change Vault rows (`plugin_update`, `plugin_autoupdate`, `plugin_quarantine`, `plugin_rollback`, `mu_plugin_deploy`). `azom-ops-bridge` is strictly self-protected across PHP, Python service, SFTP quarantine, CLI, and Dashboard (`/wp/<site_id>/plugins`).

## Related

- Operator setup & emergency runbook (Swedish): [`runbooks/wp-ops-bridge-and-deploy.md`](runbooks/wp-ops-bridge-and-deploy.md)
- Plugin lifecycle, auto-updates, WSOD quarantine & MU-plugins runbook (Swedish): [`runbooks/wp-ops-plugin-lifecycle.md`](runbooks/wp-ops-plugin-lifecycle.md)
- WordPress site ops surface: [`WORDPRESS_OPS.md`](WORDPRESS_OPS.md)
- WooCommerce & WordPress REST reference: [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md)
