# Compliance & GDPR — AzomOps Agent

**Senast uppdaterad:** 2026-07-17 · **Ansvarig:** Oscar (full_admin)

## Behandlingsregister (Art 30)

| Behandling | Syfte | Rättslig grund | Lagringstid | Plats |
|------------|-------|----------------|-------------|-------|
| Kundmail (inbound) | Support-ärendehantering | Legitimt intresse (Art 6(1)(f)) | 90 dagar efter close | `/var/lib/azom/cases.db` (Hetzner EU) |
| Kundmail-utkast (LLM) | Draft-generering | Legitimt intresse | 90 dagar efter close | Som ovan |
| Order-data (Woo API) | Order-sanning i svar | Legitimt intresse | Ephemeral (ej lagrat) | Woo-host (kundens infra) |
| Telemetry (användning) | Budget/KPI-mätning | Legitimt intresse | 90 dagar raw, 12 mån aggr | `/var/lib/azom/telemetry.jsonl` |
| Audit-log | Spårbarhet | Legitimt intresse | 12 mån | `/var/lib/azom/audit.jsonl` |
| OAuth-tokens (Gmail) | Mail-anslutning | Samtycke (användare) | Tills revoke | `/var/lib/azom/oauth/gmail.json` |

## Dataresidency & tredjepartsöverföring (P8.4)

### OpenRouter (LLM)
- **Mottagare:** OpenRouter Inc. (US-baserad)
- **Data överfört:** Kundmeddelande (upp till 4000 tecken) + order-kontext för classify/draft
- **Rättslig grund:** Legitimt intresse — nödvändigt för AI-draft-generering
- **Skydd:** TLS-transit + OpenRouter's data-retention policy (se deras DPA)
- **Risk:** Schrems II — överföring till US kräver SCC (Standard Contractual Clauses)
- **Rekommendation:** Teckna DPA med OpenRouter; överväg EU-resident model-provider (t.ex. Mistral EU) för framtida pilot
- **Mitigering:** Kundmeddelande trunkeras till 4000 tecken; inga personuppgifter i system-prompt; telemetry redacts secrets

### Telegram (bot-notiser)
- **Mottagare:** Telegram (Dubai-baserad)
- **Data överfört:** Eskalerings-sammanfattning (max 300 tecken), budget-alarm, KPI-sammanfattning
- **Inga kund-PII skickas via Telegram** — endas metadata + sammanfattningar

### WooCommerce / WordPress
- **Mottagare:** Kundens egen infra (azom.se/no/dk)
- **Data:** Order-status, produkt-data — hämtas ephemeral, ej lagrat lokalt

## Rättigheter för registrerade (Art 15–22)

| Rättighet | Implementering |
|-----------|----------------|
| **Insyn (Art 15)** | `GET /oscar/gdpr/export?email=...` (Oscar-admin) |
| **Radering (Art 17)** | `POST /oscar/gdpr/delete` (Oscar-admin) + retention-purge timer (90 dagar) |
| **Rättelse (Art 16)** | Manuell via Oscar — ändra i cases.db |
| **Portabilitet (Art 20)** | `GET /oscar/gdpr/export` returnerar JSON |
| **Invändning (Art 21)** | Manuell — stoppa poll för mailbox |
| **Automatiserat beslut (Art 22)** | Suggest-approve = human confirm, aldrig auto-send default |

## DPIA (P8.6)

En Data Protection Impact Assessment rekommenderas för:
1. AI-behandling av kundmail (classify + draft) — se ovan
2. Bulk-radering vid retention-purge — verifieras via dry-run
3. Auto-send-experiment (om aktiveras) — kräver särskild DPIA + samtycke

**Status:** Initial DPIA-dokumentation i denna fil. Fullständig DPIA bör kompletteras vid pilot-start.

## Samtycke (P8.5)

Kundmail behandlas baserat på legitimt intresse (Art 6(1)(f)) — Azom har legitimt intresse av att hantera support. Samtycke krävs ej för grundläggande support, men:

- **Auto-send-experiment** (om aktiveras) kan kräva uttryckligt samtycke enligt Art 22
- **AI-behandling** bör kommuniceras i Azoms integritetspolicy
- **Gmail OAuth** kräver användarens samtycke (OAuth-flow)

## Tekniska åtgärder

- **Kryptering i transit:** TLS för alla API-anrop (Woo, OpenRouter, Telegram, Gmail)
- **Kryptering i vila:** OAuth-tokens chmod 600; secrets.env chmod 600; DB på krypterad disk (Hetzner)
- **Åtkomstkontroll:** RBAC (Jonatan viewer, Oscar full_admin), Basic Auth + rate-limit
- **Audit-log:** Alla skriv-åtgärder loggas med actor (`/var/lib/azom/audit.jsonl`)
- **Secret redaction:** `redact_secrets()` + explicit `SECRET_ENV_KEYS`-lista
- **Retention:** 90 dagar för cases + telemetry; 12 mån för audit-log

## Incident-respons

Vid personuppgiftsincident (t.ex. data-läcka):
1. Isolera systemet (stoppa tjänster)
2. Dokumentera incident (vad, när, vilka personer berörs)
3. Notifiera Azom ansvarig (Oscar) inom 24h
4. Bedöm om anmälan till Datainspektionen krävs (72h-gräns enligt Art 33)
5. Dokumentera åtgärder i `docs/runbooks/`
