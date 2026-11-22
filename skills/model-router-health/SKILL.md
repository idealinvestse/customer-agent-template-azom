---
name: model-router-health
description: >
  Check Azom model-router health without hiding a failed route behind
  OpenRouter. Live provider calls need an explicit yes. Mock tests do not.
version: "1.0.0"
---

# model-router-health

**Purpose:** See which route would be used, and whether a failed route is reported as failed.  
**Audience:** Oscar for live tests. Coding agents for mock tests.  
**Read this first:** `docs/MODEL_ROUTER.md`, `docs/runbooks/model-oauth.md`, `SOUL.md`.

## Stop

- Do not call ChatGPT, xAI, Grok Build, or OpenRouter with a live credential unless Oscar said yes in this task.
- Do not fall through to another provider and then report success.
- Do not log tokens, authorization codes, or raw provider bodies.
- Do not read Grok Build `auth.json`.
- Do not reuse Gmail, Google Marketing, or Codex OAuth clients.

## Steps

1. Read the route order in `docs/MODEL_ROUTER.md`. Default is `chatgpt_oauth`, `xai_grok_build`, `xai_api_key`, `openrouter`.
2. Without a live yes, run the mock unit tests for `ecom_ops.model_router` only.
3. With a live yes, Oscar uses `/oscar` and **Testa modell** on one row at a time. That button must not fall back.
4. Classify a failure as `auth`, `config`, `rate limit`, or `unavailable`.
5. Check telemetry for `model_route_selected` or `model_route_error` with `route_id`, model, and usage fields. Confirm the line has no token.

## Routes

| Route | What a live test proves | If blocked |
|-------|-------------------------|------------|
| `chatgpt_oauth` | Connected session, refresh owner `azom` | Say `needs_reauth` or not configured. Do not refresh in a loop. |
| `xai_grok_build` | Delegated session owned by Grok Build | Do not open `auth.json`. |
| `xai_api_key` | Key works when Oscar has set it | 401 is auth. 429 is rate limit. |
| `openrouter` | Fallback only | A green OpenRouter result does not mean ChatGPT works. |

## Verify

- Mock tests pass, or the live row shows `ok` for the route that was tested.
- A forced failure stays a failure in the report.
- The report quotes `error_class` and `route_id` only.
