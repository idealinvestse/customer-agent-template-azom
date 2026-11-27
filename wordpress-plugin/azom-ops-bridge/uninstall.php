<?php

if (!defined('ABSPATH') && !defined('WP_UNINSTALL_PLUGIN')) {
    exit;
}

delete_option('azom_ops_maintenance_until');

global $wpdb;
if (isset($wpdb) && method_exists($wpdb, 'query')) {
    $table = $wpdb->prefix . 'azom_ops_nonces';
    $wpdb->query("DROP TABLE IF EXISTS {$table}");
    $wpdb->query("DELETE FROM {$wpdb->options} WHERE option_name LIKE 'azom_ops_nonce_%'");
}
