<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Seo {
    private const ALLOWED_OBJECT_TYPES = ['product', 'page', 'post'];
    private const ALLOWED_META_FIELDS = [
        'meta_title',
        'meta_description',
        'focus_keyphrase',
        'canonical',
        'robots',
        'og_title',
        'og_description',
        'schema_type',
        'slug',
        'title',
        'excerpt',
    ];
    private const ALLOWED_MEDIA_FIELDS = [
        'alt_text',
        'title',
        'caption',
        'description',
    ];
    private const ALLOWED_ROBOTS_TOKENS = [
        'index',
        'noindex',
        'follow',
        'nofollow',
        'noarchive',
        'nosnippet',
        'noimageindex',
    ];

    /**
     * Strict allowlist of writable postmeta keys per detected SEO plugin.
     * Never writes arbitrary user-supplied meta keys.
     */
    private const PLUGIN_META_MAP = [
        'yoast' => [
            'meta_title'       => '_yoast_wpseo_title',
            'meta_description' => '_yoast_wpseo_metadesc',
            'focus_keyphrase'  => '_yoast_wpseo_focuskw',
            'canonical'        => '_yoast_wpseo_canonical',
            'og_title'         => '_yoast_wpseo_opengraph-title',
            'og_description'   => '_yoast_wpseo_opengraph-description',
            'schema_type'      => '_yoast_wpseo_schema_article_type',
        ],
        'rankmath' => [
            'meta_title'       => 'rank_math_title',
            'meta_description' => 'rank_math_description',
            'focus_keyphrase'  => 'rank_math_focus_keyword',
            'canonical'        => 'rank_math_canonical_url',
            'og_title'         => 'rank_math_facebook_title',
            'og_description'   => 'rank_math_facebook_description',
            'schema_type'      => 'rank_math_rich_snippet',
        ],
        'aioseo' => [
            'meta_title'       => '_aioseo_title',
            'meta_description' => '_aioseo_description',
            'focus_keyphrase'  => '_aioseo_keywords',
            'canonical'        => '_aioseo_canonical_url',
            'og_title'         => '_aioseo_og_title',
            'og_description'   => '_aioseo_og_description',
            'schema_type'      => '_aioseo_schema_type',
        ],
        'seopress' => [
            'meta_title'       => '_seopress_titles_title',
            'meta_description' => '_seopress_titles_desc',
            'focus_keyphrase'  => '_seopress_analysis_target_kw',
            'canonical'        => '_seopress_robots_canonical',
            'og_title'         => '_seopress_social_fb_title',
            'og_description'   => '_seopress_social_fb_desc',
            'schema_type'      => '_seopress_pro_rich_snippets_type',
        ],
        'none' => [
            'meta_title'       => '_azom_seo_title',
            'meta_description' => '_azom_seo_description',
            'focus_keyphrase'  => '_azom_seo_focus_keyphrase',
            'canonical'        => '_azom_seo_canonical',
            'og_title'         => '_azom_seo_og_title',
            'og_description'   => '_azom_seo_og_description',
            'schema_type'      => '_azom_seo_schema_type',
        ],
    ];

    public static function detect_seo_plugin(): array {
        if (defined('WPSEO_VERSION') || class_exists('WPSEO_Meta')) {
            return [
                'slug'    => 'yoast',
                'version' => defined('WPSEO_VERSION') ? (string) WPSEO_VERSION : '',
            ];
        }
        if (defined('RANK_MATH_VERSION') || class_exists('RankMath')) {
            return [
                'slug'    => 'rankmath',
                'version' => defined('RANK_MATH_VERSION') ? (string) RANK_MATH_VERSION : '',
            ];
        }
        if (defined('AIOSEO_VERSION') || function_exists('aioseo')) {
            return [
                'slug'    => 'aioseo',
                'version' => defined('AIOSEO_VERSION') ? (string) AIOSEO_VERSION : '',
            ];
        }
        if (defined('SEOPRESS_VERSION')) {
            return [
                'slug'    => 'seopress',
                'version' => (string) SEOPRESS_VERSION,
            ];
        }
        return [
            'slug'    => 'none',
            'version' => '',
        ];
    }

    public static function handle_overview(WP_REST_Request $request): WP_REST_Response {
        unset($request);
        $detected = self::detect_seo_plugin();
        $home = trailingslashit((string) home_url('/'));

        $sitemap_urls = [];
        if ($detected['slug'] === 'yoast' || $detected['slug'] === 'rankmath') {
            $sitemap_urls[] = $home . 'sitemap_index.xml';
        } elseif ($detected['slug'] === 'aioseo' || $detected['slug'] === 'seopress') {
            $sitemap_urls[] = $home . 'sitemap.xml';
        }
        $sitemap_urls[] = $home . 'wp-sitemap.xml';
        $sitemap_urls = array_values(array_unique($sitemap_urls));

        $counts = [
            'products' => self::count_post_type('product'),
            'pages'    => self::count_post_type('page'),
            'posts'    => self::count_post_type('post'),
            'media'    => self::count_attachments(),
        ];

        return new WP_REST_Response([
            'ok'                  => true,
            'seo_plugin'          => $detected['slug'],
            'seo_plugin_version'  => $detected['version'],
            'blog_public'         => (string) get_option('blog_public', '1') !== '0',
            'permalink_structure' => (string) get_option('permalink_structure', ''),
            'home_url'            => rtrim($home, '/'),
            'sitemap_urls'        => $sitemap_urls,
            'robots_url'          => $home . 'robots.txt',
            'multilingual'        => Azom_Ops_Bridge_Translations::detect_provider(),
            'counts'              => $counts,
        ], 200);
    }

    public static function handle_entities(WP_REST_Request $request): WP_REST_Response {
        $detected = self::detect_seo_plugin();
        $plugin = $detected['slug'];

        $raw_type = strtolower(trim((string) ($request->get_param('object_type') ?? 'all')));
        if ($raw_type === '' || $raw_type === 'all') {
            $post_types = [];
            foreach (self::ALLOWED_OBJECT_TYPES as $pt) {
                if (post_type_exists($pt)) {
                    $post_types[] = $pt;
                }
            }
            if (!$post_types) {
                $post_types = ['page', 'post'];
            }
        } elseif (in_array($raw_type, self::ALLOWED_OBJECT_TYPES, true)) {
            $post_types = [$raw_type];
        } else {
            return new WP_REST_Response(['ok' => false, 'error' => 'Unsupported object_type'], 400);
        }

        $home_host = (string) wp_parse_url((string) home_url('/'), PHP_URL_HOST);

        $object_id_raw = $request->get_param('object_id');
        if ($object_id_raw !== null && trim((string) $object_id_raw) !== '') {
            $object_id = absint($object_id_raw);
            if ($object_id <= 0) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid object_id'], 400);
            }
            $post = get_post($object_id);
            $entities = [];
            if (
                $post instanceof WP_Post
                && in_array($post->post_type, $post_types, true)
                && in_array($post->post_status, ['publish', 'draft', 'private'], true)
            ) {
                $entities[] = self::build_entity_row($post, $plugin, $home_host);
            }
            return new WP_REST_Response([
                'ok'         => true,
                'seo_plugin' => $plugin,
                'entities'   => $entities,
                'total'      => count($entities),
            ], 200);
        }

        $limit = max(1, min(100, (int) ($request->get_param('limit') ?? 50)));
        $page = max(1, (int) ($request->get_param('page') ?? 1));

        $query = new WP_Query([
            'post_type'              => $post_types,
            'post_status'            => ['publish', 'draft', 'private'],
            'posts_per_page'         => $limit,
            'paged'                  => $page,
            'orderby'                => 'ID',
            'order'                  => 'ASC',
            'no_found_rows'          => false,
            'update_post_term_cache' => false,
        ]);

        $entities = [];
        foreach ($query->posts as $post) {
            if (!($post instanceof WP_Post)) {
                continue;
            }
            $entities[] = self::build_entity_row($post, $plugin, $home_host);
        }

        return new WP_REST_Response([
            'ok'         => true,
            'seo_plugin' => $plugin,
            'entities'   => $entities,
            'total'      => (int) $query->found_posts,
        ], 200);
    }

    public static function handle_update_meta(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }

        $change_id = trim((string) ($payload['change_id'] ?? ''));
        if ($change_id === '' || strlen($change_id) > 80 || !preg_match('/^[A-Za-z0-9._-]+$/', $change_id)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change_id'], 400);
        }

        $object_type = strtolower(trim((string) ($payload['object_type'] ?? '')));
        if (!in_array($object_type, self::ALLOWED_OBJECT_TYPES, true)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Unsupported object_type'], 400);
        }

        $object_id = absint($payload['object_id'] ?? 0);
        if ($object_id <= 0) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid object_id'], 400);
        }

        $fields = $payload['fields'] ?? null;
        if (!is_array($fields) || !$fields) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Missing fields object'], 400);
        }

        foreach (array_keys($fields) as $key) {
            if (!in_array((string) $key, self::ALLOWED_META_FIELDS, true)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Disallowed SEO meta field: ' . (string) $key], 400);
            }
        }

        $post = get_post($object_id);
        if (!($post instanceof WP_Post) || $post->post_type !== $object_type) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Target entity not found'], 404);
        }

        $detected = self::detect_seo_plugin();
        $plugin = $detected['slug'];

        $before = self::read_entity_seo_snapshot($post, $plugin);
        $before_sha = self::canonical_sha256($before);

        $expected_sha = trim((string) ($payload['expected_before_sha256'] ?? ''));
        if ($expected_sha !== '' && !hash_equals($before_sha, $expected_sha)) {
            return new WP_REST_Response([
                'ok'            => false,
                'error'         => 'Stale SEO meta digest mismatch',
                'before_sha256' => $before_sha,
            ], 409);
        }

        $meta_map = self::PLUGIN_META_MAP[$plugin] ?? self::PLUGIN_META_MAP['none'];
        $post_updates = ['ID' => $object_id];
        $has_post_update = false;
        $meta_writes = [];
        $robots_update = null;

        // Phase 1: Validate and sanitize every field BEFORE any DB write.
        foreach ($fields as $field => $raw_val) {
            $field_name = (string) $field;
            $val = is_scalar($raw_val) ? (string) $raw_val : '';

            if ($field_name === 'slug') {
                $clean_slug = sanitize_title($val);
                if ($clean_slug === '') {
                    return new WP_REST_Response(['ok' => false, 'error' => 'Invalid slug'], 400);
                }
                $post_updates['post_name'] = $clean_slug;
                $has_post_update = true;
                continue;
            }
            if ($field_name === 'title') {
                $post_updates['post_title'] = sanitize_text_field($val);
                $has_post_update = true;
                continue;
            }
            if ($field_name === 'excerpt') {
                $post_updates['post_excerpt'] = sanitize_textarea_field($val);
                $has_post_update = true;
                continue;
            }
            if ($field_name === 'robots') {
                $robots_update = $val;
                continue;
            }
            $meta_key = $field_name === 'schema_type'
                ? self::schema_type_meta_key($plugin, $object_type)
                : ($meta_map[$field_name] ?? '');
            if ($meta_key === '') {
                continue;
            }
            if ($field_name === 'canonical') {
                $clean_val = $val !== '' ? esc_url_raw($val) : '';
            } else {
                $clean_val = sanitize_text_field($val);
            }
            $meta_writes[] = [$meta_key, $clean_val];
        }

        // Phase 2: Run wp_update_post BEFORE postmeta writes so failures never leave partial meta writes.
        if ($has_post_update) {
            $slashed_updates = function_exists('wp_slash') ? wp_slash($post_updates) : $post_updates;
            $updated_id = wp_update_post($slashed_updates, true);
            if (is_wp_error($updated_id)) {
                return new WP_REST_Response([
                    'ok'    => false,
                    'error' => 'Post update failed: ' . $updated_id->get_error_message(),
                ], 500);
            }
        }

        // Phase 3: Apply postmeta and robots writes with wp_slash() so backslashes are preserved.
        foreach ($meta_writes as $entry) {
            [$meta_key, $clean_val] = $entry;
            if ($clean_val === '') {
                delete_post_meta($object_id, $meta_key);
            } else {
                $slashed = function_exists('wp_slash') ? wp_slash($clean_val) : $clean_val;
                update_post_meta($object_id, $meta_key, $slashed);
            }
        }
        if ($robots_update !== null) {
            self::write_robots_meta($object_id, $plugin, $robots_update);
        }

        // Phase 4: Trigger Yoast 14+ indexables rebuild on meta-only updates.
        if ($plugin === 'yoast' && !$has_post_update) {
            self::refresh_yoast_indexable($object_id);
        }

        Azom_Ops_Bridge_Ops::flush_all_caches();

        $reloaded = get_post($object_id);
        $after = ($reloaded instanceof WP_Post)
            ? self::read_entity_seo_snapshot($reloaded, $plugin)
            : $before;
        $after_sha = self::canonical_sha256($after);

        return new WP_REST_Response([
            'ok'            => true,
            'change_id'     => $change_id,
            'seo_plugin'    => $plugin,
            'object_type'   => $object_type,
            'object_id'     => $object_id,
            'before'        => $before,
            'after'         => $after,
            'before_sha256' => $before_sha,
            'after_sha256'  => $after_sha,
        ], 200);
    }

    public static function handle_media_inventory(WP_REST_Request $request): WP_REST_Response {
        $media_id_raw = $request->get_param('media_id');
        if ($media_id_raw !== null && trim((string) $media_id_raw) !== '') {
            $media_id = absint($media_id_raw);
            if ($media_id <= 0) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Invalid media_id'], 400);
            }
            $att = get_post($media_id);
            $items = [];
            if ($att instanceof WP_Post && $att->post_type === 'attachment') {
                $items[] = self::build_media_row($att);
            }
            return new WP_REST_Response([
                'ok'    => true,
                'media' => $items,
                'total' => count($items),
            ], 200);
        }

        $limit = max(1, min(100, (int) ($request->get_param('limit') ?? 100)));
        $page = max(1, (int) ($request->get_param('page') ?? 1));

        $query = new WP_Query([
            'post_type'              => 'attachment',
            'post_status'            => 'inherit',
            'posts_per_page'         => $limit,
            'paged'                  => $page,
            'orderby'                => 'ID',
            'order'                  => 'ASC',
            'no_found_rows'          => false,
            'update_post_term_cache' => false,
        ]);

        $items = [];
        foreach ($query->posts as $att) {
            if (!($att instanceof WP_Post)) {
                continue;
            }
            $items[] = self::build_media_row($att);
        }

        return new WP_REST_Response([
            'ok'    => true,
            'media' => $items,
            'total' => (int) $query->found_posts,
        ], 200);
    }

    public static function handle_update_media(WP_REST_Request $request): WP_REST_Response {
        $payload = $request->get_json_params();
        if (!is_array($payload)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid JSON'], 400);
        }

        $change_id = trim((string) ($payload['change_id'] ?? ''));
        if ($change_id === '' || strlen($change_id) > 80 || !preg_match('/^[A-Za-z0-9._-]+$/', $change_id)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid change_id'], 400);
        }

        $media_id = absint($payload['media_id'] ?? 0);
        if ($media_id <= 0) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Invalid media_id'], 400);
        }

        $fields = $payload['fields'] ?? null;
        if (!is_array($fields) || !$fields) {
            return new WP_REST_Response(['ok' => false, 'error' => 'Missing fields object'], 400);
        }

        foreach (array_keys($fields) as $key) {
            if (!in_array((string) $key, self::ALLOWED_MEDIA_FIELDS, true)) {
                return new WP_REST_Response(['ok' => false, 'error' => 'Disallowed media field: ' . (string) $key], 400);
            }
        }

        $att = get_post($media_id);
        if (!($att instanceof WP_Post) || $att->post_type !== 'attachment') {
            return new WP_REST_Response(['ok' => false, 'error' => 'Media attachment not found'], 404);
        }

        $before = self::read_media_snapshot($att);
        $before_sha = self::canonical_sha256($before);

        $expected_sha = trim((string) ($payload['expected_before_sha256'] ?? ''));
        if ($expected_sha !== '' && !hash_equals($before_sha, $expected_sha)) {
            return new WP_REST_Response([
                'ok'            => false,
                'error'         => 'Stale media digest mismatch',
                'before_sha256' => $before_sha,
            ], 409);
        }

        $alt_update = null;
        if (array_key_exists('alt_text', $fields)) {
            $alt_update = sanitize_text_field(is_scalar($fields['alt_text']) ? (string) $fields['alt_text'] : '');
        }

        $post_updates = ['ID' => $media_id];
        $has_post_update = false;
        if (array_key_exists('title', $fields)) {
            $post_updates['post_title'] = sanitize_text_field(is_scalar($fields['title']) ? (string) $fields['title'] : '');
            $has_post_update = true;
        }
        if (array_key_exists('caption', $fields)) {
            $post_updates['post_excerpt'] = sanitize_textarea_field(is_scalar($fields['caption']) ? (string) $fields['caption'] : '');
            $has_post_update = true;
        }
        if (array_key_exists('description', $fields)) {
            $post_updates['post_content'] = sanitize_textarea_field(is_scalar($fields['description']) ? (string) $fields['description'] : '');
            $has_post_update = true;
        }

        if ($has_post_update) {
            $slashed_updates = function_exists('wp_slash') ? wp_slash($post_updates) : $post_updates;
            $updated_id = wp_update_post($slashed_updates, true);
            if (is_wp_error($updated_id)) {
                return new WP_REST_Response([
                    'ok'    => false,
                    'error' => 'Media update failed: ' . $updated_id->get_error_message(),
                ], 500);
            }
        }

        if ($alt_update !== null) {
            $slashed_alt = function_exists('wp_slash') ? wp_slash($alt_update) : $alt_update;
            update_post_meta($media_id, '_wp_attachment_image_alt', $slashed_alt);
        }

        Azom_Ops_Bridge_Ops::flush_all_caches();

        $reloaded = get_post($media_id);
        $after = ($reloaded instanceof WP_Post)
            ? self::read_media_snapshot($reloaded)
            : $before;
        $after_sha = self::canonical_sha256($after);

        return new WP_REST_Response([
            'ok'            => true,
            'change_id'     => $change_id,
            'media_id'      => $media_id,
            'before'        => $before,
            'after'         => $after,
            'before_sha256' => $before_sha,
            'after_sha256'  => $after_sha,
        ], 200);
    }

    private static function build_entity_row(WP_Post $post, string $plugin, string $home_host): array {
        $snap = self::read_entity_seo_snapshot($post, $plugin);
        $content = (string) $post->post_content;
        $plain = trim(wp_strip_all_tags(strip_shortcodes($content)));
        $words = $plain !== '' ? preg_split('/\s+/u', $plain, -1, PREG_SPLIT_NO_EMPTY) : [];
        $word_count = is_array($words) ? count($words) : 0;

        $internal_links = self::extract_internal_links($content, $home_host);
        $img_stats = self::count_content_images((int) $post->ID, $content);

        return [
            'object_type'        => (string) $post->post_type,
            'object_id'          => (int) $post->ID,
            'title'              => $snap['title'],
            'slug'               => $snap['slug'],
            'url'                => (string) (get_permalink($post->ID) ?: ''),
            'status'             => (string) $post->post_status,
            'word_count'         => $word_count,
            'excerpt'            => $snap['excerpt'],
            'meta_title'         => $snap['meta_title'],
            'meta_description'   => $snap['meta_description'],
            'focus_keyphrase'    => $snap['focus_keyphrase'],
            'canonical'          => $snap['canonical'],
            'robots'             => $snap['robots'],
            'og_title'           => $snap['og_title'],
            'og_description'     => $snap['og_description'],
            'schema_type'        => $snap['schema_type'],
            'internal_links'     => $internal_links,
            'images_total'       => $img_stats['total'],
            'images_missing_alt' => $img_stats['missing_alt'],
            'before_sha256'      => self::canonical_sha256($snap),
        ];
    }

    private static function schema_type_meta_key(string $plugin, string $object_type): string {
        if ($plugin === 'yoast' && $object_type === 'page') {
            return '_yoast_wpseo_schema_page_type';
        }
        $map = self::PLUGIN_META_MAP[$plugin] ?? self::PLUGIN_META_MAP['none'];
        return (string) ($map['schema_type'] ?? '');
    }

    private static function refresh_yoast_indexable(int $post_id): void {
        $reloaded = get_post($post_id);
        if (!($reloaded instanceof WP_Post)) {
            return;
        }
        if (function_exists('do_action')) {
            do_action('wp_insert_post', $post_id, $reloaded, true);
        }
    }

    private static function read_entity_seo_snapshot(WP_Post $post, string $plugin): array {
        $pid = (int) $post->ID;
        $map = self::PLUGIN_META_MAP[$plugin] ?? self::PLUGIN_META_MAP['none'];
        $schema_key = self::schema_type_meta_key($plugin, (string) $post->post_type);

        return [
            'title'            => (string) $post->post_title,
            'slug'             => (string) $post->post_name,
            'excerpt'          => (string) $post->post_excerpt,
            'meta_title'       => (string) get_post_meta($pid, $map['meta_title'], true),
            'meta_description' => (string) get_post_meta($pid, $map['meta_description'], true),
            'focus_keyphrase'  => (string) get_post_meta($pid, $map['focus_keyphrase'], true),
            'canonical'        => (string) get_post_meta($pid, $map['canonical'], true),
            'robots'           => self::read_robots_meta($pid, $plugin),
            'og_title'         => (string) get_post_meta($pid, $map['og_title'], true),
            'og_description'   => (string) get_post_meta($pid, $map['og_description'], true),
            'schema_type'      => $schema_key !== '' ? (string) get_post_meta($pid, $schema_key, true) : '',
        ];
    }

    private static function read_robots_meta(int $post_id, string $plugin): string {
        if ($plugin === 'yoast') {
            $noindex = (string) get_post_meta($post_id, '_yoast_wpseo_meta-robots-noindex', true);
            $nofollow = (string) get_post_meta($post_id, '_yoast_wpseo_meta-robots-nofollow', true);
            $tokens = [
                ($noindex === '1') ? 'noindex' : 'index',
                ($nofollow === '1') ? 'nofollow' : 'follow',
            ];
            $adv_raw = trim((string) get_post_meta($post_id, '_yoast_wpseo_meta-robots-adv', true));
            if ($adv_raw !== '' && $adv_raw !== 'none' && $adv_raw !== '-') {
                foreach (explode(',', $adv_raw) as $tok) {
                    $clean = strtolower(trim($tok));
                    if (in_array($clean, ['noarchive', 'nosnippet', 'noimageindex'], true) && !in_array($clean, $tokens, true)) {
                        $tokens[] = $clean;
                    }
                }
            }
            return implode(',', $tokens);
        }
        if ($plugin === 'rankmath') {
            $raw = get_post_meta($post_id, 'rank_math_robots', true);
            if (is_array($raw) && $raw) {
                $clean = array_values(array_intersect(array_map('strtolower', array_map('strval', $raw)), self::ALLOWED_ROBOTS_TOKENS));
                if ($clean) {
                    return implode(',', $clean);
                }
            }
            return 'index,follow';
        }
        if ($plugin === 'seopress') {
            $noindex = (string) get_post_meta($post_id, '_seopress_robots_index', true);
            $nofollow = (string) get_post_meta($post_id, '_seopress_robots_follow', true);
            $tokens = [
                ($noindex === 'yes') ? 'noindex' : 'index',
                ($nofollow === 'yes') ? 'nofollow' : 'follow',
            ];
            if ((string) get_post_meta($post_id, '_seopress_robots_archive', true) === 'yes') {
                $tokens[] = 'noarchive';
            }
            if ((string) get_post_meta($post_id, '_seopress_robots_snippet', true) === 'yes') {
                $tokens[] = 'nosnippet';
            }
            if ((string) get_post_meta($post_id, '_seopress_robots_imageindex', true) === 'yes') {
                $tokens[] = 'noimageindex';
            }
            return implode(',', $tokens);
        }
        if ($plugin === 'aioseo') {
            $val = trim((string) get_post_meta($post_id, '_aioseo_robots_meta', true));
            return $val !== '' ? $val : 'index,follow';
        }
        $val = trim((string) get_post_meta($post_id, '_azom_seo_robots', true));
        return $val !== '' ? $val : 'index,follow';
    }

    private static function write_robots_meta(int $post_id, string $plugin, string $raw_value): void {
        $parts = array_filter(array_map(static function (string $tok): string {
            return strtolower(trim($tok));
        }, explode(',', $raw_value)));
        $tokens = array_values(array_unique(array_intersect($parts, self::ALLOWED_ROBOTS_TOKENS)));

        if ($plugin === 'yoast') {
            if (in_array('noindex', $tokens, true)) {
                update_post_meta($post_id, '_yoast_wpseo_meta-robots-noindex', '1');
            } else {
                delete_post_meta($post_id, '_yoast_wpseo_meta-robots-noindex');
            }
            if (in_array('nofollow', $tokens, true)) {
                update_post_meta($post_id, '_yoast_wpseo_meta-robots-nofollow', '1');
            } else {
                delete_post_meta($post_id, '_yoast_wpseo_meta-robots-nofollow');
            }
            $adv_tokens = array_values(array_intersect($tokens, ['noarchive', 'nosnippet', 'noimageindex']));
            if ($adv_tokens) {
                update_post_meta($post_id, '_yoast_wpseo_meta-robots-adv', implode(',', $adv_tokens));
            } else {
                delete_post_meta($post_id, '_yoast_wpseo_meta-robots-adv');
            }
            return;
        }
        if ($plugin === 'rankmath') {
            if ($tokens) {
                update_post_meta($post_id, 'rank_math_robots', $tokens);
            } else {
                delete_post_meta($post_id, 'rank_math_robots');
            }
            return;
        }
        if ($plugin === 'seopress') {
            if (in_array('noindex', $tokens, true)) {
                update_post_meta($post_id, '_seopress_robots_index', 'yes');
            } else {
                delete_post_meta($post_id, '_seopress_robots_index');
            }
            if (in_array('nofollow', $tokens, true)) {
                update_post_meta($post_id, '_seopress_robots_follow', 'yes');
            } else {
                delete_post_meta($post_id, '_seopress_robots_follow');
            }
            $seopress_adv = [
                'noarchive'    => '_seopress_robots_archive',
                'nosnippet'    => '_seopress_robots_snippet',
                'noimageindex' => '_seopress_robots_imageindex',
            ];
            foreach ($seopress_adv as $tok => $meta_key) {
                if (in_array($tok, $tokens, true)) {
                    update_post_meta($post_id, $meta_key, 'yes');
                } else {
                    delete_post_meta($post_id, $meta_key);
                }
            }
            return;
        }
        $key = ($plugin === 'aioseo') ? '_aioseo_robots_meta' : '_azom_seo_robots';
        if ($tokens) {
            update_post_meta($post_id, $key, implode(',', $tokens));
        } else {
            delete_post_meta($post_id, $key);
        }
    }

    private static function read_media_snapshot(WP_Post $att): array {
        return [
            'alt_text'    => (string) get_post_meta((int) $att->ID, '_wp_attachment_image_alt', true),
            'title'       => (string) $att->post_title,
            'caption'     => (string) $att->post_excerpt,
            'description' => (string) $att->post_content,
        ];
    }

    private static function build_media_row(WP_Post $att): array {
        $mid = (int) $att->ID;
        $snap = self::read_media_snapshot($att);
        $url = (string) (wp_get_attachment_url($mid) ?: '');
        $file_path = function_exists('get_attached_file') ? (string) get_attached_file($mid) : '';
        $filename = $file_path !== '' ? basename($file_path) : basename((string) wp_parse_url($url, PHP_URL_PATH));
        $meta = function_exists('wp_get_attachment_metadata') ? wp_get_attachment_metadata($mid) : [];
        $meta_arr = is_array($meta) ? $meta : [];
        $width = isset($meta_arr['width']) ? (int) $meta_arr['width'] : 0;
        $height = isset($meta_arr['height']) ? (int) $meta_arr['height'] : 0;
        $filesize = isset($meta_arr['filesize']) ? (int) $meta_arr['filesize'] : 0;
        if ($filesize <= 0 && $file_path !== '' && is_file($file_path)) {
            $filesize = (int) @filesize($file_path);
        }
        $parent = (int) $att->post_parent;

        return [
            'media_id'         => $mid,
            'filename'         => $filename,
            'url'              => $url,
            'mime_type'        => (string) $att->post_mime_type,
            'alt_text'         => $snap['alt_text'],
            'title'            => $snap['title'],
            'caption'          => $snap['caption'],
            'description'      => $snap['description'],
            'width'            => $width,
            'height'           => $height,
            'filesize'         => $filesize,
            'attached_post_id' => $parent > 0 ? $parent : null,
            'before_sha256'    => self::canonical_sha256($snap),
        ];
    }

    private static function extract_internal_links(string $html, string $home_host): array {
        if ($html === '' || !preg_match_all('/<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']/i', $html, $matches)) {
            return [];
        }
        $links = [];
        foreach ($matches[1] as $href) {
            $href = trim((string) $href);
            if ($href === '' || strpos($href, '#') === 0 || strpos($href, 'mailto:') === 0 || strpos($href, 'tel:') === 0) {
                continue;
            }
            if (strpos($href, '/') === 0 && strpos($href, '//') !== 0) {
                $links[] = $href;
                continue;
            }
            $host = (string) wp_parse_url($href, PHP_URL_HOST);
            if ($host !== '' && $home_host !== '' && strcasecmp($host, $home_host) === 0) {
                $path = (string) (wp_parse_url($href, PHP_URL_PATH) ?: '/');
                $links[] = $path;
            }
        }
        return array_values(array_unique($links));
    }

    private static function count_content_images(int $post_id, string $html): array {
        $total = 0;
        $missing = 0;
        if ($html !== '' && preg_match_all('/<img\b[^>]*>/i', $html, $matches)) {
            foreach ($matches[0] as $tag) {
                $total++;
                if (!preg_match('/\balt\s*=\s*["\']([^"\']*)["\']/i', $tag, $alt_m) || trim((string) $alt_m[1]) === '') {
                    $missing++;
                }
            }
        }
        if (function_exists('get_post_thumbnail_id')) {
            $thumb_id = (int) get_post_thumbnail_id($post_id);
            if ($thumb_id > 0) {
                $total++;
                $thumb_alt = trim((string) get_post_meta($thumb_id, '_wp_attachment_image_alt', true));
                if ($thumb_alt === '') {
                    $missing++;
                }
            }
        }
        return ['total' => $total, 'missing_alt' => $missing];
    }

    private static function count_post_type(string $post_type): int {
        if (!post_type_exists($post_type)) {
            return 0;
        }
        $counts = wp_count_posts($post_type);
        if (!is_object($counts)) {
            return 0;
        }
        return (int) ($counts->publish ?? 0) + (int) ($counts->draft ?? 0) + (int) ($counts->private ?? 0);
    }

    private static function count_attachments(): int {
        $counts = wp_count_posts('attachment');
        if (!is_object($counts)) {
            return 0;
        }
        return (int) ($counts->inherit ?? 0);
    }

    private static function canonical_sha256(array $snapshot): string {
        ksort($snapshot, SORT_STRING);
        $encoded = wp_json_encode($snapshot, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        return hash('sha256', is_string($encoded) ? $encoded : '');
    }
}
