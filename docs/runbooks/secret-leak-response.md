# Runbook: hemlighet har läckt

**Purpose:** Stoppa spridning av en token, API-nyckel eller OAuth-kod och lämna ett spår utan att kopiera hemligheten igen.  
**Audience:** Oscar.  
**Read this first:** [`model-oauth.md`](model-oauth.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Läcka** | En hemlighet har synts i chat, logg, git, telemetry, UI eller en export. |
| **Felklass** | `auth`, `config`, `rate limit` eller `unavailable`. Använd den, inte råtexten. |

## 1. Stoppa spridning

1. Sluta skriva hemligheten i nya meddelanden, issues och dokument.
2. Om den ligger i ett git-spår som inte är pushat: ta bort raden innan commit. Pusha inte för att "städa".
3. Om den redan är pushad: Oscar roterar hemligheten först. Historikstädning är ett separat beslut.

## 2. Spärra och rotera

| Ägare | Åtgärd |
|-------|--------|
| ChatGPT OAuth | Oscar använder **Koppla från** / **Återanslut** på `/oscar`. Azom roterar inte Grok Builds fil. |
| Grok Build | Den som äger Grok Build-sessionen loggar in igen där. Läs inte `auth.json`. |
| xAI API-nyckel | Oscar byter `XAI_API_KEY` i värdens `.env` och startar om berörda tjänster. |
| OpenRouter | Oscar byter `OPENROUTER_API_KEY`. |
| Gateway-token eller provider-nyckel i OpenClaw-config | Oscar roterar hos leverantören och uppdaterar config via `openclaw secrets`, inte genom att klistra in nyckeln i en chatt. |
| Gmail eller Google Marketing | Egen OAuth-klient. Återanvänd den inte för modellroutern. |

## 3. Sök utan att sprida

Sök efter route-id, felklass och filnamn. Sök inte genom att klistra in den läckta strängen i ett kommando som loggas.

Kontrollera därefter att träffarna är platsen, inte ett nytt exemplar av hemligheten i rapporten.

## 4. Meddela

Säg till Oscar vad som läckt (typ, inte värdet), var det syntes och om det är roterat. Jonatan får veta om kunddata ingick, utan att få själva hemligheten.

## 5. Dokumentera

Skriv datum, typ av hemlighet, yta, ägare och att värdet inte sparades i rapporten. Länka hit. Markera inte soak eller FU9 som klart.

## 6. Hindra upprepning

- Inga tokens i Markdown, tester eller exempel.
- Telemetry ska ha `route_id`, modell och usage, inte authorization headers.
- Doctor har varnat när `openclaw.json` har hemligheter i klartext. Migrera dem till SecretRefs när Oscar har ett underhållsfönster. Gör det inte mitt i en incident om gateway redan är nere.
