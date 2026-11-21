# Model router facts (package 1)

**Purpose:** Short fact base captured before the router change. Not a living operator doc.  
**Audience:** Coding agents reviewing this change.  
**Read this first:** [`../CURRENT_STATE.md`](../CURRENT_STATE.md), [`../MODEL_ROUTER.md`](../MODEL_ROUTER.md).

Recorded against branch `feat/v3-production-maturity` before the router files existed. Package version stayed **3.0.0**.

## Calls and the old gate

- `skills/ecom_ops/llm.py` called OpenRouter chat completions and checked `OPENROUTER_API_KEY` before classify, support draft, and product description.
- `skills/ecom_ops/actions/product_desc.py` had the same key check and then a template fallback.
- `skills/ecom_ops/bot/chat_agent.py` returned `FALLBACK_NO_KEY` when the key was missing, and `FALLBACK_BUDGET` when the USD cap was exhausted.
- Those gates meant a configured OAuth route could not run without an OpenRouter key.
- Tests mock `https://openrouter.ai/api/v1/chat/completions`. `test_llm_draft_context` patches `ecom_ops.llm.chat_completion`. Support handle may call both classify and draft with one mocked body.
- Budget tests expect zero HTTP calls when the only route is OpenRouter and spend is not strictly under the cap (`100` is not within `100`).
- Estimated OpenRouter cost is `0.15` / 1M input + `0.60` / 1M output. Cap lives in `config/limits.yaml`.

## OAuth, dashboard, backup

- Gmail and Google Marketing OAuth already live under `AZOM_DATA_DIR/oauth/`. They are separate from model sessions.
- Dashboard POST CSRF is enforced for authenticated POSTs. Oscar routes use the existing Oscar guard.
- `bin/backup-azom-data.sh` copies `AZOM_DATA_DIR` and previously included every OAuth JSON file.
- Docker data is `/app/.azom-data`. Systemd data is `/var/lib/azom`. No extra volume is required for `model_oauth/`.
- The image does not contain the `grok` CLI.

## Grok CLI probe 2026-11-22

Checked on this machine with flag parsing only. No model completion and no read of `auth.json`.

- This build has no `--print`.
- `--single` cannot be combined with `--prompt-file` (exit 2).
- `--prompt-file` alone is the headless one-shot input. A missing file fails before a model call.
- `--tools ""` is accepted at parse time.
- `--permission-mode plan` with stdin produced no stdout or stderr in a short probe and was killed. The route does not use that mode.
- `telemetry.record` already runs `redact_secrets`. Redaction is key-substring based. A token inside a free-text error would not be covered unless the key matched.

## Constraints that stayed

- No silent customer mail. FU9 stays unwired. `AZOM_AUTO_SEND_KILL` stays. Null-send stays default off.
- NO/DK mailboxes stay disabled. A1 soak is not marked complete.
- There is no `web/` npm app. Dashboard build is Flask templates, not `npm run build`.
- Coverage gate remains 70% on `ecom_ops`.

## SIWC contract checked 2026-11-21

Public OpenAI docs (`developers.openai.com/siwc/token-sharing-open-source/sign-in`) say:

- First registration uses `client_id=dynamic_agent_client` plus `agent_name_hint` and a stable `ext_agent_host_id`.
- The callback returns the issued client id. Token exchange uses that id, plus `resource=https://api.openai.com/v1`.
- Scope for plan usage includes `offline_access`, `resource.invoke`, and `chatgpt.tokens.use.direct`.
- Authorize and token URLs are `https://auth.openai.com/api/accounts/authorize` and `https://auth.openai.com/api/accounts/oauth/token`. Code still reads them from env.
- ID tokens must be checked against OpenAI JWKS. This repo stores `id_token` and does **not** verify the signature.
- Inference docs show `GET /v1/models` then Responses. This repo calls Responses directly, as the task required, with `stream=true` and `store=false`.
