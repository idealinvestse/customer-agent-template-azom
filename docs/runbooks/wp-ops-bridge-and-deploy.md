# Driftmanual: Azom Ops Bridge, Change Vault och säker live-deploy

**Syfte:** Steg-för-steg-guide för Oscar att aktivera WordPress-tillägget `azom-ops-bridge`, valfritt härdat deploy-SFTP-konto (`*-wp-deployer`) och per-ändring-återställning i webbpanelen (`/wp/<site_id>/changes`).  
**Målgrupp:** Oscar (full admin) och driftsansvarig.  
**Läs detta först:** [`WP_LIVE_BRIDGE_ARCHITECTURE.md`](../WP_LIVE_BRIDGE_ARCHITECTURE.md) och [`wordpress-plugin/azom-ops-bridge/README.md`](../../wordpress-plugin/azom-ops-bridge/README.md).

## Översikt

- Befintligt läs-SSH (`*-wp-reader` / `wp_ssh.json`) förblir **100 % skrivskyddat**.
- Normala tema-/pluginändringar, översättningsläsning (WPML/Polylang/TranslatePress), cache-rensning och hälsokontroller går via WordPress-pluginet **`azom-ops-bridge`** (`azom-ops/v1`, HMAC-SHA256).
- Om en PHP-ändring orsakar WSOD (kritiskt fel där WordPress REST slutar svara) kan ett separat **out-of-band SFTP-konto (`*-wp-deployer`)** återställa filerna direkt på disk.
- Varje ändring via bot eller webbpanel sparas före och efter med SHA-256 i **Change Vault** (`/wp/<site_id>/changes`).

## 1. Bygg och verifiera pluginpaketet

```bash
# cwd: repo root (dev) eller /opt/azom-agent (prod)
python bin/build-wp-bridge-plugin.py --check
# förväntat resultat: JSON med "ok": true och 0 otillåtna PHP-funktioner
```

Du kan bygga `azom-ops-bridge.zip` lokalt med `python bin/build-wp-bridge-plugin.py --out azom-ops-bridge.zip` eller ladda ner den direkt som Oscar via webbpanelen (`/wp/<site_id>/changes` → **Ladda ner plugin (.zip)**).

## 2. Installera `azom-ops-bridge` på WordPress-sidan

1. Ladda upp och aktivera `azom-ops-bridge.zip` i *wp-admin → Tillägg → Lägg till nytt*.
2. Generera en 64-teckens hemlighet på Azom-värden (`python -c "import secrets; print(secrets.token_hex(32))"`).
3. Lägg in hemligheten ovanför `"That's all, stop editing!"` i sidans `wp-config.php`:

```php
define('AZOM_OPS_BRIDGE_SECRET', '<64-teckens-hemlighet>');
```

4. Verifiera att pluginet inte loggar några hemligheter i `debug.log` (hemligheten hämtas enbart från konstanten eller `AZOM_OPS_BRIDGE_SECRET` i PHP-miljön).

## 3. Aktivera Bridge i Azoms processmiljö (`.env`)

Sätt aldrig hemligheter i repo-filer eller webbformulär för live-flaggor. Lägg till i `/opt/azom-agent/.env` (med `AZOM_USE_MOCK=0`):

```dotenv
WP_BRIDGE_SECRET_AZOM_NO=<samma-64-teckens-hemlighet>
AZOM_WP_BRIDGE_ENABLED=1
```

Verifiera read-only hälsokontroll i mock eller live:

```bash
python -m ecom_ops --actor oscar wp bridge-health --id azom-no
# förväntat resultat: "ok": true med wordpress_version, woocommerce_version, php_version, multilingual
```

> **Obs:** Pluginet skapar automatiskt databas-tabellen `<prefix>azom_ops_nonces` för HMAC replay-skydd och använder WordPress-optionen `azom_ops_maintenance_until` för tidsbegränsat underhållsläge. Vid avinstallation via wp-admin rensar `uninstall.php` både tabellen och optionen. `AZOM_WP_DEPLOY_KILL=1` stoppar nu även `bridge-cache-flush` och `bridge-maintenance` utöver deploy och rollback.

## 4. Valfritt: Härdat `*-wp-deployer` SFTP-konto (WSOD-skydd)

För out-of-band fildeploy och återställning om PHP skulle krascha:

1. Skapa ett dedikerat Linux-konto som slutar på **`-wp-deployer`** (t.ex. `azom-wp-deployer`) med egen SSH-nyckel (aldrig samma nyckel som `*-wp-reader`).
2. Begränsa kontot i `/etc/ssh/sshd_config`:

```sshconfig
Match User *-wp-deployer
    ForceCommand internal-sftp
    ChrootDirectory /var/www/azom
    PermitTunnel no
    AllowTcpForwarding no
    X11Forwarding no
    PasswordAuthentication no
```

3. Öppna webbpanelen `/wp/<site_id>/ssh`, fyll i sektionen **Deploy SFTP (`*-wp-deployer`)** och spara den separata privata nyckeln (sparas isolerat i `wp_deploy_ssh.json` och `AZOM_DATA_DIR/ssh-deploy-keys/<site_id>`).
4. Aktivera i `/opt/azom-agent/.env`:

```dotenv
AZOM_WP_DEPLOY_SSH=1
```

## 5. Slå på live-deploy och återställning (Oscar)

Båda spärrarna är avstängda som standard (`0`):

```dotenv
AZOM_WP_LIVE_DEPLOY_ENABLED=1
AZOM_WP_RESTORE_ENABLED=1
```

- Öppna en verifierad plan under `/wp/<site_id>/reviews/<plan_id>` och använd **Deploya till live** (kräver låst `base_sha256` + `work_sha256` samt passerade kontroller).
- Öppna `/wp/<site_id>/changes` för att se alla Woo/WP/fil-ändringar, klicka på en post → **Verifiera snapshot** → **Torrkör återställning** → **Återställ ändring**.
- Återställning via CLI kräver också exakt hash:

```bash
python -m ecom_ops --mock --actor oscar wp rollback --id mock-staging --change chg-xxxxxxxxxxxx --execute --expected-sha256 <64-hex>
```

## 6. Nödstopp (Kill-Switch)

Om något oväntat händer, stoppa omedelbart all live-deploy, bridge-skrivning, deploy-SFTP och återställning genom att sätta i `.env` och starta om tjänsterna:

```dotenv
AZOM_WP_DEPLOY_KILL=1
```

När `AZOM_WP_DEPLOY_KILL=1` är satt vägrar systemet fail-closed med `DeployKillSwitchActive` / `BridgeDisabled` / `SshDeployDisabled` oavsett andra miljöflaggor.
