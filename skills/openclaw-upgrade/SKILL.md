---
name: openclaw-upgrade
description: >
  Upgrade the local OpenClaw npm client. Backup, stop the local node,
  install a pinned version, run doctor --lint, and report blockers.
  Does not deploy Azom or send mail. Keeps a local gateway up when gateway.mode is local.
version: "1.0.0"
---

# openclaw-upgrade

**Purpose:** Upgrade the OpenClaw client that is installed on this machine.  
**Audience:** The operator who owns the Windows client, and coding agents they ask.  
**Read this first:** `docs/runbooks/openclaw-upgrade.md`, `docs/CURRENT_STATE.md`.

## Stop

- Do not send customer mail or change orders.
- Do not push, merge, or deploy Azom.
- Do not run `openclaw doctor --fix` unless the operator agreed to the exact change list.
- Do not enable the scheduled task `OpenClaw Gateway` while `gateway.mode` is `remote`. When the mode is `local`, leave that task enabled and listening on `127.0.0.1:18789`.
- Do not read, copy, or rotate Grok Build `auth.json`.
- Do not print tokens or API keys.
- If the upgrade fails, do not unpack the backup over the live state. Write the error and the rollback command.

## Steps

1. Record `openclaw --version` and `Get-Command openclaw`.
2. Confirm install method. On this machine it is npm global.
3. Create `%USERPROFILE%\openclaw-backup-<timestamp>` with state tar, workspace tar, config tar, and sqlite copies including `-wal` and `-shm`.
4. Save `doctor --lint` and `gateway status` into that directory.
5. Stop the local `node run` process started from `.openclaw\node.cmd`, and stop the local gateway if it is running, so npm can replace files.
6. Run `npm install -g openclaw@2026.11.6`.
7. Run `openclaw --version`, `openclaw doctor --lint`, `openclaw gateway status`, and `openclaw health`.
8. If `gateway.mode` is `local`, start the gateway again and point `node.cmd` at `--host 127.0.0.1`. The env var alone does not change the node target.
9. Write `docs/openclaw-upgrade-report.md` with versions, backup path, and blockers. No secrets.

## Verify

- Version string contains `2026.11.6`.
- `SOUL.md` and `AGENTS.md` still exist in `C:\Users\Django\clawd`.
- If `gateway.mode` is `local`, the gateway task is running and `127.0.0.1:18789` answers. If the mode is still `remote`, the local task stays disabled.
- Channels and OAuth that cannot be reached are `BLOCKED` with the exact reason.

## Rollback

`npm install -g openclaw@2026.6.10`  
Expect the old version string. Leave state in place if config was not rewritten.
