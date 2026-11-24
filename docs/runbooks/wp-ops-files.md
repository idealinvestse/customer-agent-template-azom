# Runbook: Redigera en tema- eller pluginfil

**Purpose:** Ändra en befintlig fil inne i ett tema eller en plugin utan att öppna ett fritt skal.  
**Audience:** Oscar för produktion. Operator får föreslå och skriva på staging. Jonatan kan läsa.  
**Read this first:** [`../WORDPRESS_OPS.md`](../WORDPRESS_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Typad sökväg** | Azom räknar ut sökvägen från sajt, tema eller plugin, och relativ fil. Ingen kommandosträng. |
| **Förslag** | Ett plan-id och två digest. Filen skrivs inte. Brödtexten sparas inte. |
| **Skrivning** | Samma text skickas igen. Filen måste fortfarande matcha den första digesten. |
| **`.azom-bak`** | En kopia bredvid filen på samma maskin. Den är inte off-site och ersätter inte en backup. |

## Gör / gör inte

**Gör:**

- Låt `AZOM_USE_MOCK=1` vara på tills Oscar har godkänt en namngiven värd.
- Sätt `AZOM_WP_FILES_SSH=1` bara i processmiljön, inte via dashboardens env-overlay, och först när det dedikerade SFTP-kontot är klart och verifierat.
- Håll filen till `.php`, `.css`, `.js`, `.json`, `.txt` eller `.html` inne i temat eller pluginen.
- Läs förslaget och skicka samma text till `file-apply`.

**Gör inte:**

- Skicka `sed`, `vim` eller ett eget SSH-kommando. Live-transporten använder SFTP utan fjärrskal.
- Peka `ssh_host` i registret mot en kundsajt utan ett avgränsat godkännande. Värdar som slutar på `.example.test` kopplas inte upp.
- Skapa en ny fil via CLI, radera en fil, eller redigera `wp-config.php` den här vägen. En chattplan för en ny fil kräver att föräldrakatalogen redan finns och att bas-hashen är tomfilens hash.
- Behandla `.azom-bak` som en off-site-backup eller som en produktionsåterställning.
- Markera fas 4, live soak eller managed hosting som klart.

## SSH-inställningar

Öppna webbplatsen och länken SSH-inställningar.

- Rot på servern och värdnamn sparas för den sajten. Ett tomt fält, eller samma värde som registret, ärver `config/wp_sites.yaml`. Roten måste ha minst tre led. Den får inte börja med `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/tmp`, `/run`, `/var/log`, `/var/lib`, `/var/run` eller `/var/spool`.
- Ange ett dedikerat användarnamn som slutar på `-wp-reader` och port per sajt. WordPress SFTP ärver inte `SSH_USER` eller `SSH_IDENTITY_FILE`; använd aldrig `oscar` eller `root` för filtransporten. `SSH_HOST`, `SSH_USER` och `SSH_IDENTITY_FILE` är andra SSH-integrationers inställningar.
- WordPress SFTP använder inte globala SSH-nycklar. Andra SSH-integrationers globala inställningar ändras inte från den här körboken.
- Klistra in den privata nyckeln en gång. Den får inte ha en lösenfras. Sidan visar sedan bara att nyckeln är sparad. En tom ruta ändrar den inte. Kryssrutan tar bort den. Endast den lagrade per-site-nyckeln används för WordPress SFTP.
- Klistra in värdens publika nyckel, en rad. Oscar ser fingeravtrycket SHA256. Jonatan ser bara om den är satt. Live SSH vägrar utan den, innan någon socket öppnas.
- Livefilåtkomst använder endast den lagrade per-site-nyckeln och en SSH-användare vars namn slutar på `-wp-reader`. Kontot måste vara separat från `oscar`.
- Servern måste tvinga `internal-sftp -R`, neka lösenord, skal, PTY och forwarding för kontot. Ge det bara läs- och traverseringsrätt till godkända tema-/pluginmappar. Ta bort symlänkar som pekar utanför dessa mappar och använd begränsad chroot när serverns kataloglayout tillåter det. Verifiera effektiv åtkomst som tjänstekontot; det får inte kunna läsa `wp-config.php`, backup eller credentials.
- Botten ansluter till SFTP-subsystemet, kontrollerar pinnad host key före autentisering, avvisar symlänkar och läser högst 64 KiB per fil och 256 poster per katalog. Den begär aldrig ett fjärrskal.
- Live-SFTP är skrivskyddad. `file-apply` vägras på live-SSH; mockflödet fortsätter stödja verifiering av förslag och apply.
- Lösenord skickas inte. Formuläret öppnar inte SSH och kan inte slå på `AZOM_WP_FILES_SSH`.
- `bin/backup-azom-data.sh` tar inte med `ssh-keys/` eller `wp_file_payloads/`. En vanlig tarball innehåller inte den privata nyckeln och inte diffens brödtext.
- Jonatan ser rot, värd, användare, port och om en nyckel finns. Jonatan ser inte sökvägen till identitetsfilen och har inget formulär. Både staging och produktion kräver Oscar för att spara.
- En trasig rad i `wp_ssh.json` hoppas över för den sajten. Övriga sajter laddas. En oläsbar fil stoppar ytan med texten `wp_ssh.json kan inte läsas.`

## 1. Läs filen

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock --actor jonatan wp files --id mock-staging --kind theme --slug twentytwentyfive
# expect: ok true, live_calls false, ssh false
```

Dashboard: öppna webbplatsen och länken Tema- och pluginfiler. Själva webbplatssidan läser inga filer.

## 2. Föreslå

Skriv den nya texten i en lokal fil. Lägg inte texten i argv.

```bash
python -m ecom_ops --mock --actor agent wp file-propose --id mock-staging --kind theme --slug twentytwentyfive --path style.css --from ./edit.css
# expect: ok true, executed false, ett plan-id fe- och ingen brödtext i JSON
```

Produktion kräver Oscar (`WP_OPS_ADMIN` och `CODE_EDIT`). Staging kräver operator.

## 3. Skriv

```bash
python -m ecom_ops --mock --actor agent wp file-apply --id mock-staging --plan fe-aaaaaaaaaaaa --from ./edit.css
# expect: ok true, executed true, live_calls false
# expect: filen matchar texten, och en syskonfil med suffixet .azom-bak finns
```

Om filen ändrats efter förslaget blir svaret `StaleFile` och filen lämnas orörd. Om texten inte matchar förslaget blir svaret `DigestMismatch`.

## 4. Diffplan i chatt och panel

Chatten kan föreslå en namngiven diff. Planen sparar hashar. Brödtexten ligger i `AZOM_DATA_DIR/wp_file_payloads/<plan_id>`, inte i chatten, auditen eller plan-JSON.

- Oscar granskar diffen på `GET /wp/<site_id>/files/plan/<plan_id>`. Jonatan får 403.
- Staging skrivs bara av knappen `wpfile:stage:{plan_id}` och bara mot det lokala mockträdet. `action:yes` och fri text «ja» skriver inte filen.
- `POST /wp/<site_id>/files/deploy` kräver Oscar, CSRF, plan-id, båda hasharna, sökväg och miljö. Svaret är `BackupNotVerified`. Ingen produktionsfil ändras.
- `wp_rollback_file` och `restore()` svarar `RestoreNotInMvp`.
- En plan utan payload, eller en plan äldre än 12 timmar, kan inte tillämpas. Raderna raderas inte av utgången.
- WooCommerce-kärnan, ett känt föräldratema och hög risk (kassa, varukorg, betalning, gateway) stoppas före skrivning.
- Detta är inte fas 4, inte en verifierad backup och inte en live-skrivning. `AZOM_WP_FILES_SSH` ska vara av.

## 5. Verifiera

- Audit-raden `wp_ops_file_edit` har sökväg, längd och digest. Den har inte filens text.
- `restore()` svarar fortfarande `RestoreNotInMvp`.
- En grön status är inte tecknet på att redigeringen lyckades. Dashboarden visar förslaget och skrivningen med uppmärksamhet, inte som ok.
