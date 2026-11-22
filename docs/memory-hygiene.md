# Minneshygien

**Purpose:** Regler för vad agenter får spara i OpenClaw-minne och i Azom-dokument.  
**Audience:** Coding agents och Oscar.  
**Read this first:** [`CURRENT_STATE.md`](CURRENT_STATE.md), [`../SOUL.md`](../SOUL.md), [`../AGENTS.md`](../AGENTS.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Dagligt minne** | `memory/YYYY-MM-DD.md` i OpenClaw-workspacet. |
| **Långt minne** | `MEMORY.md` i samma workspace. Filen finns inte förrän någon skapar den. |
| **Arkiv** | Flytta eller märk posten som arkiverad. Radera inte. |

## Vad som ska sparas

- Bestående preferenser, till exempel språk och godkännandekrav.
- Aktiva projekt och vem som äger ett mänskligt steg.
- Beslut som fortfarande styr drift, med datum och källa.

## Vad som ska arkiveras

- Utgångna modellval när config har ett nyare val.
- Felsökning som redan är löst.
- Dagliga anteckningar som inte är driftfakta.
- Personliga eller känsliga uppdrag som inte behövs för support.

Förslag skrivs i [`memory-cleanup-proposals.md`](memory-cleanup-proposals.md). Ingen agent arkiverar eller raderar dem utan mänskligt ja.

## Hur ofta

- Vid en uppgradering: gå igenom dagfiler och förslagslistan.
- Ungefär var 30:e dag: Oscar eller operatören tittar på listan.
- Efter en OAuth-incident: kontrollera att inget minne innehåller token eller authorization code.

## Gamla modellval

OpenClaws lokala primärmodell står i config, inte i minnet. Azoms route-ordning står i [`MODEL_ROUTER.md`](MODEL_ROUTER.md). En gammal anteckning om OpenRouter som enda väg är arkiv, inte en order.

## OAuth-minnen

Spara ägare och statusord: `needs_reauth`, refresh owner `azom` eller `grok-build`. Spara aldrig refresh-token, access token, authorization code eller innehållet i Grok Builds `auth.json`.

## Projektstatus

`docs/CURRENT_STATE.md` vinner över minne. Soak och FU9 är inte klara förrän den filen säger det, och bara Oscar och Jonatan får markera soak.

## Personuppgifter

Spara inte kundmejl, adress eller orderinnehåll i minnesfiler. Ett ärende-id räcker när en människa behöver följa upp. Kundmail skickas inte från minnesunderhåll.
