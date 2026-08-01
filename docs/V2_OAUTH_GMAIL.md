# Gmail OAuth (browser-consent)

**Purpose:** Steg för Gmail browser-OAuth till Azom mail-connector.  
**Audience:** Jonatan (consent) / Oscar (client id/secret).  
**Read this first:** [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md), [`.env.example`](../.env.example), [`CURRENT_STATE.md`](CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Browser OAuth** | Jonatan kopplar Gmail via dashboard → Google consent → tokens på disk. |
| **Tokenfil** | `AZOM_DATA_DIR/oauth/gmail.json` (läge 0600). |
| **Env-fallback** | Utan lagrad token kan `MAIL_OAUTH_*` env användas; lagrade tokens prioriteras. |

Outlook / Exchange Graph har **ingen** dashboard-OAuth-UI — endast env + probe.

## Flöde

1. Jonatan öppnar dashboard → **Onboarding** → **Koppla Gmail** (`/oauth/gmail/start`).
2. Dashboard redirectar till Google (kräver `MAIL_OAUTH_CLIENT_ID` + `MAIL_OAUTH_CLIENT_SECRET`).
3. Google redirectar till `/oauth/gmail/callback?code=...&state=...` (publik route — ingen Basic Auth; skyddas av OAuth `state`).
4. Tokens sparas i `AZOM_DATA_DIR/oauth/gmail.json`.
5. `config_from_env()` föredrar lagrade tokens framför `MAIL_OAUTH_*` env när `MAIL_PROVIDER=gmail`.

Status-JSON: `GET /oauth/gmail/status` (auth).

## Env

```bash
MAIL_PROVIDER=gmail
MAIL_OAUTH_CLIENT_ID=...
MAIL_OAUTH_CLIENT_SECRET=...
MAIL_OAUTH_REDIRECT_URI=https://dashboard.example.com/oauth/gmail/callback
```

Default redirect (dev): `http://127.0.0.1:8080/oauth/gmail/callback`  
(byggs från `DASHBOARD_HOST` / `DASHBOARD_PORT` om URI saknas).

## Mock

När `AZOM_USE_MOCK=1` sparar `/oauth/gmail/start` mock-tokens utan att anropa Google.

## Produktion (Hetzner + reverse proxy)

1. Registrera redirect URI i Google Cloud Console (OAuth 2.0 Web client).
2. Sätt `MAIL_OAUTH_REDIRECT_URI` till publika HTTPS-callback.
3. Dashboard lyssnar på `127.0.0.1:8080`; proxya `/oauth/gmail/*` till Flask.
4. Systemd-data: tokens landar under `/var/lib/azom/oauth/`.

## Säkerhet

**Gör:**

- Validera OAuth `state` (TTL ca 10 min — redan i kod).
- Håll client secret i env / `secrets.env`, inte i git.
- Vid revoked token: se [`runbooks/gmail-oauth-revoked.md`](runbooks/gmail-oauth-revoked.md).

**Gör inte:**

- Logga tokens i telemetry eller eskaleringar.
- Förvänta dig att Jonatan kan skicka mail bara för att hen kopplat Gmail — send styrs av RBAC (`CASE_REPLY` / operator).

## Relaterat

- Mail providers: [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md)
- Pilot: [`PILOT_OPS.md`](PILOT_OPS.md)
