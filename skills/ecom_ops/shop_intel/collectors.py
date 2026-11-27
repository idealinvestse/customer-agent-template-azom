"""Read-only, zero-PII collectors for Shop Intelligence.

Delegates all HTTP/SFTP I/O to existing ``WooCommerceClient``, ``WordPressClient``,
``WpBridgeClient``, and ``wp_ops`` file transports. Never imports ``requests``,
``urllib``, ``http.client``, or ``subprocess``, and never reads customers, orders,
order notes, users, comments, or secrets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Actor, Permission
from ecom_ops.shop_intel.config import ShopIntelConfig, is_mock_mode
from ecom_ops.shop_intel.store import scrub_payload, scrub_text
from ecom_ops.wp_ops.config import ssh_files_enabled
from ecom_ops.wp_ops.models import Site

_MAX_WEBHOOKS = 50
_MAX_THEME_FILES = 120


def extract_url_host(raw_url: str) -> str:
    """Extract lowercase hostname from a URL without importing ``urllib``."""
    text = str(raw_url or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    host_port = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host_port:
        host_port = host_port.rsplit("@", 1)[-1]
    host = host_port.split(":", 1)[0].strip().lower()
    return host


def _mock_supplementary_pages(base_url: str) -> list[dict[str, Any]]:
    """Deterministic Nordic policy/brand pages used only when default mock WP transport is unmodified."""
    root = base_url.rstrip("/") or "https://mock.local"
    return [
        {
            "id": 9001,
            "type": "page",
            "slug": "frakt-leverans",
            "title": "Frakt & Leverans",
            "status": "publish",
            "link": f"{root}/frakt-leverans/",
            "content": (
                "<p>Fri frakt över 999 kr i Sverige och Norge. Vi skickar din beställning med PostNord "
                "och Bring inom 1-3 vardagar. Du får spårningslänk via e-post så snart paketet lämnar vårt lager.</p>"
            ),
        },
        {
            "id": 9002,
            "type": "page",
            "slug": "retur-angerratt",
            "title": "Retur & Ångerrätt",
            "status": "publish",
            "link": f"{root}/retur-angerratt/",
            "content": (
                "<p>Du har alltid 30 dagars öppet köp och 14 dagars lagstadgad ångerrätt när du handlar hos Azom. "
                "Vi lämnar 2 års nordisk garanti på alla CarPlay-adaptrar. Kontakta oss så hjälper vi dig!</p>"
            ),
        },
        {
            "id": 9003,
            "type": "page",
            "slug": "faq",
            "title": "Vanliga frågor (FAQ)",
            "status": "publish",
            "link": f"{root}/faq/",
            "content": (
                "<p>Passar Azom CarPlay min bil? Våra adaptrar fungerar med trådlös Apple CarPlay och Android Auto "
                "i Volvo, BMW, Audi och VW med fabriksmonterat CarPlay. Plug-and-play på 10 sekunder.</p>"
            ),
        },
    ]


def _serialize_wp_post(post: Any) -> dict[str, Any]:
    raw = getattr(post, "raw", None)
    raw_dict = raw if isinstance(raw, dict) else {}
    content_obj = raw_dict.get("content")
    if isinstance(content_obj, dict):
        content = str(content_obj.get("rendered") or content_obj.get("raw") or "")
    else:
        content = str(content_obj or "")
    slug = str(raw_dict.get("slug") or "").strip().lower()
    if not slug and getattr(post, "link", None):
        slug = str(post.link).rstrip("/").rsplit("/", 1)[-1].lower()
    return {
        "id": int(getattr(post, "id", 0) or raw_dict.get("id") or 0),
        "type": str(getattr(post, "type", "page") or "page"),
        "slug": scrub_text(slug, max_chars=120),
        "title": scrub_text(getattr(post, "title", "") or "", max_chars=300),
        "status": str(getattr(post, "status", "publish") or "publish"),
        "link": scrub_text(getattr(post, "link", "") or "", max_chars=300),
        "content": scrub_text(content, max_chars=12_000),
    }


def _default_woo_client(site: Site, *, market: str) -> Any | None:
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        return client_from_env(
            base_url=site.canonical_url,
            domain=market,
            use_mock=is_mock_mode(),
        )
    except Exception:
        return None


def _default_wp_client(site: Site, *, market: str) -> Any | None:
    try:
        from ecom_ops.integrations.wordpress import wp_client_from_env

        return wp_client_from_env(
            base_url=site.canonical_url,
            domain=market,
            use_mock=is_mock_mode(),
        )
    except Exception:
        return None


def _default_bridge_client(site: Site) -> Any | None:
    try:
        from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

        return bridge_client_for_site(site, force_mock=is_mock_mode())
    except Exception:
        return None


def _default_file_transport(site: Site, data_dir: Path) -> Any | None:
    if is_mock_mode():
        from ecom_ops.wp_ops.file_transport import LocalTree

        root = site.files_root or "/var/www/mock"
        return LocalTree(root, data_dir / "wp_files_mock" / site.id)
    try:
        from ecom_ops.integrations.wp_files_ssh import build_file_transport

        return build_file_transport(site, data_dir)
    except Exception:
        return None


def _market_for_site(site: Site) -> str:
    host = extract_url_host(site.canonical_url)
    for suffix in ("se", "no", "dk", "fi"):
        if host.endswith(f".{suffix}") or site.id.endswith(f"-{suffix}"):
            return suffix
    return "no" if "no" in site.id else "se"


def _collect_woo_signals(
    woo: Any,
    *,
    domains: set[str],
    cfg: ShopIntelConfig,
    max_products: int,
    sources: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "settings_groups": {},
        "shipping_zones": [],
        "payment_gateways": [],
        "tax_classes": [],
        "system_status": {},
        "products": [],
        "categories": [],
        "attributes": [],
        "webhooks": [],
    }
    if woo is None:
        return out

    need_platform = bool(domains & {"platform_rules", "security_posture", "theme_architecture"})
    need_catalog = "catalog_pricing" in domains or "brand_voice_copy" in domains or "security_posture" in domains
    need_plugins = "plugins_ecosystem" in domains or "security_posture" in domains

    if need_platform:
        for group_id in ("general", "products", "shipping", "tax", "checkout", "account"):
            if hasattr(woo, "get_settings_group"):
                try:
                    out["settings_groups"][group_id] = scrub_payload(woo.get_settings_group(group_id))
                except Exception as exc:
                    warnings.append(f"woo_settings_{group_id}: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "list_shipping_zones"):
            try:
                zones = woo.list_shipping_zones()
                enriched_zones: list[dict[str, Any]] = []
                for z in zones:
                    if not isinstance(z, dict):
                        continue
                    zid = z.get("id")
                    methods: list[dict[str, Any]] = []
                    if zid is not None and hasattr(woo, "list_shipping_zone_methods"):
                        try:
                            methods = woo.list_shipping_zone_methods(zid)
                        except Exception as exc:
                            warnings.append(f"woo_shipping_methods_{zid}: {scrub_text(exc, max_chars=120)}")
                    enriched_zones.append({**scrub_payload(z), "methods": scrub_payload(methods)})
                out["shipping_zones"] = enriched_zones
                sources.append("woo_rest:shipping_zones")
            except Exception as exc:
                warnings.append(f"woo_shipping_zones: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "list_payment_gateways"):
            try:
                out["payment_gateways"] = scrub_payload(woo.list_payment_gateways())
                sources.append("woo_rest:payment_gateways")
            except Exception as exc:
                warnings.append(f"woo_payment_gateways: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "list_tax_classes"):
            try:
                out["tax_classes"] = scrub_payload(woo.list_tax_classes())
                sources.append("woo_rest:tax_classes")
            except Exception as exc:
                warnings.append(f"woo_tax_classes: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "get_system_status"):
            try:
                st = woo.get_system_status()
                raw_st = getattr(st, "raw", None)
                env_st = raw_st.get("environment") if isinstance(raw_st, dict) else {}
                out["system_status"] = scrub_payload(
                    {
                        "woocommerce_version": getattr(st, "version", None),
                        "wordpress_version": getattr(st, "wordpress_version", None),
                        "php_version": (
                            env_st.get("php_version") if isinstance(env_st, dict) else None
                        ),
                        "active_plugins": list(getattr(st, "active_plugins", []) or []),
                    }
                )
                sources.append("woo_rest:system_status")
            except Exception as exc:
                warnings.append(f"woo_system_status: {scrub_text(exc, max_chars=160)}")

    if need_catalog:
        try:
            products: list[dict[str, Any]] = []
            if hasattr(woo, "list_all_products"):
                per_page = min(100, max(1, max_products))
                max_pages = max(1, (max_products + per_page - 1) // per_page)
                for item in woo.list_all_products(per_page=per_page, max_pages=max_pages):
                    if isinstance(item, dict):
                        products.append(scrub_payload(item))
                    if len(products) >= max_products:
                        break
            elif hasattr(woo, "list_products"):
                for item in woo.list_products(per_page=min(100, max_products), page=1):
                    if isinstance(item, dict):
                        products.append(scrub_payload(item))
            out["products"] = products[:max_products]
            sources.append("woo_rest:products")
        except Exception as exc:
            warnings.append(f"woo_products: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "list_product_categories"):
            try:
                out["categories"] = scrub_payload(woo.list_product_categories(per_page=100, page=1))
                sources.append("woo_rest:categories")
            except Exception as exc:
                warnings.append(f"woo_categories: {scrub_text(exc, max_chars=160)}")
        if hasattr(woo, "list_product_attributes"):
            try:
                out["attributes"] = scrub_payload(woo.list_product_attributes())
                sources.append("woo_rest:attributes")
            except Exception as exc:
                warnings.append(f"woo_attributes: {scrub_text(exc, max_chars=160)}")

    if need_plugins and hasattr(woo, "list_webhooks"):
        try:
            raw_hooks = woo.list_webhooks(per_page=_MAX_WEBHOOKS, page=1)
            safe_hooks: list[dict[str, Any]] = []
            for hook in raw_hooks:
                if not isinstance(hook, dict):
                    continue
                delivery_host = extract_url_host(str(hook.get("delivery_url") or ""))
                safe_hooks.append(
                    {
                        "id": hook.get("id"),
                        "name": scrub_text(hook.get("name") or "", max_chars=120),
                        "topic": scrub_text(hook.get("topic") or "", max_chars=120),
                        "status": scrub_text(hook.get("status") or "", max_chars=40),
                        "delivery_host": delivery_host,
                    }
                )
            out["webhooks"] = safe_hooks
            sources.append("woo_rest:webhooks")
        except Exception as exc:
            warnings.append(f"woo_webhooks: {scrub_text(exc, max_chars=160)}")

    return out


def _collect_wp_signals(
    wp: Any,
    *,
    site: Site,
    domains: set[str],
    max_pages: int,
    sources: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "settings": {},
        "themes": [],
        "templates": [],
        "plugins": [],
        "namespaces": [],
        "pages": [],
        "posts": [],
    }
    if wp is None:
        return out

    if hasattr(wp, "get_settings"):
        try:
            out["settings"] = scrub_payload(wp.get_settings())
            sources.append("wp_rest:settings")
        except Exception as exc:
            warnings.append(f"wp_settings: {scrub_text(exc, max_chars=160)}")

    if domains & {"theme_architecture", "security_posture"}:
        if hasattr(wp, "list_themes"):
            try:
                out["themes"] = scrub_payload(wp.list_themes())
                sources.append("wp_rest:themes")
            except Exception as exc:
                warnings.append(f"wp_themes: {scrub_text(exc, max_chars=160)}")
        if hasattr(wp, "list_templates"):
            try:
                out["templates"] = scrub_payload(wp.list_templates())
                sources.append("wp_rest:templates")
            except Exception as exc:
                warnings.append(f"wp_templates: {scrub_text(exc, max_chars=160)}")

    if domains & {"plugins_ecosystem", "security_posture"}:
        if hasattr(wp, "list_plugins"):
            try:
                out["plugins"] = scrub_payload(wp.list_plugins())
                sources.append("wp_rest:plugins")
            except Exception as exc:
                warnings.append(f"wp_plugins: {scrub_text(exc, max_chars=160)}")
        if hasattr(wp, "discover_namespaces"):
            try:
                out["namespaces"] = scrub_payload(wp.discover_namespaces())
                sources.append("wp_rest:namespaces")
            except Exception as exc:
                warnings.append(f"wp_namespaces: {scrub_text(exc, max_chars=160)}")

    if domains & {"brand_voice_copy", "policies_legal", "security_posture"}:
        if hasattr(wp, "list_pages"):
            try:
                pages_raw = wp.list_pages(per_page=min(100, max(1, max_pages)), page=1)
                serialized = [_serialize_wp_post(p) for p in pages_raw][:max_pages]
                # Enrich only when running against the unmodified default mock transport
                if is_mock_mode():
                    transport = getattr(wp, "transport", None)
                    posts_map = getattr(transport, "posts", None)
                    if isinstance(posts_map, dict) and set(posts_map.keys()) == {1, 2}:
                        serialized.extend(_mock_supplementary_pages(site.canonical_url))
                        sources.append("mock_fixture:pages")
                out["pages"] = serialized[:max_pages]
                sources.append("wp_rest:pages")
            except Exception as exc:
                warnings.append(f"wp_pages: {scrub_text(exc, max_chars=160)}")

    if "brand_voice_copy" in domains and hasattr(wp, "list_posts"):
        try:
            posts_raw = wp.list_posts(per_page=min(20, max_pages), page=1)
            out["posts"] = [_serialize_wp_post(p) for p in posts_raw][:20]
            sources.append("wp_rest:posts")
        except Exception as exc:
            warnings.append(f"wp_posts: {scrub_text(exc, max_chars=160)}")

    return out


def _collect_bridge_signals(
    bridge: Any,
    *,
    domains: set[str],
    sources: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "health": {},
        "plugin_inventory": {},
        "seo_overview": {},
    }
    if bridge is None:
        return out

    if hasattr(bridge, "health"):
        try:
            out["health"] = scrub_payload(bridge.health())
            sources.append("wp_bridge:health")
        except Exception as exc:
            warnings.append(f"bridge_health: {scrub_text(exc, max_chars=160)}")

    if (domains & {"plugins_ecosystem", "theme_architecture", "security_posture"}) and hasattr(
        bridge, "plugin_inventory"
    ):
        try:
            out["plugin_inventory"] = scrub_payload(bridge.plugin_inventory(include_updates=False))
            sources.append("wp_bridge:plugin_inventory")
        except Exception as exc:
            warnings.append(f"bridge_plugin_inventory: {scrub_text(exc, max_chars=160)}")

    if (domains & {"security_posture", "brand_voice_copy", "platform_rules"}) and hasattr(
        bridge, "seo_overview"
    ):
        try:
            out["seo_overview"] = scrub_payload(bridge.seo_overview())
            sources.append("wp_bridge:seo_overview")
        except Exception as exc:
            warnings.append(f"bridge_seo_overview: {scrub_text(exc, max_chars=160)}")

    return out


def _collect_theme_files(
    *,
    site: Site,
    actor: Actor,
    active_theme_slug: str,
    parent_theme_slug: str,
    data_dir: Path,
    files_factory: Callable[[Site], Any] | None,
    sources: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Read theme source files via ``FileTransport`` only when actor has WP_OPS_OPERATE/ADMIN."""
    can_read_files = actor.has(Permission.WP_OPS_OPERATE) or actor.has(Permission.WP_OPS_ADMIN)
    if not can_read_files:
        return {"sftp_source": "skipped_rbac", "files": {}}

    transport = files_factory(site) if files_factory is not None else _default_file_transport(site, data_dir)
    if transport is None:
        return {"sftp_source": "unavailable", "files": {}}

    is_ssh = bool(getattr(transport, "is_ssh", False))
    if is_ssh and not ssh_files_enabled():
        return {"sftp_source": "disabled_ssh_gate", "files": {}}

    slug = str(active_theme_slug or "").strip().lower()
    if not slug:
        return {"sftp_source": "sftp" if is_ssh else "local_tree", "files": {}}

    text_files: dict[str, str] = {}
    try:
        from ecom_ops.wp_ops.review.component_reader import read_site_component

        raw_pairs = read_site_component(transport, site.files_root, "theme", slug)
        for rel, data in raw_pairs[:_MAX_THEME_FILES]:
            try:
                text_files[str(rel)] = data.decode("utf-8", errors="replace")
            except Exception:
                continue
        if parent_theme_slug and parent_theme_slug != slug:
            try:
                parent_pairs = read_site_component(transport, site.files_root, "theme", parent_theme_slug)
                for rel, data in parent_pairs[:_MAX_THEME_FILES]:
                    prefixed = f"_parent/{rel}"
                    if prefixed not in text_files:
                        text_files[prefixed] = data.decode("utf-8", errors="replace")
            except Exception:
                pass
        sftp_source = "sftp" if is_ssh else "local_tree"
        sources.append(f"wp_files:{sftp_source}")
        return {"sftp_source": sftp_source, "files": text_files}
    except Exception as exc:
        warnings.append(f"theme_files: {scrub_text(exc, max_chars=160)}")
        return {"sftp_source": "unavailable", "files": {}}


def collect_shop_signals(
    site: Site,
    *,
    actor: Actor,
    domains: Sequence[str],
    mode: str,
    cfg: ShopIntelConfig,
    data_dir: Path,
    woo_client_factory: Callable[..., Any] | None = None,
    wp_client_factory: Callable[..., Any] | None = None,
    bridge_client_factory: Callable[[Site], Any] | None = None,
    files_factory: Callable[[Site], Any] | None = None,
) -> dict[str, Any]:
    """Collect bounded, read-only, zero-PII store signals for the requested domains."""
    domain_set = set(domains)
    sources: list[str] = []
    warnings: list[str] = []
    market = _market_for_site(site)

    max_products = min(50, cfg.max_products_sample) if mode == "quick" else cfg.max_products_sample
    max_pages = min(20, cfg.max_pages_sample) if mode == "quick" else cfg.max_pages_sample

    woo = (
        woo_client_factory(base_url=site.canonical_url, domain=market)
        if woo_client_factory is not None
        else _default_woo_client(site, market=market)
    )
    wp = (
        wp_client_factory(base_url=site.canonical_url, domain=market)
        if wp_client_factory is not None
        else _default_wp_client(site, market=market)
    )
    bridge = (
        bridge_client_factory(site)
        if bridge_client_factory is not None
        else _default_bridge_client(site)
    )

    woo_data = _collect_woo_signals(
        woo,
        domains=domain_set,
        cfg=cfg,
        max_products=max_products,
        sources=sources,
        warnings=warnings,
    )
    wp_data = _collect_wp_signals(
        wp,
        site=site,
        domains=domain_set,
        max_pages=max_pages,
        sources=sources,
        warnings=warnings,
    )
    bridge_data = _collect_bridge_signals(
        bridge,
        domains=domain_set,
        sources=sources,
        warnings=warnings,
    )

    active_theme = ""
    parent_theme = ""
    for theme in wp_data.get("themes") or []:
        if isinstance(theme, dict) and str(theme.get("status") or "").lower() == "active":
            active_theme = str(theme.get("stylesheet") or "")
            parent_theme = str(theme.get("template") or "")
            if parent_theme == active_theme:
                parent_theme = ""
            break
    if not active_theme:
        health_theme = (bridge_data.get("health") or {}).get("active_theme")
        if isinstance(health_theme, str):
            active_theme = health_theme.strip()
        elif isinstance(health_theme, dict):
            active_theme = str(health_theme.get("stylesheet") or health_theme.get("slug") or "").strip()

    theme_files: dict[str, Any] = {"sftp_source": "skipped_mode", "files": {}}
    if domain_set & {"theme_architecture", "security_posture"}:
        if mode == "quick":
            theme_files = {"sftp_source": "skipped_quick", "files": {}}
        else:
            theme_files = _collect_theme_files(
                site=site,
                actor=actor,
                active_theme_slug=active_theme,
                parent_theme_slug=parent_theme,
                data_dir=data_dir,
                files_factory=files_factory,
                sources=sources,
                warnings=warnings,
            )

    return {
        "site": {
            "id": site.id,
            "display_name": scrub_text(site.display_name, max_chars=120),
            "canonical_url": scrub_text(site.canonical_url, max_chars=240),
            "environment": site.environment,
            "wordpress_version": site.wordpress_version,
            "php_version": site.php_version,
            "woocommerce": site.woocommerce,
            "market": market,
        },
        "woo": woo_data,
        "wp": wp_data,
        "bridge": bridge_data,
        "theme_files": theme_files,
        "sources": list(dict.fromkeys(sources)),
        "warnings": warnings,
    }


__all__ = [
    "collect_shop_signals",
    "extract_url_host",
]
