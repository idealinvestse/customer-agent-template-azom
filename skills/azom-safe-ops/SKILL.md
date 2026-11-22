---
name: azom-safe-ops
description: >
  Read-only Azom ops by default. Writes, mail, and order changes need
  an explicit approve path. Null-send when the mode is uncertain.
  Never wire FU9 auto-send from this skill.
version: "1.0.0"
---

# azom-safe-ops

**Purpose:** Safe default for order, case, and mail work in this repo.  
**Audience:** Coding agents and the OpenClaw hybrid bot.  
**Read this first:** `SOUL.md`, `docs/CASES.md`, `docs/CURRENT_STATE.md`.

## Stop

- Do not send customer mail from free text, from this skill, or from a poll run.
- Do not change order status without a verified order and an allowed actor.
- Do not enable `auto_send_enabled` or wire FU9.
- Do not enable NO/DK mailboxes.
- Do not mark live soak complete.
- Do not print secrets.
- Escalate critical, code edit, secrets, and unsafe SSH to Oscar.

## Defaults

- Local commands use `python -m ecom_ops --mock`.
- If live versus mock is unclear, add `--null-send` and do not send.
- Reads: `cases list`, `cases show`, `mail fetch` in mock, `status`, `soak-preflight`.
- Writes that reach a customer need `/cases approve`, the approve button, dashboard approve, or `cases reply` from an allowed actor.

## Steps

1. Read the case or order with tools. If the tool is empty, say so.
2. Draft in the product UI or `cases draft`. Drafting does not send.
3. Show the next human action. Do not perform it yourself when it sends mail or mutates an order.
4. Escalate abuse, legal, refund disputes, and secret failures to Oscar.

## Verify

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock cases list --status open,escalated
# expect: JSON ok, list may be empty, nothing sent
```

A verification command that would send mail or change a live order is a failed use of this skill.
