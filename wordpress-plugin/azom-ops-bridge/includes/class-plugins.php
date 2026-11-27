<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Plugins {
    private const BRIDGE_SLUG = 'azom-ops-bridge';
    private const CRITICAL_PLUGIN_SLUGS = [
        'woocommerce',
        'klarna-checkout-for-woocommerce',
        'klarna-payments-for-woocommerce',
        'woo-vipps',
        'woocommerce-gateway-stripe',
        'svea-checkout-for-woocommerce',
        'azom-ops-bridge',
    ];
    private const MAX_PLUGIN_BACKUPS = 30;
    private const MAX_DEBUG_LOG_BYTES = 16384;
    private const DEFAULT_LOG_LINES = 80;
    private const MAX_LOG_LINES = 300;
    private const MAX_HOOKS_QUERY = 25;
    private const DEFAULT_DIAGNOSTIC_HOOKS = [
        'auto_update_plugin',
        'woocommerce_before_calculate_totals',
        'woocommerce_checkout_process',
    ];

    public static function plugin_backup_dir(): string {
        $snap_root = Azom_Ops_Bridge_Snapshots::snapshot_dir();
        if ($snap_root === '') {
            return '';
        }
        $dir = trailingslashit($snap_root) . 'plugin-backups';
        if (is_link($dir)) {
            return '';
        }
        if (!is_dir($dir) && !wp_mkdir_p($dir)) {
            return '';
        }
        $htaccess = $dir . '/.htaccess';
        if (!is_file($htaccess)) {
            @file_put_contents($htaccess, "Deny from all\n<IfModule mod_authz_core.c>\nRequire all denied\n</IfModule>\n");
        }
        $webconfig = $dir . '/web.config';
        if (!is_file($webconfig)) {
            @file_put_contents($webconfig, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<configuration><system.webServer><authorization><deny users=\"*\" /></authorization></system.webServer></configuration>\n");
        }
        $index = $dir . '/index.php';
        if (!is_file($index)) {
            @file_put_contents($index, "<?php\n// Silence is golden.\n");
        }
        return $dir;
    }

    public static function handle_inventory(WP_REST_Request $request): WP_REST_Response {
        global $wp_version;
        self::load_plugin_admin_includes();

        $include_updates_raw = $request->get_param('include_updates');
        $include_updates = $include_updates_raw === null ? true : !in_array((string) $include_updates_raw, ['0', 'false', 'no'], true);

        $all_plugins = function_exists('get_plugins') ? (array) get_plugins() : [];
        $active_plugins = array_map('strval', (array) get_option('active_plugins', []));
        $network_active_map = is_multisite() ? (array) get_site_option('active_sitewide_plugins', []) : [];
        $auto_update_list = array_map('strval', (array) get_site_option('auto_update_plugins', []));
        $paused_map = function_exists('wp_get_paused_plugins') ? (array) wp_get_paused_plugins() : [];

        $update_responses = [];
        if ($include_updates) {
            $transient = get_site_transient('update_plugins');
            if (!is_object($transient) && function_exists('wp_update_plugins')) {
                @wp_update_plugins();
                $transient = get_site_transient('update_plugins');
            }
            if (is_object($transient) && isset($transient->response) && is_array($transient->response)) {
                $update_responses = $transient->response;
            }
        }

        $hpos_compat_map = self::detect_hpos_compatibility_map();
        $plugin_root = defined('WP_PLUGIN_DIR') ? (string) WP_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'plugins');

        $plugins_out = [];
        foreach ($all_plugins as $plugin_file => $meta) {
            $plugin_file = (string) $plugin_file;
            $meta = is_array($meta) ? $meta : [];
            $slug = self::plugin_slug_from_file($plugin_file);
            $is_network = isset($network_active_map[$plugin_file]);
            $is_active = $is_network || in_array($plugin_file, $active_plugins, true);

            $upd = isset($update_responses[$plugin_file]) && is_object($update_responses[$plugin_file])
                ? $update_responses[$plugin_file]
                : null;
            $new_version = ($upd !== null && !empty($upd->new_version)) ? (string) $upd->new_version : null;
            $update_available = $new_version !== null && $new_version !== (string) ($meta['Version'] ?? '');

            $wc_tested = '';
            $full_main = trailingslashit($plugin_root) . $plugin_file;
            if (function_exists('get_file_data') && is_file($full_main) && !is_link($full_main)) {
                $extra = @get_file_data($full_main, ['wc_tested' => 'WC tested up to']);
                if (is_array($extra) && isset($extra['wc_tested'])) {
                    $wc_tested = trim((string) $extra['wc_tested']);
                }
            }

            $requires_plugins_raw = (string) ($meta['RequiresPlugins'] ?? '');
            $requires_plugins = [];
            if ($requires_plugins_raw !== '') {
                foreach (explode(',', $requires_plugins_raw) as $dep) {
                    $clean_dep = trim($dep);
                    if ($clean_dep !== '') {
                        $requires_plugins[] = $clean_dep;
                    }
                }
            }

            $hpos_compat = null;
            if (isset($hpos_compat_map[$plugin_file])) {
                $hpos_compat = $hpos_compat_map[$plugin_file];
            }

            $plugins_out[] = [
                'slug'             => $slug,
                'plugin_file'      => $plugin_file,
                'name'             => (string) ($meta['Name'] ?? $slug),
                'version'          => (string) ($meta['Version'] ?? ''),
                'active'           => $is_active,
                'network_active'   => $is_network,
                'update_available' => $update_available,
                'new_version'      => $new_version,
                'auto_update'      => in_array($plugin_file, $auto_update_list, true),
                'requires_wp'      => (string) ($meta['RequiresWP'] ?? ($upd !== null && isset($upd->requires) ? (string) $upd->requires : '')),
                'requires_php'     => (string) ($meta['RequiresPHP'] ?? ($upd !== null && isset($upd->requires_php) ? (string) $upd->requires_php : '')),
                'tested_wp'        => $upd !== null && isset($upd->tested) ? (string) $upd->tested : '',
                'wc_tested_up_to'  => $wc_tested,
                'hpos_compatible'  => $hpos_compat,
                'is_paused'        => isset($paused_map[$plugin_file]),
                'is_critical'      => in_array(strtolower($slug), self::CRITICAL_PLUGIN_SLUGS, true),
                'is_bridge'        => self::is_bridge_plugin($plugin_file),
                'requires_plugins' => $requires_plugins,
            ];
        }

        // MU-plugins (WordPress loads them in alphabetical filename order).
        $mu_dir = defined('WPMU_PLUGIN_DIR') ? (string) WPMU_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'mu-plugins');
        $raw_mu = function_exists('get_mu_plugins') ? (array) get_mu_plugins() : [];
        ksort($raw_mu, SORT_STRING);
        $mu_out = [];
        $load_order = 0;
        foreach ($raw_mu as $mu_file => $meta) {
            $load_order++;
            $mu_file = (string) $mu_file;
            $meta = is_array($meta) ? $meta : [];
            $mu_slug = pathinfo($mu_file, PATHINFO_FILENAME);
            $full_mu = trailingslashit($mu_dir) . $mu_file;
            $bytes = (is_file($full_mu) && !is_link($full_mu)) ? @file_get_contents($full_mu) : false;
            $sha = is_string($bytes) ? hash('sha256', $bytes) : '';
            $is_loader = false;
            if (is_dir(trailingslashit($mu_dir) . $mu_slug)) {
                $is_loader = true;
            } elseif (is_string($bytes) && preg_match('/\b(?:require|require_once|include|include_once)\b/i', $bytes) === 1) {
                $is_loader = true;
            }
            $mu_out[] = [
                'slug'       => $mu_slug,
                'file'       => $mu_file,
                'name'       => (string) ($meta['Name'] ?? $mu_slug),
                'version'    => (string) ($meta['Version'] ?? ''),
                'is_loader'  => $is_loader,
                'sha256'     => $sha,
                'load_order' => $load_order,
            ];
        }

        // Drop-ins are strictly read-only inspection.
        $raw_dropins = function_exists('get_dropins') ? (array) get_dropins() : [];
        ksort($raw_dropins, SORT_STRING);
        $dropins_out = [];
        foreach ($raw_dropins as $dropin_file => $meta) {
            $meta = is_array($meta) ? $meta : [];
            $dropins_out[] = [
                'file'        => (string) $dropin_file,
                'name'        => (string) ($meta['Name'] ?? (string) $dropin_file),
                'description' => function_exists('wp_strip_all_tags')
                    ? (string) wp_strip_all_tags((string) ($meta['Description'] ?? ''))
                    : strip_tags((string) ($meta['Description'] ?? '')),
                'read_only'   => true,
            ];
        }

        $constants = [
            'disallow_file_mods'         => defined('DISALLOW_FILE_MODS') && (bool) DISALLOW_FILE_MODS,
            'automatic_updater_disabled' => defined('AUTOMATIC_UPDATER_DISABLED') && (bool) AUTOMATIC_UPDATER_DISABLED,
            'wp_auto_update_core'        => defined('WP_AUTO_UPDATE_CORE') ? WP_AUTO_UPDATE_CORE : null,
        ];

        $autoupdate_filters = self::inspect_hook_callbacks('auto_update_plugin');

        return new WP_REST_Response([
            'ok'                 => true,
            'plugins'            => $plugins_out,
            'mu_plugins'         => $mu_out,
            'dropins'            => $dropins_out,
            'constants'          => $constants,
            'autoupdate_filters' => $autoupdate_filters,
            'wp_version'         => (string) ($wp_version ?? ''),
            'php_version'        => PHP_VERSION,
        ], 200);
    }

    public static function handle_update(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }

        $change_id = trim((string) ($payload['change_id'] ?? ''));
        if (!preg_match('/^[A-Za-z0-9_-]{4,48}$/', $change_id)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change_id'], 400);
        }

        $plugin_file = trim((string) ($payload['plugin_file'] ?? ($payload['plugin'] ?? '')));
        if (!self::is_valid_plugin_file($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid plugin_file'], 400);
        }
        if (self::is_bridge_plugin($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Self-update of azom-ops-bridge is refused.'], 403);
        }
        if (defined('DISALLOW_FILE_MODS') && DISALLOW_FILE_MODS) {
            return new WP_REST_Response(['ok' => false, 'error' => 'DISALLOW_FILE_MODS is enabled on this site.'], 403);
        }

        self::load_plugin_admin_includes();
        $all_plugins = function_exists('get_plugins') ? (array) get_plugins() : [];
        if (!isset($all_plugins[$plugin_file])) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Plugin not installed.', 'plugin_file' => $plugin_file], 404);
        }

        $slug = self::plugin_slug_from_file($plugin_file);
        $from_version = (string) ($all_plugins[$plugin_file]['Version'] ?? '');
        $target_version = isset($payload['target_version']) && $payload['target_version'] !== null
            ? trim((string) $payload['target_version'])
            : '';
        $backup_before = !array_key_exists('backup_before', $payload) || (bool) $payload['backup_before'];
        $probe_health = !array_key_exists('probe_health', $payload) || (bool) $payload['probe_health'];
        $canary_paths = Azom_Ops_Bridge_Ops::sanitize_canary_paths(
            array_key_exists('canary_paths', $payload) ? $payload['canary_paths'] : null
        );
        if (is_wp_error($canary_paths)) {
            return new WP_REST_Response([
                'ok'    => false,
                'error' => $canary_paths->get_error_message(),
            ], 400);
        }

        $was_network = is_multisite() && function_exists('is_plugin_active_for_network') && (bool) is_plugin_active_for_network($plugin_file);
        $was_active = $was_network || (function_exists('is_plugin_active') && (bool) is_plugin_active($plugin_file));

        $transient = get_site_transient('update_plugins');
        if ((!is_object($transient) || !isset($transient->response[$plugin_file])) && function_exists('wp_update_plugins')) {
            @wp_update_plugins();
            $transient = get_site_transient('update_plugins');
        }

        if ($target_version !== '') {
            $avail = (is_object($transient) && isset($transient->response[$plugin_file]->new_version))
                ? (string) $transient->response[$plugin_file]->new_version
                : '';
            if ($avail !== '' && $avail !== $target_version) {
                return new WP_REST_Response([
                    'ok'          => false,
                    'error'       => 'Requested target_version does not match available update version.',
                    'plugin_file' => $plugin_file,
                ], 409);
            }
        }

        $backup_id = '';
        $backup_sha256 = '';
        $backup_path = '';
        if ($backup_before) {
            $backup_res = self::create_plugin_backup($change_id, $plugin_file, $slug);
            if (is_wp_error($backup_res)) {
                return new WP_REST_Response([
                    'ok'            => false,
                    'plugin_file'   => $plugin_file,
                    'slug'          => $slug,
                    'from_version'  => $from_version,
                    'to_version'    => $from_version,
                    'backup_id'     => '',
                    'backup_sha256' => '',
                    'http_status'   => 500,
                    'auto_reverted' => false,
                    'error'         => $backup_res->get_error_message(),
                ], 500);
            }
            $backup_id = (string) $backup_res['backup_id'];
            $backup_sha256 = (string) $backup_res['backup_sha256'];
            $backup_path = (string) $backup_res['backup_path'];
        }

        require_once ABSPATH . 'wp-admin/includes/file.php';
        require_once ABSPATH . 'wp-admin/includes/misc.php';
        require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';

        $skin = new Automatic_Upgrader_Skin();
        $upgrader = new Plugin_Upgrader($skin);
        $upgraded = $upgrader->upgrade($plugin_file);

        self::invalidate_plugin_opcache($plugin_file, $slug);
        if (function_exists('wp_clean_plugins_cache')) {
            wp_clean_plugins_cache(true);
        }
        Azom_Ops_Bridge_Ops::flush_all_caches();

        if ($upgraded !== true) {
            if (is_wp_error($upgraded)) {
                $err_msg = $upgraded->get_error_message();
            } elseif ($upgraded === null) {
                $err_msg = 'Plugin_Upgrader returned no-op (update package unavailable or already current).';
            } else {
                $err_msg = 'Plugin_Upgrader failed.';
            }
            $reverted = false;
            if ($backup_path !== '' && is_file($backup_path)) {
                $restore = self::restore_plugin_from_zip($backup_path, $plugin_file, $slug);
                $reverted = !is_wp_error($restore);
                self::invalidate_plugin_opcache($plugin_file, $slug);
                if (function_exists('wp_clean_plugins_cache')) {
                    wp_clean_plugins_cache(true);
                }
            }
            if ($was_active) {
                @activate_plugin($plugin_file, '', $was_network, true);
            }
            Azom_Ops_Bridge_Ops::flush_all_caches();
            return new WP_REST_Response([
                'ok'            => false,
                'plugin_file'   => $plugin_file,
                'slug'          => $slug,
                'from_version'  => $from_version,
                'to_version'    => $from_version,
                'backup_id'     => $backup_id,
                'backup_sha256' => $backup_sha256,
                'http_status'   => 500,
                'auto_reverted' => $reverted,
                'error'         => self::scrub_text($err_msg),
            ], 500);
        }

        $refreshed = function_exists('get_plugins') ? (array) get_plugins() : [];
        $to_version = (string) ($refreshed[$plugin_file]['Version'] ?? $from_version);

        // Plugin_Upgrader::upgrade() deactivates active plugins during REST requests without
        // reactivating them; reactivate BEFORE running the loopback health probe.
        if ($was_active) {
            $reactivated = activate_plugin($plugin_file, '', $was_network, true);
            if (is_wp_error($reactivated)) {
                $reverted = false;
                if ($backup_path !== '' && is_file($backup_path)) {
                    $restore = self::restore_plugin_from_zip($backup_path, $plugin_file, $slug);
                    $reverted = !is_wp_error($restore);
                    self::invalidate_plugin_opcache($plugin_file, $slug);
                    if (function_exists('wp_clean_plugins_cache')) {
                        wp_clean_plugins_cache(true);
                    }
                    if ($reverted) {
                        @activate_plugin($plugin_file, '', $was_network, true);
                    }
                }
                Azom_Ops_Bridge_Ops::flush_all_caches();
                return new WP_REST_Response([
                    'ok'            => false,
                    'plugin_file'   => $plugin_file,
                    'slug'          => $slug,
                    'from_version'  => $from_version,
                    'to_version'    => $reverted ? $from_version : $to_version,
                    'backup_id'     => $backup_id,
                    'backup_sha256' => $backup_sha256,
                    'http_status'   => 500,
                    'auto_reverted' => $reverted,
                    'error'         => self::scrub_text('Failed to reactivate plugin after update: ' . $reactivated->get_error_message()),
                ], 500);
            }
            Azom_Ops_Bridge_Ops::flush_all_caches();
        }

        $http_status = 200;
        $canary_results = [];
        if ($probe_health) {
            $probe = self::run_canary_probes($canary_paths);
            $http_status = (int) ($probe['http_status'] ?? 500);
            $canary_results = is_array($probe['canary_results'] ?? null) ? $probe['canary_results'] : [];
            if (empty($probe['ok']) || $http_status >= 400 || $http_status === 0) {
                $reverted = false;
                if ($backup_path !== '' && is_file($backup_path)) {
                    $restore = self::restore_plugin_from_zip($backup_path, $plugin_file, $slug);
                    $reverted = !is_wp_error($restore);
                    self::invalidate_plugin_opcache($plugin_file, $slug);
                    if (function_exists('wp_clean_plugins_cache')) {
                        wp_clean_plugins_cache(true);
                    }
                    if ($was_active && $reverted) {
                        @activate_plugin($plugin_file, '', $was_network, true);
                    }
                    Azom_Ops_Bridge_Ops::flush_all_caches();
                }
                return new WP_REST_Response([
                    'ok'             => false,
                    'plugin_file'    => $plugin_file,
                    'slug'           => $slug,
                    'from_version'   => $from_version,
                    'to_version'     => $reverted ? $from_version : $to_version,
                    'backup_id'      => $backup_id,
                    'backup_sha256'  => $backup_sha256,
                    'http_status'    => $http_status,
                    'auto_reverted'  => $reverted,
                    'canary_results' => $canary_results,
                    'error'          => 'Post-update canary health probe failed; reverted automatically.',
                ], 500);
            }
        }

        return new WP_REST_Response([
            'ok'             => true,
            'plugin_file'    => $plugin_file,
            'slug'           => $slug,
            'from_version'   => $from_version,
            'to_version'     => $to_version,
            'backup_id'      => $backup_id,
            'backup_sha256'  => $backup_sha256,
            'http_status'    => $http_status,
            'auto_reverted'  => false,
            'canary_results' => $canary_results,
            'error'          => null,
        ], 200);
    }

    public static function handle_rollback(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }

        $change_id = trim((string) ($payload['change_id'] ?? ''));
        if (!preg_match('/^[A-Za-z0-9_-]{4,48}$/', $change_id)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change_id'], 400);
        }

        $plugin_file = trim((string) ($payload['plugin_file'] ?? ''));
        if (!self::is_valid_plugin_file($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid plugin_file'], 400);
        }
        if (self::is_bridge_plugin($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Rollback of azom-ops-bridge is refused.'], 403);
        }

        $slug = self::plugin_slug_from_file($plugin_file);
        $backup_id = trim((string) ($payload['backup_id'] ?? ''));
        $zip_path = self::resolve_backup_zip($backup_id, $slug);
        if (is_wp_error($zip_path)) {
            $status = $zip_path->get_error_code() === 'backup_not_found' ? 404 : 400;
            return new WP_REST_Response(['ok' => false, 'error' => $zip_path->get_error_message()], $status);
        }

        $probe_health = !array_key_exists('probe_health', $payload) || (bool) $payload['probe_health'];
        $restore = self::restore_plugin_from_zip($zip_path, $plugin_file, $slug);
        if (is_wp_error($restore)) {
            return new WP_REST_Response(['ok' => false, 'error' => $restore->get_error_message()], 500);
        }

        self::load_plugin_admin_includes();
        self::invalidate_plugin_opcache($plugin_file, $slug);
        if (function_exists('wp_clean_plugins_cache')) {
            wp_clean_plugins_cache(true);
        }
        Azom_Ops_Bridge_Ops::flush_all_caches();

        $http_status = $probe_health ? self::run_loopback_probe() : 200;
        $refreshed = function_exists('get_plugins') ? (array) get_plugins() : [];
        $restored_version = (string) ($refreshed[$plugin_file]['Version'] ?? '');

        return new WP_REST_Response([
            'ok'               => $http_status < 400 && $http_status !== 0,
            'plugin_file'      => $plugin_file,
            'slug'             => $slug,
            'restored_version' => $restored_version,
            'backup_id'        => $backup_id,
            'http_status'      => $http_status,
        ], ($http_status < 400 && $http_status !== 0) ? 200 : 500);
    }

    public static function handle_autoupdate(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }
        $changes = $payload['changes'] ?? null;
        if (!is_array($changes) || empty($changes)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Missing changes array'], 400);
        }

        $validated = [];
        foreach ($changes as $row) {
            if (!is_array($row)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change entry'], 400);
            }
            $plugin_file = trim((string) ($row['plugin_file'] ?? ''));
            if (!self::is_valid_plugin_file($plugin_file)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid plugin_file', 'plugin_file' => $plugin_file], 400);
            }
            if (self::is_bridge_plugin($plugin_file)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Auto-update mutation of azom-ops-bridge is refused.'], 403);
            }
            if (!array_key_exists('enabled', $row) || !is_bool($row['enabled'])) {
                return new WP_REST_Response(['ok' => false, 'error' => 'enabled must be a boolean', 'plugin_file' => $plugin_file], 400);
            }
            $validated[] = ['plugin_file' => $plugin_file, 'enabled' => (bool) $row['enabled']];
        }

        $before = array_values(array_unique(array_map('strval', (array) get_site_option('auto_update_plugins', []))));
        sort($before, SORT_STRING);
        $set = array_fill_keys($before, true);

        $applied = [];
        foreach ($validated as $item) {
            $pf = $item['plugin_file'];
            $had = isset($set[$pf]);
            if ($item['enabled']) {
                $set[$pf] = true;
            } else {
                unset($set[$pf]);
            }
            $applied[] = [
                'plugin_file' => $pf,
                'enabled'     => $item['enabled'],
                'changed'     => $had !== $item['enabled'],
            ];
        }

        $after = array_keys($set);
        sort($after, SORT_STRING);
        update_site_option('auto_update_plugins', $after);

        $warnings = [];
        if (defined('AUTOMATIC_UPDATER_DISABLED') && AUTOMATIC_UPDATER_DISABLED) {
            $warnings[] = 'AUTOMATIC_UPDATER_DISABLED is enabled; WordPress background auto-updates will not run.';
        }
        if (defined('DISALLOW_FILE_MODS') && DISALLOW_FILE_MODS) {
            $warnings[] = 'DISALLOW_FILE_MODS is enabled; WordPress cannot apply auto-updates.';
        }
        $filters = self::inspect_hook_callbacks('auto_update_plugin');
        if (!empty($filters)) {
            $warnings[] = 'Active auto_update_plugin filter callbacks may override auto_update_plugins option.';
        }

        return new WP_REST_Response([
            'ok'              => true,
            'before'          => $before,
            'after'           => $after,
            'applied'         => $applied,
            'filter_warnings' => $warnings,
        ], 200);
    }

    public static function handle_toggle(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }
        $plugin_file = trim((string) ($payload['plugin_file'] ?? ''));
        if (!self::is_valid_plugin_file($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid plugin_file'], 400);
        }
        if (self::is_bridge_plugin($plugin_file)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Toggling azom-ops-bridge is refused.'], 403);
        }
        if (!array_key_exists('active', $payload) || !is_bool($payload['active'])) {
            return new WP_REST_Response(['ok' => false, 'error' => 'active must be a boolean'], 400);
        }
        $want_active = (bool) $payload['active'];
        $probe_health = !array_key_exists('probe_health', $payload) || (bool) $payload['probe_health'];

        self::load_plugin_admin_includes();
        $all_plugins = function_exists('get_plugins') ? (array) get_plugins() : [];
        if (!isset($all_plugins[$plugin_file])) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Plugin not installed.', 'plugin_file' => $plugin_file], 404);
        }

        $was_network = is_multisite() && function_exists('is_plugin_active_for_network') && (bool) is_plugin_active_for_network($plugin_file);
        $was_active = $was_network || (function_exists('is_plugin_active') && (bool) is_plugin_active($plugin_file));

        if ($want_active) {
            $res = activate_plugin($plugin_file, '', $was_network, false);
            if (is_wp_error($res)) {
                return new WP_REST_Response([
                    'ok'            => false,
                    'plugin_file'   => $plugin_file,
                    'active'        => $was_active,
                    'auto_reverted' => true,
                    'http_status'   => 500,
                    'error'         => self::scrub_text($res->get_error_message()),
                ], 500);
            }
        } else {
            deactivate_plugins([$plugin_file], false, $was_network);
        }

        Azom_Ops_Bridge_Ops::flush_all_caches();

        $http_status = 200;
        if ($probe_health) {
            $http_status = self::run_loopback_probe();
            if ($http_status >= 400 || $http_status === 0) {
                if ($want_active && !$was_active) {
                    deactivate_plugins([$plugin_file], true, $was_network);
                } elseif (!$want_active && $was_active) {
                    @activate_plugin($plugin_file, '', $was_network, true);
                }
                Azom_Ops_Bridge_Ops::flush_all_caches();
                return new WP_REST_Response([
                    'ok'            => false,
                    'plugin_file'   => $plugin_file,
                    'active'        => $was_active,
                    'auto_reverted' => true,
                    'http_status'   => $http_status,
                    'error'         => 'Post-toggle loopback health probe failed; reverted automatically.',
                ], 500);
            }
        }

        return new WP_REST_Response([
            'ok'            => true,
            'plugin_file'   => $plugin_file,
            'active'        => $want_active,
            'auto_reverted' => false,
            'http_status'   => $http_status,
        ], 200);
    }

    public static function handle_diagnostics(WP_REST_Request $request): WP_REST_Response {
        self::load_plugin_admin_includes();

        $log_lines_raw = $request->get_param('log_lines');
        $log_lines = $log_lines_raw === null ? self::DEFAULT_LOG_LINES : (int) $log_lines_raw;
        if ($log_lines < 0) {
            $log_lines = 0;
        }
        if ($log_lines > self::MAX_LOG_LINES) {
            $log_lines = self::MAX_LOG_LINES;
        }

        $hooks_param = trim((string) $request->get_param('hooks'));
        $requested_hooks = [];
        if ($hooks_param !== '') {
            foreach (explode(',', $hooks_param) as $h) {
                $clean_h = trim($h);
                if ($clean_h !== '' && preg_match('/^[A-Za-z0-9_.:\/-]{1,120}$/', $clean_h)) {
                    $requested_hooks[] = $clean_h;
                }
                if (count($requested_hooks) >= self::MAX_HOOKS_QUERY) {
                    break;
                }
            }
        }
        if (empty($requested_hooks)) {
            $requested_hooks = self::DEFAULT_DIAGNOSTIC_HOOKS;
        }

        // 1. Paused plugins from WordPress Recovery Mode
        $paused_raw = function_exists('wp_get_paused_plugins') ? (array) wp_get_paused_plugins() : [];
        $paused_out = [];
        foreach ($paused_raw as $plugin_file => $err) {
            $err = is_array($err) ? $err : [];
            $paused_out[] = [
                'plugin_file'   => (string) $plugin_file,
                'slug'          => self::plugin_slug_from_file((string) $plugin_file),
                'error_type'    => isset($err['type']) ? (int) $err['type'] : 0,
                'error_message' => self::scrub_text((string) ($err['message'] ?? '')),
                'file'          => self::scrub_text((string) ($err['file'] ?? '')),
                'line'          => isset($err['line']) ? (int) $err['line'] : 0,
            ];
        }

        // 2. Bounded, scrubbed debug.log tail (<= 16 KiB)
        $debug_log_tail = self::read_scrubbed_debug_log_tail($log_lines);

        // 3. Targeted $wp_filter hook priority map
        $hook_map = [];
        foreach ($requested_hooks as $hook_name) {
            $hook_map[$hook_name] = self::inspect_hook_callbacks($hook_name);
        }

        // 4. Available pbackup-*.zip backups
        $available_backups = self::list_plugin_backups();

        // 5. Bridge health snapshot
        $bridge_health = Azom_Ops_Bridge_Health::build_health_payload();

        return new WP_REST_Response([
            'ok'                => true,
            'paused_plugins'    => $paused_out,
            'debug_log_tail'    => $debug_log_tail,
            'hook_map'          => $hook_map,
            'available_backups' => $available_backups,
            'bridge_health'     => $bridge_health,
        ], 200);
    }

    private static function load_plugin_admin_includes(): void {
        if (!function_exists('get_plugins') || !function_exists('get_mu_plugins') || !function_exists('get_dropins')) {
            require_once ABSPATH . 'wp-admin/includes/plugin.php';
        }
    }

    private static function plugin_slug_from_file(string $plugin_file): string {
        $clean = trim(str_replace('\\', '/', $plugin_file), '/');
        if (strpos($clean, '/') !== false) {
            return explode('/', $clean, 2)[0];
        }
        return (string) pathinfo($clean, PATHINFO_FILENAME);
    }

    private static function is_bridge_plugin(string $plugin_file): bool {
        $slug = strtolower(self::plugin_slug_from_file($plugin_file));
        return $slug === self::BRIDGE_SLUG || strtolower(trim($plugin_file)) === 'azom-ops-bridge/azom-ops-bridge.php';
    }

    private static function is_valid_plugin_file(string $plugin_file): bool {
        if ($plugin_file === '' || strpos($plugin_file, '..') !== false || strpos($plugin_file, "\0") !== false) {
            return false;
        }
        return preg_match('/^[A-Za-z0-9][A-Za-z0-9._-]{0,60}(?:\/[A-Za-z0-9][A-Za-z0-9._-]{0,80})?\.php$/', $plugin_file) === 1;
    }

    private static function detect_hpos_compatibility_map(): array {
        $out = [];
        if (class_exists(\Automattic\WooCommerce\Utilities\FeaturesUtil::class)
            && method_exists(\Automattic\WooCommerce\Utilities\FeaturesUtil::class, 'get_compatible_plugins_for_feature')) {
            $info = \Automattic\WooCommerce\Utilities\FeaturesUtil::get_compatible_plugins_for_feature('custom_order_tables');
            if (is_array($info)) {
                foreach ((array) ($info['compatible'] ?? []) as $pf) {
                    $out[(string) $pf] = true;
                }
                foreach ((array) ($info['incompatible'] ?? []) as $pf) {
                    $out[(string) $pf] = false;
                }
            }
        }
        return $out;
    }

    private static function inspect_hook_callbacks(string $hook_name): array {
        global $wp_filter;
        if (!isset($wp_filter[$hook_name])) {
            return [];
        }
        $hook = $wp_filter[$hook_name];
        $callbacks_by_priority = [];
        if (is_object($hook) && isset($hook->callbacks) && is_array($hook->callbacks)) {
            $callbacks_by_priority = $hook->callbacks;
        } elseif (is_array($hook)) {
            $callbacks_by_priority = $hook;
        }
        ksort($callbacks_by_priority, SORT_NUMERIC);
        $entries = [];
        foreach ($callbacks_by_priority as $priority => $bucket) {
            if (!is_array($bucket)) {
                continue;
            }
            foreach ($bucket as $cb_info) {
                if (!is_array($cb_info) || !isset($cb_info['function'])) {
                    continue;
                }
                $entries[] = [
                    'priority'      => (int) $priority,
                    'callback'      => self::format_callback_name($cb_info['function']),
                    'accepted_args' => isset($cb_info['accepted_args']) ? (int) $cb_info['accepted_args'] : 1,
                ];
            }
        }
        return $entries;
    }

    private static function format_callback_name($fn): string {
        if (is_string($fn)) {
            return $fn;
        }
        if (is_array($fn) && count($fn) === 2) {
            $target = is_object($fn[0]) ? get_class($fn[0]) : (string) $fn[0];
            return $target . '::' . (string) $fn[1];
        }
        if ($fn instanceof \Closure) {
            return 'Closure';
        }
        if (is_object($fn)) {
            return get_class($fn) . '::__invoke';
        }
        return 'unknown';
    }

    private static function create_plugin_backup(string $change_id, string $plugin_file, string $slug) {
        if (!class_exists('ZipArchive')) {
            return new WP_Error('zip_unavailable', 'PHP ZipArchive extension is required for plugin backups.');
        }
        $backup_dir = self::plugin_backup_dir();
        if ($backup_dir === '') {
            return new WP_Error('backup_dir_unavailable', 'Plugin backup directory is unavailable.');
        }

        $plugin_root = defined('WP_PLUGIN_DIR') ? (string) WP_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'plugins');
        $real_plugin_root = realpath($plugin_root);
        if ($real_plugin_root === false || is_link($plugin_root)) {
            return new WP_Error('plugin_root_invalid', 'WP_PLUGIN_DIR is invalid or a symlink.');
        }

        $is_dir_plugin = strpos($plugin_file, '/') !== false;
        $source_path = $is_dir_plugin
            ? ($real_plugin_root . '/' . $slug)
            : ($real_plugin_root . '/' . $plugin_file);
        if (is_link($source_path)) {
            return new WP_Error('symlink_blocked', 'Refusing to back up symlinked plugin path.');
        }
        $real_source = realpath($source_path);
        $root_prefix = rtrim($real_plugin_root, '/\\') . DIRECTORY_SEPARATOR;
        if ($real_source === false || strpos($real_source, $root_prefix) !== 0) {
            return new WP_Error('plugin_missing', 'Plugin path could not be resolved inside WP_PLUGIN_DIR.');
        }

        $safe_slug = preg_replace('/[^A-Za-z0-9._-]/', '_', $slug);
        $backup_id = 'pbackup-' . $change_id . '-' . $safe_slug . '.zip';
        $backup_path = $backup_dir . '/' . $backup_id;
        $tmp_path = $backup_path . '.tmp-' . wp_generate_password(6, false, false);

        $zip = new \ZipArchive();
        if ($zip->open($tmp_path, \ZipArchive::CREATE | \ZipArchive::OVERWRITE) !== true) {
            return new WP_Error('zip_create_failed', 'Failed to create plugin backup archive.');
        }

        if ($is_dir_plugin && is_dir($real_source)) {
            $iterator = new \RecursiveIteratorIterator(
                new \RecursiveDirectoryIterator($real_source, \FilesystemIterator::SKIP_DOTS),
                \RecursiveIteratorIterator::SELF_FIRST
            );
            $source_prefix = rtrim($real_source, '/\\') . DIRECTORY_SEPARATOR;
            foreach ($iterator as $file_info) {
                $item_path = $file_info->getPathname();
                if (is_link($item_path)) {
                    $zip->close();
                    @unlink($tmp_path);
                    return new WP_Error('symlink_blocked', 'Symlink encountered inside plugin directory.');
                }
                $rel = str_replace('\\', '/', substr($item_path, strlen($source_prefix)));
                if ($rel === '' || strpos($rel, '..') !== false) {
                    continue;
                }
                $arc_name = $slug . '/' . $rel;
                if ($file_info->isDir()) {
                    $zip->addEmptyDir($arc_name);
                } elseif ($file_info->isFile()) {
                    $zip->addFile($item_path, $arc_name);
                }
            }
        } elseif (is_file($real_source)) {
            $zip->addFile($real_source, $plugin_file);
        } else {
            $zip->close();
            @unlink($tmp_path);
            return new WP_Error('plugin_missing', 'Plugin source missing on disk.');
        }

        if (!$zip->close()) {
            @unlink($tmp_path);
            return new WP_Error('zip_close_failed', 'Failed to finalize plugin backup archive.');
        }
        if (!@rename($tmp_path, $backup_path)) {
            @unlink($tmp_path);
            return new WP_Error('zip_rename_failed', 'Failed to commit plugin backup archive.');
        }

        self::prune_plugin_backups($backup_dir);
        $sha = @hash_file('sha256', $backup_path);
        return [
            'backup_id'     => $backup_id,
            'backup_path'   => $backup_path,
            'backup_sha256' => is_string($sha) ? $sha : '',
        ];
    }

    private static function resolve_backup_zip(string $backup_id, string $expected_slug) {
        if (strpos($backup_id, '/') !== false || strpos($backup_id, '\\') !== false || strpos($backup_id, '..') !== false || strpos($backup_id, "\0") !== false) {
            return new WP_Error('invalid_backup_id', 'Path traversal refused in backup_id.');
        }
        $safe_expected = (string) preg_replace('/[^A-Za-z0-9._-]/', '_', $expected_slug);
        if ($safe_expected === '') {
            return new WP_Error('invalid_backup_id', 'Invalid expected plugin slug.');
        }
        if (preg_match('/^pbackup-((?:chg|plu|rb)-[a-f0-9]{12})-([A-Za-z0-9._-]{1,61})\.zip$/i', $backup_id, $m) === 1) {
            if (strtolower((string) $m[2]) !== strtolower($safe_expected)) {
                return new WP_Error('backup_slug_mismatch', 'Backup archive does not belong to target plugin.');
            }
        } else {
            if (preg_match('/^pbackup-[A-Za-z0-9_-]{4,48}-[A-Za-z0-9._-]{1,61}\.zip$/', $backup_id) !== 1) {
                return new WP_Error('invalid_backup_id', 'Invalid backup_id format.');
            }
            $expected_suffix = '-' . $safe_expected . '.zip';
            if (
                strlen($backup_id) <= strlen('pbackup-') + strlen($expected_suffix)
                || strtolower(substr($backup_id, -strlen($expected_suffix))) !== strtolower($expected_suffix)
            ) {
                return new WP_Error('backup_slug_mismatch', 'Backup archive does not belong to target plugin.');
            }
            $change_part = substr($backup_id, strlen('pbackup-'), -strlen($expected_suffix));
            if (preg_match('/^[A-Za-z0-9_-]{4,48}$/', $change_part) !== 1) {
                return new WP_Error('invalid_backup_id', 'Invalid change_id in backup_id.');
            }
        }
        $backup_dir = self::plugin_backup_dir();
        if ($backup_dir === '') {
            return new WP_Error('backup_dir_unavailable', 'Plugin backup directory is unavailable.');
        }
        $real_dir = realpath($backup_dir);
        if ($real_dir === false || is_link($backup_dir)) {
            return new WP_Error('backup_dir_unavailable', 'Plugin backup directory is invalid.');
        }
        $candidate = $real_dir . '/' . $backup_id;
        if (is_link($candidate)) {
            return new WP_Error('symlink_blocked', 'Backup symlink is refused.');
        }
        if (!is_file($candidate)) {
            return new WP_Error('backup_not_found', 'Requested plugin backup archive not found.');
        }
        $real_zip = realpath($candidate);
        $prefix = rtrim($real_dir, '/\\') . DIRECTORY_SEPARATOR;
        if ($real_zip === false || strpos($real_zip, $prefix) !== 0) {
            return new WP_Error('symlink_blocked', 'Backup path escaped backup directory.');
        }
        return $real_zip;
    }

    private static function restore_plugin_from_zip(string $zip_path, string $plugin_file, string $slug) {
        if (!class_exists('ZipArchive')) {
            return new WP_Error('zip_unavailable', 'PHP ZipArchive extension is required for rollback.');
        }
        $plugin_root = defined('WP_PLUGIN_DIR') ? (string) WP_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'plugins');
        $real_plugin_root = realpath($plugin_root);
        if ($real_plugin_root === false || is_link($plugin_root)) {
            return new WP_Error('plugin_root_invalid', 'WP_PLUGIN_DIR is invalid or a symlink.');
        }

        $zip = new \ZipArchive();
        if ($zip->open($zip_path) !== true) {
            return new WP_Error('zip_open_failed', 'Could not open plugin backup archive.');
        }

        $is_dir_plugin = strpos($plugin_file, '/') !== false;
        $entries = [];
        for ($i = 0; $i < $zip->numFiles; $i++) {
            $stat = $zip->statIndex($i);
            if (!is_array($stat) || !isset($stat['name'])) {
                $zip->close();
                return new WP_Error('zip_corrupt', 'Corrupt entry in plugin backup archive.');
            }
            $name = str_replace('\\', '/', (string) $stat['name']);
            if ($name === '' || $name[0] === '/' || strpos($name, '..') !== false || strpos($name, "\0") !== false || strpos($name, ':') !== false) {
                $zip->close();
                return new WP_Error('zip_unsafe_entry', 'Unsafe path in plugin backup archive.');
            }
            // Reject symlink entries encoded in Unix external attributes (0120000).
            $opsys = 0;
            $attr = 0;
            if ($zip->getExternalAttributesIndex($i, $opsys, $attr) && $opsys === \ZipArchive::OPSYS_UNIX) {
                $mode = ($attr >> 16) & 0170000;
                if ($mode === 0120000) {
                    $zip->close();
                    return new WP_Error('symlink_blocked', 'Symlink entry inside backup archive is refused.');
                }
            }
            if ($is_dir_plugin) {
                if (strpos($name, $slug . '/') !== 0 && $name !== $slug) {
                    $zip->close();
                    return new WP_Error('zip_scope_violation', 'Backup archive entry outside plugin directory.');
                }
            } elseif ($name !== $plugin_file) {
                $zip->close();
                return new WP_Error('zip_scope_violation', 'Backup archive entry does not match single-file plugin.');
            }
            $entries[] = $name;
        }

        if (empty($entries)) {
            $zip->close();
            return new WP_Error('zip_empty', 'Plugin backup archive is empty.');
        }

        if (!$is_dir_plugin) {
            $bytes = $zip->getFromName($plugin_file);
            $zip->close();
            if ($bytes === false) {
                return new WP_Error('zip_read_failed', 'Failed to read single-file plugin from archive.');
            }
            $target = $real_plugin_root . '/' . $plugin_file;
            if (is_link($target)) {
                return new WP_Error('symlink_blocked', 'Target plugin file is a symlink.');
            }
            $tmp = $target . '.azom-rb-' . wp_generate_password(8, false, false);
            if (@file_put_contents($tmp, $bytes) === false || !@rename($tmp, $target)) {
                @unlink($tmp);
                return new WP_Error('restore_write_failed', 'Failed to restore single-file plugin.');
            }
            return true;
        }

        $rand = wp_generate_password(8, false, false);
        $stage_root = $real_plugin_root . '/.azom-stage-' . $slug . '-' . $rand;
        if (!wp_mkdir_p($stage_root)) {
            $zip->close();
            return new WP_Error('stage_dir_failed', 'Failed to create staging restore directory.');
        }

        foreach ($entries as $entry_name) {
            $dest = $stage_root . '/' . $entry_name;
            if (substr($entry_name, -1) === '/') {
                if (!wp_mkdir_p($dest)) {
                    $zip->close();
                    self::remove_tree($stage_root);
                    return new WP_Error('stage_mkdir_failed', 'Failed to create restored subdirectory.');
                }
                continue;
            }
            $parent = dirname($dest);
            if (!is_dir($parent) && !wp_mkdir_p($parent)) {
                $zip->close();
                self::remove_tree($stage_root);
                return new WP_Error('stage_mkdir_failed', 'Failed to create restored parent directory.');
            }
            $bytes = $zip->getFromName($entry_name);
            if ($bytes === false || @file_put_contents($dest, $bytes) === false) {
                $zip->close();
                self::remove_tree($stage_root);
                return new WP_Error('stage_write_failed', 'Failed to extract file from backup archive.');
            }
        }
        $zip->close();

        $staged_plugin_dir = $stage_root . '/' . $slug;
        if (!is_dir($staged_plugin_dir)) {
            self::remove_tree($stage_root);
            return new WP_Error('stage_missing_root', 'Extracted backup did not contain expected plugin directory.');
        }

        $target_dir = $real_plugin_root . '/' . $slug;
        if (is_link($target_dir)) {
            self::remove_tree($stage_root);
            return new WP_Error('symlink_blocked', 'Target plugin directory is a symlink.');
        }

        $old_dir = $real_plugin_root . '/.azom-old-' . $slug . '-' . $rand;
        $moved_old = false;
        if (is_dir($target_dir)) {
            if (!@rename($target_dir, $old_dir)) {
                self::remove_tree($stage_root);
                return new WP_Error('swap_backup_failed', 'Failed to move existing plugin directory aside.');
            }
            $moved_old = true;
        }

        if (!@rename($staged_plugin_dir, $target_dir)) {
            if ($moved_old && is_dir($old_dir)) {
                @rename($old_dir, $target_dir);
            }
            self::remove_tree($stage_root);
            return new WP_Error('swap_commit_failed', 'Failed to commit restored plugin directory.');
        }

        self::remove_tree($stage_root);
        if ($moved_old && is_dir($old_dir)) {
            self::remove_tree($old_dir);
        }
        return true;
    }

    private static function remove_tree(string $dir): void {
        if ($dir === '' || !is_dir($dir) || is_link($dir)) {
            return;
        }
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($dir, \FilesystemIterator::SKIP_DOTS),
            \RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($iterator as $item) {
            $path = $item->getPathname();
            if ($item->isDir() && !is_link($path)) {
                @rmdir($path);
            } else {
                @unlink($path);
            }
        }
        @rmdir($dir);
    }

    private static function invalidate_plugin_opcache(string $plugin_file, string $slug): void {
        $plugin_root = defined('WP_PLUGIN_DIR') ? (string) WP_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'plugins');
        $is_dir_plugin = strpos($plugin_file, '/') !== false;
        if (!$is_dir_plugin) {
            $file_path = trailingslashit($plugin_root) . $plugin_file;
            if (is_file($file_path)) {
                Azom_Ops_Bridge_Snapshots::invalidate_opcache($file_path);
            }
            return;
        }
        $dir = trailingslashit($plugin_root) . $slug;
        if (!is_dir($dir) || is_link($dir)) {
            return;
        }
        try {
            $iterator = new \RecursiveIteratorIterator(
                new \RecursiveDirectoryIterator($dir, \FilesystemIterator::SKIP_DOTS),
                \RecursiveIteratorIterator::LEAVES_ONLY,
                \RecursiveIteratorIterator::CATCH_GET_CHILD
            );
            foreach ($iterator as $item) {
                if ($item->isFile() && strtolower($item->getExtension()) === 'php') {
                    Azom_Ops_Bridge_Snapshots::invalidate_opcache($item->getPathname());
                }
            }
        } catch (\UnexpectedValueException $e) {
            return;
        }
    }

    private static function run_canary_probes(array $paths): array {
        return Azom_Ops_Bridge_Ops::probe_canary_paths($paths, Azom_Ops_Bridge_Ops::build_probe_headers());
    }

    private static function run_loopback_probe(): int {
        $probe = Azom_Ops_Bridge_Ops::probe_canary_paths(['/']);
        return (int) ($probe['http_status'] ?? 500);
    }

    private static function list_plugin_backups(): array {
        $dir = self::plugin_backup_dir();
        if ($dir === '' || !is_dir($dir)) {
            return [];
        }
        $files = glob($dir . '/pbackup-*.zip') ?: [];
        usort($files, static function (string $a, string $b): int {
            return (int) @filemtime($b) <=> (int) @filemtime($a);
        });
        $out = [];
        foreach (array_slice($files, 0, self::MAX_PLUGIN_BACKUPS) as $file) {
            if (is_link($file) || !is_file($file)) {
                continue;
            }
            $base = basename($file);
            $slug = '';
            if (preg_match('/^pbackup-(?:chg|plu|rb)-[a-f0-9]{12}-([A-Za-z0-9._-]{1,61})\.zip$/i', $base, $m) === 1) {
                $slug = (string) $m[1];
            } elseif (preg_match('/^pbackup-[A-Za-z0-9_]{4,48}-([A-Za-z0-9._-]{1,61})\.zip$/', $base, $m) === 1) {
                $slug = (string) $m[1];
            } elseif (preg_match('/^pbackup-[A-Za-z0-9_-]{4,48}?-([A-Za-z0-9._-]{1,61})\.zip$/', $base, $m) === 1) {
                $slug = (string) $m[1];
            }
            $mtime = @filemtime($file);
            $out[] = [
                'backup_id'    => $base,
                'slug'         => $slug,
                'bytes'        => (int) (@filesize($file) ?: 0),
                'modified_gmt' => $mtime !== false ? gmdate('Y-m-d\TH:i:s\Z', (int) $mtime) : '',
            ];
        }
        return $out;
    }

    private static function prune_plugin_backups(string $dir): void {
        if ($dir === '' || !is_dir($dir)) {
            return;
        }
        $files = glob($dir . '/pbackup-*.zip') ?: [];
        if (count($files) <= self::MAX_PLUGIN_BACKUPS) {
            return;
        }
        usort($files, static function (string $a, string $b): int {
            return (int) @filemtime($b) <=> (int) @filemtime($a);
        });
        foreach (array_slice($files, self::MAX_PLUGIN_BACKUPS) as $stale) {
            @unlink($stale);
        }
    }

    private static function read_scrubbed_debug_log_tail(int $max_lines): array {
        if ($max_lines <= 0) {
            return [];
        }
        $log_path = trailingslashit(WP_CONTENT_DIR) . 'debug.log';
        if (is_link($log_path) || !is_file($log_path) || !is_readable($log_path)) {
            return [];
        }
        $size = @filesize($log_path);
        if ($size === false || $size <= 0) {
            return [];
        }
        $fp = @fopen($log_path, 'rb');
        if ($fp === false) {
            return [];
        }
        $read_bytes = min((int) $size, self::MAX_DEBUG_LOG_BYTES);
        if ($size > $read_bytes) {
            @fseek($fp, -$read_bytes, SEEK_END);
        }
        $raw = (string) @fread($fp, $read_bytes);
        @fclose($fp);

        if ($raw === '') {
            return [];
        }
        $lines = preg_split("/\r\n|\n|\r/", trim($raw));
        if (!is_array($lines)) {
            return [];
        }
        $tail = array_slice($lines, -$max_lines);
        $clean = [];
        foreach ($tail as $line) {
            $clean[] = self::scrub_text((string) $line);
        }
        return $clean;
    }

    private static function scrub_text(string $text): string {
        if ($text === '') {
            return '';
        }
        // Scrub absolute server filesystem paths before returning diagnostics.
        if (defined('WP_CONTENT_DIR') && WP_CONTENT_DIR !== '') {
            $text = str_replace(rtrim((string) WP_CONTENT_DIR, '/\\'), '<WP_CONTENT>', $text);
        }
        if (defined('ABSPATH') && ABSPATH !== '') {
            $text = str_replace(rtrim((string) ABSPATH, '/\\'), '<ABSPATH>', $text);
        }
        if (defined('AZOM_BRIDGE_SECRET') && (string) AZOM_BRIDGE_SECRET !== '') {
            $text = str_replace((string) AZOM_BRIDGE_SECRET, '[REDACTED]', $text);
        }
        if (defined('DB_PASSWORD') && (string) DB_PASSWORD !== '') {
            $text = str_replace((string) DB_PASSWORD, '[REDACTED]', $text);
        }
        // Redact common bearer tokens, query/key secrets, and Woo/Stripe API keys.
        $patterns = [
            '/(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s"\']+/i'                           => '$1[REDACTED]',
            '/(Bearer\s+)[A-Za-z0-9._\-+\/=]{12,}/i'                                          => '$1[REDACTED]',
            '/((?:api[_-]?key|secret|token|password|passwd|pwd|client_secret)\s*[=:]\s*)[^\s&"\',;]+/i' => '$1[REDACTED]',
            '/\b(?:ck|cs|sk|pk)_(?:live|test)?_[A-Za-z0-9]{16,}\b/'                            => '[REDACTED]',
            '/\b(?:ck|cs)_[a-f0-9]{32,}\b/i'                                                   => '[REDACTED]',
        ];
        foreach ($patterns as $regex => $replacement) {
            $replaced = preg_replace($regex, $replacement, $text);
            if (is_string($replaced)) {
                $text = $replaced;
            }
        }
        return $text;
    }
}
