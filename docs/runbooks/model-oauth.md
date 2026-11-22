# Runbook: modellrouter och ChatGPT OAuth

**Purpose:** Ansluta och felsöka modellroutes utan att läcka tokens eller maskera ett OAuth-fel med OpenRouter.  
**Audience:** Oscar. Jonatan använder inte OAuth-knapparna.  
**Read this first:** [`../PILOT_OPS.md`](../PILOT_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md), [`../MODEL_ROUTER.md`](../MODEL_ROUTER.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Refresh owner** | Vem som får rotera credential. ChatGPT: Azom. Grok Build: Grok Build. API-nycklar: Oscar. |
| **needs_reauth** | Refresh-token är ogiltig. Automatisk refresh är stoppad tills Oscar återansluter. |
| **Test utan fallback** | Knappen Testa modell anropar bara den valda routen. |

## Gör / gör inte

**Gör:**

- Sätt SIWC-URL:er från aktuell OpenAI-dokumentation innan första anslutningen.
- Använd en egen host-id per VPS. Återanvänd samma värde vid omstart.
- Kör Testa modell på ChatGPT innan routen står först i ordningen.
- Håll en separat krypterad kopia av `model_oauth/` bara om du behöver återställa off-box.

**Gör inte:**

- Lägg refresh-token i `.env`, SQLite, Telegram eller en vanlig backup.
- Läs eller kopiera Grok Builds `auth.json`.
- Återanvänd Gmail-, Google Marketing- eller Codex-klient.
- Tolka ett grönt OpenRouter-svar som att ChatGPT fungerar.
- Markera live soak eller FU9 som klart från den här runbooken.

## 1. ChatGPT OAuth

1. På hosten `/opt/azom-agent`, redigera `.env` (värden lämnas tomma i repo):
   - `OPENAI_SIWC_CLIENT_ID=dynamic_agent_client` vid första registreringen
   - `OPENAI_SIWC_REDIRECT_URI` exakt som dashboard-callback, till exempel `https://<dashboard>/oauth/openai/callback`
   - `OPENAI_SIWC_AUTHORIZATION_URL` och `OPENAI_SIWC_TOKEN_URL` från aktuell SIWC-dokumentation
   - `OPENAI_SIWC_HOST_ID` en stabil opak sträng för den här VPS:en
   - `OPENAI_MODEL` om du inte vill ha default `gpt-5.2`
2. Starta om dashboard: `sudo systemctl restart azom-dashboard`.
3. Logga in som Oscar. Öppna `/oscar`. Välj **Anslut ChatGPT**.
4. Godkänn ChatGPT-plananvändning i webbläsaren. Callbacken sparar utfärdat `client_id` i `AZOM_DATA_DIR/model_oauth/openai_default.json` med läge `0600`.
5. På `/oscar`: generation minst 1, needs_reauth nej, refresh owner `azom`, plananvändning beviljad.
6. Tryck **Testa modell** på raden `chatgpt_oauth`. Förväntat: senaste test `ok`. Vid fel står `misslyckades` och felklass. OpenRouter anropas inte.

**Förväntat:** Ingen token syns i sidan, i redirecten efter callback, eller i telemetry.

Återanslutning använder sparat utfärdat client id, inte `dynamic_agent_client`. **Koppla från** raderar sessionfilen.

## 2. Refresh ownership

| Route | Ägare | Vad Oscar gör |
|-------|--------|----------------|
| `chatgpt_oauth` | Azom | Ingenting vid vanlig expiry. Vid `needs_reauth`: Återanslut. |
| `xai_grok_build` | Grok Build | `grok login --device-auth` på hosten. Azom rör inte credential-filen. |
| `xai_api_key` | Oscar | Byt `XAI_API_KEY` i `.env` och starta om tjänsterna. |
| `openrouter` | Oscar | Byt `OPENROUTER_API_KEY` i `.env`. |

## 3. xAI Grok Build

1. Installera Grok Build på hosten. Imagen innehåller inte binären.
2. Kör `grok login --device-auth` som den användare som kör tjänsten.
3. Sätt `AZOM_XAI_GROK_BUILD=1` eller `XAI_GROK_BIN=/full/path/grok`. Sätt `XAI_MODEL` vid behov. Azom anropar `grok` en turn med `--prompt-file`, utan `--single` och utan `--permission-mode plan`. Prompten ligger i en temporär fil, inte på argv och inte på stdin. Tom `--tools` betyder inga inbyggda verktyg. Status visar inte ansluten bara för att flaggan är satt. En session-pin från `/model` skickas bara när OpenRouter är första konfigurerade route.
4. Starta om `azom-dashboard`, `azom-bot` och cases-poll.
5. Testa raden `xai_grok_build` på `/oscar` utan fallback.

Direkt xAI OAuth är inte implementerat. Vänta tills xAI:s klientregistrering, scopes och token-endpoint är verifierade.

## 4. xAI API-nyckel

1. Sätt `XAI_API_KEY` i `.env`. Sätt inte nyckeln i git.
2. Testa raden `xai_api_key`. 401 betyder ogiltig nyckel. 429 är rate limit.

## 5. OpenRouter-fallback

1. `OPENROUTER_API_KEY` får vara tom när en annan route är konfigurerad.
2. Capen i `config/limits.yaml` gäller bara uppskattad OpenRouter-kostnad.
3. När capen är slut och ingen annan route finns: utkast faller tillbaka till mall. Order och ärenden fungerar ändå.

## 6. Route order

Default är `chatgpt_oauth,xai_grok_build,xai_api_key,openrouter`. Okonfigurerade routes hoppas över.

Innan ChatGPT-testet utan fallback är grönt:

```bash
# cwd: /opt/azom-agent
# i .env
AZOM_MODEL_ROUTE_ORDER=xai_grok_build,xai_api_key,openrouter
```

Kontrollera vald route i telemetry: action `model_route_selected` med `route_id` och `model`. Fel loggas som `model_route_error` med `error_class` och `error_code`, inte med svarstext.

## 7. Backup

`bin/backup-azom-data.sh` tar inte med `model_oauth/`. En vanlig tarball kan inte återställa ChatGPT-sessionen.

1. Om off-box-kopia behövs: kopiera katalogen till en krypterad plats som bara Oscar når.
2. Lägg den inte i samma backup som `cases.db`.
3. Efter restore: kontrollera `needs_reauth` och kör Testa modell utan fallback.

## 8. Incident: invalid_grant

**Symptom:** `/oscar` visar needs_reauth ja. Utkast går vidare till nästa konfigurerade route, eller till mall om ingen annan route fungerar.

**Gör:**

1. Tryck inte på något som roterar token i en loop. Koden har redan stoppat automatisk refresh.
2. Läs telemetry efter `error_code=invalid_grant` eller `needs_reauth`. Kopiera inte råa rader om de skulle innehålla hemligheter. De ska inte göra det.
3. Öppna **Återanslut** och godkänn igen.
4. Kör **Testa modell** på `chatgpt_oauth`. Förväntat: ok, och needs_reauth nej.

**persist_failed:** leverantören kan ha roterat token innan Azom hann spara. Återanslut. Återanvänd inte den gamla sessionfilen.

## 9. Kontrollera att rätt route används

```bash
# cwd: /opt/azom-agent
# env: prod, AZOM_USE_MOCK=0
sudo -u azom /opt/azom-agent/.venv/bin/python -m ecom_ops status
# expect: processen lever; status visar inte själva token

sudo -u azom grep model_route_selected /var/lib/azom/telemetry.jsonl | tail -5
# expect: route_id för den route som faktiskt svarade
```

Dashboardens senaste test ska matcha samma `route_id`.

## 10. Token safety

**Gör:**

- Behandla refresh-token som en hemlighet med ägare. ChatGPT ägs av Azom. Grok Build ägs av Grok Build.
- Vid `invalid_grant` eller `needs_reauth`: stoppa fler refresh-försök och be Oscar återansluta.
- Sök i loggar efter felklass och route-id, inte efter tokensträngar.

**Gör inte:**

- Läsa, kopiera eller rotera Grok Builds `auth.json`.
- Återanvända Gmail, Google Marketing, Codex CLI eller någon annan applikations OAuth-klient.
- Skriva tokens, authorization codes eller råa provider-svar till logg, telemetry, UI, SQLite eller export.
- Köra ett betalt provideranrop från den här runbooken utan Oscars uttryckliga ja.
