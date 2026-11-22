# Model router

**Purpose:** Architecture and safety contract for Azom model calls: ChatGPT OAuth, xAI Grok Build, xAI API key, and OpenRouter.  
**Audience:** Coding agents and developers. Oscar operates the live connect steps in [`runbooks/model-oauth.md`](runbooks/model-oauth.md).  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md).

## Glossary

| Term | Meaning |
|------|---------|
| **Route order token** | Value in `AZOM_MODEL_ROUTE_ORDER`: `chatgpt_oauth`, `xai_grok_build`, `xai_api_key`, `openrouter`. |
| **route_id** | Stable id on the result. OpenRouter's id is `openrouter_api_key`. The order token stays `openrouter`. |
| **Azom-owned refresh** | Azom stores the refresh token and rotates it. Only ChatGPT OAuth does this. |
| **Delegated refresh** | Another program owns the session. Grok Build does this. Azom does not read that file. |
| **Plan usage** | ChatGPT scope `chatgpt.tokens.use.direct`. Subscription use, not a metered API bill. |
| **Issued client id** | Client id OpenAI returns on the first Sign in with ChatGPT callback. It is not `dynamic_agent_client`. |

## Do / Do not

**Do:**

- Call models only through `ecom_ops.llm` or `ecom_ops.model_router`.
- Keep customer mail, order writes, and SSH on their existing HITL and allowlist paths.
- Store ChatGPT sessions only under `AZOM_DATA_DIR/model_oauth/`.
- Treat a failed isolated model test as a failure. The test does not fall through to another route.

**Do not:**

- Require `OPENROUTER_API_KEY` before another configured route may run.
- Log access tokens, refresh tokens, id tokens, authorization codes, PKCE verifiers, or raw provider bodies.
- Read, copy, or rotate Grok Build `auth.json`.
- Reuse the Gmail, Google Marketing, or Codex OAuth client.
- Claim direct app-owned xAI OAuth. It is not implemented.
- Mark a route successful when its call failed.
- Wire this router to send mail or change orders.

## Package

| Module | Role |
|--------|------|
| `skills/ecom_ops/llm.py` | Facade. `chat_completion`, `classify_support_with_llm`, `draft_support_with_llm`, `generate_product_desc_with_llm` keep their signatures. |
| `skills/ecom_ops/model_router/router.py` | Order, skip unconfigured, one attempt per route, telemetry. |
| `skills/ecom_ops/model_router/store.py` | Atomic session files, mode `0600`, process lock. |
| `skills/ecom_ops/model_router/openai_chatgpt.py` | Sign in with ChatGPT + Responses API. |
| `skills/ecom_ops/model_router/xai_grok_build.py` | Delegated Grok Build CLI. |
| `skills/ecom_ops/model_router/xai_api_key.py` | `https://api.x.ai/v1/chat/completions`. |
| `skills/ecom_ops/model_router/openrouter.py` | Existing chat-completions fallback and USD estimate. |

`ModelRouteError` is a `RuntimeError`. Subclasses: `ModelConfigError`, `ModelAuthError`, `ModelRateLimitError`, `ModelUnavailableError`. Each has a short `.code`.

## Route order

Default:

```text
chatgpt_oauth,xai_grok_build,xai_api_key,openrouter
```

The router skips a route whose `is_configured()` is false. It tries the rest in order. It tries each route once, except ChatGPT inference may refresh and replay once after HTTP 401. If every attempted route fails, it raises `ModelRouteError`. It never returns a failed route as success.

Grok Build is configured only when `AZOM_XAI_GROK_BUILD` is `1`/`true`/`yes`/`on`, or when `XAI_GROK_BIN` is set to a non-empty path. A `grok` binary on `PATH` does not turn the route on.

Until Oscar has completed a live ChatGPT test **without fallback**, set:

```text
AZOM_MODEL_ROUTE_ORDER=xai_grok_build,xai_api_key,openrouter
```

Unconfigured ChatGPT is already skipped, so the default order is safe when the SIWC env is empty.

## Refresh ownership

| Order token | route_id | Who refreshes | `app_owned_refresh` | `cost_usd` |
|-------------|----------|---------------|---------------------|------------|
| `chatgpt_oauth` | `chatgpt_oauth` | Azom | true | `0.0` (subscription) |
| `xai_grok_build` | `xai_grok_build` | Grok Build | false | `0.0`, `usage_known=false` unless the CLI reports usage (it does not today) |
| `xai_api_key` | `xai_api_key` | Operator rotates `XAI_API_KEY` | false | `0.0` (provider usage logged when present; not added to the USD cap) |
| `openrouter` | `openrouter_api_key` | Operator rotates `OPENROUTER_API_KEY` | false | Estimated. Callers record that estimate once against the cap. |

## ChatGPT OAuth

Azom is its own public OAuth client. Endpoints come from env:

| Env | Role |
|-----|------|
| `OPENAI_SIWC_CLIENT_ID` | `dynamic_agent_client` for the first registration, or a saved issued id |
| `OPENAI_SIWC_REDIRECT_URI` | Dashboard callback, exact match |
| `OPENAI_SIWC_AUTHORIZATION_URL` | Authorize endpoint. Not hardcoded. |
| `OPENAI_SIWC_TOKEN_URL` | Token endpoint. Not hardcoded. |
| `OPENAI_SIWC_HOST_ID` | Stable opaque host id for this VPS. Saved on the session. |
| `OPENAI_MODEL` | Responses model. Default `gpt-5.2` when unset. |
| `OPENAI_SIWC_SCOPE` | Optional. Default includes `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`. |
| `OPENAI_SIWC_RESOURCE` | Optional. Default `https://api.openai.com/v1`. |
| `OPENAI_SIWC_AGENT_NAME` | Optional. Default `Azom`. Sent only on first dynamic registration. |

Flow:

1. Oscar opens `/oauth/openai/start` (Oscar role, existing Basic Auth).
2. Azom creates random `state` (10 minutes), PKCE S256, and `nonce`.
3. The browser goes to the configured authorize URL. `offline_access` is always in the default scope.
4. `/oauth/openai/callback` checks `state` with `hmac.compare_digest`. A mismatch does not clear the pending session. Expiry does.
5. First registration must receive an issued `client_id` on the callback. That id is what the token request uses. `dynamic_agent_client` is never stored as the issued id. A callback client id that disagrees with an already issued id is rejected.
6. The token request is form-encoded and includes `resource`. The whole token set is saved atomically with `generation=1`.
7. Inference uses `POST https://api.openai.com/v1/responses` with `stream=true`, `store=false`, `input` as an array, and `max_output_tokens`. System text goes to `instructions`. Developer text stays `developer`. `temperature` and `max_tokens` are not sent.
8. SSE is read until `response.completed` or `[DONE]`. Visible text is the joined `response.output_text.delta` values. If a later `response.output_text.done` `text`, or `response.completed` `output_text` / `output[].content[].text`, is non-empty and the deltas are empty or that full text starts with the delta text, the full text is used. Deltas and the full text are not concatenated. Usage comes from `response.completed`. Classify asks for 512 output tokens, and Oscar's isolated test asks for 512, because a reasoning model can spend a small budget before any visible text. Draft stays at 400 and product copy at 600. `temperature` is still not sent.

Refresh:

- Runs under the provider+account lock (15s timeout). `fcntl.flock` on Ubuntu. `msvcrt` on Windows so local tests can serialize threads.
- After the lock, the session is read again. A newer fresh generation is used and is not rotated again.
- Refresh is not done before `earliest_refresh_at`. The token field is treated as seconds from now.
- HTTP 401 on inference refreshes at most once, even if the access token has not expired, then replays the request once. A second 401 stops.
- HTTP 403 and 429 do not refresh. 403 is `ModelAuthError` code `forbidden`. 429 is `ModelRateLimitError`.
- `invalid_grant`: if another process already saved a newer fresh generation, that generation is used. Otherwise `needs_reauth=true` and automatic refresh stops.
- If the provider rotated the token and Azom cannot save the new file, the session is marked `needs_reauth` with code `persist_failed`. Reconnect. Do not keep refreshing.

`id_token` is stored and is not signature-verified. `status()` sets `id_token_verified` to false. Do not treat email or `sub` inside that token as checked.

The authorize redirect does not include `id_token_hint`, so the id token is not placed in a `Location` header.

## xAI

**Grok Build** runs `XAI_GROK_BIN` (default `grok` once the route is enabled) as one headless turn: `--model`, `--prompt-file`, `--max-turns 1`, `--no-subagents`, `--disable-web-search`, `--no-memory`, `--verbatim`, `--tools` with an empty list. The prompt is written to a temp file mode `0600` inside an empty temp directory that is removed afterwards. It is not placed on argv and it is not written to stdin. `--single` is not used: this CLI rejects `--single` together with `--prompt-file`. `--permission-mode plan` is not used: a probe of that mode waited without printing a completion. `--print` does not exist on this build. A missing binary is `ModelConfigError`. A timeout is `ModelUnavailableError`. A non-zero exit whose stderr looks like a login failure is `ModelAuthError` with code `exit_N`. Every other non-zero exit is `ModelUnavailableError` with code `exit_N`. The error string does not copy stderr. `status()` reports `connected=false` and `auth_checked=false` even when the route is enabled, because checking login would spawn the CLI or read `auth.json`. `refresh_owner` is `grok_build`. An empty tool list is the restriction Azom can set without guessing tool names. It is not a separate sandbox proof. The router still tries the next configured route after a Grok failure.

**API key** sends `model`, `messages`, `max_tokens`, and `temperature` to `https://api.x.ai/v1/chat/completions`. 401 is auth, 429 is rate limit, other 4xx/5xx are unavailable.

**TODO / blocker:** direct app-owned xAI OAuth refresh is not implemented. Do not add it until xAI's current client registration, scopes, token endpoint, and inference contract are verified. The note lives in `skills/ecom_ops/model_router/xai_grok_build.py`.

## OpenRouter and budget

OpenRouter stays a fallback. The chat-completions URL, `HTTP-Referer: https://azom.se`, and `X-Title: AzomOps-Agent` are unchanged. Estimated cost is `0.15` USD / 1M input tokens + `0.60` USD / 1M output tokens, minimum `0.0001` when usage exists.

The USD cap in `config/limits.yaml` applies to that estimate only. Route telemetry stores `cost_usd=0` and puts `estimated_cost_usd` in meta. Classify, draft, product description, and chat record that estimate once on success. An unparseable classify or product body still records it. Oscar's isolated OpenRouter test records `llm_model_test` with the same estimate. The estimate is at least `0.0001` for any returned completion, including when the provider omits token counts.

If the cap is exhausted and no other route is configured, classify, draft, and product description return `None` and telemetry records `llm_budget_skip` without an HTTP call. If another route is configured, those flows may still call it.

## Session files

```text
$AZOM_DATA_DIR/model_oauth/<provider>_<account_id>.json
$AZOM_DATA_DIR/model_oauth/<provider>_<account_id>.lock
```

Writes use a temp file in the same directory, `fsync`, `os.replace`, and `chmod 0600`. `public_view()` whitelists status fields. It does not include `access_token`, `refresh_token`, `id_token`, `code_verifier`, `oauth_state`, or `nonce`.

`model_oauth/` is gitignored. `bin/backup-azom-data.sh` excludes it. A normal backup cannot restore ChatGPT. Oscar keeps a separate encrypted copy only if off-box restore is required.

Last isolated test result is `AZOM_DATA_DIR/model_router_status.json` (route id, ok, error class, error code, model, provider, time). It is outside `model_oauth/` so ordinary backups may keep it.

## Dashboard

Oscar only, existing CSRF on POST:

| Action | Path |
|--------|------|
| Status on `/oscar` | Preferred route, order (not editable), model id, last test, needs_reauth, generation, expires, refresh owner, host id, plan-usage flag |
| Save model ids | `POST /oscar/models/settings` writes `OPENAI_MODEL`, `XAI_MODEL`, `OPENROUTER_MODEL`, and `AZOM_XAI_GROK_BUILD` to `runtime.env`. Empty model fields stay unchanged. |
| Anslut / Återanslut | `GET /oauth/openai/start` |
| Callback | `GET /oauth/openai/callback` (state check; code is not copied into the next redirect) |
| Testa modell | `POST /oscar/models/test` with one `route_id`. Prompt: `Reply with the single word PONG. Do not call tools.` `max_output_tokens` is 512. The page does not call it on load. |
| Koppla från | `POST /oscar/models/chatgpt/disconnect` |
| xAI | Grok Build enable flag and model id. `XAI_API_KEY` is an Oscar secret. Direct xAI OAuth stays unavailable. |
| Behavior | `GET/POST /oscar/behavior` edits `chat`, `classify`, and `draft` in `config/prompts.yaml` and bumps the version of a changed text. |
| Usage | `GET /data/telemetry` filters action, route, and errors. Meta is shown without secret-like keys. |
| History | `GET /data/audit` reads `audit.jsonl`. Jonatan can read it. A `cases` target links to the case. |

## Telemetry

A successful route records provider, `route_id`, model, token counts when known, `usage_known`, and `cost_kind` (`subscription`, `unmetered`, or `estimated`). Failures record `route_id`, `error_class`, and `error_code` only. `llm_support_classify`, `llm_support_draft`, and `llm_product_desc` store the model id from the result that served the call. They do not store the OpenRouter default when another route answered. An unparseable classify or product body stores that same model id. `redact_secrets` still runs. Keys containing `TOKEN`, `CODE_VERIFIER`, `AUTHORIZATION_CODE`, or `OAUTH_STATE` are redacted. Do not put raw provider text in meta.

## Tests

`tests/test_model_router.py`, `tests/test_model_session_store.py`, `tests/test_openai_chatgpt_oauth.py`, `tests/test_xai_routes.py`, `tests/test_llm_router_integration.py`, `tests/test_model_dashboard.py`.

`tests/conftest.py` clears SIWC, xAI, and route-order env for each test and resets the router singleton. It does not clear `OPENROUTER_API_KEY`.
