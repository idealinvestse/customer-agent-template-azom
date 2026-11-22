# Förslag: minnesstädning

**Purpose:** Förslagslista. Inget i den här filen är utfört.  
**Audience:** Oscar.  
**Read this first:** [`memory-hygiene.md`](memory-hygiene.md).

Inga minnen, databaser eller workspace-filer har raderats av uppgraderingen.

| Fil | Post | Varför den är föråldrad | Rekommenderad åtgärd | Risk | Mänskligt ja |
|-----|------|-------------------------|----------------------|------|----------------|
| `C:\Users\Django\clawd\memory\2026-07-11.md` | Daganteckning 2026-07-11 | Inte ett driftfaktum. Beskriver ett vuxet rollspelsuppdrag och en minnesflush. Ska inte styra senare beslut. | Arkivera till `memory/archive/` med samma filnamn. Radera inte. | Låg för drift. Texten kan vara privat. | Ja |
| `C:\Users\Django\clawd\docs\LLM_MODEL_CATALOG.md` | Katalog genererad 2026-06-28 | Stor genererad lista. Primärmodellen kan ha ändrats i config sedan dess. | Regenerera med `node scripts/scan-cloud-llm-models.mjs` när operatören vill. Inte handredigera. | Medel: en gammal katalog kan få agenten att välja en modell som inte är laddad. | Ja, innan regenerering |
| `C:\Users\Django\.openclaw\agents\main\sessions\*.jsonl` | 74 föräldralösa transkript enligt doctor | Inte refererade av `sessions.json`. | Låt `openclaw doctor` arkivera dem i ett underhållsfönster. Inte i den här körningen. | Låg om de är föräldralösa. Medel om doctor har fel. | Ja |
| `C:\Users\Django\clawd-onyx\` | Oanvänd workspace-kopia | Inte `agents.defaults.workspace`. Standardmall, inga egna minnen. | Lämna kvar. Märk som arkiv. Radera inte. | Låg. | Ja, om någon senare vill ta bort katalogen |
| Azom-repo | Ingen `MEMORY.md` | Saknas. Inget långt minne att rensa. | Skapa inte en tom fil. | Ingen. | Nej |

OpenClaw-config med modellistor och hemligheter i klartext är inte ett minnesdokument. Den rörs inte här. Se [`runbooks/secret-leak-response.md`](runbooks/secret-leak-response.md).
