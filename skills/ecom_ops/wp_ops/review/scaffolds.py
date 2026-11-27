"""Starter WordPress theme, site-plugin, and WooCommerce HPOS scaffolds."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.review.workspace_io import (
    SLUG,
    ReviewError,
    _base_dir,
    _check_no_symlink,
    _decode_utf8_bytes,
    _work_dir,
    read_manifest,
)
from ecom_ops.wp_ops.review.workspace_ops import apply_work_changes

_CHILD_FUNCTIONS_PHP = (
    "<?php\n"
    "/**\n"
    " * Child theme functions and asset enqueues.\n"
    " */\n\n"
    "defined('ABSPATH') || exit;\n\n"
    "add_action('wp_enqueue_scripts', static function (): void {\n"
    "    wp_enqueue_style(\n"
    "        'azom-child-style',\n"
    "        get_stylesheet_uri(),\n"
    "        [],\n"
    "        wp_get_theme()->get('Version')\n"
    "    );\n"
    "});\n"
)

_WOO_HPOS_COMPAT_PHP = (
    "<?php\n"
    "/**\n"
    " * WooCommerce High-Performance Order Storage (HPOS) compatibility.\n"
    " *\n"
    " * WC tested up to: 9.6\n"
    " */\n\n"
    "defined('ABSPATH') || exit;\n\n"
    "if (!function_exists('azom_declare_woo_hpos_compat')) {\n"
    "    function azom_declare_woo_hpos_compat(string $plugin_file = __FILE__): void {\n"
    "        add_action('before_woocommerce_init', static function () use ($plugin_file): void {\n"
    "            if (class_exists(\\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::class)) {\n"
    "                \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility(\n"
    "                    'custom_order_tables',\n"
    "                    $plugin_file,\n"
    "                    true\n"
    "                );\n"
    "            }\n"
    "        });\n"
    "    }\n"
    "}\n"
)

_WOO_HPOS_WIRE_SNIPPET = (
    "\nrequire_once __DIR__ . '/includes/woo-hpos-compat.php';\n"
    "add_action('before_woocommerce_init', static function (): void {\n"
    "    if (class_exists(\\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::class)) {\n"
    "        \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);\n"
    "    }\n"
    "});\n"
)


def child_skeleton(stylesheet: str) -> list[tuple[str, bytes]]:
    if not SLUG.fullmatch(stylesheet):
        raise ReviewError("SlugRejected")
    css = (
        "/*\n"
        "Theme Name: Azom Child\n"
        f"Template: {stylesheet}\n"
        "*/\n"
    ).encode()
    return [("style.css", css), ("functions.php", b"<?php\n")]


def plugin_skeleton(slug: str) -> list[tuple[str, bytes]]:
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    php = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom Site {slug}\n"
        " */\n"
    ).encode()
    return [(f"{slug}.php", php)]


def mu_plugin_skeleton(slug: str) -> list[tuple[str, bytes]]:
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    php = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom MU {slug}\n"
        " * Description: Must-use plugin loaded automatically by WordPress.\n"
        " * Version: 0.1.0\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n"
    ).encode()
    return [(f"{slug}.php", php)]


ScaffoldPlan = tuple[list[dict[str, Any]], list[str], list[str], list[str]]
ScaffoldBuilder = Callable[[Path, str, dict[str, Any]], ScaffoldPlan]


def _queue_create_if_missing(
    work: Path,
    rel: str,
    content: str,
    changes: list[dict[str, Any]],
    created: list[str],
    skipped: list[str],
) -> None:
    target = _check_no_symlink(work, rel.split("/"))
    if target.exists():
        skipped.append(rel)
    else:
        changes.append({"op": "create", "path": rel, "content": content})
        created.append(rel)


def _build_child_theme_basics(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    parent = slug[:-6] if slug.endswith("-child") and len(slug) > 6 else "twentytwentyfive"
    if not SLUG.fullmatch(parent):
        parent = "twentytwentyfive"
    style_content = (
        "/*\n"
        "Theme Name: Azom Child\n"
        f"Template: {parent}\n"
        "Version: 0.1.0\n"
        "*/\n"
    )
    _queue_create_if_missing(work, "style.css", style_content, changes, created, skipped)
    _queue_create_if_missing(work, "functions.php", _CHILD_FUNCTIONS_PHP, changes, created, skipped)
    return changes, created, [], skipped


def _build_site_plugin_basics(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    if not slug:
        raise ReviewError("SlugRejected")
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    main_content = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom Site ({slug})\n"
        " * Description: Site-specific customisations.\n"
        " * Version: 0.1.0\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n"
    )
    _queue_create_if_missing(work, f"{slug}.php", main_content, changes, created, skipped)
    return changes, created, [], skipped


def _build_woo_compat_declaration(work: Path, slug: str, manifest: dict[str, Any]) -> ScaffoldPlan:
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    _queue_create_if_missing(
        work,
        "includes/woo-hpos-compat.php",
        _WOO_HPOS_COMPAT_PHP,
        changes,
        created,
        skipped,
    )
    kind = str(manifest.get("kind") or "")
    if slug and kind != "theme":
        main_rel = f"{slug}.php"
        main_target = _check_no_symlink(work, [main_rel])
        if main_target.is_file():
            existing_bytes = main_target.read_bytes()
            existing_text = _decode_utf8_bytes(existing_bytes)
            if "custom_order_tables" in existing_text or "woo-hpos-compat.php" in existing_text:
                skipped.append(main_rel)
            else:
                if not existing_text.strip():
                    raise ReviewError("NotText")
                new_text = existing_text.rstrip() + "\n" + _WOO_HPOS_WIRE_SNIPPET
                changes.append(
                    {
                        "op": "edit",
                        "path": main_rel,
                        "base_sha256": hashlib.sha256(existing_bytes).hexdigest(),
                        "edits": [{"old_text": existing_text, "new_text": new_text}],
                    }
                )
                updated.append(main_rel)
        else:
            main_content = (
                "<?php\n"
                "/**\n"
                f" * Plugin Name: Azom Site ({slug})\n"
                " * Description: Site-specific customisations.\n"
                " * Version: 0.1.0\n"
                " * WC tested up to: 9.6\n"
                " */\n\n"
                "defined('ABSPATH') || exit;\n"
                + _WOO_HPOS_WIRE_SNIPPET
            )
            changes.append({"op": "create", "path": main_rel, "content": main_content})
            created.append(main_rel)
    return changes, created, updated, skipped


def _php_prefix(slug: str) -> str:
    return slug.replace("-", "_")


def _build_mu_plugin_basics(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    if not slug:
        raise ReviewError("SlugRejected")
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    prefix = _php_prefix(slug)
    main_content = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom MU ({slug})\n"
        " * Description: Must-use plugin loaded automatically before standard plugins.\n"
        " * Version: 0.1.0\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        f"if (!function_exists('{prefix}_bootstrap')) {{\n"
        f"    function {prefix}_bootstrap(): void {{\n"
        "        // Note: MU-plugins auto-load; activation hooks never fire.\n"
        "        if (class_exists('WooCommerce')) {\n"
        "            // Guarded WooCommerce hooks go here.\n"
        "        }\n"
        "    }\n"
        f"    add_action('muplugins_loaded', '{prefix}_bootstrap');\n"
        "}\n"
    )
    _queue_create_if_missing(work, f"{slug}.php", main_content, changes, created, skipped)
    return changes, created, [], skipped


def _build_mu_plugin_loader(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    if not slug:
        raise ReviewError("SlugRejected")
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    prefix = _php_prefix(slug)
    loader_content = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom MU Loader ({slug})\n"
        " * Description: Must-use loader bootstrapping modular subdirectory files.\n"
        " * Version: 0.1.0\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        "$bootstrap = __DIR__ . '/includes/bootstrap.php';\n"
        "if (is_readable($bootstrap)) {\n"
        "    require_once $bootstrap;\n"
        "}\n"
    )
    bootstrap_content = (
        "<?php\n"
        "/**\n"
        f" * Modular bootstrap for MU-plugin {slug}.\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        f"if (!function_exists('{prefix}_init')) {{\n"
        f"    function {prefix}_init(): void {{\n"
        "        // Register guarded site-level filters and actions here.\n"
        "    }\n"
        f"    add_action('plugins_loaded', '{prefix}_init', 20);\n"
        "}\n"
    )
    _queue_create_if_missing(work, f"{slug}.php", loader_content, changes, created, skipped)
    _queue_create_if_missing(work, "includes/bootstrap.php", bootstrap_content, changes, created, skipped)
    return changes, created, [], skipped


def _build_plugin_hook_extension(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    if not slug:
        raise ReviewError("SlugRejected")
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    prefix = _php_prefix(slug)
    main_content = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom Hook Extension ({slug})\n"
        " * Description: Safe third-party customization via WordPress/WooCommerce hooks (never edits vendor files).\n"
        " * Version: 0.1.0\n"
        " * WC tested up to: 9.6\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        "require_once __DIR__ . '/includes/woo-hpos-compat.php';\n"
        "azom_declare_woo_hpos_compat(__FILE__);\n"
        "require_once __DIR__ . '/includes/hooks.php';\n"
    )
    hooks_content = (
        "<?php\n"
        "/**\n"
        f" * Guarded action and filter adapters for {slug}.\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        f"if (!function_exists('{prefix}_register_hooks')) {{\n"
        f"    function {prefix}_register_hooks(): void {{\n"
        "        if (!class_exists('WooCommerce')) {\n"
        "            return;\n"
        "        }\n"
        "        // Example: add_filter('woocommerce_product_tabs', static fn(array $tabs): array => $tabs, 20);\n"
        "    }\n"
        f"    add_action('plugins_loaded', '{prefix}_register_hooks', 20);\n"
        "}\n"
    )
    _queue_create_if_missing(work, f"{slug}.php", main_content, changes, created, skipped)
    _queue_create_if_missing(work, "includes/woo-hpos-compat.php", _WOO_HPOS_COMPAT_PHP, changes, created, skipped)
    _queue_create_if_missing(work, "includes/hooks.php", hooks_content, changes, created, skipped)
    return changes, created, [], skipped


def _build_custom_plugin_full(work: Path, slug: str, _manifest: dict[str, Any]) -> ScaffoldPlan:
    if not slug:
        raise ReviewError("SlugRejected")
    changes: list[dict[str, Any]] = []
    created: list[str] = []
    skipped: list[str] = []
    class_name = "Azom_" + "".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)
    main_content = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom Custom ({slug})\n"
        " * Description: Modular custom WooCommerce/WordPress plugin with HPOS compatibility and clean uninstall.\n"
        " * Version: 0.1.0\n"
        " * Requires at least: 6.5\n"
        " * Requires PHP: 8.1\n"
        " * Requires Plugins: woocommerce\n"
        " * WC tested up to: 9.6\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        "require_once __DIR__ . '/includes/woo-hpos-compat.php';\n"
        "azom_declare_woo_hpos_compat(__FILE__);\n"
        "require_once __DIR__ . '/includes/class-plugin.php';\n\n"
        "add_action('plugins_loaded', static function (): void {\n"
        "    if (!class_exists('WooCommerce')) {\n"
        "        return;\n"
        "    }\n"
        f"    {class_name}::instance()->boot();\n"
        "}, 20);\n"
    )
    class_content = (
        "<?php\n"
        "/**\n"
        f" * Core plugin controller for {slug}.\n"
        " */\n\n"
        "defined('ABSPATH') || exit;\n\n"
        f"if (!class_exists('{class_name}')) {{\n"
        f"    final class {class_name} {{\n"
        "        private static ?self $instance = null;\n\n"
        "        public static function instance(): self {\n"
        "            if (self::$instance === null) {\n"
        "                self::$instance = new self();\n"
        "            }\n"
        "            return self::$instance;\n"
        "        }\n\n"
        "        public function boot(): void {\n"
        "            // Register plugin hooks here.\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    uninstall_content = (
        "<?php\n"
        "/**\n"
        f" * Clean uninstall handler for {slug}.\n"
        " */\n\n"
        "defined('WP_UNINSTALL_PLUGIN') || exit;\n\n"
        f"delete_option('{_php_prefix(slug)}_settings');\n"
    )
    _queue_create_if_missing(work, f"{slug}.php", main_content, changes, created, skipped)
    _queue_create_if_missing(work, "includes/woo-hpos-compat.php", _WOO_HPOS_COMPAT_PHP, changes, created, skipped)
    _queue_create_if_missing(work, "includes/class-plugin.php", class_content, changes, created, skipped)
    _queue_create_if_missing(work, "uninstall.php", uninstall_content, changes, created, skipped)
    return changes, created, [], skipped


_SCAFFOLD_BUILDERS: dict[str, ScaffoldBuilder] = {
    "child_theme_basics": _build_child_theme_basics,
    "site_plugin_basics": _build_site_plugin_basics,
    "woo_compat_declaration": _build_woo_compat_declaration,
    "mu_plugin_basics": _build_mu_plugin_basics,
    "mu_plugin_loader": _build_mu_plugin_loader,
    "plugin_hook_extension": _build_plugin_hook_extension,
    "custom_plugin_full": _build_custom_plugin_full,
}
SCAFFOLD_TEMPLATES = frozenset(_SCAFFOLD_BUILDERS)
_SCAFFOLD_TEMPLATES = SCAFFOLD_TEMPLATES


def scaffold_work(
    ws_root: Path,
    template: str,
    component_slug: str | None = None,
) -> dict[str, Any]:
    """Create starter theme/plugin/WooCommerce HPOS files safely in ``work/``."""
    work = _work_dir(ws_root)
    _base_dir(ws_root)
    builder = _SCAFFOLD_BUILDERS.get(template)
    if builder is None:
        raise ReviewError("InvalidToolArguments")

    manifest = read_manifest(ws_root)
    slug = (
        component_slug
        if component_slug is not None
        else str(manifest.get("work_slug") or manifest.get("slug") or "")
    )
    if component_slug is not None and (
        not isinstance(component_slug, str) or not SLUG.fullmatch(component_slug)
    ):
        raise ReviewError("SlugRejected")
    if slug and not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")

    changes, created, updated, skipped = builder(work, slug, manifest)
    if changes:
        apply_work_changes(ws_root, changes, allow_create=True, allow_delete=False)

    return {
        "template": template,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


__all__ = [
    "SCAFFOLD_TEMPLATES",
    "_SCAFFOLD_TEMPLATES",
    "child_skeleton",
    "mu_plugin_skeleton",
    "plugin_skeleton",
    "scaffold_work",
]
