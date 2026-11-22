# Grok Build Prompt — Bounded WordPress Operations Agent

Use the prompt below with the Grok Build plan phase. The worktree currently contains unrelated, uncommitted changes. Preserve them.

> **Current-state note:** The repository now has named, server-authorized WordPress read tools, a bounded 3-call / 30-second tool loop, actor/channel/conversation/site context with a 12-hour TTL, and immutable update/restore previews. Those plans have no approval or execution path. Live health checks, scanning, version discovery, update execution, and production restore are unavailable. This prompt describes future implementation requirements, not current bot capability. Do not claim these features are live or add an execution adapter unless a separate owner request explicitly authorizes that work.

---

## Goal

Implement a bounded, site-aware WordPress operations agent in Azom that feels more capable in natural language while keeping every operation typed, permission-checked, audited, and fail-closed. The user must be able to ask about a registered site's status, backup state, security findings, and available updates; the agent may make a small number of follow-up reads; and it may prepare an immutable change plan. Production changes and any restore must require explicit human approval tied to that exact plan.

Do not turn this into a general-purpose OpenClaw shell agent or public multi-tenant SaaS.

## Context dump

- **Goal:** Add a bounded tool loop and persistent per-site operations context to the Azom WordPress operations surface.
- **Workdir:** `E:\git\customer-agent-template-azom`
- **Zone:** yellow — customer-facing operations and a gated live SSH file path exist in the worktree.
- **Requester:** Project owner.
- **Prior decisions:** Production updates/restores remain human-approved. No model-generated shell/PHP. Unknown capability is not success. Never expose credentials or raw provider errors.
- **Relevant files:**
  - `docs/CURRENT_STATE.md`
  - `docs/WORDPRESS_OPS.md`
  - `docs/WOO_WORDPRESS.md`
  - `skills/ecom_ops/bot/chat_agent.py`
  - `skills/ecom_ops/bot/handlers.py`
  - `skills/ecom_ops/bot/catalog.py`
  - `skills/ecom_ops/bot/dialog_actions.py`
  - `skills/ecom_ops/wp_ops/`
  - `skills/ecom_ops/rbac.py`
  - `infrastructure/dashboard/wp_routes.py`
  - `skills/ecom_ops/integrations/wp_files_ssh.py`
  - `config/wp_sites.yaml`
  - Existing tests named `test_wp_*`, `test_chat_agent.py`, `test_handlers_rbac_upfront.py`, and `test_wp_dashboard.py`.
- **Constraints:** Preserve all pre-existing worktree changes. Do not read `.env`, credentials, private keys, OAuth state, or auth files. Do not contact customer sites or providers, enable `AZOM_WP_FILES_SSH`, run live checks, update customer code, restore production, deploy, commit, or push. Use mocks/synthetic sites only.
- **Definition of done:** The assistant can safely answer multi-step WordPress ops questions using registered read tools; site context and open operation plans do not cross site or actor boundaries; writes are impossible through model tool calls; approved actions are revalidated server-side; missing adapters stay explicitly unsupported; tests and living docs describe the actual implementation.

## Current implementation facts to verify before editing

1. `docs/WORDPRESS_OPS.md` reports Phase 1 mock checks and Phase 2 local mock backup manifests/drills. It says checks do not call customer sites, no off-site backup exists, production restore is unavailable, and update/checksum/scanner phases are not implemented.
2. `skills/ecom_ops/wp_ops/service.py` uses mock connectors by default. Its `restore()` currently refuses. A mock backup manifest is metadata, not site files or a database copy.
3. `skills/ecom_ops/bot/chat_agent.py` now has a named WordPress tool loop. Verify the exact current schemas, server-side authorization, result caps, timeout, and prompt registry behavior before changing it.
4. The content catalog can read pages and propose page edits, but it is distinct from the WordPress site-operations surface.
5. Live typed file editing can be enabled by `AZOM_WP_FILES_SSH=1` when mock mode is off. Check `skills/ecom_ops/integrations/wp_files_ssh.py` for the current explicit non-root user and pinned-host-key guards; preserve them and do not restore a `root` fallback.
6. `config/wp_sites.yaml` contains synthetic `example.test` sites. Do not replace or contact them.

If the code differs from these notes, treat code/config as current truth, record the discrepancy, and keep the same safety boundaries.

## Required behavior

### A. Named, typed WordPress tools

Create a provider-neutral, server-side allowlist with explicit JSON schemas and strict argument validation. At minimum support:

- `wp_list_sites()` — only sites visible to the authenticated actor.
- `wp_site_status(site_id)` — health and inventory from the existing operations service.
- `wp_backup_status(site_id)` — latest verified backup metadata and its real status.
- `wp_security_findings(site_id)` — findings and coverage state. If no scanner/checksum adapter is configured, return `unknown` / `not_configured`, never an empty “clean” result.
- `wp_prepare_update_plan(site_id, component_filter?)` — read-only discovery and a preview. Do not execute updates. If update discovery is not available for that site, return an explicit unsupported/unknown state rather than inventing versions.
- `wp_list_open_operations(site_id)` — only this site's active incidents and pending plans, after authorization.

Use stable site IDs from `config/wp_sites.yaml`; never accept a URL, document root, SSH host, environment variable name, or credential as a model-supplied tool argument. Resolve all such values from the trusted registry after checking the actor's site scope. Reuse `WpOpsService`/typed services instead of duplicating policy inside the model layer.

Tool result shapes must be bounded, redacted, and honest about `ok`, `unknown`, `unsupported`, and failure. Do not return page content, customer records, secret refs' values, auth headers, raw provider bodies, or file contents unless a distinct existing permissioned content action explicitly requires them.

### B. Bounded agent loop

Implement a provider-neutral loop through the existing model-router facade; do not add direct provider calls in `chat_agent.py`.

- Allow at most **3 tool calls per user turn**, **30 seconds total tool time**, and a bounded result size (for example, 4 KB per result and 10 KB total). Centralize the limits in named constants/config with safe caps.
- Only the named read/plan tools above can be called by the model. `wp_prepare_update_plan` is read-only and creates no executable approval record by itself.
- Validate every tool name, schema, site ID, permission, and call count at the execution boundary. Unknown tool, malformed arguments, unauthorized site, timeout, and budget exhaustion fail closed.
- Return each tool result to the model for a bounded follow-up decision, then ask for the final answer. Do not recursively call indefinitely. If a step fails, report the failure and preserve useful earlier results without claiming success.
- Never show raw tool protocol, XML, JSON envelope, prompt, or internal errors to users. Do not hide a failed tool behind a success from another result.
- Keep existing model routes working. Add mock tests for supported and unsupported provider capabilities; do not weaken route/auth behavior to make tools work.

### C. Previewed, immutable change plans and human approval

Create a typed plan model for future updates and restores. A plan must include:

- unguessable `plan_id`, `site_id`, environment, operation type, target components, current/target versions when verified, backup reference/status, preconditions, staging evidence, expected impact, rollback/recovery note, created/expiry times, creating actor, and a digest of the exact plan payload;
- status such as `pending_approval`, `approved`, `executing`, `completed`, `failed`, `expired`, or `invalidated`;
- audit references without secrets or customer content.

Plan creation is preview-only. No model tool may execute a plan, perform a production update, or restore data.

For any production update or restore:

1. Show the exact immutable plan and impact to the user.
2. Require an explicit UI/button callback bound to `plan_id`, actor, site, and plan digest. A free-text “yes/ok”, model inference, or a different plan must not approve it.
3. Re-check current RBAC, environment, site scope, plan expiry, payload digest, capabilities, backup health, and preconditions at approval and again at execution.
4. Any change to the site, target, backup, component/version, or plan invalidates prior approval.
5. Production restore requires elevated `WP_OPS_RESTORE` plus a second explicit confirmation and a change/incident reference. If the actual restore adapter is absent, refuse with `RestoreNotInMvp`; do not simulate a restore or mark it complete.
6. Production updates remain unavailable until a concrete, reviewed adapter exists. An approved preview alone is not permission to implement arbitrary SSH/WP-CLI execution.

Keep the existing product/content edit confirmation path separate from infrastructure operations. Do not silently change product/page writes or case-mail behavior as part of this task.

### D. Site-isolated continuity and open operations

Persist only minimal operational continuity under `AZOM_DATA_DIR`, using the repository's existing safe storage/locking conventions. Scope every row by at least `actor_id`, authenticated `conversation_id`/channel, and `site_id`; include an environment ID where applicable.

- Keep sticky site context, the last bounded status result, and IDs of active incidents/plans only.
- On every read or resume, re-authorize actor-to-site access server-side. Never trust a site ID restored from model history alone.
- A switch of site or actor clears incompatible sticky state. Cross-site ID guessing must return the same safe unknown/denied behavior without revealing site existence.
- Do not persist credentials, secret values, customer content, full tool transcripts, raw provider responses, or free-form sensitive prompt text.
- Add a retention/clear path for stale or completed context and a user-visible way to switch/clear site context. Expired plans are unusable even if still stored.
- Keep this single-customer and registered-sites only. Do not introduce cross-customer tenancy assumptions.

### E. Permissions and transport hardening

- Preserve existing RBAC and fail-closed actor mapping. Read and plan permissions are distinct from change/restore permissions.
- Enforce authorization inside each service/tool handler, not only in the prompt, dashboard, CLI, or Telegram UI.
- Remove the `root` SSH default. A live SSH operation requires an explicitly configured non-root account, pinned host key, exact registered site, and existing opt-in. Add tests proving missing/invalid user refuses before a network socket or subprocess is opened.
- Keep `AZOM_WP_FILES_SSH` off in tests and default configuration. Do not expand the file editor into a command runner.
- Audit actor, site ID, tool/plan/action IDs, result class, and approval reference; redact error strings and secrets.

## Implementation approach

1. Inspect current diff/status and instructions. Map the existing model-router and callback contracts before editing; preserve unrelated changes.
2. Add typed models and per-site read services with mock adapters first.
3. Add the bounded tool loop and attach sanitized tool results for follow-up reasoning.
4. Add immutable plans and approval validation, without enabling live production adapters.
5. Add site/actor-scoped continuity and expiry/clear behavior.
6. Harden SSH identity default and add regression coverage.
7. Update `docs/WORDPRESS_OPS.md`, `docs/CURRENT_STATE.md`, `docs/SYSTEM_OVERVIEW.md`, `docs/CLI_REFERENCE.md`, and operator runbooks only where behavior actually changed. Label live capabilities as unavailable until a live adapter is implemented and explicitly enabled.

## Required tests and acceptance cases

Add or update mock/unit tests; run only relevant tests during the build, then the repository's documented quality checks if time permits. Never target a customer site.

Tests must prove:

- authorized read calls work for a registered synthetic site and always report true result provenance;
- unavailable backup/scanner/update providers produce `unknown`/`unsupported`, never green/clean/success;
- no more than 3 tool calls execute and total tool time/result bytes are bounded;
- malformed/unknown tool, bad site ID, cross-site access, expired plan, modified plan, changed backup/version, and unauthorized actor fail closed;
- model-generated tool calls cannot perform writes, shell, PHP, or restore;
- production update/restore has no side effect before an explicit valid callback and server-side revalidation;
- restore stays refused if no real adapter exists;
- state for actor A/site A cannot be read, resumed, or approved by actor B/site B;
- state expiry/clear works and persisted context contains no credentials or raw customer content;
- missing SSH user refuses; no fallback to root; fake runner confirms no subprocess/network for refused inputs;
- success, partial failure, timeout, and provider-unavailable replies are accurately described;
- existing content change confirmation, mailbox approval, and unrelated model routes regressions remain covered.

## Out of scope

- Contacting customer WordPress sites, production backups, live updates, restore, plugin/theme installation, or security scans.
- Enabling automation flags, adding customer credentials/hosts, changing production scheduler/SSH settings, or deployment.
- Arbitrary command execution, model-written PHP, automated production mutations, or default-on auto-update.
- Implementing a malware-removal service, 24/7 SLA, public multi-tenant SaaS, or storage/scanner vendor without owner selection.
- Commit, push, merge, or modifying unrelated worktree changes.

At completion, report changed files, tool names and limits, authorization design, tests run and results, known unsupported providers, and any remaining owner decision. Do not claim managed hosting or live WordPress operations unless verified with an owner-approved staging pilot.

---
