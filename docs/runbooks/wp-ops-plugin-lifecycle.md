# Driftmanual: Säker plugin-livscykel, autouppdateringar, WSOD-återställning och MU-plugins

**Syfte:** Steg-för-steg-guide för Oscar att inventera, planera och köra säkra pluginuppdateringar (`plu-*`), hantera autouppdateringspolicyer (`pau-*`), återställa eller karantänsätta trasiga plugins vid PHP-krasch/WSOD (`plr-*`) samt hantera MU-plugins och drop-ins på registrerade WordPress/WooCommerce-sajter.  
**Målgrupp:** Oscar (full admin) och driftsansvarig.  
**Läs detta först:** [`WP_LIVE_BRIDGE_ARCHITECTURE.md`](../WP_LIVE_BRIDGE_ARCHITECTURE.md), [`wp-ops-bridge-and-deploy.md`](wp-ops-bridge-and-deploy.md), [`WORDPRESS_OPS.md`](../WORDPRESS_OPS.md) och [`skills/wordpress-plugin-ops/SKILL.md`](../../skills/wordpress-plugin-ops/SKILL.md).

## 1. Förvillkor och fail-closed miljöflaggor (`.env`)

Alla skrivande och live-exekverande pluginfunktioner är **avstängda som standard** och skyddade i `PROTECTED_OVERLAY_KEYS` (kan aldrig slås på via webbpanelens inställningar). Sätt dem endast explicit i `/opt/azom-agent/.env` tillsammans med `AZOM_USE_MOCK=0`:

| Miljövariabel | Funktion | Standard |
|---|---|---|
| `AZOM_USE_MOCK` | `1` = deterministiskt mockläge, `0` = live-behörig | `1` |
| `AZOM_WP_BRIDGE_ENABLED` | Tillåter live HMAC-anrop till `azom-ops-bridge` (`azom-ops/v1`) | `0` |
| `AZOM_WP_PLUGIN_UPDATES_ENABLED` | Tillåter exekvering av godkända `plu-*`-uppdateringsplaner | `0` |
| `AZOM_WP_AUTOUPDATE_WRITE_ENABLED` | Tillåter exekvering av godkända `pau-*`-autouppdateringsplaner | `0` |
| `AZOM_WP_RESTORE_ENABLED` | Tillåter exekvering av godkända `plr-*`-återställningar och karantän | `0` |
| `AZOM_WP_DEPLOY_SSH` | Aktiverar dedikerat `*-wp-deployer`-SFTP-konto för out-of-band WSOD-räddning | `0` |
| `AZOM_WP_LIVE_DEPLOY_ENABLED` | Aktiverar `DeployOrchestrator` för verifierade arbetsytor / MU-plugins | `0` |
| `AZOM_WP_DEPLOY_KILL` | Globalt nödstopp för bridge-skrivning, deploy och återställning | ej satt (`0`) |
| `AZOM_WP_PLUGIN_MUTATE_KILL` | Plugin-specifikt nödstopp för uppdateringar, autouppdateringar och karantän | ej satt (`0`) |

> **Säkerhetsgarantier:**
> - `*-wp-reader` (`SshFileTransport`) är alltid 100 % skrivskyddat (`SshWriteDisabled`).
> - `azom-ops-bridge` är självskyddat och kan **aldrig** inaktiveras, karantänsättas, autouppdateras eller raderas via automatisering.
> - Kritiska e-handelsplugins (`woocommerce`, `klarna-checkout-for-woocommerce`, `klarna-payments-for-woocommerce`, `woo-vipps`, `woocommerce-gateway-stripe`, `svea-checkout-for-woocommerce`, `azom-ops-bridge`) är alltid exkluderade från bulk- och autouppdateringar om inte Oscar uttryckligen godkänner dem.
> - Fri text i Messenger/Telegram (`"ja"`, `"godkänn"`) kör aldrig en uppdatering, autouppdateringsändring eller återställning.

## 2. Köra säkra pluginuppdateringar (enskilt, utvalda, policy eller alla)

### Steg 1 — Inventera plugins, kompatibilitet och overrides

```bash
# Mock-exempel (byt till --actor oscar utan --mock i live när flaggorna är påslagna)
python -m ecom_ops --mock --actor oscar wp plugins-inventory --id mock-staging
```

Kontrollera i svaret (eller i webbpanelen `/wp/<site_id>/plugins`):
- `update_available` / `new_version`, `requires_wp`, `requires_php`, `wc_tested_up_to`, `hpos_compatible`
- `constants.disallow_file_mods` (om `true` nekar WordPress och Bridge filuppdateringar)
- För WooCommerce/betalningsplugins: kör även mallkontroll (`wc_template_overrides_audit` i chatt eller granska temats `woocommerce/`-overrides) före uppdatering.

### Steg 2 — Skapa låst SHA-256-uppdateringsplan (`plu-*`, 12 h TTL)

```bash
# Policybaserad (rekommenderad standard: icke-kritiska först med kanariefågel-ordning)
python -m ecom_ops --mock --actor oscar wp plugins-plan-update --id mock-staging --mode policy --policy non_critical_only

# Enskilt plugin eller urval
python -m ecom_ops --mock --actor oscar wp plugins-plan-update --id mock-staging --mode single --slugs yoast-seo
python -m ecom_ops --mock --actor oscar wp plugins-plan-update --id mock-staging --mode selected --slugs akismet,contact-form-7

# Kritiska Woo/betalningsplugins (kräver explicit --include-critical och Oscar WP_OPS_ADMIN)
python -m ecom_ops --mock --actor oscar wp plugins-plan-update --id mock-staging --mode single --slugs woocommerce --include-critical
```

Granska alltid:
- `plan_id` (`plu-...`) och `plan_sha256`
- `batches` (kanariefågel-ordning: icke-kritiska körs först, kritiska sist; avbryter direkt vid fel)
- `skipped` och `warnings` (major-versionshopp, PHP/WP-krav, saknad HPOS-deklaration)

### Steg 3 — Exekvera med låst hash (Oscar HITL)

Kör via webbpanelen (`/wp/<site_id>/plugins`) eller via CLI med exakt `plan_sha256`:

```bash
python -m ecom_ops --mock --actor oscar wp plugins-update --id mock-staging --plan plu-xxxxxxxxxxxx --expected-sha256 <64-hex>
```

Vid exekvering gör `azom-ops-bridge` per plugin:
1. Skapar en lokal för-backup (`uploads/azom-ops-snapshots/plugin-backups/pbackup-<change_id>-<slug>.zip`) och registrerar SHA-256-snapshot i Change Vault (`category="plugin_update"`).
2. Kör WordPress inbyggda `Plugin_Upgrader`, invaliderar OPcache och tömmer cache.
3. Kör loopback-hälsokontroll — vid 5xx eller fel återställs pluginet automatiskt från ZIP-backupen (`auto_reverted: true`) och kanariekörningen stannar omedelbart.

### Steg 4 — Efterkontroll

- Kontrollera `wp bridge-health --id <site_id>` och `/wp/<site_id>/changes`.
- Vid WooCommerce-/betalningsuppdateringar: verifiera publik produktsida, varukorg och kassa manuellt i webbläsaren.

## 3. Hantera autouppdateringspolicyer (`pau-*`)

1. Inspektera nuvarande policy och drift mot live-sajten (inkl. `auto_update_plugin`-filter i kod som kan köra över databasoptionen):

```bash
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --show
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --drift
```

2. Skapa en låst autouppdateringsplan (`pau-*`):

```bash
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --plan --preset non_critical_only
```

Tillgängliga förval: `disable_all`, `non_critical_only` (rekommenderad standard — stänger av autouppdatering på kritiska Woo/betalningsplugins), `security_only`, `custom`.

3. Granska `filter_warnings` och `plan_sha256`, och applicera sedan som Oscar:

```bash
python -m ecom_ops --mock --actor oscar wp plugins-autoupdate --id mock-staging --execute --plan pau-xxxxxxxxxxxx --expected-sha256 <64-hex>
```

Ändringen loggas med före-/efter-lista i Change Vault (`category="plugin_autoupdate"`).

## 4. Nödåterställning vid trasigt plugin eller WSOD (`plr-*`)

### Steg 1 — Triage och diagnostik

```bash
python -m ecom_ops --mock --actor oscar wp bridge-health --id mock-staging
python -m ecom_ops --mock --actor oscar wp plugins-diagnostics --id mock-staging --log-lines 80
```

Granska även senaste ändringar under `/wp/<site_id>/changes` (eller `wp_change_history` i chatten).

### Fall A — WordPress / `azom-ops-bridge` svarar fortfarande

Använd **Bridge-rollback** från en verifierad Change Vault-post (`chg-...`) eller inaktivera pluginet:

```bash
# 1. Planera rollback mot snapshot
python -m ecom_ops --mock --actor oscar wp plugins-recovery --id mock-staging --slug broken-plugin --action rollback_snapshot --change chg-xxxxxxxxxxxx

# 2. Exekvera med låst hash (kräver WP_OPS_ADMIN + WP_OPS_RESTORE + AZOM_WP_RESTORE_ENABLED=1 i live)
python -m ecom_ops --mock --actor oscar wp plugins-recovery --id mock-staging --slug broken-plugin --action rollback_snapshot --execute --plan plr-xxxxxxxxxxxx --expected-sha256 <64-hex>
```

### Fall B — PHP Fatal / WSOD (`azom-ops-bridge` och wp-admin svarar med HTTP 500)

Använd **out-of-band SFTP-karantän** via dedikerat `*-wp-deployer`-konto (`AZOM_WP_DEPLOY_SSH=1` + `AZOM_WP_RESTORE_ENABLED=1`), som inte kräver att PHP körs:

```bash
# Flytta trasigt plugin ur wp-content/plugins/<slug> -> <slug>.azom-quarantine-<id>
python -m ecom_ops --mock --actor oscar wp plugins-quarantine --id mock-staging --slug broken-plugin --kind plugin

# Eller karantänsätt ett trasigt MU-plugin i wp-content/mu-plugins/
python -m ecom_ops --mock --actor oscar wp plugins-quarantine --id mock-staging --slug broken-mu --kind mu_plugin
```

1. Verifiera att sajten svarar 200 OK igen (`wp bridge-health --id <site_id>`).
2. När orsaken är åtgärdad eller återställd kan pluginet tas tillbaka ur karantän:

```bash
python -m ecom_ops --mock --actor oscar wp plugins-unquarantine --id mock-staging --slug broken-plugin --kind plugin
```

3. Alternativt återställ ändrade filer via Change Vault med exakt hash:

```bash
python -m ecom_ops --mock --actor oscar wp rollback --id mock-staging --change chg-xxxxxxxxxxxx --execute --expected-sha256 <64-hex>
```

## 5. MU-plugins och drop-ins

### Inspektera MU-plugins och drop-ins

```bash
python -m ecom_ops --mock --actor oscar wp plugins-mu --id mock-staging
```

- **Laddningsordning:** `wp-content/mu-plugins/` (alfabetiskt) → aktiva vanliga plugins → aktivt tema.
- **Drop-ins** (`object-cache.php`, `advanced-cache.php`, `db.php`, `maintenance.php`, `fatal-error-handler.php`, `sunrise.php`) visas strikt **skrivskyddat** och får aldrig skrivas över via Azom-verktyg.

### Utveckla, deploya och rulla tillbaka ett MU-plugin

1. Öppna en isolerad `wr-*`-arbetsyta med `kind="mu_plugin"` och använd mallen `mu_plugin_basics` (en fil) eller `mu_plugin_loader` (loader-fil + underkatalog).
2. Kör `wp_workspace_static_checks` (kontrollerar bl.a. `mu_activation_hook_noop`, `unguarded_woo_call` och HPOS) och `wp_workspace_run_checks`.
3. Deploya verifierad plan via webbpanelen `/wp/<site_id>/reviews/<plan_id>` (registreras i Change Vault under `category="mu_plugin_deploy"`).
4. Vid fel: rulla tillbaka via `/wp/<site_id>/changes/<change_id>` eller karantänsätt via `plugins-quarantine --kind mu_plugin`.

## 6. Nödstopp (Kill-Switches)

Stoppa omedelbart all plugin-skrivning i `/opt/azom-agent/.env` och starta om tjänsterna:

```dotenv
AZOM_WP_PLUGIN_MUTATE_KILL=1
# Och för totalt stopp av all WP-deploy/bridge/rollback:
AZOM_WP_DEPLOY_KILL=1
```

Båda flaggorna slår till fail-closed direkt före nätverks- eller SFTP-anrop.
