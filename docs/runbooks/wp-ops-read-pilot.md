# Runbook: läspilot för WordPress (inte aktiverad)

**Purpose:** Checklista för en framtida läsning mot en namngiven sajt. Filen slår inte på någon liveväg.  
**Audience:** Oscar, som ensam godkänner en senare läspilot. Jonatan kan läsa listan.  
**Read this first:** [`../WORDPRESS_OPS.md`](../WORDPRESS_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md), [`wp-ops-backup.md`](wp-ops-backup.md), [`../PILOT_OPS.md`](../PILOT_OPS.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Kapabilitet** | Värdet i registret är bara `available`, `unavailable` eller `unknown`. Saknat värde är `unknown`. `unknown` är inte tillgängligt. |
| **registry_only** | Operatörsetikett, inte ett kodvärde. Fältet lagras och visas. Ingen adapter anropar det. |
| **Publicerad körning** | `check_run.published` är sant bara när serverns policy och den aktuella bas-/arbetshashen matchar och varje obligatoriskt jobb har status `passed` med `executed` sant. |
| **Mock-backup** | Metadata i `wp_ops_state.json`. `green` är falskt. `completion: verified` betyder att digesten matchar, inte att sajtbyte återställts. |

## Nuvarande läge på de syntetiska sajterna

Registret är `config/wp_sites.mock.yaml`. Inget i den filen är en kundsajt.

| Sajt | Miljö | Det som koden faktiskt gör |
|------|--------|----------------------------|
| `mock-shop` | `production` | `pending_updates` är `null` i den publika sajtposten, inte `0`. Backup i översikten har `green` falskt. `restore()` svarar `RestoreNotInMvp`. |
| `mock-staging` | `staging` | `capabilities.staging` är `unavailable`. Drillen tittar på `environment`, inte på den flaggan. En drill mot staging kan därför bokföras som metadata även när flaggan är `unavailable`. |

| Fält | Registervärde | Tolkning i den här fasen |
|------|----------------|--------------------------|
| `hosting_api`, `wp_cli` | `unknown` | `registry_only`. Ingen host-API- eller WP-CLI-adapter körs. |
| `backup`, `restore` | `unknown` | Mock-manifest respektive `RestoreNotInMvp`. Inte verifierad sajtåterställning. |
| `uptime` | `available` | Lokal mock-prob. En träff på `*.example.test` är inte en kundsond. |
| `security_scanner` | `unknown` | Inte konfigurerad. Ingen live-skanning ingår i den här checklistan. |
| `encryption` | `unconfigured` | Digesten är SHA-256 av metadata. Ingen nyckel finns. |
| `offsite` | alltid falskt på mock-manifestet | Se undantaget för agentbackupen nedan. |

## Vad som behöver anslutas innan en läsning

1. En namngiven sajt i registret, med pinnad hostnyckel och ett eget `*-wp-reader`-konto. Oscar godkänner kontot och att `AZOM_WP_FILES_SSH=1` sätts. Mockläge (`AZOM_USE_MOCK=1`) tvingar ändå den lokala trädmocken.
2. PHP på värden om `php_lint` ska räknas. Utan binären är raden `unavailable` / `php_binary_missing`, `executed` falskt.
3. En pinnad lokal WordPress-profil och en webbläsare i sandlådan om `browser_smoke` och `egress_probe` ska räknas. `AZOM_WP_SANDBOX` är av. `bin/fetch-wp-runtime.py` kan hämta WordPress 7.1.5 och, på Windows x86/x64, PHP 8.3.37 till en katalog utanför repot. Hämtningen startar ingen server och slår inte på sandlådan. Linux och macOS avslutas med `php_binary_unpinned` innan någon nedladdning, eftersom PHP inte publicerar en officiell binär för dem. Ingen image dras.
4. En dashboardbas på HTTPS om Telegram ska visa granskningslänken. Länken är en inloggad GET och godkänner inget.

## Vem godkänner

- Oscar (`full_admin`, `WP_OPS_ADMIN`) godkänner läsflaggan, sandlådan och varje stagingåtgärd.
- Jonatan (`viewer`, `WP_OPS_READ`) kan läsa. Han godkänner inte staging eller produktion.
- Agenten (`operator`, `WP_OPS_OPERATE`) kan köra mock-kontroller och en staging-drill. Den kan inte slå på liveflaggor.
- Ett GET, en länk, ett namn i chatten eller ett generiskt ”ja” godkänner ingenting.

## Hur beviset ska se ut

En körning som får öppna förhandsvisning eller lokal staging har:

- `check_run.run_id` på formen `cr-` plus 12 hextecken
- samma `site_id`, `plan_id`, `base_sha256` och `work_sha256` som arbetsytan
- `profile` `wp-review-catalog` och `profile_version` `1`
- `published` sant
- varje obligatoriskt jobb med status `passed`, `executed` sant och `simulated` falskt

Det här är inte det beviset: `timeout`, `unavailable`, `not_applicable`, `cancelled`, `simulated`, ett gammalt `approved_for_staging`, eller `woo_static` med `evidence_class: static_precheck` och `runtime_proof` falskt. `woo_static` är ett statiskt förtest.

Förhandsvisning mot en sandlåda som inte har `state: running` svarar `PreviewBlocked`. En drill som bara kontrollerade metadata är inte ”restore verified”.

## Vad som ska förbli avstängt

- Produktionsåterställning (`RestoreNotInMvp` för varje aktör).
- Produktionsskrivning. POST för produktionsgranskning svarar `BackupNotVerified` och skriver inte.
- Live-SFTP förblir skrivskyddad. Ingen fjärrshell.
- `AZOM_WP_SANDBOX` och `AZOM_WP_FILES_SSH` förblir av tills Oscar sätter dem för en namngiven pilot.
- Betalning, checkout och kundmejl från den här ytan.
- Live-skanning, liveuppdatering och kontakt med en kundsajt. Den här checklistan inför inte det.

## Agentbackup är ett annat flöde

`bin/backup-azom-data.sh` kan rsynca `AZOM_BACKUP_DIR` när `--dest` eller `AZOM_BACKUP_DEST` är satt. Tarbollen kan innehålla `wp_ops_state.json` och `wp_review_plans.json`. Den utesluter `wp_workspaces/`. En plan utan arbetsyta blir `not_resumable` och kan inte återuppta kontroller. Det är inte en sajtåterställning. Kör inte skriptet mot `/var/lib/azom` från den här checklistan.

## Okänt, med ägare

| Fråga | Ägare | Status |
|-------|--------|--------|
| Var den pinnade WordPress-imagen och webbläsarprofilen ska lagras | Oscar | Okänd. Ingen image är dragen i den här sprinten. |
| Om en rsync-destination får innehålla `wp_review_plans.json` | Oscar | Okänd. Skriptet inkluderar filen när destinationen är satt. |
| Om startsidans jobb (`page_review`) ska bära `plan_id` och `run_id` | Oscar | Inte byggt. Jobbet har sajt-id. Fillänkens granskningssida visar körnings-id. |
| Vem som startar om Telegram-boten efter en leverans | Oscar | Den redan körande processen läser inte ny kod förrän en begärd omstart. |

## Gör inte

- Markera läspilot, live soak eller en återställning som klar för att mocksviten är grön.
- Läs produktionsnycklar, `.env` eller backupinnehåll för att bocka av listan.
- Använd ett test som hoppar över `AZOM_WP_RUNTIME_E2E` som bevis för att WordPress eller en webbläsare körts.
