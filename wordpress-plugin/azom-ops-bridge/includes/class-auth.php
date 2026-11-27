<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Auth {
    private const MAX_SKEW_SECONDS = 300;
    private const NONCE_TTL_SECONDS = 600;
    private const MIN_SECRET_LEN = 32;
    private const DB_VERSION_OPTION = 'azom_ops_bridge_nonce_table_version';
    private const DB_SCHEMA_VERSION = '1';
    private static bool $table_checked = false;

    public static function install_table(): void {
        global $wpdb;
        if (!isset($wpdb) || !method_exists($wpdb, 'query')) {
            return;
        }
        $table = self::table_name();
        $charset = method_exists($wpdb, 'get_charset_collate') ? $wpdb->get_charset_collate() : '';
        $sql = "CREATE TABLE IF NOT EXISTS {$table} (
            nonce_hash CHAR(64) NOT NULL,
            expires_at BIGINT UNSIGNED NOT NULL,
            PRIMARY KEY (nonce_hash),
            KEY expires_idx (expires_at)
        ) {$charset};";
        $res = $wpdb->query($sql);
        self::$table_checked = true;
        if ($res !== false && function_exists('update_option')) {
            update_option(self::DB_VERSION_OPTION, self::DB_SCHEMA_VERSION, true);
        }
    }

    private static function ensure_table_installed(): void {
        if (self::$table_checked) {
            return;
        }
        self::$table_checked = true;
        if (function_exists('get_option') && (string) get_option(self::DB_VERSION_OPTION, '') === self::DB_SCHEMA_VERSION) {
            return;
        }
        self::install_table();
    }

    public static function table_name(): string {
        global $wpdb;
        $prefix = isset($wpdb->prefix) ? (string) $wpdb->prefix : 'wp_';
        return $prefix . 'azom_ops_nonces';
    }

    public static function is_secret_configured(): bool {
        if (!defined('AZOM_BRIDGE_SECRET')) {
            return false;
        }
        $secret = trim((string) AZOM_BRIDGE_SECRET);
        return strlen($secret) >= self::MIN_SECRET_LEN;
    }

    public static function verify_request(WP_REST_Request $request) {
        if (!self::is_secret_configured()) {
            return new WP_Error('azom_bridge_unconfigured', 'Bridge secret is not configured.', ['status' => 503]);
        }

        if (!self::ip_allowed()) {
            return new WP_Error('azom_bridge_ip_denied', 'Client IP not allowlisted.', ['status' => 403]);
        }

        $ts_raw = trim((string) $request->get_header('X-Azom-Timestamp'));
        $nonce  = trim((string) $request->get_header('X-Azom-Nonce'));
        $sig    = strtolower(trim((string) $request->get_header('X-Azom-Signature')));

        if ($ts_raw === '' || !ctype_digit($ts_raw) || $nonce === '' || !preg_match('/^[a-f0-9]{16,64}$/', $nonce)) {
            return new WP_Error('azom_bridge_bad_headers', 'Missing or invalid auth headers.', ['status' => 401]);
        }

        $ts = (int) $ts_raw;
        if (abs(time() - $ts) > self::MAX_SKEW_SECONDS) {
            return new WP_Error('azom_bridge_expired', 'Request timestamp outside allowed window.', ['status' => 401]);
        }

        $method = strtoupper((string) $request->get_method());
        $signed_target = self::canonical_route($request);
        $body = (string) $request->get_body();
        $body_hash = hash('sha256', $body);
        $canonical = $ts_raw . "\n" . $nonce . "\n" . $method . "\n" . $signed_target . "\n" . $body_hash;

        $expected = hash_hmac('sha256', $canonical, trim((string) AZOM_BRIDGE_SECRET));
        if (!preg_match('/^[a-f0-9]{64}$/', $sig) || !hash_equals($expected, $sig)) {
            return new WP_Error('azom_bridge_sig_invalid', 'Invalid HMAC signature.', ['status' => 401]);
        }

        if (!self::consume_nonce($nonce)) {
            return new WP_Error('azom_bridge_replay', 'Nonce already used.', ['status' => 401]);
        }
        return true;
    }

    private static function canonical_route(WP_REST_Request $request): string {
        $route = (string) $request->get_route();
        $query_params = $request->get_query_params();
        if (!is_array($query_params)) {
            return $route;
        }
        // Exclude WP internal routing query vars from HMAC target matching.
        unset($query_params['rest_route'], $query_params['_method'], $query_params['_envelope']);
        if (!$query_params) {
            return $route;
        }
        ksort($query_params, SORT_STRING);
        $qs = http_build_query($query_params, '', '&', PHP_QUERY_RFC3986);
        return $qs !== '' ? ($route . '?' . $qs) : $route;
    }

    private static function consume_nonce(string $nonce): bool {
        global $wpdb;
        $hash = hash('sha256', $nonce);
        $now = time();
        $expires = $now + self::NONCE_TTL_SECONDS;

        if (isset($wpdb) && method_exists($wpdb, 'prepare')) {
            self::ensure_table_installed();
            $table = self::table_name();
            // Prune expired nonces so old entries never pile up.
            $wpdb->query($wpdb->prepare("DELETE FROM {$table} WHERE expires_at < %d", $now));
            $inserted = $wpdb->query(
                $wpdb->prepare(
                    "INSERT IGNORE INTO {$table} (nonce_hash, expires_at) VALUES (%s, %d)",
                    $hash,
                    $expires
                )
            );
            if ($inserted === false) {
                self::install_table();
                $inserted = $wpdb->query(
                    $wpdb->prepare(
                        "INSERT IGNORE INTO {$table} (nonce_hash, expires_at) VALUES (%s, %d)",
                        $hash,
                        $expires
                    )
                );
            }
            if (is_int($inserted)) {
                return $inserted === 1;
            }
        }

        // Fail-closed DB option fallback if custom table write failed.
        $opt_key = 'azom_ops_nonce_' . $hash;
        return add_option($opt_key, (string) $expires, '', 'no');
    }

    private static function ip_allowed(): bool {
        if (!defined('AZOM_BRIDGE_ALLOWED_IPS')) {
            return true;
        }
        $raw = trim((string) AZOM_BRIDGE_ALLOWED_IPS);
        if ($raw === '') {
            return true;
        }
        $remote = isset($_SERVER['REMOTE_ADDR']) ? trim((string) $_SERVER['REMOTE_ADDR']) : '';
        if ($remote === '') {
            return false;
        }
        $entries = array_filter(array_map('trim', explode(',', $raw)));
        foreach ($entries as $cidr) {
            if (self::ip_in_cidr($remote, $cidr)) {
                return true;
            }
        }
        return false;
    }

    private static function ip_in_cidr(string $ip, string $cidr): bool {
        if (strpos($cidr, '/') === false) {
            $ip_bin = @inet_pton($ip);
            $cidr_bin = @inet_pton($cidr);
            if ($ip_bin === false || $cidr_bin === false) {
                return false;
            }
            return strlen($ip_bin) === strlen($cidr_bin) && hash_equals($ip_bin, $cidr_bin);
        }
        [$subnet, $bits_str] = explode('/', $cidr, 2);
        $bits_str = trim($bits_str);
        if ($bits_str === '' || !ctype_digit($bits_str)) {
            return false;
        }
        $ip_bin = @inet_pton($ip);
        $sub_bin = @inet_pton(trim($subnet));
        if ($ip_bin === false || $sub_bin === false || strlen($ip_bin) !== strlen($sub_bin)) {
            return false;
        }
        $bits = (int) $bits_str;
        $max_bits = strlen($ip_bin) * 8;
        if ($bits < 0 || $bits > $max_bits) {
            return false;
        }
        $full_bytes = intdiv($bits, 8);
        $rem_bits = $bits % 8;
        if ($full_bytes > 0 && substr($ip_bin, 0, $full_bytes) !== substr($sub_bin, 0, $full_bytes)) {
            return false;
        }
        if ($rem_bits > 0) {
            $mask = (~((1 << (8 - $rem_bits)) - 1)) & 0xFF;
            return (ord($ip_bin[$full_bytes]) & $mask) === (ord($sub_bin[$full_bytes]) & $mask);
        }
        return true;
    }
}
