<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Health {
    public static function build_health_payload(): array {
        global $wp_version;
        if (!function_exists('get_plugins') || !function_exists('get_mu_plugins') || !function_exists('get_dropins')) {
            require_once ABSPATH . 'wp-admin/includes/plugin.php';
        }

        $theme = wp_get_theme();
        $parent = $theme->parent();
        $active_plugins = (array) get_option('active_plugins', []);
        if (is_multisite()) {
            $network_plugins = array_keys((array) get_site_option('active_sitewide_plugins', []));
            $active_plugins = array_values(array_unique(array_merge($active_plugins, $network_plugins)));
        }
        $all_plugins = function_exists('get_plugins') ? get_plugins() : [];
        $plugins_out = [];
        foreach ($active_plugins as $slug) {
            $meta = isset($all_plugins[$slug]) && is_array($all_plugins[$slug]) ? $all_plugins[$slug] : [];
            $plugins_out[] = [
                'plugin'  => (string) $slug,
                'name'    => (string) ($meta['Name'] ?? $slug),
                'version' => (string) ($meta['Version'] ?? ''),
            ];
        }

        $woo_version = defined('WC_VERSION') ? (string) WC_VERSION : '';
        $hpos_enabled = false;
        if (class_exists(\Automattic\WooCommerce\Utilities\OrderUtil::class)
            && method_exists(\Automattic\WooCommerce\Utilities\OrderUtil::class, 'custom_orders_table_usage_is_enabled')) {
            $hpos_enabled = (bool) \Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled();
        }

        $cache_plugins = self::detect_cache_plugins($active_plugins);
        $multilingual  = Azom_Ops_Bridge_Translations::detect_provider();
        $maintenance_active = Azom_Ops_Bridge_Ops::is_maintenance_active();

        $opcache_enabled = false;
        $restrict_api = trim((string) ini_get('opcache.restrict_api'));
        if (function_exists('opcache_get_status') && ($restrict_api === '' || strpos(__FILE__, $restrict_api) === 0)) {
            $opcache_enabled = @opcache_get_status(false) !== false;
        }

        $mu_plugins_count = function_exists('get_mu_plugins') ? count((array) get_mu_plugins()) : 0;
        $dropins_count = function_exists('get_dropins') ? count((array) get_dropins()) : 0;
        $paused_count = function_exists('wp_get_paused_plugins') ? count((array) wp_get_paused_plugins()) : 0;

        return [
            'ok'                   => true,
            'bridge_version'       => AZOM_OPS_BRIDGE_VERSION,
            'wordpress_version'    => (string) ($wp_version ?? ''),
            'php_version'          => PHP_VERSION,
            'woocommerce_version'  => $woo_version,
            'hpos_enabled'         => $hpos_enabled,
            'theme'                => [
                'stylesheet' => (string) $theme->get_stylesheet(),
                'template'   => (string) $theme->get_template(),
                'name'       => (string) $theme->get('Name'),
                'version'    => (string) $theme->get('Version'),
                'is_child'   => $parent instanceof WP_Theme,
                'parent'     => $parent instanceof WP_Theme ? (string) $parent->get_stylesheet() : '',
                'is_block'   => function_exists('wp_is_block_theme') ? (bool) wp_is_block_theme() : false,
            ],
            'active_plugins'       => $plugins_out,
            'mu_plugins_count'     => $mu_plugins_count,
            'dropins_count'        => $dropins_count,
            'paused_plugins_count' => $paused_count,
            'cache_plugins'        => $cache_plugins,
            'multilingual'         => $multilingual,
            'maintenance_active'   => $maintenance_active,
            'opcache_enabled'      => $opcache_enabled,
            'memory_limit'         => (string) ini_get('memory_limit'),
            'site_url'             => (string) site_url(),
            'home_url'             => (string) home_url(),
        ];
    }

    public static function handle_health(WP_REST_Request $request): WP_REST_Response {
        unset($request);
        return new WP_REST_Response(self::build_health_payload(), 200);
    }

    public static function handle_revisions(WP_REST_Request $request): WP_REST_Response {
        $post_id = absint($request->get_param('post_id'));
        if ($post_id <= 0) {
            return new WP_REST_Response([
                'ok'        => true,
                'post_id'   => 0,
                'revisions' => [],
                'snapshots' => Azom_Ops_Bridge_Snapshots::list_local_snapshots(),
            ], 200);
        }
        $revs = wp_get_post_revisions($post_id, ['posts_per_page' => 20]);
        $items = [];
        foreach ($revs as $rev) {
            $items[] = [
                'id'           => (int) $rev->ID,
                'parent'       => (int) $rev->post_parent,
                'author'       => (int) $rev->post_author,
                'modified_gmt' => (string) $rev->post_modified_gmt,
                'title'        => (string) $rev->post_title,
            ];
        }
        return new WP_REST_Response([
            'ok'        => true,
            'post_id'   => $post_id,
            'revisions' => $items,
            'snapshots' => Azom_Ops_Bridge_Snapshots::list_local_snapshots(),
        ], 200);
    }

    private static function detect_cache_plugins(array $active_plugins): array {
        $known = [
            'litespeed-cache' => 'litespeed',
            'wp-rocket'       => 'wp_rocket',
            'w3-total-cache'  => 'w3tc',
            'wp-super-cache'  => 'wp_super_cache',
            'sg-cachepress'   => 'sg_optimizer',
            'autoptimize'     => 'autoptimize',
            'redis-cache'     => 'redis_object_cache',
        ];
        $found = [];
        foreach ($active_plugins as $slug) {
            $dir = strtok((string) $slug, '/');
            if (isset($known[$dir])) {
                $found[] = $known[$dir];
            }
        }
        return array_values(array_unique($found));
    }
}
