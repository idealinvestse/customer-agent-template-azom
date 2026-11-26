<?php
if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_filter(
    'pre_http_request',
    static function ( $preempt, $parsed_args, $url ) {
        $host = strtolower( (string) parse_url( (string) $url, PHP_URL_HOST ) );
        if ( $host === '127.0.0.1' || $host === 'localhost' ) {
            return $preempt;
        }
        if ( defined( 'AZOM_EGRESS_LOG' ) ) {
            $entry = json_encode(
                array(
                    'url'    => (string) $url,
                    'host'   => $host,
                    'source' => 'wordpress_http',
                ),
                JSON_UNESCAPED_SLASHES
            );
            @file_put_contents( AZOM_EGRESS_LOG, $entry . "\n", FILE_APPEND | LOCK_EX );
        }
        return new WP_Error( 'azom_egress_blocked', 'External HTTP blocked in synthetic review sandbox.' );
    },
    1,
    3
);
