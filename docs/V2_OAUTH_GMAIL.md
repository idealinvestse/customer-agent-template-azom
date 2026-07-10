# V2: Gmail OAuth consent

Browser-based Gmail OAuth for Azom mail connector. Outlook/Graph remain env-based (deferred).

## Flow

1. Jonatan opens dashboard → **Onboarding** → **Koppla Gmail** (`/oauth/gmail/start`).
2. Dashboard redirects to Google consent (requires `MAIL_OAUTH_CLIENT_ID` + `MAIL_OAUTH_CLIENT_SECRET`).
3. Google redirects to `/oauth/gmail/callback?code=...&state=...`.
4. Tokens saved to `AZOM_DATA_DIR/oauth/gmail.json` (mode 0600).
5. `config_from_env()` prefers stored tokens over `MAIL_OAUTH_*` env vars when `MAIL_PROVIDER=gmail`.

## Env vars

```bash
MAIL_PROVIDER=gmail
MAIL_OAUTH_CLIENT_ID=...
MAIL_OAUTH_CLIENT_SECRET=...
MAIL_OAUTH_REDIRECT_URI=https://dashboard.example.com/oauth/gmail/callback
```

Default redirect (dev): `http://127.0.0.1:8080/oauth/gmail/callback`

## Mock mode

When `AZOM_USE_MOCK=1`, `/oauth/gmail/start` stores mock tokens without calling Google.

## Production (Hetzner + reverse proxy)

- Register redirect URI in Google Cloud Console (OAuth 2.0 Web client).
- Set `MAIL_OAUTH_REDIRECT_URI` to the public HTTPS callback URL.
- Dashboard stays on `127.0.0.1:8080` behind nginx/Caddy; proxy `/oauth/gmail/callback` to Flask.

## Security

- OAuth `state` parameter validated (10 min TTL).
- Tokens never logged in telemetry or escalations.
- Jonatan (viewer) can connect mail via dashboard; send remains operator/Oscar RBAC.
