# Pilot-drift — AzomOps

**Purpose:** Daglig drift, dashboard-användning, live soak-checklista och eskalering för Azom-piloten.  
**Audience:** Oscar (host/secrets) och Jonatan (approve). Coding agents får läsa men **får inte** markera soak som klar.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`CASES.md`](CASES.md), [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Daily driver** | Meta Messenger — primär chat för Jonatan. |
| **Backup chat** | Telegram — samma bot-hjärna. |
| **Full power** | Dashboard — draft-redigering, orderpanel, poll, bulk close, settings. |
| **Live soak (A1)** | Mänsklig verifiering på riktig host innan auto-send ens diskuteras. |
| **★ / suggest-approve** | Systemet föreslår approve; människa måste fortfarande bekräfta. |

## Roller i drift

| Vem | Gör |
|-----|-----|
| **Oscar** | Host, secrets, probes, systemd, backup, eskaleringar, experiment-flaggor |
| **Jonatan** | Triage, ★-urval, godkänn & skicka, stäng utan svar när lämpligt |
| **Agent (automation)** | Poll, draft, readiness — **skickar inte** kundmail utan human path |

## Ytor (var gör du vad)

| Yta | Roll | Doc |
|-----|------|-----|
| Messenger | Daglig triage / approve | [`MESSENGER_OPENCLAW.md`](MESSENGER_OPENCLAW.md) |
| Telegram | Backup | [`TELEGRAM_OPENCLAW.md`](TELEGRAM_OPENCLAW.md) |
| Dashboard `/cases` | Full power | denna fil + [`CASES.md`](CASES.md) |
| Dashboard `/onboarding` | Checklist, Gmail connect | — |
| Dashboard `/settings` | Jonatan non-secret | — |
| Dashboard `/oscar` | Secrets, probes, eskaleringar | — |
| CLI | Poll, status, kpis | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) |

### Dashboard — snabbkarta

| Path | Syfte |
|------|--------|
| `/` | Översikt, nav-badges, probe-status |
| `/onboarding` | Wizard: secrets checklist, health, Gmail |
| `/settings` | Non-secret config (Jonatan) |
| `/cases` | Kö, filter `suggest=1`, draft, approve, close |
| `/cases/<id>` | Orderpanel, spara draft, regenerate, approve-confirm |
| `/oscar` | Admin: secrets, probes, resolve escalations |
| `/oauth/gmail/start` | Gmail browser OAuth |
| `/health` | Liveness + poll-readiness (publik) |

Basic Auth: `jonatan` / `DASHBOARD_PASSWORD` · `oscar` / `DASHBOARD_OSCAR_PASSWORD`.  
Mock-lösen `jonatan`/`oscar` gäller **bara** när `AZOM_USE_MOCK=1`.

**Gör:** Bulk close med RBAC/CSRF när det är avsiktligt.  
**Gör inte:** Förvänta dig bulk approve/send — det finns inte med flit.

## Prod-tjänster (systemd)

| Unit | Syfte |
|------|--------|
| `azom-dashboard.service` | Flask 127.0.0.1:8080 |
| `azom-bot.service` | Telegram long-poll |
| `azom-cases-poll.timer` | Cases poll var 5:e minut |
| `azom-daily-brief.timer` | Daglig brief |

Sökvägar: kod `/opt/azom-agent`, data `/var/lib/azom`, loggar `/var/log/azom`, env `/opt/azom-agent/.env`.

## Daglig hälsokoll

```bash
# cwd: /opt/azom-agent (prod) — AZOM_USE_MOCK=0
source .venv/bin/activate
python -m ecom_ops status
curl -sS http://127.0.0.1:8080/health | head
python -m ecom_ops cases list --status open,escalated
python -m ecom_ops kpis --days 7
bash bin/daily-brief-azom.sh
# expect: status/health utan kritiska fel; partial poll → eskalering / last_case_poll.errors
```

Messenger: `/help` `/cases` `/brief`  
Telegram backup: samma kommandon.

## Backup

Känd backup-väg ska finnas för:

- `$AZOM_DATA_DIR/cases.db`
- `$AZOM_DATA_DIR/secrets.env`
- `$AZOM_DATA_DIR/oauth/gmail.json` (om Gmail OAuth används)

Vid korrupt DB: [`runbooks/cases-db-corrupt.md`](runbooks/cases-db-corrupt.md).

## Live soak-checklista (A1 / FU6)

**Ägare:** Oscar (ops) + Jonatan (approve-sample).  
**Status:** **Inte körd på prod.** Agents får inte markera detta som klart.  
**Syfte:** Verifiera suggest-approve, approve&nästa, poll och KPI på riktig (eller staging) host innan auto-send-diskussion.

### Pre-flight

```text
[ ] AZOM_USE_MOCK=0 på prod-host
[ ] TELEGRAM_ALLOWED_CHAT_IDS satt
[ ] TELEGRAM_ACTOR_MAP satt (unmapped nekas när map finns)
[ ] MESSENGER_ALLOWED_PSIDS satt (Jonatan PSID i prod)
[ ] MESSENGER_ACTOR_MAP satt
[ ] MESSENGER_PAGE_ACCESS_TOKEN + APP_SECRET + VERIFY_TOKEN satt
[ ] AZOM_DASHBOARD_PUBLIC_URL satt (HTTPS, utan trailing slash)
[ ] curl /health → messenger.send_enabled: true
[ ] Meta webhook: messages + messaging_postbacks → /webhooks/messenger
[ ] AZOM_AUTO_SEND_KILL=1 valfritt bälte; cases_ai auto_send_enabled: false
[ ] Mailbox / Gmail OAuth OK
[ ] systemd: azom-dashboard, azom-bot, azom-cases-poll.timer active
[ ] Backupväg känd för cases.db + secrets.env
```

### Soak-script (en session)

```bash
cd /opt/azom-agent
source .venv/bin/activate
export AZOM_USE_MOCK=0

python -m ecom_ops status
curl -sS http://127.0.0.1:8080/health | head

python -m ecom_ops cases poll
# expect: created/skipped/errors; partial errors → escalation + last_case_poll.errors

python -m ecom_ops cases list --status open,escalated
python -m ecom_ops kpis --days 7
bash bin/daily-brief-azom.sh
```

### Jonatan — Messenger (primär)

```text
[ ] /cases — kö med ★-antal
[ ] Visa ett ★-ärende — order/market stämmer (SE/NO/DK)
[ ] Godkänn & skicka en gång — status → replied
[ ] Godkänn & nästa om kö finns
[ ] Deep link “Öppna i dashboard” → /cases/{id}?from=messenger
[ ] En regenerate (valfritt) — skickar inte
```

### Jonatan — dashboard / Telegram (backup)

```text
[ ] /cases?suggest=1 — notera n★
[ ] Öppna ★-case — orderpanel visar status/total
[ ] Godkänn & nästa — landar på nästa open
[ ] Telegram: Visa + Godkänn på säkert rutinärende
[ ] En regenerate — skickar inte
```

### ★-kvalitet (n≥10 när volym tillåter)

False-positive suggest på return/billing/abuse måste vara **0**. Vid FP: skapa ticket och **sänk inte** trösklar.

### Efter soak

```text
[ ] python -m ecom_ops kpis --days 7 → median TTA / n_approved
[ ] Fyll outcome-rad nedan med riktiga tal
[ ] Behåll auto_send_enabled: false
```

### Outcome-logg

| date | host | n poll create | n★ sample | FP suggest | TTA median | notes |
|------|------|---------------|-----------|------------|------------|-------|
| 2026-07-29 | _blocked_on: Oscar prod access_ | — | — | — | — | **Soak ej klar.** Oscar + Jonatan måste köra checklistan och ersätta denna rad. |

### Explicit blocker

- **Blocked since:** 2026-07-29  
- **Unblock:** Kör pre-flight + soak på prod; fyll outcome med riktiga tal.  
- **Inte i soak:** Aktivera `auto_send_enabled` (se FU9 i [`CASES.md`](CASES.md)); sänka suggest-confidence utan fixtures.

## Baseline (support-tid)

För att kunna mäta “50% mindre support-tid” behövs en mänskligt ifylld baseline:

| Fält | Exempel |
|------|---------|
| `start_date` | datum då mätning börjar |
| `hours_per_week_or_proxy` | Jonatans supporttimmar/vecka **eller** median `time_to_approve_sec` × veckovolym från `python -m ecom_ops kpis` |
| `source` | var siffran kommer ifrån |
| `notes` | blockers, undantagsveckor |

Siffror får **inte** hittas på av agents. Lägg resultatet i ops-anteckningar / Oscar-ägd lagring efter soak — inte som påhittad “klart”-status i repo.

## Mock soft-soak (utvecklare / agent)

```bash
# cwd: repo-root, AZOM_USE_MOCK=1
bash bin/mock-soak-azom.sh
python -m ecom_ops classify-eval
python -m ecom_ops kpis --days 7
# expect: grönt lokalt — ersätter INTE live soak
```

## Incident-runbooks

| Scenario | Runbook |
|----------|---------|
| Woo webhook avstängd | [`runbooks/woo-webhook-disabled.md`](runbooks/woo-webhook-disabled.md) |
| OpenRouter-budget slut | [`runbooks/openrouter-budget-exhausted.md`](runbooks/openrouter-budget-exhausted.md) |
| Mail-poll fastnar | [`runbooks/mail-poll-stuck.md`](runbooks/mail-poll-stuck.md) |
| Gmail OAuth revoked | [`runbooks/gmail-oauth-revoked.md`](runbooks/gmail-oauth-revoked.md) |
| cases.db korrupt | [`runbooks/cases-db-corrupt.md`](runbooks/cases-db-corrupt.md) |
| Dashboard rate-limit 429 | [`runbooks/dashboard-rate-limited.md`](runbooks/dashboard-rate-limited.md) |

Index: [`runbooks/README.md`](runbooks/README.md).

## Eskalering

| Trigger | Till |
|---------|------|
| abuse / legal / critical | Oscar ticket |
| secrets / OAuth / probe-fel | Oscar UI |
| kod/SSH utanför allowlist | Oscar |
| osäker retur/refund | människa, ingen ★-suggest |

## Relaterat

- Install: [`AUTO_INSTALL.md`](AUTO_INSTALL.md) · [`DEPLOY_UBUNTU24_HETZNER.md`](DEPLOY_UBUNTU24_HETZNER.md)
- Mail: [`MAIL_PROVIDERS.md`](MAIL_PROVIDERS.md)
- Compliance: [`COMPLIANCE.md`](COMPLIANCE.md)
