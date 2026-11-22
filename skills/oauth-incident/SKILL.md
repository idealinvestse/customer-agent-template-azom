---
name: oauth-incident
description: >
  Handle invalid_grant or needs_reauth on Azom model OAuth. Stop refresh
  loops, ask for reconnect, and keep tokens out of logs and notes.
version: "1.0.0"
---

# oauth-incident

**Purpose:** Respond when ChatGPT OAuth or another model credential is no longer valid.  
**Audience:** Oscar. Agents may diagnose and must not rotate foreign credentials.  
**Read this first:** `docs/runbooks/model-oauth.md`, `docs/runbooks/secret-leak-response.md`.

## Stop

- Do not keep refreshing after `invalid_grant` or `needs_reauth`.
- Do not read, copy, or rotate Grok Build `auth.json`.
- Do not reuse Gmail, Google Marketing, or Codex OAuth clients for ChatGPT.
- Do not paste tokens, authorization codes, or provider bodies into the incident note.
- Do not mark soak or FU9 complete.

## Steps

1. Identify the route: `chatgpt_oauth`, `xai_grok_build`, `xai_api_key`, or `openrouter`.
2. If the error is `invalid_grant` or the status says `needs_reauth`, treat automatic refresh as already stopped.
3. Ask Oscar to reconnect that route. ChatGPT reconnect is **Återanslut** on `/oscar`. Grok Build reconnect is `grok login --device-auth` by the Grok Build owner.
4. Search logs for `error_code` and `route_id` only.
5. After reconnect, Oscar runs **Testa modell** on that row with no fallback.
6. Write an incident note: date, route, error class, owner, and that the secret value was not stored.

## Verify

- Status shows `needs_reauth` no after a successful reconnect.
- The test result is `ok` or a named error class.
- No new copy of the token exists in the note, telemetry sample, or this skill.
