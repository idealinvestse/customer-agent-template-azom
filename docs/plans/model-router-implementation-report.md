# Model router implementation report

**Purpose:** Close-out for the model-router change. Not a living operator doc.  
**Audience:** Oscar and the next reviewer.  
**Read this first:** [`../MODEL_ROUTER.md`](../MODEL_ROUTER.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

## Bedömning

**REDO FÖR GRANSKNING**

Koden är inte live-verifierad mot OpenAI eller xAI. ChatGPT ska inte stå först i prod-ordningen förrän Oscars test utan fallback är grönt. Paketversionen är oförändrad **3.0.0**. Ingenting är pushat, mergat eller driftsatt.

## 1. Commit och branch

- Branch: `feat/v3-production-maturity`
- Bas-commit (HEAD, oförändrad): `90b305a` `docs: add maturity assessment`
- Modellroutern ligger ocommittad i arbetskopian.

## 2. Filer skapade och ändrade

Nya:

- `skills/ecom_ops/model_router/` (`__init__.py`, `errors.py`, `base.py`, `store.py`, `router.py`, `openrouter.py`, `openai_chatgpt.py`, `xai_grok_build.py`, `xai_api_key.py`)
- `tests/test_model_router.py`
- `tests/test_model_session_store.py`
- `tests/test_openai_chatgpt_oauth.py`
- `tests/test_xai_routes.py`
- `tests/test_llm_router_integration.py`
- `tests/test_model_dashboard.py`
- `docs/MODEL_ROUTER.md`
- `docs/runbooks/model-oauth.md`
- `docs/plans/model-router-facts.md`
- denna rapport

Ändrade: `.env.example`, `.gitignore`, `AGENTS.md`, `README.md`, `SOUL.md`, `bin/backup-azom-data.sh`, `docs/COMPLIANCE.md`, `docs/CURRENT_STATE.md`, `docs/DEVELOPER_GUIDE.md`, `docs/DOCKER_CONFIG_OVERLAY.md`, `docs/PILOT_OPS.md`, `docs/README.md`, `docs/SYSTEM_OVERVIEW.md`, `docs/TELEGRAM_OPENCLAW.md`, `docs/TEMPLATE_GUIDE.md`, `docs/runbooks/README.md`, `docs/runbooks/openrouter-budget-exhausted.md`, `infrastructure/dashboard/oauth_routes.py`, `infrastructure/dashboard/oscar_routes.py`, `infrastructure/dashboard/templates/oscar.html`, `skills/ecom-ops/SKILL.md`, `skills/ecom_ops/actions/product_desc.py`, `skills/ecom_ops/bot/chat_agent.py`, `skills/ecom_ops/llm.py`, `skills/ecom_ops/security.py`, `tests/conftest.py`.

## 3. Router-arkitektur

`llm.py` är en facade. `chat_completion`, `classify_support_with_llm`, `draft_support_with_llm` och `generate_product_desc_with_llm` behåller sina signaturer. Tillgänglighet avgörs av routern. `OPENROUTER_API_KEY` är inte längre en spärr.

Defaultordning: `chatgpt_oauth,xai_grok_build,xai_api_key,openrouter`. Okonfigurerade routes hoppas över. Ett försök per route, utom ett ChatGPT-401 som får en refresh och en omsändning. `ModelRouteError` om ingen route lyckas. En misslyckad route rapporteras inte som success.

Order-token `openrouter` mappar till `route_id` `openrouter_api_key`.

Chatt skickar inte en OpenRouter-modellslug till ChatGPT eller xAI. En session-pin gäller bara när OpenRouter är första konfigurerade route. Övriga anrop lämnar `model` tom så att routen använder `OPENAI_MODEL` eller `XAI_MODEL`.

## 4. ChatGPT OAuth-flöde

Azom är en egen publik klient. Authorize- och token-URL läses från env. Första registreringen kan använda `dynamic_agent_client` plus `agent_name_hint=Azom`. Callbackens utfärdade `client_id` sparas och används vid token exchange och senare refresh. `dynamic_agent_client` sparas inte som utfärdat id. En avvikande callback-`client_id` avvisas.

PKCE S256, tidsbegränsad `state` (`hmac.compare_digest`), `nonce`, scope med `offline_access` och dokumenterade plan-scopes, `resource` default `https://api.openai.com/v1`. `ext_agent_host_id` sparas på sessionen.

Inferens: `POST https://api.openai.com/v1/responses`, `stream=true`, `store=false`, `input` som array, system till `instructions`, developer förblir developer, `max_output_tokens`, ingen `temperature` och ingen `max_tokens`. SSE-text från `response.output_text.delta`. Saknas deltan, eller om en senare heltext börjar med deltexten, används `response.output_text.done` eller `output_text` / `output[].content[].text` på `response.completed`. Deltan och heltext slås inte ihop. Usage kommer från `response.completed`. `cost_usd=0.0`. Klassificering och Oscars isolerade test ber om 512 output-tokens så att en resonemangsmodell hinner skriva synlig text.

`id_token` lagras och signaturverifieras inte. UI och status säger `id_token_verified=false`. `id_token_hint` skickas inte i redirecten.

## 5. Refresh ownership

| Route | Ägare | App-ägd refresh |
|-------|--------|-----------------|
| `chatgpt_oauth` | Azom | ja |
| `xai_grok_build` | Grok Build | nej |
| `xai_api_key` | Oscar via `XAI_API_KEY` | nej |
| `openrouter` | Oscar via `OPENROUTER_API_KEY` | nej |

ChatGPT-refresh tar processlås, läser om sessionen och roterar inte om en nyare färsk generation redan finns. Lyckad refresh ökar `generation` och sparar hela paketet atomiskt. `invalid_grant` sätter `needs_reauth` om ingen nyare generation finns, och stoppar automatisk refresh. Misslyckad sparning efter rotation sätter `needs_reauth` med kod `persist_failed`.

401 på inferens ger högst en refresh och en omsändning, även om access-token inte hunnit gå ut. 403 och 429 triggar inte refresh. Refresh före `earliest_refresh_at` görs inte. Fältet tolkas som sekunder från nu.

## 6. xAI Grok Build

Opt-in via `AZOM_XAI_GROK_BUILD` eller satt `XAI_GROK_BIN`. En `grok` på PATH räcker inte. Anropet är `grok --model … --prompt-file <temp>/prompt.txt --max-turns 1 --no-subagents --disable-web-search --no-memory --verbatim --tools ""`. Prompten skrivs till filen, inte till argv och inte till stdin. `--single` används inte, eftersom den här CLI:n avvisar den tillsammans med `--prompt-file`. `--permission-mode plan` används inte, eftersom en probe väntade utan att skriva ett svar. Saknad binär är `ModelConfigError`. Timeout är `ModelUnavailableError`. Icke-noll-exit med inloggningslik stderr är `ModelAuthError` med kod `exit_N`. Övrig icke-noll-exit är `ModelUnavailableError` med kod `exit_N`. Stderr kopieras inte till feltexten. `status()` sätter `connected=false` och `auth_checked=false` även när routen är på, eftersom en inloggningsprobe skulle starta CLI:t eller läsa `auth.json`. Azom läser inte `auth.json`. `usage_known=false`, `cost_usd=0`. Routern försöker nästa konfigurerade route efter ett Grok-fel.

Direkt app-ägd xAI OAuth är inte implementerad. TODO står i `xai_grok_build.py`.

## 7. xAI API-nyckel

`XAI_API_KEY` mot `https://api.x.ai/v1/chat/completions` med model, messages, max_tokens, temperature och usage. 401 auth, 429 rate limit, övriga 4xx/5xx unavailable. `refresh_owner=operator`. Kostnad räknas inte mot USD-capen.

## 8. OpenRouter-fallback

Befintligt chat-completions-anrop, referer `https://azom.se`, titel `AzomOps-Agent`. Uppskattning 0.15 USD/1M in + 0.60 USD/1M ut, minst 0.0001 per svar. Route-telemetry har `cost_usd=0` och `estimated_cost_usd` i meta. Anroparen bokför uppskattningen en gång, även när classify eller produkttext inte kan parsas. Oscars OpenRouter-test bokför `llm_model_test`. Capen gäller bara den uppskattningen. Saknas andra routes och capen är slut hoppas LLM över utan HTTP, och mallen används.

## 9. Telemetri och usage

Lyckad route: provider, `route_id`, model, tokenantal när det finns, `usage_known`, `cost_kind` (`subscription`, `unmetered`, `estimated`). Fel: `error_class` och `error_code`. Inga tokenvärden i meta. `llm_support_classify`, `llm_support_draft` och `llm_product_desc` sparar modell-id från resultatet som svarade, inte OpenRouter-default när en annan route tjänade anropet. `redact_secrets` täcker nycklar med `TOKEN`, `CODE_VERIFIER`, `AUTHORIZATION_CODE` och `OAUTH_STATE`.

## 10. Säkerhet

- Sessionfiler: temp + `fsync` + `os.replace`, `chmod 0600`. Katalogen sätts till `0700` där OS tillåter.
- Lås 15 sekunder. POSIX `fcntl`, Windows `msvcrt` så lokala tester kan serialisera trådar.
- `public_view` är en whitelist utan access, refresh, id token, verifier, state eller nonce.
- `model_oauth/` är gitignorad och undantagen i `bin/backup-azom-data.sh`.
- `.env.example` har tomma hemligheter. Exempel-URL:er är kommentarer.
- Dashboard-test och frånkoppling är Oscar-POST med befintlig CSRF. Start är Oscar-GET. Callbacken kopierar inte koden till nästa redirect.
- Routern anropar inte mejl, order eller SSH. Grok-subprocess är en turn med tom verktygslista i en tom katalog. Det är inte ett bevisat sandbox-kontrakt från xAI.
- HITL, null-send, auto-send-kill och mallfallback är oförändrade. FU9 är inte inkopplad.

## 11. Dashboard

Oscar-sektion på `/oscar`: aktiv route, senaste test, needs_reauth, generation, expires, refresh owner, host-id, plananvändning, och att ID-token-signaturen inte är verifierad.

Knappar: Anslut, Återanslut (`/oauth/openai/start`), Testa modell (`POST /oscar/models/test`, fast prompt, ingen fallback), Koppla från.

## 12. Tester tillagda

De sex testfilerna ovan. De täcker route-ordning, store (atomiskt, 0600 på POSIX, lås, public view, clear, generation), PKCE, state, utfärdat client id, refresh-lås, en 401-retry, 429/403, `invalid_grant`, Responses-payload, SSE, Grok utan `auth.json`, API-nyckelns felkoder, facade utan OpenRouter-nyckel, budget utan HTTP, och att chatt inte skickar OpenRouter-slug till en annan route.

## 13. Testresultat

Körda i den här sessionen, efter rättningen av Grok-anropet, SSE-texten, tokenbudgeten och modellfältet i telemetrin:

| Kommando | Resultat |
|----------|----------|
| `python -m pytest -q --tb=line` | exit 0. Progress 100%. 590 tester räknade från punktmatrisen (8×72+14). Loggen skrev ingen separat `passed`-rad. |
| `python -m ruff check skills tests infrastructure/dashboard` | All checks passed |
| `python -m ecom_ops version` | `3.0.0`, paket `azom-ecom-ops` |
| `python -m ecom_ops status` | `ok: true`, version 3.0.0. Lokal process hade `mock: false` eftersom skalet inte satte `AZOM_USE_MOCK`. Inget modell- eller mejlanrop. |
| `bash tests/test_spinup.sh` | Avbruten: Git Bash hittade inte `python`. Samma CLI-steg kördes i PowerShell med `AZOM_USE_MOCK=1`: order, produkt, support, ssh uptime, ssh `rm -rf /` eskalerar, mail send/fetch, andra order-körningen. Alla `ok` utom den avsiktliga eskaleringen. |
| `cd web && npm run build` | Inte kört. Det finns ingen `web/`-app. Dashboard är Flask. |

Inga betalda provideranrop. Inga riktiga kundmejl.

## 14. Manuella operatörsteg

Se [`../runbooks/model-oauth.md`](../runbooks/model-oauth.md).

1. Sätt SIWC-URL:er från aktuell OpenAI-dokumentation. Första `OPENAI_SIWC_CLIENT_ID=dynamic_agent_client`. Stabil `OPENAI_SIWC_HOST_ID`.
2. Starta om dashboard. Oscar: Anslut ChatGPT, sedan Testa modell på `chatgpt_oauth`.
3. Tills det testet är grönt: `AZOM_MODEL_ROUTE_ORDER=xai_grok_build,xai_api_key,openrouter`.
4. Grok Build: `grok login --device-auth` på hosten, sedan `AZOM_XAI_GROK_BUILD=1`. Binären finns inte i Docker-imagen.
5. Vanlig backup återställer inte `model_oauth/`. Kryptera en separat kopia om off-box-restore behövs.

## 15. Kvarvarande risker

- ID-token-signatur och nonce kontrolleras inte. Ingen kodväg behandlar tokenet som verifierat.
- `earliest_refresh_at` antas vara sekunder från nu. Ett absolut unix-värde från leverantören skulle skjuta upp refresh fel.
- Authorization code kan ligga i webbläsarhistorik och i reverse-proxyns access-logg för callbacken. Azom skriver den inte i nästa redirect, UI eller telemetry. PKCE gör koden oanvändbar utan verifier.
- Tom `--tools` är inte live-bevisat som “inga verktyg”. En framtida CLI kan ignorera flaggan. Isolerat test på hosten är kravet.
- Flaggtolkningen är probead utan modellkomplettering: `--single` tillsammans med `--prompt-file` avslutas med kod 2, och `--prompt-file` ensam är headless-inmatningen. En riktig promptfil mot modellen är inte körd.
- `redact_secrets` skrubbar inte fri text. Nuvarande felmeta är klass och kod.
- OAuth-state-filen låses inte tillsammans med sessionen. Två callbacks kan passera state-jämförelsen. PKCE stoppar en andra inlösen.
- Katalogläget `0700` är best effort på Windows.
- En 401 medan `earliest_refresh_at` ligger i framtiden refreshar inte. Anropet faller vidare.

## 16. Blockers

- Live Sign in with ChatGPT är inte kört. Oscar måste bekräfta authorize-URL, token-URL, utfärdat client id och scope `chatgpt.tokens.use.direct`.
- ID-token ska enligt OpenAI verifieras mot JWKS. Det är inte implementerat.
- Direkt xAI OAuth är blockerad tills xAI:s aktuella registrering, scopes, token-endpoint och inferenskontrakt är lästa och verifierade. Ingen fejkad refresh finns.
- Modellista `GET /v1/models` används inte. Routen anropar Responses med `OPENAI_MODEL` eller default `gpt-5.2`, som uppdraget krävde.

## 17. Rekommenderad nästa iteration

1. Oscar kör anslutning och test utan fallback på en VPS. Först därefter får `chatgpt_oauth` stå först.
2. Verifiera JWKS, issuer, audience, exp och nonce innan `id_token` används som identitet.
3. Bekräfta på hosten att `grok --prompt-file` med tom `--tools` skriver ett svar och inte kör verktyg. Justera flaggorna om hjälpen skiljer sig.
4. Lägg inte till app-ägd xAI OAuth förrän kontraktet är verifierat.

## Vad som faktiskt verifierades

- Full pytest exit 0 efter Grok-, SSE-, tokenbudget- och telemetrirättningen (590 tester).
- Ruff grön.
- Version 3.0.0.
- Router, store, OAuth-mock, xAI-mock, facade utan OpenRouter-nyckel, budget-skip, dashboard-test utan fallback.
- Säkerhetsgranskare (subagent) och oberoende granskare (subagent) läste diffen. Deras måste-fixar är åtgärdade: Grok anropas inte som fri agent i repot, chatt skickar inte OpenRouter-slug till andra routes, oparsbar OpenRouter-svar och Oscar-test bokför uppskattad kostnad en gång.

## Vad som kräver manuell live-verifiering

- ChatGPT-anslutning, utfärdat client id, refresh och isolerat modelltest.
- Att Grok Build på VPS:en accepterar `--prompt-file` och tom `--tools`, och skriver ett svar.
- Att rätt `route_id` syns i `telemetry.jsonl` under riktig last.
- Att reverse-proxyn inte loggar callbackens query.

## OAuth-kontrakt som fortfarande måste verifieras mot leverantör

- OpenAI SIWC: JWKS-validering av `id_token`, exakt betydelse av `earliest_refresh_at`, och att Responses med `store=false` fungerar med plan-scope.
- xAI: inget app-ägt OAuth-kontrakt är verifierat, och inget är implementerat.

## Subagents

Använda subagents: säkerhetsgranskning (`pstack:comment-sicko`) och oberoende granskning (`pstack:poteto-agent`). Båda var läsande och ändrade inga filer.

Kartläggning, testdesign, implementation och dokumentation gjordes i den här sessionen, inte av separata subagents. Det fanns inga namngivna subagent-typer för de rollerna. En generell subagent hade kunnat ta dem. Det gjordes inte, för att hålla implementationen i en arbetskopia.
