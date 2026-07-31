# Mail-providers — setup och felsökning

**Purpose:** Förklara hur mail ansluts (Gmail, Outlook, Graph, IMAP, POP3), vilka env-variabler som krävs, och hur man felsöker.  
**Audience:** Oscar (secrets) och operatörer; coding agents som rör mail-integration.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`.env.example`](../.env.example), [`CASES.md`](CASES.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **MAIL_PROVIDER** | Vilken connector som används: `gmail` \| `outlook` \| `exchange_graph` \| `generic_imap` \| `generic_pop3`. |
| **env_prefix** | Prefix per mailbox i `config/mailboxes.yaml` (t.ex. `MAIL_SE_`) så credentials kan isoleras. |
| **XOAUTH2** | OAuth-token mot IMAP/SMTP istället för app-lösenord. |
| **Graph** | Microsoft Graph REST (`exchange_graph`) — inte samma sak som Outlook IMAP. |
| **Fail-closed probe** | Med `AZOM_USE_MOCK=0` kör Oscar mail-probe en riktig `fetch(limit=1)`. |

## Viktiga regler

**Gör:**

- Lägg credentials i `.env` / `AZOM_DATA_DIR/secrets.env` — aldrig i gitad YAML.
- Behåll `AZOM_USE_MOCK=1` lokalt tills credentials är avsiktliga.
- Använd trådheaders (`In-Reply-To` / `References`) via cases reply — inte fri SMTP utan cases-flödet för kundärenden.

**Gör inte:**

- Aktivera NO/DK-mailboxar (`enabled: true`) utan Oscars credentials + skriftligt OK.
- Förvänta dig Outlook browser-OAuth i dashboard — den UI:n finns inte; använd env + probe.
- Ignorera partial poll-fel — de skapar eskalering och syns i `last_case_poll.json`.

## Provider-matris

Sätt `MAIL_PROVIDER` plus radens variabler (se även `.env.example`).

### `generic_imap` / `generic_pop3`

Krävs:

- `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`
- SMTP: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USE_TLS` / `SMTP_USE_SSL`
- IMAP: `IMAP_HOST`, `IMAP_PORT`, `IMAP_USE_SSL`  
  eller POP3: `POP3_HOST`, `POP3_PORT`, `POP3_USE_SSL`

### `gmail`

Antingen:

1. **App-lösenord** — `MAIL_PASSWORD` (+ användare/from), SMTP/IMAP-defaults om host saknas, **eller**
2. **OAuth2 / XOAUTH2** — `MAIL_OAUTH_*` och/eller dashboard browser consent → tokens i `AZOM_DATA_DIR/oauth/gmail.json`.

Dashboard: `/oauth/gmail/start` (se [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md)).  
Refresh-token persisteras vid förnyelse; utgångna tokens är härdade i v2.3.

### `outlook`

- `MAIL_USERNAME`, `MAIL_FROM` + antingen `MAIL_PASSWORD` (app-lösenord) **eller** `MAIL_OAUTH_*` (XOAUTH2).
- Sätt Outlook SMTP/IMAP-host om provider-defaults inte räcker.
- **Ingen** dashboard Outlook OAuth-UI — endast env + Oscar probe.

### `exchange_graph`

Krävs:

- `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_USER`
- Valfritt: `GRAPH_BASE_URL` (default `https://graph.microsoft.com/v1.0`)

Används **inte** för vanlig Outlook IMAP-raden ovan.

## Per-mailbox prefix

I `config/mailboxes.yaml` kan en mailbox ha `env_prefix` (t.ex. `MAIL_SE_`).

Prioritet:

1. `{prefix}USERNAME` / `{prefix}PASSWORD` / `{prefix}FROM` / `{prefix}OAUTH_*`
2. Annars ofixade `MAIL_*`
3. Host/port/TLS: `{prefix}SMTP_HOST` etc., annars `SMTP_*` / `IMAP_*` / `POP3_*` / provider-defaults

Exempel:

```bash
MAIL_SE_USERNAME=support@azom.se
MAIL_SE_PASSWORD=...
MAIL_SE_FROM=support@azom.se
```

## Mailbox-config (cases)

```yaml
# config/mailboxes.yaml (utdrag)
mailboxes:
  - id: support_default
    address: support@azom.se
    market: se
    language: sv
    enabled: true
    # provider: gmail   # valfri override av MAIL_PROVIDER
    # env_prefix: MAIL_SE_
```

NO/DK-rader ska vara `enabled: false` tills Oscar säger annars ([`CURRENT_STATE.md`](CURRENT_STATE.md)).

## CLI (mock-säkert)

```bash
# cwd: repo-root, AZOM_USE_MOCK=1
python -m ecom_ops --mock mail fetch --limit 5
# expect: JSON med meddelanden från mock-store

python -m ecom_ops --mock mail send --to a@b.co --subject "Test" --body "Hej"
# expect: ok true i mock

python -m ecom_ops --mock cases poll
# expect: ingest från konfigurerade (mock) mailboxar
```

Live (endast med avsiktliga credentials):

```bash
# AZOM_USE_MOCK=0 på host
python -m ecom_ops mail fetch --limit 1
python -m ecom_ops status
```

## Oscar secrets probe

- Endpoint: `POST /oscar/secrets/test` (mail-target).
- `AZOM_USE_MOCK=0` → riktig `fetch(limit=1)`.
- `AZOM_USE_MOCK=1` → ingen nätverkstrafik.

## Felsökning (kort)

| Symptom | Kolla | Runbook / nästa steg |
|---------|--------|----------------------|
| Poll skapar inga cases | credentials, `enabled`, provider | [`runbooks/mail-poll-stuck.md`](runbooks/mail-poll-stuck.md) |
| Gmail OAuth trasig | `oauth/gmail.json`, refresh | [`runbooks/gmail-oauth-revoked.md`](runbooks/gmail-oauth-revoked.md) · [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md) |
| Probe fail i live | samma som fetch(limit=1) | Oscar secrets UI + logs |
| Fel market/order | mailbox `market` + Woo `domain=` | [`WOO_WORDPRESS.md`](WOO_WORDPRESS.md) |

## Relaterat

- Cases-flöde: [`CASES.md`](CASES.md)
- Gmail OAuth: [`V2_OAUTH_GMAIL.md`](V2_OAUTH_GMAIL.md)
- Pilot / soak: [`PILOT_OPS.md`](PILOT_OPS.md)
