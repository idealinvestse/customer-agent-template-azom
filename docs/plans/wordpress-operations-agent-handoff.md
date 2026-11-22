# WordPress Operations Agent — Grok Build Handoff

**Purpose:** Define the product and implementation plan for safe, observable technical operations across the customer's WordPress sites.  
**Audience:** Grok Build and the project owner.  
**Read this first:** [`CURRENT_STATE.md`](../CURRENT_STATE.md), [`AGENTS.md`](../../AGENTS.md), [`SYSTEM_OVERVIEW.md`](../SYSTEM_OVERVIEW.md), [`WOO_WORDPRESS.md`](../WOO_WORDPRESS.md), [`PILOT_OPS.md`](../PILOT_OPS.md), [`SOUL.md`](../../SOUL.md).

**Document status:** Proposal for implementation. It does not claim that the workflows below are implemented or authorized for production.

## 1. Product objective

Extend AzomOps so an authorized operator can inspect, protect, maintain, and recover the customer's WordPress sites from a controlled operations surface. The customer's primary use is WordPress site operations. Existing support-case, Messenger, Telegram, WooCommerce, and marketing features are secondary to this scope.

This is initially a **single-customer, multi-site operations product**, not public multi-tenant SaaS. Each site must have an explicit identity, environment, access profile, backup target, maintenance policy, and audit history.

The system must distinguish these service layers:

1. **Site operations automation:** inventory, health checks, backup orchestration, update plans, approved execution, and incident evidence.
2. **Hosting and security services:** hosting, WAF, malware response, external backup storage, and 24/7 response. These are supplied by a hosting/security provider or a separately staffed service unless explicitly implemented and contracted.
3. **Content administration:** current WordPress REST client functions. Keep this separate from infrastructure maintenance permissions.

Do not market this as managed WordPress hosting until the customer-facing operational workflows, restore drills, monitoring, and support responsibilities are in place.

## 2. Current repository baseline

### Implemented

`skills/ecom_ops/integrations/wordpress.py` provides a REST client for:

- Posts: list, read, create, update, delete.
- Pages: list and read.
- Media, users, comments: list.
- Settings: read and update.
- Namespace discovery.

The existing factory resolves a base URL from shared environment configuration and accepts a `domain=` value. Credentials are currently read from shared `WP_USERNAME` and `WP_APP_PASSWORD` variables. The dashboard WordPress probe calls `list_posts(per_page=1)` as a connectivity check.

### Not implemented as an operator workflow

Repository search shows no customer-facing WordPress operations UI, CLI command, or action service for the REST client beyond the dashboard connectivity probe. The client does not update WordPress core, plugins, or themes; create or verify full-site backups; restore a site; scan files for malicious changes; or monitor public uptime.

`WP_BASE_URL` and the shared credentials do not provide independent per-site credential isolation. Do not treat `domain=` as tenant or credential isolation.

### Product constraints

- Keep human approval for production changes and restores. No default-on production updates.
- Do not add an arbitrary shell/SSH tool. Do not let the model compose shell commands.
- Keep secrets out of SQLite, logs, telemetry, UI, exports, and generated plans.
- Use mock/synthetic sites in development. No live provider/site calls or production mutations without explicit owner enablement.
- Preserve unrelated worktree changes. Check `git status -sb` before editing.
- Do not weaken existing RBAC, HITL, fail-closed behavior, or WooCommerce customer-mail controls.

## 3. Target capabilities and safety model

### 3.1 Site inventory and access

Create an explicit per-site registry with at least:

- Stable site ID, display name, canonical URL, environment (`production` or `staging`), business owner, hosting provider, WordPress version, PHP version, WooCommerce presence, critical URLs, and maintenance window.
- Capability status for hosting API, WP-CLI, staging, backup, restore, uptime check, and security scanner. Unknown means unavailable; do not assume access.
- A unique secret reference per site and environment. Revoke independently. Never return credential values to the UI.
- A service account with only the required WordPress capabilities. Keep content editing permissions separate from plugin/theme/core administration. Use HTTPS for Application Password requests; WordPress describes them as revocable per-application credentials.

Prefer hosting-provider APIs for backups, staging, and restore where available. Use a small, typed operations runner on a host only where provider APIs cannot do the job. The runner must accept structured operation IDs and validated arguments, not arbitrary commands.

### 3.2 Health and inventory (read-only first)

Provide scheduled and on-demand checks that report timestamp, site ID, check name, result, duration, and a redacted error class:

- HTTP(S) uptime for the homepage and selected critical URLs.
- TLS certificate expiry and DNS resolution, where the monitoring provider exposes these safely.
- WordPress REST/API reachability and authentication probe without returning customer content.
- WordPress, PHP, WooCommerce, theme, and plugin versions; available update counts; active/inactive state.
- WordPress Site Health findings where a supported interface is available.
- Latest successful backup, age, size, integrity verification, and restore-drill date.
- Core/plugin checksum results and vulnerability findings from an identified scanner/data source.

Run external uptime checks independently of the WordPress application process. Alert only after a configurable confirmation rule (for example, repeated failures) and deduplicate incidents. A single failed probe is not proof that a site is down.

### 3.3 Backup and restore

Back up the database and files as one identifiable set. Include WordPress core/custom files as needed, themes, plugins, uploads, and database. Exclude caches and reproducible temporary data. Store backups encrypted, access-controlled, and off-site; record metadata and checksums, not secrets. Define retention per customer and hosting limits.

Every backup operation must record site, environment, start/end, destination reference, byte count, checksum/integrity result, and completion state. Do not report success until verification passes.

Restore requirements:

- MVP restores only to a staging/recovery target and verifies the restored copy.
- A production restore requires explicit admin approval, a named incident/change, a recent verified backup, and a visible impact warning.
- For WooCommerce sites, never silently restore an old production database: it can discard orders and other writes created after the backup. Require a recovery plan for order/data reconciliation and a declared maintenance window.
- Rehearse restore on a schedule agreed with the owner; log the result and measured recovery time.

WordPress's backup guide treats database and files as a set, recommends backup frequency based on change volume, and recommends copies in separate locations. Translate this into customer-specific recovery point and recovery time objectives before promising them.

### 3.4 Updates and change control

Use a plan → approval → execution → verification state machine. An update plan shows the site/environment, component, current and target versions, risk class, backup ID, staging result, expected downtime, and rollback path.

Required sequence:

1. Inventory and check compatibility/update availability.
2. Verify a recent backup and, where possible, update a staging clone first.
3. Run pre-update health and smoke checks.
4. Present a human-readable plan. No production write occurs before explicit approval tied to that plan.
5. Execute only fixed, allowlisted operations through a typed adapter, one site at a time. Never run model-generated shell or PHP.
6. Verify homepage, critical URLs, REST API, admin availability, checkout/cart/order-read path when WooCommerce is present, and error/health signals.
7. Mark success only after checks pass. If checks fail, stop further updates, alert an operator, and offer a rollback plan.

Risk rules:

- Initial release: all production updates require human approval.
- Do not update all plugins/themes/core in one unbounded batch.
- Treat major core, WooCommerce, payment, shipping, security, caching, and custom plugins as high risk; require staging evidence and explicit approval.
- Any future auto-update policy is opt-in per site and component, limited to a reviewed class of security/minor updates, after a successful soak and owner approval. Keep it off by default.

WP-CLI is an appropriate fixed-command adapter for hosts where it is already supported. Its official commands support dry-run and scoped plugin updates and checksum verification. The implementation must still respect host ownership, permissions, and the operator's allowlist.

### 3.5 Security checks

Build layered checks, not a claim of guaranteed malware detection:

- Verify WordPress core checksums and plugin checksums where the official source provides them.
- Identify components without checksum coverage (premium, custom, or modified plugins/themes) as **unverified**, not clean.
- Integrate one selected vulnerability intelligence/scanning provider; document license, coverage, update time, and false-positive handling.
- Report risky configuration, stale versions, unexpected admin accounts, and relevant Site Health findings when available.
- Store findings and evidence references, not full sensitive files or tokens.
- Security finding response is triage and escalation. Automated deletion, plugin deactivation, file quarantine, and account removal are out of MVP.

WordPress core checksum verification is useful integrity evidence; it is not a complete malware scanner and should not be represented as one.

### 3.6 RBAC, audit, and user experience

Add WordPress-specific permissions through the existing RBAC system; do not hardcode Azom people into reusable code:

- Read-only operator: inventory, health, backup/update status, findings.
- Site operator: prepare backup/update plans and run approved non-production checks.
- Site admin: approve production updates and backup policy changes.
- Restore authority: explicit elevated permission for production restore; require a second confirmation and incident/change reference.

Every mutation has a durable audit event: actor, permission, site/environment, operation and plan IDs, approval reference, timestamps, result, and redacted error code. Do not log request authorization headers, application passwords, raw database exports, or provider response bodies.

Proposed UI (routes are new design targets, not existing routes):

- Sites overview: status, last check, versions, pending updates, latest backup, open incidents.
- Site detail: history, findings, maintenance window, backup inventory, and staged update plan.
- Approval view: exact affected components, expected impact, preconditions, and rollback route.
- Incident view: monitoring timeline, operator notes, and recovery steps.

Keep content editing actions separate from system maintenance actions so a content role cannot update code or restore databases.

## 4. Delivery phases

### Phase 0 — Customer discovery and access map

For each site, document host, access method, production/staging URL, WP/PHP/Woo versions, critical URLs, backup provider, current backup/restore behavior, security provider, uptime provider, maintenance window, and owner-approved recovery objectives. Do not request or paste secrets into the discovery document.

**Exit:** every capability is marked available, unavailable, or unknown; customer approves scope and the first pilot site.

### Phase 1 — Site registry, health, and inventory (read-only)

Implement per-site configuration, credential references, site isolation, scheduled health/inventory checks, external uptime connector, alert deduplication, audit records, mocks, and a status surface.

**Exit:** mocked tests cover multiple sites and credential separation; a staging-only live pilot demonstrates health checks and alerts without mutation.

### Phase 2 — Backup and restore assurance

Implement provider-backed or host-run backup jobs, encryption/retention metadata, integrity checks, restore to staging, and an operator runbook. Select storage and keys with the owner; do not invent a storage provider or silently send backups to a third party.

**Exit:** a staging restore drill passes for files and database; evidence and measured recovery time are recorded; production restore remains admin-only.

### Phase 3 — Security inventory and findings

Implement core/plugin checksum verification, version inventory, Site Health integration where available, and one approved vulnerability scanner adapter. Add an “unknown/uncovered” outcome for unsupported components.

**Exit:** mock findings cover clean, vulnerable, modified, unsupported, timeout, and scanner-unavailable outcomes without exposing secrets.

### Phase 4 — Staged updates

Implement update availability, plan generation, approval binding, staging execution, pre/post checks, fixed allowlisted WP-CLI/hosting adapter, and stop/rollback escalation. Production execution is manual approval only.

**Exit:** tests prove no write before approval, approval cannot be reused after plan changes, partial failures stop the batch, logs are redacted, and WooCommerce checks are included when applicable.

### Phase 5 — Controlled pilot and service readiness

Start with one non-critical staging site, then one owner-selected production site with a documented maintenance window. Observe health, backup, security findings, update plans, and operator burden over an agreed soak period. Do not auto-update or promise an SLA until evidence supports it.

**Exit:** owner signs off the runbooks, alert ownership, support hours, backup retention, recovery objectives, update policy, and production authorization.

## 5. Minimum acceptance criteria for first production release

- Each site has separate identity and secret references; cross-site access tests deny access by default.
- All remote calls use HTTPS and finite timeouts; secrets and sensitive response bodies are redacted.
- A failed/unknown backup or health check never displays as green.
- No production update or restore can run without the correct permission and approval record.
- Production restore cannot be launched from free text or by the model.
- Update plan changes invalidate prior approval.
- Jobs are idempotent or have safe duplicate detection, per-site locking, timeout, bounded retries, and clear partial-failure states.
- Backup restore-to-staging drill passes, and the record includes integrity and recovery-time evidence.
- Uptime alerts are externally checked, deduplicated, and routed to an assigned human owner.
- Tests use mocks or disposable staging sites; no test points at customer production.
- Operations docs cover install, secrets, backup, restore, updates, security findings, alerts, rollback, and incident response.

## 6. Proposed initial scope (MVP)

Include:

- Per-site inventory and read-only health/uptime status.
- Backup orchestration through one chosen provider/runner and restore-to-staging.
- Core/plugin checksum and update inventory with coverage labels.
- Staging-first update plan with explicit approval and post-update smoke checks.
- Audit log, RBAC, alert routing, mocks, runbooks, and dashboard summaries.

Defer:

- Autonomous production updates or restores.
- Malware cleanup, file quarantine, account deletion, or arbitrary shell access.
- 24/7 incident response or a contractual availability SLA before monitoring and staffing are proven.
- New multi-tenant SaaS control plane. This project remains for one customer and its explicitly registered sites.
- Automated hosting migrations, DNS changes, payments, refunds, and Woo order mutation as part of WordPress maintenance.

## 7. Handoff instructions for Grok Build

Read the repository instructions and inspect the current branch/worktree before editing. Preserve all existing unrelated modifications. Begin with a short implementation proposal that maps each phase to files, data models, permissions, and tests. Implement one phase at a time, starting with Phase 1 and mock infrastructure. Do not deploy, run production checks, contact customer sites, use real credentials, turn on automatic updates, or change production settings. Ask the project owner for a scoped approval before any live operation.

Use only official WordPress/WP-CLI capabilities behind explicit typed adapters. Never expose a general-purpose shell or PHP execution facility to the model. Keep external provider selection behind configuration/interfaces rather than hardcoding vendor-specific behavior into action functions.

Primary references:

- [WordPress backups](https://developer.wordpress.org/advanced-administration/security/backup/)
- [WordPress hardening](https://developer.wordpress.org/advanced-administration/security/hardening/)
- [WordPress Application Passwords](https://developer.wordpress.org/advanced-administration/security/application-passwords/)
- [WordPress Site Health](https://wordpress.org/documentation/site-health/)
- [WP-CLI plugin updates](https://developer.wordpress.org/cli/commands/plugin/update/)
- [WP-CLI core checksum verification](https://developer.wordpress.org/cli/commands/core/verify-checksums/)
