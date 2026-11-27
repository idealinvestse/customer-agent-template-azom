<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Snapshots {
    private const ALLOWED_EXTENSIONS = ['php', 'css', 'scss', 'js', 'json', 'txt', 'md', 'html', 'pot', 'po'];
    private const ALLOWED_OPS = ['created', 'updated', 'deleted'];
    private const ALLOWED_COMPONENT_KINDS = ['theme', 'plugin', 'mu_plugin'];
    private const BLOCKED_SLUGS = ['woocommerce', 'azom-ops-bridge'];
    private const BLOCKED_DROPIN_FILES = [
        'advanced-cache.php',
        'db.php',
        'db-error.php',
        'install.php',
        'maintenance.php',
        'object-cache.php',
        'php-error.php',
        'fatal-error-handler.php',
        'sunrise.php',
        'blog-deleted.php',
        'blog-inactive.php',
        'blog-suspended.php',
    ];
    private const MAX_FILE_BYTES = 65536;
    private const MAX_SNAPSHOT_READ_BYTES = 358400;
    private const MAX_STORED_SNAPSHOTS = 50;

    public static function snapshot_dir(): string {
        $upload = wp_upload_dir();
        if (!empty($upload['error']) || empty($upload['basedir'])) {
            return '';
        }
        $dir = trailingslashit((string) $upload['basedir']) . 'azom-ops-snapshots';
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

    public static function list_local_snapshots(): array {
        $dir = self::snapshot_dir();
        if ($dir === '' || !is_dir($dir)) {
            return [];
        }
        $files = glob($dir . '/snap-*.json') ?: [];
        usort($files, static function (string $a, string $b): int {
            return (int) @filemtime($b) <=> (int) @filemtime($a);
        });
        $out = [];
        foreach (array_slice($files, 0, 20) as $file) {
            $out[] = basename($file);
        }
        return $out;
    }

    private static function prune_snapshots(string $dir): void {
        if ($dir === '' || !is_dir($dir)) {
            return;
        }
        $files = glob($dir . '/snap-*.json') ?: [];
        if (count($files) <= self::MAX_STORED_SNAPSHOTS) {
            return;
        }
        usort($files, static function (string $a, string $b): int {
            return (int) @filemtime($b) <=> (int) @filemtime($a);
        });
        foreach (array_slice($files, self::MAX_STORED_SNAPSHOTS) as $stale) {
            @unlink($stale);
        }
    }

    public static function handle_get_snapshot(WP_REST_Request $request): WP_REST_Response {
        $kind = sanitize_key((string) $request->get_param('kind'));
        if ($kind === 'theme_file' || $kind === 'plugin_file' || $kind === 'mu_plugin_file' || $kind === 'mu_plugin') {
            if ($kind === 'theme_file') {
                $comp_kind = 'theme';
            } elseif ($kind === 'plugin_file') {
                $comp_kind = 'plugin';
            } else {
                $comp_kind = 'mu_plugin';
            }
            $slug = trim((string) $request->get_param('slug'));
            $rel  = (string) $request->get_param('path');
            $resolved = self::resolve_component_file($comp_kind, $slug, $rel, false);
            if (is_wp_error($resolved)) {
                if ($comp_kind === 'mu_plugin' && $resolved->get_error_code() === 'missing_mu_dir') {
                    return new WP_REST_Response([
                        'ok'      => true,
                        'kind'    => $kind,
                        'exists'  => false,
                        'sha256'  => hash('sha256', ''),
                        'bytes'   => 0,
                        'content' => '',
                    ], 200);
                }
                return new WP_REST_Response(['ok' => false, 'error' => $resolved->get_error_message()], 400);
            }
            $exists = is_file($resolved);
            if ($exists) {
                $sz = @filesize($resolved);
                if ($sz === false || $sz > self::MAX_SNAPSHOT_READ_BYTES) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Target file exceeds snapshot read limit'], 413);
                }
            }
            $bytes = '';
            if ($exists) {
                $read = file_get_contents($resolved);
                if ($read === false) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Failed to read file'], 500);
                }
                $bytes = $read;
            }
            return new WP_REST_Response([
                'ok'      => true,
                'kind'    => $kind,
                'exists'  => $exists,
                'sha256'  => hash('sha256', $bytes),
                'bytes'   => strlen($bytes),
                'content' => base64_encode($bytes),
            ], 200);
        }

        if ($kind === 'settings') {
            $data = [
                'title'       => (string) get_option('blogname', ''),
                'description' => (string) get_option('blogdescription', ''),
            ];
            $sha = self::canonical_sha256($data);
            if ($sha === '') {
                return new WP_REST_Response(['ok' => false, 'error' => 'Snapshot JSON encoding failed'], 500);
            }
            return new WP_REST_Response(['ok' => true, 'kind' => 'settings', 'data' => $data, 'sha256' => $sha], 200);
        }

        if ($kind === 'page' || $kind === 'post') {
            $id = absint($request->get_param('id'));
            $post = $id > 0 ? get_post($id) : null;
            if (!$post || $post->post_type !== $kind) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Not found'], 404);
            }
            $data = [
                'id'           => (int) $post->ID,
                'title'        => (string) $post->post_title,
                'content'      => (string) $post->post_content,
                'status'       => (string) $post->post_status,
                'modified_gmt' => (string) $post->post_modified_gmt,
            ];
            $sha = self::canonical_sha256($data);
            if ($sha === '') {
                return new WP_REST_Response(['ok' => false, 'error' => 'Snapshot JSON encoding failed'], 500);
            }
            return new WP_REST_Response(['ok' => true, 'kind' => $kind, 'data' => $data, 'sha256' => $sha], 200);
        }

        return new WP_REST_Response(['ok' => false, 'error' => 'Unsupported snapshot kind'], 400);
    }

    public static function handle_apply(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }
        $change_id = trim((string) ($payload['change_id'] ?? ''));
        if (!preg_match('/^[A-Za-z0-9_-]{4,48}$/', $change_id)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change_id'], 400);
        }
        $comp_kind = sanitize_key((string) ($payload['kind'] ?? ''));
        $slug      = trim((string) ($payload['slug'] ?? ''));
        $files     = $payload['files'] ?? null;
        if (!in_array($comp_kind, self::ALLOWED_COMPONENT_KINDS, true) || !is_array($files) || !$files) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid apply payload'], 400);
        }

        $pre_state = [];
        $targets   = [];
        foreach ($files as $item) {
            if (!is_array($item)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid file entry'], 400);
            }
            $rel = (string) ($item['path'] ?? '');
            $op  = (string) ($item['op'] ?? 'updated');
            if (!in_array($op, self::ALLOWED_OPS, true)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Unsupported file operation', 'path' => $rel], 400);
            }
            $expected_sha = (string) ($item['expected_before_sha256'] ?? '');
            $resolved = self::resolve_component_file($comp_kind, $slug, $rel, $op !== 'deleted');
            if (is_wp_error($resolved)) {
                return new WP_REST_Response(['ok' => false, 'error' => $resolved->get_error_message()], 400);
            }

            $exists = is_file($resolved);
            if ($op === 'created' && $exists) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Target file already exists for create', 'path' => $rel], 409);
            }
            $prev_bytes = null;
            if ($exists) {
                $read = file_get_contents($resolved);
                if ($read === false) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Unreadable target file', 'path' => $rel], 500);
                }
                $prev_bytes = $read;
            }
            if ($expected_sha !== '') {
                $actual_sha = $prev_bytes !== null ? hash('sha256', $prev_bytes) : hash('sha256', '');
                if (!hash_equals($expected_sha, $actual_sha)) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Stale target digest mismatch', 'path' => $rel], 409);
                }
            }

            $prev_mode = 0644;
            if ($exists) {
                $perms = @fileperms($resolved);
                if ($perms !== false) {
                    $prev_mode = $perms & 0777;
                }
            }

            $new_bytes = null;
            if ($op !== 'deleted') {
                $b64 = (string) ($item['content_b64'] ?? '');
                $decoded = base64_decode($b64, true);
                if ($decoded === false || strlen($decoded) > self::MAX_FILE_BYTES) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Invalid or oversized file bytes', 'path' => $rel], 400);
                }
                if (strtolower(pathinfo($resolved, PATHINFO_EXTENSION)) === 'php' && !self::php_tokens_valid($decoded)) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'PHP tokenizer check failed', 'path' => $rel], 422);
                }
                $new_bytes = $decoded;
            }

            $pre_state[$rel] = [
                'resolved' => $resolved,
                'existed'  => $exists,
                'bytes'    => $prev_bytes,
                'mode'     => $prev_mode,
            ];
            $targets[] = [
                'rel'      => $rel,
                'op'       => $op,
                'resolved' => $resolved,
                'bytes'    => $new_bytes,
                'mode'     => $prev_mode,
            ];
        }

        // Persist local pre-change snapshot copy before touching target files.
        $snap_dir = self::snapshot_dir();
        if ($snap_dir === '') {
            return new WP_REST_Response(['ok' => false, 'error' => 'Snapshot storage directory unavailable'], 500);
        }
        $snap_path = $snap_dir . '/snap-' . $change_id . '.json';
        $encoded_snap = [];
        foreach ($pre_state as $rel => $st) {
            $encoded_snap[$rel] = [
                'existed' => $st['existed'],
                'mode'    => $st['mode'],
                'b64'     => $st['bytes'] !== null ? base64_encode($st['bytes']) : '',
            ];
        }
        $snap_json = wp_json_encode(['change_id' => $change_id, 'kind' => $comp_kind, 'slug' => $slug, 'files' => $encoded_snap]);
        if ($snap_json === false || @file_put_contents($snap_path, $snap_json, LOCK_EX) === false) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Failed to persist local snapshot'], 500);
        }
        self::prune_snapshots($snap_dir);

        $written_rels = [];
        foreach ($targets as $t) {
            if ($t['op'] === 'deleted') {
                if (is_file($t['resolved'])) {
                    if (!@unlink($t['resolved'])) {
                        $rb_errors = self::restore_pre_state(array_intersect_key($pre_state, array_flip($written_rels)));
                        return new WP_REST_Response([
                            'ok'              => false,
                            'error'           => 'Failed to unlink target file; reverted.',
                            'path'            => $t['rel'],
                            'auto_reverted'   => empty($rb_errors),
                            'rollback_errors' => $rb_errors,
                        ], 500);
                    }
                    self::invalidate_opcache($t['resolved']);
                }
                $written_rels[] = $t['rel'];
                continue;
            }
            if (!self::atomic_write_target($t['resolved'], (string) $t['bytes'], (int) $t['mode'])) {
                $rb_errors = self::restore_pre_state(array_intersect_key($pre_state, array_flip($written_rels)));
                return new WP_REST_Response([
                    'ok'              => false,
                    'error'           => 'Failed to write target file atomically; reverted.',
                    'path'            => $t['rel'],
                    'auto_reverted'   => empty($rb_errors),
                    'rollback_errors' => $rb_errors,
                ], 500);
            }
            $written_rels[] = $t['rel'];
        }

        // Flush caches BEFORE the loopback probe so stale page cache cannot mask a PHP fatal.
        // DB-backed nonces in Azom_Ops_Bridge_Auth remain intact across wp_cache_flush().
        Azom_Ops_Bridge_Ops::flush_all_caches();

        // Loopback health probe — auto-rollback immediately on WP_Error or HTTP >= 400.
        $probe_url = add_query_arg('azom_probe', (string) time(), home_url('/'));
        $probe = wp_remote_get($probe_url, [
            'timeout'   => 5,
            'sslverify' => false,
            'headers'   => Azom_Ops_Bridge_Ops::build_probe_headers(),
        ]);
        $code = is_wp_error($probe) ? 500 : (int) wp_remote_retrieve_response_code($probe);
        if ($code >= 400 || $code === 0) {
            $rb_errors = self::restore_pre_state($pre_state);
            Azom_Ops_Bridge_Ops::flush_all_caches();
            return new WP_REST_Response([
                'ok'              => false,
                'error'           => 'Post-apply loopback health probe failed; reverted automatically.',
                'http_status'     => $code,
                'auto_reverted'   => empty($rb_errors),
                'rollback_errors' => $rb_errors,
            ], 500);
        }

        return new WP_REST_Response([
            'ok'            => true,
            'change_id'     => $change_id,
            'files_written' => count($targets),
            'http_status'   => $code,
        ], 200);
    }

    public static function handle_rollback(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }
        $comp_kind = sanitize_key((string) ($payload['kind'] ?? ''));
        $slug      = trim((string) ($payload['slug'] ?? ''));
        $files     = $payload['files'] ?? null;
        if (!in_array($comp_kind, self::ALLOWED_COMPONENT_KINDS, true) || !is_array($files) || !$files) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid rollback payload'], 400);
        }

        // Pre-validate every rollback entry and capture pre-state before mutating disk so mid-loop failures revert cleanly.
        $validated = [];
        $pre_state = [];
        foreach ($files as $item) {
            if (!is_array($item)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid file entry'], 400);
            }
            $rel = (string) ($item['path'] ?? '');
            $op  = (string) ($item['op'] ?? 'updated');
            if (!in_array($op, self::ALLOWED_OPS, true)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Unsupported rollback op', 'path' => $rel], 400);
            }
            $resolved = self::resolve_component_file($comp_kind, $slug, $rel, $op !== 'deleted');
            if (is_wp_error($resolved)) {
                return new WP_REST_Response(['ok' => false, 'error' => $resolved->get_error_message()], 400);
            }
            $decoded = null;
            if ($op !== 'deleted') {
                $decoded = base64_decode((string) ($item['content_b64'] ?? ''), true);
                if ($decoded === false || strlen($decoded) > self::MAX_FILE_BYTES) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Invalid rollback bytes', 'path' => $rel], 400);
                }
            }
            $existed = is_file($resolved);
            $prev_bytes = null;
            $mode = 0644;
            if ($existed) {
                $read = file_get_contents($resolved);
                if ($read === false) {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Unreadable rollback target', 'path' => $rel], 500);
                }
                $prev_bytes = $read;
                $perms = @fileperms($resolved);
                if ($perms !== false) {
                    $mode = $perms & 0777;
                }
            }
            $pre_state[$rel] = [
                'resolved' => $resolved,
                'existed'  => $existed,
                'bytes'    => $prev_bytes,
                'mode'     => $mode,
            ];
            $validated[] = ['rel' => $rel, 'op' => $op, 'resolved' => $resolved, 'bytes' => $decoded, 'mode' => $mode];
        }

        $applied = 0;
        $written_rels = [];
        foreach ($validated as $entry) {
            if ($entry['op'] === 'deleted') {
                if (is_file($entry['resolved'])) {
                    if (!@unlink($entry['resolved'])) {
                        $rb_errors = self::restore_pre_state(array_intersect_key($pre_state, array_flip($written_rels)));
                        return new WP_REST_Response([
                            'ok'              => false,
                            'error'           => 'Rollback unlink failed; reverted earlier files.',
                            'path'            => $entry['rel'],
                            'auto_reverted'   => empty($rb_errors),
                            'rollback_errors' => $rb_errors,
                        ], 500);
                    }
                    self::invalidate_opcache($entry['resolved']);
                }
                $written_rels[] = $entry['rel'];
                $applied++;
                continue;
            }
            if (!self::atomic_write_target($entry['resolved'], (string) $entry['bytes'], (int) $entry['mode'])) {
                $rb_errors = self::restore_pre_state(array_intersect_key($pre_state, array_flip($written_rels)));
                return new WP_REST_Response([
                    'ok'              => false,
                    'error'           => 'Rollback write failed; reverted earlier files.',
                    'path'            => $entry['rel'],
                    'auto_reverted'   => empty($rb_errors),
                    'rollback_errors' => $rb_errors,
                ], 500);
            }
            $written_rels[] = $entry['rel'];
            $applied++;
        }
        Azom_Ops_Bridge_Ops::flush_all_caches();
        return new WP_REST_Response(['ok' => true, 'restored_files' => $applied], 200);
    }

    private static function canonical_sha256(array $data): string {
        ksort($data, SORT_STRING);
        $json = wp_json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if (!is_string($json)) {
            return '';
        }
        return hash('sha256', $json);
    }

    private static function atomic_write_target(string $resolved, string $bytes, int $mode): bool {
        $dir = dirname($resolved);
        if (!is_dir($dir) && !wp_mkdir_p($dir)) {
            return false;
        }
        $tmp = $resolved . '.azom-tmp-' . wp_generate_password(8, false, false);
        if (@file_put_contents($tmp, $bytes) === false) {
            @unlink($tmp);
            return false;
        }
        @chmod($tmp, $mode);
        if (!@rename($tmp, $resolved)) {
            @unlink($tmp);
            return false;
        }
        self::invalidate_opcache($resolved);
        return true;
    }

    public static function invalidate_opcache(string $path): void {
        $restrict_api = trim((string) ini_get('opcache.restrict_api'));
        if ($restrict_api !== '' && strpos(__FILE__, $restrict_api) !== 0) {
            return;
        }
        if (function_exists('opcache_invalidate')) {
            @opcache_invalidate($path, true);
        }
    }

    private static function restore_pre_state(array $pre_state): array {
        $errors = [];
        foreach ($pre_state as $rel => $st) {
            if (!$st['existed']) {
                if (is_file($st['resolved'])) {
                    if (!@unlink($st['resolved'])) {
                        $errors[] = (string) $rel . ': unlink failed';
                    } else {
                        self::invalidate_opcache($st['resolved']);
                    }
                }
            } elseif ($st['bytes'] !== null) {
                $mode = isset($st['mode']) ? (int) $st['mode'] : 0644;
                if (!self::atomic_write_target($st['resolved'], (string) $st['bytes'], $mode)) {
                    $errors[] = (string) $rel . ': write failed';
                }
            }
        }
        return $errors;
    }

    private static function php_tokens_valid(string $code): bool {
        if (!function_exists('token_get_all')) {
            return false;
        }
        try {
            if (defined('TOKEN_PARSE')) {
                @token_get_all($code, TOKEN_PARSE);
            } else {
                @token_get_all($code);
            }
            return true;
        } catch (\ParseError | \CompileError $e) {
            return false;
        }
    }

    private static function resolve_component_file(string $kind, string $slug, string $rel, bool $create_mu_root = false) {
        $clean = trim(str_replace('\\', '/', $rel), '/');
        if ($clean === '' || strpos($clean, '..') !== false || strpos($clean, "\0") !== false) {
            return new WP_Error('invalid_path', 'Invalid relative path.');
        }
        $ext = strtolower(pathinfo($clean, PATHINFO_EXTENSION));
        if (!in_array($ext, self::ALLOWED_EXTENSIONS, true)) {
            return new WP_Error('invalid_ext', 'File extension not allowed.');
        }

        if ($kind === 'mu_plugin') {
            $leaf_lower = strtolower(basename($clean));
            if (in_array($leaf_lower, self::BLOCKED_DROPIN_FILES, true)) {
                return new WP_Error('dropin_blocked', 'WordPress drop-ins are read-only and cannot be modified.');
            }
            $is_root_slug = ($slug === '' || $slug === '_root' || $slug === 'mu-plugins');
            if (!$is_root_slug) {
                if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$/', $slug) || strpos($slug, '..') !== false) {
                    return new WP_Error('invalid_slug', 'Invalid component slug.');
                }
                if (in_array(strtolower($slug), self::BLOCKED_SLUGS, true)) {
                    return new WP_Error('blocked_slug', 'Component is protected.');
                }
            }
            $mu_root = defined('WPMU_PLUGIN_DIR') ? (string) WPMU_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'mu-plugins');
            if (is_link($mu_root)) {
                return new WP_Error('symlink_blocked', 'MU-plugin root symlink is refused.');
            }
            if (!is_dir($mu_root)) {
                if (!$create_mu_root || !wp_mkdir_p($mu_root)) {
                    return new WP_Error('missing_mu_dir', 'MU-plugin directory not found.');
                }
            }
            $real_mu = realpath($mu_root);
            if ($real_mu === false || !is_dir($real_mu) || is_link($mu_root)) {
                return new WP_Error('missing_mu_dir', 'MU-plugin directory not found.');
            }

            if ($is_root_slug) {
                $rel_under_mu = $clean;
            } elseif (strtolower($clean) === strtolower($slug) && substr(strtolower($slug), -4) === '.php') {
                $rel_under_mu = $clean;
            } elseif (strtolower($clean) === strtolower($slug . '.php') && !is_dir($real_mu . '/' . $slug) && is_file($real_mu . '/' . $clean)) {
                $rel_under_mu = $clean;
            } else {
                $rel_under_mu = $slug . '/' . $clean;
            }

            $parts = array_values(array_filter(explode('/', $rel_under_mu), static function (string $p): bool {
                return $p !== '';
            }));
            if (empty($parts)) {
                return new WP_Error('invalid_path', 'Invalid MU-plugin relative path.');
            }
            if (in_array(strtolower($parts[0]), self::BLOCKED_SLUGS, true)) {
                return new WP_Error('blocked_slug', 'Component is protected.');
            }
            // Reject any intermediate or leaf symlinks strictly inside WPMU_PLUGIN_DIR.
            $cursor = $real_mu;
            foreach ($parts as $part) {
                if ($part === '.' || $part === '..') {
                    return new WP_Error('invalid_path', 'Path traversal refused.');
                }
                $cursor .= '/' . $part;
                if (is_link($cursor)) {
                    return new WP_Error('symlink_blocked', 'Symlinks are refused.');
                }
            }
            $target = $real_mu . '/' . implode('/', $parts);
            // Walk up to the nearest existing ancestor and verify it stays strictly within WPMU_PLUGIN_DIR.
            $ancestor = dirname($target);
            while ($ancestor !== '' && $ancestor !== dirname($ancestor) && !file_exists($ancestor)) {
                $ancestor = dirname($ancestor);
            }
            $ancestor_real = realpath($ancestor);
            $prefix = rtrim($real_mu, '/\\') . DIRECTORY_SEPARATOR;
            if ($ancestor_real === false || ($ancestor_real !== $real_mu && strpos($ancestor_real . DIRECTORY_SEPARATOR, $prefix) !== 0)) {
                return new WP_Error('symlink_blocked', 'Resolved parent escaped WPMU_PLUGIN_DIR boundary.');
            }
            return $target;
        }

        if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$/', $slug) || strpos($slug, '..') !== false) {
            return new WP_Error('invalid_slug', 'Invalid component slug.');
        }
        if (in_array(strtolower($slug), self::BLOCKED_SLUGS, true)) {
            return new WP_Error('blocked_slug', 'Component is protected.');
        }
        if ($kind === 'theme') {
            $root_parent = function_exists('get_theme_root') ? (string) get_theme_root($slug) : (trailingslashit(WP_CONTENT_DIR) . 'themes');
        } else {
            $root_parent = defined('WP_PLUGIN_DIR') ? (string) WP_PLUGIN_DIR : (trailingslashit(WP_CONTENT_DIR) . 'plugins');
        }
        $base_dir = trailingslashit($root_parent) . $slug;
        $real_base = realpath($base_dir);
        if ($real_base === false || !is_dir($real_base) || is_link($base_dir)) {
            return new WP_Error('missing_component', 'Component directory not found.');
        }
        // Reject any intermediate or leaf symlinks inside the component directory.
        $parts = array_values(array_filter(explode('/', $clean), static function (string $p): bool {
            return $p !== '';
        }));
        $cursor = $real_base;
        foreach ($parts as $part) {
            $cursor .= '/' . $part;
            if (is_link($cursor)) {
                return new WP_Error('symlink_blocked', 'Symlinks are refused.');
            }
        }
        $target = $real_base . '/' . $clean;
        $parent_real = realpath(dirname($target));
        if ($parent_real !== false) {
            $prefix = rtrim($real_base, '/\\') . DIRECTORY_SEPARATOR;
            if ($parent_real !== $real_base && strpos($parent_real . DIRECTORY_SEPARATOR, $prefix) !== 0) {
                return new WP_Error('symlink_blocked', 'Resolved parent escaped component boundary.');
            }
        }
        return $target;
    }
}
