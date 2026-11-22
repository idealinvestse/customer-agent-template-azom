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
- Sätt `AZOM_WP_FILES_SSH=1` bara i processmiljön, inte via dashboardens env-overlay, och bara när mock är av.
- Håll filen till `.php`, `.css`, `.js`, `.json`, `.txt` eller `.html` inne i temat eller pluginen.
- Läs förslaget och skicka samma text till `file-apply`.

**Gör inte:**

- Skicka `sed`, `vim` eller ett eget SSH-kommando. Den vanliga allowlisten vägrar dem.
- Peka `ssh_host` i registret mot en kundsajt utan ett avgränsat godkännande. Värdar som slutar på `.example.test` kopplas inte upp.
- Skapa en ny fil, radera en fil, eller redigera `wp-config.php` den här vägen.
- Behandla `.azom-bak` som en off-site-backup eller som en produktionsåterställning.
- Markera fas 4, live soak eller managed hosting som klart.

## SSH-inställningar

Öppna webbplatsen och länken SSH-inställningar.

- Rot på servern och värdnamn sparas för den sajten. Ett tomt fält, eller samma värde som registret, ärver `config/wp_sites.yaml`. Roten måste ha minst tre led. Den får inte börja med `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/tmp`, `/run`, `/var/log`, `/var/lib`, `/var/run` eller `/var/spool`.
- Användare, port och en lokal sökväg till en nyckel kan sättas per sajt. Tomt fält ärver `SSH_USER`, `SSH_PORT` och `SSH_IDENTITY_FILE`; det finns ingen standardanvändare. Användaren måste vara explicit och får inte vara `root`. Saknad eller root-användare stoppas innan ssh-processen startar. `SSH_HOST` är den andra SSH-klienten, inte filtransportens värd.
- Den globala reserven skrivs till `runtime.env` bara när fältet har ett värde. En rad i `secrets.env` med samma namn används före den.
- Klistra in den privata nyckeln en gång. Den får inte ha en lösenfras. Sidan visar sedan bara att nyckeln är sparad. En tom ruta ändrar den inte. Kryssrutan tar bort den. Den sparade nyckeln används före den lokala sökvägen.
- Klistra in värdens publika nyckel, en rad. Oscar ser fingeravtrycket SHA256. Jonatan ser bara om den är satt. Live SSH vägrar utan den, innan någon socket öppnas.
- Lösenord skickas inte. Formuläret öppnar inte SSH och kan inte slå på `AZOM_WP_FILES_SSH`.
- `bin/backup-azom-data.sh` tar inte med `ssh-keys/`. En vanlig tarball innehåller inte den privata nyckeln.
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

## 4. Verifiera

- Audit-raden `wp_ops_file_edit` har sökväg, längd och digest. Den har inte filens text.
- `restore()` svarar fortfarande `RestoreNotInMvp`.
- En grön status är inte tecknet på att redigeringen lyckades. Dashboarden visar förslaget och skrivningen med uppmärksamhet, inte som ok.
