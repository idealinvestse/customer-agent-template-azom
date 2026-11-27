<?php
/**
 * Plugin Name: Azom Ops Bridge
 * Plugin URI:  https://azom.no
 * Description: Authenticated, fail-closed health, multilingual, cache, snapshot, plugin lifecycle, SEO metadata/media, and atomic rollback bridge for AzomOps-Agent.
 * Version:     1.2.0
 * Requires at least: 6.4
 * Requires PHP: 8.1
 * Author:      Azom Ops
 * License:     GPL-2.0-or-later
 * Text Domain: azom-ops-bridge
 * WC requires at least: 8.0
 * WC tested up to: 9.6
 */

if (!defined('ABSPATH')) {
    exit;
}

define('AZOM_OPS_BRIDGE_VERSION', '1.2.0');
define('AZOM_OPS_BRIDGE_DIR', plugin_dir_path(__FILE__));

require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-auth.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-health.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-translations.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-snapshots.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-ops.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-plugins.php';
require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-seo.php';

register_activation_hook(__FILE__, [Azom_Ops_Bridge_Auth::class, 'install_table']);

add_action('before_woocommerce_init', static function (): void {
    if (class_exists(\Automattic\WooCommerce\Utilities\FeaturesUtil::class)) {
        \Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);
    }
});

// Exempt /wp-json/azom-ops/v1/* from third-party "Disable REST API" / Force Login gates;
// every route still fails closed inside Azom_Ops_Bridge_Auth::verify_request().
add_filter('rest_authentication_errors', static function ($result) {
    $uri = isset($_SERVER['REQUEST_URI']) ? (string) $_SERVER['REQUEST_URI'] : '';
    $path = (string) (wp_parse_url($uri, PHP_URL_PATH) ?: '');
    $rest_route = isset($_GET['rest_route']) ? (string) $_GET['rest_route'] : '';
    $is_bridge_path = ($path !== '' && preg_match('#/(?:wp-json/)?azom-ops/v1(?:/|$)#', $path) === 1);
    $is_bridge_route = (strpos($rest_route, '/azom-ops/v1/') === 0 || $rest_route === '/azom-ops/v1');
    if ($is_bridge_path || $is_bridge_route) {
        if (is_wp_error($result)) {
            return null;
        }
    }
    return $result;
}, PHP_INT_MAX);

add_action('template_redirect', [Azom_Ops_Bridge_Ops::class, 'maybe_render_maintenance'], 0);

add_action('rest_api_init', static function (): void {
    $namespace = 'azom-ops/v1';

    register_rest_route($namespace, '/health', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Health::class, 'handle_health'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/revisions', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Health::class, 'handle_revisions'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/translations', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Translations::class, 'handle_translations'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/snapshot', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Snapshots::class, 'handle_get_snapshot'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/apply', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Snapshots::class, 'handle_apply'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/rollback', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Snapshots::class, 'handle_rollback'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/cache/flush', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Ops::class, 'handle_cache_flush'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/maintenance', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Ops::class, 'handle_maintenance'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/inventory', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_inventory'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/update', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_update'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/rollback', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_rollback'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/autoupdate', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_autoupdate'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/toggle', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_toggle'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/plugins/diagnostics', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Plugins::class, 'handle_diagnostics'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/seo/overview', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Seo::class, 'handle_overview'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/seo/entities', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => [Azom_Ops_Bridge_Seo::class, 'handle_entities'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/seo/meta', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => [Azom_Ops_Bridge_Seo::class, 'handle_update_meta'],
        'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
    ]);

    register_rest_route($namespace, '/seo/media', [
        [
            'methods'             => WP_REST_Server::READABLE,
            'callback'            => [Azom_Ops_Bridge_Seo::class, 'handle_media_inventory'],
            'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
        ],
        [
            'methods'             => WP_REST_Server::CREATABLE,
            'callback'            => [Azom_Ops_Bridge_Seo::class, 'handle_update_media'],
            'permission_callback' => [Azom_Ops_Bridge_Auth::class, 'verify_request'],
        ],
    ]);
});

add_action('admin_menu', static function (): void {
    add_management_page(
        'Azom Ops Bridge',
        'Azom Ops Bridge',
        'manage_options',
        'azom-ops-bridge',
        static function (): void {
            if (!current_user_can('manage_options')) {
                wp_die(esc_html__('Access denied.', 'azom-ops-bridge'));
            }
            $secret_ok = Azom_Ops_Bridge_Auth::is_secret_configured();
            $ips = defined('AZOM_BRIDGE_ALLOWED_IPS') ? (string) AZOM_BRIDGE_ALLOWED_IPS : '';
            echo '<div class="wrap"><h1>Azom Ops Bridge (' . esc_html(AZOM_OPS_BRIDGE_VERSION) . ')</h1>';
            echo '<p><strong>HMAC Secret:</strong> ' . ($secret_ok ? 'Configured (>=32 chars)' : 'MISSING or too short (fail-closed)') . '</p>';
            echo '<p><strong>IP Allowlist:</strong> ' . ($ips !== '' ? esc_html($ips) : 'Not set (HMAC + DB nonce only)') . '</p>';
            echo '<h2>wp-config.php configuration</h2>';
            echo '<pre>define(\'AZOM_BRIDGE_SECRET\', \'&lt;64-char-random-hex&gt;\');' . "\n";
            echo 'define(\'AZOM_BRIDGE_ALLOWED_IPS\', \'&lt;azom-agent-ipv4&gt;/32\');</pre>';
            echo '</div>';
        }
    );
});
