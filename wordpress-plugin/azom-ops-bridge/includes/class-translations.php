<?php

if (!defined('ABSPATH')) {
    exit;
}

final class Azom_Ops_Bridge_Translations {
    public static function detect_provider(): string {
        // Check Polylang before icl_object_id() because Polylang provides WPML compat shims.
        if (function_exists('pll_languages_list') || defined('POLYLANG_VERSION')) {
            return 'polylang';
        }
        if (defined('ICL_SITEPRESS_VERSION') || function_exists('icl_object_id')) {
            return 'wpml';
        }
        if (class_exists('TRP_Translate_Press')) {
            return 'translatepress';
        }
        return 'none';
    }

    public static function handle_translations(WP_REST_Request $request): WP_REST_Response {
        $provider = self::detect_provider();
        $post_id = absint($request->get_param('post_id'));
        $languages = self::languages($provider);
        $translations = [];

        if ($post_id > 0) {
            $translations = self::entity_translations($provider, $post_id, $languages);
        }

        return new WP_REST_Response([
            'ok'           => true,
            'provider'     => $provider,
            'default_lang' => self::default_language($provider),
            'languages'    => $languages,
            'post_id'      => $post_id > 0 ? $post_id : null,
            'translations' => $translations,
        ], 200);
    }

    private static function default_language(string $provider): string {
        if ($provider === 'wpml') {
            return (string) apply_filters('wpml_default_language', '');
        }
        if ($provider === 'polylang' && function_exists('pll_default_language')) {
            return (string) pll_default_language('slug');
        }
        if ($provider === 'translatepress') {
            $opts = get_option('trp_settings', []);
            if (is_array($opts) && !empty($opts['default-language'])) {
                return (string) $opts['default-language'];
            }
        }
        return (string) get_locale();
    }

    private static function languages(string $provider): array {
        if ($provider === 'wpml') {
            $raw = apply_filters('wpml_active_languages', null, ['skip_missing' => 0]);
            if (is_array($raw)) {
                $out = [];
                foreach ($raw as $code => $info) {
                    $out[] = [
                        'code'   => (string) $code,
                        'name'   => (string) ($info['native_name'] ?? $info['translated_name'] ?? $code),
                        'locale' => (string) ($info['default_locale'] ?? ''),
                        'active' => !empty($info['active']),
                    ];
                }
                return $out;
            }
        }
        if ($provider === 'polylang' && function_exists('pll_languages_list')) {
            $default = function_exists('pll_default_language') ? (string) pll_default_language('slug') : '';
            $raw_langs = pll_languages_list(['fields' => []]);
            if (is_array($raw_langs) && $raw_langs && is_object(reset($raw_langs))) {
                $out = [];
                foreach ($raw_langs as $lang_obj) {
                    $slug = (string) ($lang_obj->slug ?? '');
                    if ($slug === '') {
                        continue;
                    }
                    $out[] = [
                        'code'   => $slug,
                        'name'   => (string) ($lang_obj->name ?? $slug),
                        'locale' => (string) ($lang_obj->locale ?? ''),
                        'active' => $default !== '' ? ($slug === $default) : false,
                    ];
                }
                return $out;
            }
            $slugs = (array) pll_languages_list(['fields' => 'slug']);
            $out = [];
            foreach ($slugs as $slug) {
                $s = (string) $slug;
                $out[] = ['code' => $s, 'name' => $s, 'locale' => '', 'active' => $s === $default];
            }
            return $out;
        }
        if ($provider === 'translatepress' && class_exists('TRP_Translate_Press')) {
            $opts = get_option('trp_settings', []);
            $langs = is_array($opts) && isset($opts['translation-languages']) ? (array) $opts['translation-languages'] : [];
            $default = is_array($opts) && isset($opts['default-language']) ? (string) $opts['default-language'] : '';
            $out = [];
            foreach ($langs as $lang) {
                $loc = (string) $lang;
                $out[] = [
                    'code'   => $loc,
                    'name'   => $loc,
                    'locale' => $loc,
                    'active' => $default !== '' && $loc === $default,
                ];
            }
            return $out;
        }
        $loc = (string) get_locale();
        return [['code' => $loc, 'name' => $loc, 'locale' => $loc, 'active' => true]];
    }

    private static function entity_translations(string $provider, int $post_id, array $languages): array {
        $post_type = get_post_type($post_id) ?: 'post';
        $map = [];
        if ($provider === 'wpml') {
            foreach ($languages as $lang) {
                $code = (string) ($lang['code'] ?? '');
                if ($code === '') {
                    continue;
                }
                $trid = (int) apply_filters('wpml_object_id', $post_id, $post_type, false, $code);
                $map[$code] = [
                    'id'     => $trid > 0 ? $trid : null,
                    'status' => $trid > 0 ? (string) get_post_status($trid) : 'missing',
                    'title'  => $trid > 0 ? (string) get_the_title($trid) : '',
                ];
            }
        } elseif ($provider === 'polylang' && function_exists('pll_get_post')) {
            foreach ($languages as $lang) {
                $code = (string) ($lang['code'] ?? '');
                if ($code === '') {
                    continue;
                }
                $trid = (int) pll_get_post($post_id, $code);
                $map[$code] = [
                    'id'     => $trid > 0 ? $trid : null,
                    'status' => $trid > 0 ? (string) get_post_status($trid) : 'missing',
                    'title'  => $trid > 0 ? (string) get_the_title($trid) : '',
                ];
            }
        } elseif ($provider === 'translatepress') {
            $post = get_post($post_id);
            if ($post) {
                $permalink = get_permalink($post_id) ?: '';
                $trp = class_exists('TRP_Translate_Press') ? \TRP_Translate_Press::get_trp_instance() : null;
                $url_converter = ($trp && method_exists($trp, 'get_component')) ? $trp->get_component('url_converter') : null;
                foreach ($languages as $lang) {
                    $code = (string) ($lang['code'] ?? '');
                    if ($code === '') {
                        continue;
                    }
                    $lang_url = ($url_converter && method_exists($url_converter, 'get_url_for_language'))
                        ? (string) $url_converter->get_url_for_language($code, $permalink, '')
                        : $permalink;
                    $map[$code] = [
                        'id'     => $post_id,
                        'status' => (string) $post->post_status,
                        'title'  => (string) $post->post_title,
                        'url'    => $lang_url,
                    ];
                }
            }
        }
        return $map;
    }
}
