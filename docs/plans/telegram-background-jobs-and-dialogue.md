# Telegram Background Jobs and Continued Dialogue

**Purpose:** Plan durable background execution for Azom Telegram turns, tools, and already-approved actions while the user can continue messaging the agent.

**Audience:** Project owner, developers, coding agents, and Grok Build.

**Read this first:** [`CURRENT_STATE.md`](../CURRENT_STATE.md), [`DEVELOPER_GUIDE.md`](../DEVELOPER_GUIDE.md), [`TELEGRAM_OPENCLAW.md`](../TELEGRAM_OPENCLAW.md), [`CASES.md`](../CASES.md), [`WORDPRESS_OPS.md`](../WORDPRESS_OPS.md).

**Status:** Implemented in the Telegram runtime. Deployment and operator acceptance remain outstanding. Static syntax/lint checks passed; no tests or live Telegram calls were run for this change.

## Goal

Let the Telegram receiver stay responsive while model turns, named read tools, and explicitly approved actions run in the background. A user must be able to see what is running, continue the conversation, and request cancellation where it is safe. Work must survive a process restart without losing updates or blindly repeating external side effects.

Keep the current human-approval paths, RBAC checks, tool limits, site isolation, and Messenger behavior. This plan does not add WordPress update or restore execution; those capabilities remain preview-only or unsupported.

## Implementation delivered

- `skills/ecom_ops/bot/job_store.py` stores the Telegram inbox, jobs, steer mailbox, safe progress events, conversation state, poller lease, and delivery state in `AZOM_DATA_DIR/telegram_jobs.sqlite3`.
- `skills/ecom_ops/bot/__main__.py` acknowledges durable updates before moving the poll cursor, handles `/status`, `/jobs`, `/stop`, `/cancel`, and `/reconcile` out of band, and runs a bounded worker pool. Default concurrency is two, capped at four.
- Jobs are serialized per Telegram chat. Read turns accept up to three steer messages and apply them at model/tool checkpoints. Approved actions queue later chat input and use a process-wide action lock, with a per-site lock for WordPress content changes.
- `skills/ecom_ops/bot/store.py` migrates the existing Telegram JSON conversation file once into SQLite. Messenger continues using its current synchronous JSON store.
- Read jobs can be replayed after restart. Approved actions that may have started are never replayed; they require Oscar's explicit `/reconcile <job-id> done|not_applied` after an external check. Generic confirmation buttons carry a hash of their pending action/session snapshot; stale buttons are rejected, and repeat clicks on the same Telegram button are deduplicated. Case `sending` claims remain closed while a related job is running or needs reconciliation.
- `bin/backup-azom-data.sh` creates an online SQLite backup of the job database and excludes Node/Python libraries and the copied runtime databases from the state tarball.

WordPress tool limits, server-side RBAC, site binding, approval buttons, null-send, and update/restore preview-only behavior are unchanged. This is implemented but not live-accepted: validate the restart, duplicate-update, steering, action-reconciliation, and outbox flows in mock/synthetic conditions before deployment. Live Telegram must be separately enabled by the operator.

## Datalasse reference

The local Datalasse review compares two Telegram runtimes, so use the right behavior from each:

- **Hermes Telegram** receives out-of-band input during an active turn, supports `interrupt`, `queue`, and `steer` busy-input modes, edits a long-running working status, and records delivery state. The reviewed host was configured for `interrupt`, which cancels active work. The review recommends `steer` when an incoming message should guide a long job without tearing it down.
- **OpenClaw/Moss Telegram** sequences work by chat/thread and documents `streaming.mode: progress`, which keeps a status draft with tool lines and sends the final reply separately. OpenClaw also queues follow-up input, but the review distinguishes this from Hermes inserting an out-of-band steer into the active turn.
- Datalasse's conversation policy says under three seconds should produce only the final reply; for longer work, show concise progress without noisy tool chatter. Delivery recovery is at-least-once, with a recovered reply after a restart rather than an exactly-once guarantee.

The review explicitly says it is not a live capture of the OpenClaw VPS. Treat it as an architecture and UX reference, then verify the deployed behavior during acceptance.

Reference files in the adjacent Datalasse checkout: `E:\git\datalasse\docs\reviews\2026-08-28-hermes-openclaw-telegram-dialog.md` (especially the Hermes/OpenClaw Telegram pipelines and section B1) and `E:\git\datalasse\docs\CONVERSATION-FLOW.md` (section 8).

**Azom recommendation:** Use Hermes-style steer semantics for read-only conversational jobs, OpenClaw-style per-conversation sequencing and concise progress, and a stricter queue for approved actions. Do not adopt `interrupt` as the default.

## Previous Azom baseline

- `skills/ecom_ops/bot/__main__.py` polls Telegram and calls `_handle_update()` inline. `_handle_update()` waits for `BotHandler.handle()` to finish before the poll loop receives the next update. The current checkout has editable status/answer drafts, but they are callbacks on that same synchronous call and do not make update intake asynchronous.
- `skills/ecom_ops/bot/handlers.py` runs chat work, callback actions, and slash-command work synchronously. `chat_agent.run_chat()` performs prefetch, model calls, and the bounded WordPress loop in sequence.
- `skills/ecom_ops/bot/store.py` rewrites one JSON document for conversation state without a thread lock, row-level transactions, version checks, or update-id deduplication. Multiple background workers cannot safely write this file concurrently.
- WordPress model tools are named and server-authorized. The tool loop is bounded to three calls, 30 seconds, 4 KiB per result, and 10 KiB total. Those limits remain in force inside a background job.
- Mutating callbacks are explicit. For example, case mail send claims a case before sending. The existing stale-claim sweeper can reopen `sending` cases after ten minutes; this must be coordinated with durable job leases before approved actions run in the background.
- Messenger has a separate synchronous webhook and state file. Keep it outside the first Telegram async rollout.

## User-visible behavior

1. A Telegram update is durably accepted and acknowledged quickly. A fast job under three seconds returns only its final reply.
2. A longer job has a stable job id, one editable status message, tool-status events, and one editable answer draft. The final `BotReply` replaces the draft and keeps its current buttons.
3. Extend the existing `/status` with the active job and phase for this conversation. `/jobs` lists active, queued, and reconciliation-required jobs for this conversation. Extend the existing `/stop` / `/cancel` flow controls to accept an optional job id and request cooperative cancellation when the job is still at a safe checkpoint.
4. While a read-only chat job is active, ordinary new text is accepted as a **steer message** and attached to that job's mailbox. The worker consumes it at the next checkpoint and tells the user it has been added. It does not start a second turn against the same history.
5. While an approved action is active, new ordinary text is queued as the next turn. It cannot alter the approved payload. `/status` and `/cancel` remain out-of-band.
6. Tool progress contains only a safe tool label and state. Never include arguments, credentials, customer payloads, raw provider errors, or private model reasoning.

If a model request is already in flight, a steer cannot change that HTTP request in place. The worker applies the steer after the request returns and before its next tool or final-answer checkpoint. Do not claim instantaneous steering while the model or an external provider call is blocked.


## Implemented architecture

### 1. Durable Telegram inbox and job store

Add a SQLite store under `AZOM_DATA_DIR` using WAL, `busy_timeout`, and short transactions. Avoid Redis, Celery, or another external service for this single-host bot.

Tables:

- `telegram_inbox`: unique Telegram `update_id`, received time, safe processing state, and the minimum payload needed to create a job. Persist inbox insertion before advancing the long-poll offset.
- `bot_jobs`: job id, unique source update/callback id, channel, chat/thread identity, resolved actor, sequence, job kind, minimal payload, state, lease owner/expiry, cancellation flag, timestamps, and safe final result reference.
- `bot_job_events`: ordered status/progress/tool events. Store labels and state only; do not store tool arguments or raw tool results here.
- `bot_jobs.result_json`, `delivery_state`, and persisted Telegram message ids provide the outbox. Delivery retries edit the existing draft where possible; at-least-once delivery can still produce duplicates.
- `telegram_conversations`: versioned state rows keyed by channel + chat id + optional Telegram thread id, or an equivalent transactional conversation store.

Use atomic job claim/lease transactions and unique constraints to deduplicate update ids and callback-query ids. Limit persisted raw input and result retention; keep it under the protected `AZOM_DATA_DIR` with restrictive file permissions. Never persist credentials or provider responses.

Migrate `telegram_state.json` once into the Telegram conversation table, preserving its 24-hour idle TTL, history cap, pending action, sticky ids, and actor-bound WordPress context. Keep the existing Messenger state file and behavior unchanged.

### 2. Responsive receiver and bounded workers

- Keep exactly one Telegram long-poll receiver per bot token. Add a process/database singleton lease so a second `azom-bot.service` instance cannot poll the same token.
- The receiver only validates/records updates, handles small control commands, acknowledges callback queries, and enqueues work. It must never call `run_chat()` or execute an approved action inline.
- Start a small bounded worker pool in the existing `azom-bot.service` initially, with a default concurrency of two on the supported 4 GB host. Configure the cap explicitly and monitor queue age, worker failures, and model spend before increasing it.
- Enforce one active worker per conversation key. Different conversations may run concurrently up to the global cap. Use a site-level lock for mutating site actions so separate chats cannot race on one site.
- Workers can call the existing synchronous handler/model/tool code. Preserve the current model-router facade and provider streaming callbacks; thread-based execution is sufficient for these blocking I/O tasks and does not require converting the entire bot to `asyncio`.

### 3. Job classes and busy-input policy

Classify work before enqueueing:

| Job kind | Examples | Incoming text while active |
|---|---|---|
| `chat_read` | Natural-language answer, named WordPress reads, bounded follow-up reads | Add to steer mailbox; worker consumes at checkpoints |
| `approved_action` | Callback-approved case reply, order status, typed product/site edit | Queue as next turn; never modify the approved action |
| `command_read` | `/cases list`, order lookup, `/wp sites` | Serialize per conversation; simple `/status` and job controls stay out-of-band |
| `system_control` | `/status`, `/jobs`, `/cancel` | Run in receiver from the job store without waiting for the active worker |

The worker checks steer/cancel messages before starting another tool, after each tool result, and before final answer generation. A steer does not reset the existing WordPress call/time/result budgets. If the job has no safe checkpoint before its deadline, finish or cancel at the next safe point and schedule the steer as a normal follow-up.

Cancellation is cooperative. It can stop read-only work between calls. It cannot undo a side effect that has begun. For actions already in an irreversible section, report that cancellation arrived too late and continue to reconcile the actual outcome.

### 4. Approved actions in the background

1. A user must still click the existing approval button; free text such as “yes” never creates an `approved_action` job.
2. At enqueue time, validate the callback id, pending-action id/digest, actor, permission, and expiry, then consume the approval exactly once in a transaction.
3. At worker execution time, re-resolve the actor and re-check the permission, resource/site scope, action digest, and current resource state. Do not trust the prompt, callback text, or queued actor fields by themselves.
4. Add an idempotency key and persist an `action_started` checkpoint before calling the external side effect. Persist the structured outcome and final user message afterward.
5. If the process stops after an external side effect may have happened but before its result is recorded, use `needs_reconciliation`. Do not blindly repeat email sends or site writes. Coordinate the case `sending` claim with worker leases so `release_stale_send_claims()` cannot reopen a case while its action job is alive or when delivery is uncertain.
6. Keep the existing null-send, kill switches, typed write paths, and HITL boundaries. WordPress update and restore plans remain non-executable; no new model-callable mutation tool is added.

### 5. Recovery and delivery

On startup, reclaim expired leases for safe read-only jobs and resume from a recorded checkpoint or restart them with a visible retry state. An expired lease for an action job becomes `needs_reconciliation` unless the action layer proves the side effect did not start.

Keep the active status-message id and answer-draft message id in the outbox. Resume by editing those messages where possible. Telegram `sendMessage` has no transactional idempotency key, so delivery is at-least-once: persist receipts and surface a recovered reply if send completion is uncertain. Never equate exactly-once delivery with exactly-once external execution.

Bound queue depth and steer-mailbox size. On overload, reject a new job with a clear response rather than accepting work that cannot be serviced. Expire completed jobs and stored inputs on a documented retention window; keep audit entries limited to actor, job/action id, tool name, result class, timestamps, and approval reference.

## Original implementation phases and delivery status

Core implementation for phases 0–3 is in the working tree; the synthetic acceptance scenarios have not been run. Phase 4 remains outstanding: rollout gating, deployment, queue health visibility, and live operator acceptance. The phase bullets below preserve the original plan for traceability.

### Phase 0 - Contracts and migration (delivered)

- Define conversation identity, job types/states, steer semantics, action idempotency, outbox recovery, retention, and overload response.
- Implement SQLite schema, transactional store, singleton poll lease, and one-time migration from `telegram_state.json`.
- Synthetic acceptance of the durable store and migration remains a rollout prerequisite; it has not been run for this change.

### Phase 1 - Background read-only chat and tools (delivered)

- Split update intake from job execution. Enqueue ordinary Telegram messages, run `chat_read` jobs in the bounded worker pool, and preserve current status/answer streaming in the worker.
- Add per-conversation single-flight, queue acknowledgement, `/status`, and `/jobs`.
- Carry existing read-tool status events into `bot_job_events`; enforce all current named-tool, site authorization, and loop limits in the worker.
- Keep Messenger on the synchronous webhook path.

### Phase 2 - Steer, cancellation, and restart recovery (delivered)

- Add the per-job steer mailbox and safe checkpoints in `chat_agent.py` and the bounded WordPress tool loop.
- Add cooperative cancellation for read-only model/tool work.
- Add restart behavior for queued/running read jobs, status restoration, outbox editing, and queue-overload handling.

### Phase 3 - Approved action jobs (delivered)

- Convert callback-approved Telegram actions into durable `approved_action` jobs.
- Add callback deduplication, immutable action payload digest, actor/RBAC rechecks, site locks, and reconciliation states.
- Review case-send claims and stale-claim recovery before enabling background email sends. Preserve existing button-only approval semantics.
- Do not add WordPress update/restore execution.

### Phase 4 - Rollout and operations (outstanding)

- Gate the new Telegram worker path with `AZOM_TELEGRAM_ASYNC`; keep it off until mock soak and owner review pass.
- Add health/status fields for queued/running/failed/stuck jobs and oldest queue age. Do not expose payloads or credentials.
- Update the systemd unit only after shutdown/recovery behavior is verified. Stop intake, drain workers to a bounded deadline, and persist resumable state on graceful shutdown.
- Enable on one allowlisted operator chat first, then expand only after duplicate, restart, queue, and callback scenarios pass.

## Tests and acceptance criteria

Add mock/synthetic tests for:

- a slow model/tool job does not prevent the receiver from accepting `/status` or another message;
- one active job per conversation and concurrent work across two different conversations without lost state;
- steer is consumed only at a safe checkpoint; an approved action is never changed by a later message;
- duplicate Telegram updates or callback queries create one job and do not duplicate an action;
- worker restart resumes a read job, while an uncertain action becomes `needs_reconciliation` and is not retried;
- case mail approval is still button-only, permission-checked, null-send-aware, and protected from stale-claim double-send;
- WP tool budgets, named schemas, server-side RBAC, selected-site binding, and existing preview-only update/restore behavior remain enforced;
- retention removes completed job payloads without deleting current conversation context prematurely;
- Messenger webhook behavior stays synchronous and unchanged.

Done means the receiver remains responsive during background work, users can see/control jobs without cross-conversation data leakage, safe read jobs recover after restart, uncertain writes never auto-repeat, and existing HITL/RBAC/tool limits still hold. Run the full tests and mock soak during implementation; this plan itself does not run or change tests.

## Implementation files

- `skills/ecom_ops/bot/job_store.py` - durable inbox, leases, job events, steer mailbox, outbox, and controls.
- `skills/ecom_ops/bot/__main__.py` - responsive long-poll intake, worker pool, status streaming, and delivery retries.
- `skills/ecom_ops/bot/store.py`, `handlers.py`, and `chat_agent.py` - transactional conversation state, queued action handling, and safe steer checkpoints.
- `skills/ecom_ops/wp_ops/agent_tools.py` - tool progress checkpoints and bounded named-tool execution.
- `skills/ecom_ops/cases/store.py` - stale `sending` claim coordination with uncertain Telegram action jobs.
- `bin/backup-azom-data.sh`, `.env.example`, and the Telegram/system docs - backup, worker configuration, and operating guidance.
