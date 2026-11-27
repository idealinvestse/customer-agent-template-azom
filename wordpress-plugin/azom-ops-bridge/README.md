# Azom Ops Bridge (WordPress Companion Plugin)

**Purpose:** Lightweight, fail-closed WordPress plugin providing real runtime health, multilingual translation lookup (WPML/Polylang/TranslatePress), cache purge, timed maintenance mode, and atomic snapshot/rollback endpoints for `AzomOps-Agent`.

## Installation

1. Build the deterministic ZIP with `python bin/build-wp-bridge-plugin.py --out azom-ops-bridge.zip` (or download from Dashboard → Webbplatser → Ändringar & Återställning).
2. Upload & activate in WordPress Admin (`Plugins → Add New → Upload Plugin`).
3. Add a per-site 64-char secret (and optional IP CIDR allowlist) to `wp-config.php`:
   ```php
   define('AZOM_BRIDGE_SECRET', '<64-char-random-hex>');
   define('AZOM_BRIDGE_ALLOWED_IPS', '<azom-agent-ip>/32');
   ```
4. Set `WP_BRIDGE_SECRET_<SITE_ID>` and `AZOM_WP_BRIDGE_ENABLED=1` on the Azom agent host.
