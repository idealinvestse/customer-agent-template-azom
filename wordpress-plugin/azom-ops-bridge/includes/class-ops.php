<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Ops {
    private const MAX_MAINTENANCE_SECONDS = 900;
    private const MAINTENANCE_OPTION = 'azom_ops_maintenance_until';
    private const PROBE_MAX_SKEW_SECONDS = 60;
    private const MAX_CANARY_PATHS = 6;
    private const BLOCKED_CANARY_PREFIXES = [
        '/wp-admin',
        '/wp-login',
        '/wp-json',
        '/xmlrpc',
    ];
    private const FATAL_BODY_MARKERS = [
        'Fatal error:',
        'Parse error:',
        'There has been a critical error on this website',
        'Det har uppstått ett kritiskt fel på den här webbplatsen',
        'Det har oppstått en kritisk feil på dette nettstedet',
        'Der er opstået en kritisk fejl på dette websted',
    ];

    public static function build_probe_headers(): array {
        $headers = ['Cache-Control' => 'no-cache'];
        if (Azom_Ops_Bridge_Auth::is_secret_configured()) {
            $ts = (string) time();
            $sig = hash_hmac('sha256', 'azom-probe:' . $ts, trim((string) AZOM_BRIDGE_SECRET));
            $headers['X-Azom-Health-Probe'] = $ts . '.' . $sig;
        }
        return $headers;
    }

    public static function sanitize_canary_paths($raw_paths) {
        if ($raw_paths === null) {
            return ['/'];
        }
        if (!is_array($raw_paths)) {
            return new WP_Error('invalid_canary_paths', 'canary_paths must be an array of relative paths.');
        }
        if (empty($raw_paths)) {
            return ['/'];
        }
        $clean = [];
        $seen = [];
        foreach ($raw_paths as $entry) {
            if (!is_string($entry)) {
                return new WP_Error('invalid_canary_path', 'canary_paths entries must be strings.');
            }
            $path = trim($entry);
            if ($path === '' || $path[0] !== '/') {
                return new WP_Error('invalid_canary_path', 'Canary path must be a relative path starting with /.');
            }
            if (
                strpos($path, '..') !== false
                || strpos($path, '//') !== false
                || strpos($path, '://') !== false
                || strpos($path, '?') !== false
                || strpos($path, '#') !== false
                || strpos($path, "\0") !== false
            ) {
                return new WP_Error('invalid_canary_path', 'Unsafe characters in canary_paths entry.');
            }
            if (preg_match('#^/(?:[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?)?$#', $path) !== 1 || strlen($path) > 120) {
                return new WP_Error('invalid_canary_path', 'Invalid canary_paths entry format.');
            }
            $lower = strtolower($path);
            foreach (self::BLOCKED_CANARY_PREFIXES as $blocked) {
                if ($lower === $blocked || strpos($lower, $blocked . '/') === 0 || strpos($lower, $blocked . '.') === 0) {
                    return new WP_Error('blocked_canary_path', 'Blocked administrative path in canary_paths.');
                }
            }
            if (!isset($seen[$path])) {
                $seen[$path] = true;
                $clean[] = $path;
                if (count($clean) >= self::MAX_CANARY_PATHS) {
                    break;
                }
            }
        }
        return empty($clean) ? ['/'] : $clean;
    }

    public static function detect_fatal_body(string $body, int $status = 200): bool {
        if ($status >= 200 && $status < 300 && trim($body) === '') {
            return true;
        }
        foreach (self::FATAL_BODY_MARKERS as $marker) {
            if (stripos($body, $marker) !== false) {
                return true;
            }
        }
        return false;
    }

    public static function probe_canary_paths(array $paths, ?array $probe_headers = null): array {
        $targets = empty($paths) ? ['/'] : array_slice($paths, 0, self::MAX_CANARY_PATHS);
        $headers = $probe_headers !== null ? $probe_headers : self::build_probe_headers();
        $results = [];
        $all_ok = true;
        $overall_status = 200;

        foreach ($targets as $path) {
            $probe_url = add_query_arg('azom_probe', (string) time(), home_url((string) $path));
            $probe = wp_remote_get($probe_url, [
                'timeout'     => 8,
                'sslverify'   => false,
                'redirection' => 2,
                'headers'     => $headers,
            ]);
            if (is_wp_error($probe)) {
                $results[] = [
                    'path'           => (string) $path,
                    'status'         => 0,
                    'ok'             => false,
                    'fatal_detected' => false,
                ];
                $all_ok = false;
                $overall_status = 500;
                continue;
            }
            $code = (int) wp_remote_retrieve_response_code($probe);
            $body = (string) wp_remote_retrieve_body($probe);
            $status_ok = ($code >= 200 && $code < 400);
            $fatal = self::detect_fatal_body($body, $code);
            $path_ok = $status_ok && !$fatal;
            $results[] = [
                'path'           => (string) $path,
                'status'         => $code,
                'ok'             => $path_ok,
                'fatal_detected' => $fatal,
            ];
            if (!$path_ok) {
                $all_ok = false;
                $overall_status = ($code >= 400) ? $code : 500;
            }
        }

        return [
            'ok'             => $all_ok,
            'http_status'    => $overall_status,
            'canary_results' => $results,
        ];
    }

    public static function is_valid_probe_request(): bool {
        $raw = isset($_SERVER['HTTP_X_AZOM_HEALTH_PROBE']) ? trim((string) $_SERVER['HTTP_X_AZOM_HEALTH_PROBE']) : '';
        if ($raw === '' || !Azom_Ops_Bridge_Auth::is_secret_configured()) {
            return false;
        }
        if (preg_match('/^(\d{10,12})\.([a-f0-9]{64})$/i', $raw, $m) !== 1) {
            return false;
        }
        $ts = (int) $m[1];
        if (abs(time() - $ts) > self::PROBE_MAX_SKEW_SECONDS) {
            return false;
        }
        $expected = hash_hmac('sha256', 'azom-probe:' . $m[1], trim((string) AZOM_BRIDGE_SECRET));
        return hash_equals($expected, strtolower($m[2]));
    }

    public static function flush_all_caches(): array {
        $flushed = [];
        if (function_exists('wp_cache_flush')) {
            wp_cache_flush();
            $flushed[] = 'wp_object_cache';
        }
        if (function_exists('wc_delete_product_transients')) {
            wc_delete_product_transients();
            $flushed[] = 'wc_product_transients';
        }
        if (defined('LSCWP_V') && has_action('litespeed_purge_all')) {
            do_action('litespeed_purge_all');
            $flushed[] = 'litespeed';
        }
        if (function_exists('rocket_clean_domain')) {
            rocket_clean_domain();
            $flushed[] = 'wp_rocket';
        }
        if (function_exists('w3tc_flush_all')) {
            w3tc_flush_all();
            $flushed[] = 'w3tc';
        }
        if (function_exists('wp_cache_clear_cache')) {
            wp_cache_clear_cache();
            $flushed[] = 'wp_super_cache';
        }
        if (function_exists('sg_cachepress_purge_cache')) {
            sg_cachepress_purge_cache();
            $flushed[] = 'sg_optimizer';
        }
        if (class_exists('autoptimizeCache') && method_exists('autoptimizeCache', 'clearall')) {
            \autoptimizeCache::clearall();
            $flushed[] = 'autoptimize';
        }
        return $flushed;
    }

    public static function is_maintenance_active(): bool {
        $until = (int) get_option(self::MAINTENANCE_OPTION, 0);
        if ($until > 0 && time() < $until) {
            return true;
        }
        if ($until > 0) {
            delete_option(self::MAINTENANCE_OPTION);
        }
        return is_file(ABSPATH . '.maintenance');
    }

    public static function maybe_render_maintenance(): void {
        if (!self::is_maintenance_active()) {
            return;
        }
        if (defined('WP_CLI') && WP_CLI) {
            return;
        }
        if (function_exists('current_user_can') && current_user_can('manage_options')) {
            return;
        }
        if (self::is_valid_probe_request()) {
            return;
        }
        $uri = isset($_SERVER['REQUEST_URI']) ? (string) $_SERVER['REQUEST_URI'] : '';
        $path = (string) (wp_parse_url($uri, PHP_URL_PATH) ?: '');
        $rest_route = isset($_GET['rest_route']) ? (string) $_GET['rest_route'] : '';
        $is_bridge_path = ($path !== '' && preg_match('#/(?:wp-json/)?azom-ops/v1(?:/|$)#', $path) === 1);
        $is_bridge_route = (strpos($rest_route, '/azom-ops/v1/') === 0 || $rest_route === '/azom-ops/v1');
        if ($is_bridge_path || $is_bridge_route) {
            return;
        }
        $until = (int) get_option(self::MAINTENANCE_OPTION, 0);
        $retry_after = max(30, $until > time() ? ($until - time()) : 120);
        status_header(503);
        header('Retry-After: ' . $retry_after);
        wp_die(
            esc_html__('Webplatsen uppdateras just nu. Försök igen om en kort stund.', 'azom-ops-bridge'),
            esc_html__('Underhållsläge', 'azom-ops-bridge'),
            ['response' => 503]
        );
    }

    public static function handle_cache_flush(WP_REST_Request $request): WP_REST_Response {
        unset($request);
        $flushed = self::flush_all_caches();
        return new WP_REST_Response([
            'ok'      => true,
            'flushed' => $flushed,
        ], 200);
    }

    public static function handle_maintenance(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }
        $enabled = !empty($payload['enabled']);
        $legacy_file = ABSPATH . '.maintenance';
        if (is_file($legacy_file)) {
            @unlink($legacy_file);
        }
        if (!$enabled) {
            delete_option(self::MAINTENANCE_OPTION);
            return new WP_REST_Response(['ok' => true, 'enabled' => false], 200);
        }
        $duration = (int) ($payload['duration_seconds'] ?? 300);
        if ($duration < 30) {
            $duration = 30;
        }
        if ($duration > self::MAX_MAINTENANCE_SECONDS) {
            $duration = self::MAX_MAINTENANCE_SECONDS;
        }
        // Option-backed timed maintenance keeps /wp-json/azom-ops/v1/* reachable so
        // /maintenance {"enabled":false} and /apply health probes cannot lock themselves out.
        $until = time() + $duration;
        update_option(self::MAINTENANCE_OPTION, $until, false);
        return new WP_REST_Response([
            'ok'               => true,
            'enabled'          => true,
            'duration_seconds' => $duration,
            'until'            => $until,
        ], 200);
    }
}
