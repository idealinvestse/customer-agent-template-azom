"""SeoStudioService facade: audits, SHA-256 fix plans, Change Vault apply, strategies, campaigns, link packages."""

from __future__ import annotations

import hmac
import html as html_lib
import inspect
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied
from ecom_ops.seo.content_audit import (
    analyze_gsc_quick_wins,
    audit_entities_on_page,
    audit_internal_links,
    audit_media_seo,
    compute_seo_score,
    detect_keyword_cannibalization,
)
from ecom_ops.seo.models import (
    GSC_NOT_CONNECTED_NOTE,
    LINK_PACKAGE_STATUSES,
    VALID_AUDIT_SCOPES,
    VALID_FIX_TYPES,
    VALID_TARGET_TYPES,
    SeoAuditReport,
    SeoFixBatch,
    SeoFixPlan,
    SeoIssue,
    canonical_sha256,
    new_seo_id,
    normalize_market,
    utc_now_iso,
)
from ecom_ops.seo.planner import (
    build_campaign,
    build_link_package,
    build_strategy,
    draft_batch_fix_items,
    render_link_package_brief,
    validate_link_package_inputs,
)
from ecom_ops.seo.policy import (
    FIX_PLAN_TTL_HOURS,
    classify_anchor,
    evaluate_anchor_mix,
    evaluate_cumulative_anchor_mix,
    require_link_package_approve,
    require_link_package_draft,
    require_seo_apply,
    require_seo_propose,
    require_seo_read,
    seo_write_allowed,
)
from ecom_ops.seo.research_bridge import (
    run_autonomous_seo_pipeline,
    vet_link_vendors_via_web,
)
from ecom_ops.seo.store import SeoStudioStore
from ecom_ops.seo.technical import (
    analyze_html_seo,
    analyze_robots_txt,
    analyze_sitemap_xml,
    audit_nordic_hreflang,
    audit_nordic_parity_matrix,
    crawl_site_pages,
    expand_sitemap_index,
    fetch_public_seo_resource,
    mock_technical_fixtures,
    verify_backlink_placement,
)
from ecom_ops.seo.vendors import (
    NORDIC_DISCLOSURE_CHECKLIST,
    get_vendor,
    vet_vendor_candidate,
)
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode
from ecom_ops.wp_ops.locks import hold_site
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.registry import load_sites, site_by_id


class TargetNotFound(ValueError):
    """Raised fail-closed when a live SEO fix target cannot be found."""


_ALLOWED_LINK_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"ready_for_review", "cancelled"}),
    "ready_for_review": frozenset({"approved_for_manual_order", "draft", "cancelled"}),
    "approved_for_manual_order": frozenset({"ordered_external", "cancelled"}),
    "ordered_external": frozenset({"delivered", "cancelled"}),
    "delivered": frozenset(),
    "cancelled": frozenset(),
}

_OSCAR_ONLY_LINK_STATUSES: frozenset[str] = frozenset(
    {"approved_for_manual_order", "ordered_external", "delivered", "cancelled"}
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_BRIDGE_META_FIELDS: frozenset[str] = frozenset(
    {
        "meta_title",
        "meta_description",
        "focus_keyphrase",
        "canonical",
        "robots",
        "og_title",
        "og_description",
        "schema_type",
        "slug",
        "title",
        "excerpt",
    }
)
_BRIDGE_MEDIA_FIELDS: frozenset[str] = frozenset({"alt_text", "title", "caption", "description"})
_WOO_PRODUCT_CONTENT_FIELDS: frozenset[str] = frozenset(
    {"name", "description", "short_description", "slug"}
)
_WP_POST_CONTENT_FIELDS: frozenset[str] = frozenset({"title", "content", "excerpt", "slug"})
_SCHEMA_FIELDS: frozenset[str] = frozenset({"schema_type", "schema_json"})
_NUMERIC_TARGET_TYPES: frozenset[str] = frozenset({"product", "page", "post", "media"})

_ENTITY_SNAPSHOT_KEYS: tuple[str, ...] = (
    "title",
    "slug",
    "excerpt",
    "meta_title",
    "meta_description",
    "focus_keyphrase",
    "canonical",
    "robots",
    "og_title",
    "og_description",
    "schema_type",
)
_MEDIA_SNAPSHOT_KEYS: tuple[str, ...] = ("alt_text", "title", "caption", "description")

_MAX_BRIDGE_LOOKUP_PAGES = 20
_MAX_LIVE_HREFLANG_FETCHES = 5


def _strip_html_word_count(html_text: str) -> int:
    clean = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(html_text or "")))
    return len(re.findall(r"[A-Za-z0-9ÅÄÖåäöØøÆæ]+", clean))


def _entity_snapshot_sha256(before: dict[str, Any]) -> str:
    """Hash exact 11 keys matching ``class-seo.php::read_entity_seo_snapshot``."""
    snap = {k: str(before.get(k) or "") for k in _ENTITY_SNAPSHOT_KEYS}
    if not snap["robots"]:
        snap["robots"] = "index,follow"
    return canonical_sha256(snap)


def _media_snapshot_sha256(before: dict[str, Any]) -> str:
    """Hash exact 4 keys matching ``class-seo.php::read_media_snapshot``."""
    snap = {k: str(before.get(k) or "") for k in _MEDIA_SNAPSHOT_KEYS}
    return canonical_sha256(snap)


def _allowed_proposed_fields(fix_type: str, target_type: str) -> frozenset[str]:
    if target_type == "media":
        if fix_type != "media_alt":
            raise ValueError(
                f"fix_type={fix_type!r} is not allowed for target_type='media' (use 'media_alt')"
            )
        return _BRIDGE_MEDIA_FIELDS
    if fix_type == "media_alt":
        raise ValueError(
            f"fix_type='media_alt' requires target_type='media', got {target_type!r}"
        )
    if target_type == "product":
        if fix_type == "seo_meta":
            return _BRIDGE_META_FIELDS
        if fix_type == "product_seo":
            return _BRIDGE_META_FIELDS | _WOO_PRODUCT_CONTENT_FIELDS
        if fix_type == "schema_jsonld":
            return _SCHEMA_FIELDS
        raise ValueError(f"fix_type={fix_type!r} is not supported for target_type='product'")
    if target_type in {"page", "post"}:
        if fix_type == "seo_meta":
            return _BRIDGE_META_FIELDS
        if fix_type == "page_seo":
            return _BRIDGE_META_FIELDS | _WP_POST_CONTENT_FIELDS
        if fix_type == "schema_jsonld":
            return _SCHEMA_FIELDS
        raise ValueError(f"fix_type={fix_type!r} is not supported for target_type={target_type!r}")
    if target_type in {"category", "site"}:
        if fix_type == "seo_meta":
            return _BRIDGE_META_FIELDS
        if fix_type == "schema_jsonld":
            return _SCHEMA_FIELDS
        raise ValueError(f"fix_type={fix_type!r} is not supported for target_type={target_type!r}")
    raise ValueError(f"Unsupported target_type {target_type!r}")


def _fallback_mock_entities(base_url: str) -> list[dict[str, Any]]:
    """Fallback mock Nordic CarPlay entities used if bridge lacks SEO endpoints."""
    origin = base_url.rstrip("/") or "https://mock-shop.example.test"
    return [
        {
            "object_type": "product",
            "object_id": 501,
            "title": "Azom CarPlay Skjerm Pro 9\"",
            "slug": "carplay-skarm-pro",
            "url": f"{origin}/produkt/carplay-skarm-pro/",
            "status": "publish",
            "word_count": 118,
            "excerpt": "Trådløs Apple CarPlay & Android Auto skjerm.",
            "meta_title": "Azom CarPlay Skjerm Pro 9\" – Trådløs Apple CarPlay & Android Auto til alle biler i Norge",
            "meta_description": "",
            "focus_keyphrase": "trådløs carplay skjerm",
            "canonical": f"{origin}/produkt/carplay-skarm-pro/",
            "robots": "index, follow",
            "og_title": "Azom CarPlay Skjerm Pro",
            "og_description": "",
            "schema_type": "Product",
            "internal_links": [
                {"href": f"{origin}/kategori/apple-carplay/", "anchor": "Apple CarPlay"},
                {"href": f"{origin}/guide/montera-carplay/", "anchor": "Läs mer"},
            ],
            "images_total": 3,
            "images_missing_alt": 1,
            "before_sha256": canonical_sha256({"object_id": 501, "meta_title": "long"}),
        },
        {
            "object_type": "product",
            "object_id": 502,
            "title": "Azom CarPlay Adapter Mini",
            "slug": "carplay-adapter-mini",
            "url": f"{origin}/produkt/carplay-adapter-mini/",
            "status": "publish",
            "word_count": 210,
            "excerpt": "Kompakt USB-adapter for trådløs CarPlay.",
            "meta_title": "Trådløs CarPlay Adapter Mini | Azom",
            "meta_description": "Gjør fabrikkmontert CarPlay trådløs på 10 sekunder med Azom Mini Adapter. Rask levering og 2 års garanti i hele Norge.",
            "focus_keyphrase": "trådløs carplay adapter",
            "canonical": f"{origin}/produkt/carplay-adapter-mini/",
            "robots": "index, follow",
            "og_title": "Azom CarPlay Adapter Mini",
            "og_description": "Trådløs CarPlay adapter",
            "schema_type": "Product",
            "internal_links": [],
            "images_total": 2,
            "images_missing_alt": 0,
            "before_sha256": canonical_sha256({"object_id": 502, "meta_title": "adapter"}),
        },
        {
            "object_type": "page",
            "object_id": 12,
            "title": "Apple CarPlay til Bil – Komplett Guide",
            "slug": "apple-carplay-guide",
            "url": f"{origin}/guide/montera-carplay/",
            "status": "publish",
            "word_count": 480,
            "excerpt": "Steg-for-steg guide for å montere trådløs CarPlay skjerm i bilen.",
            "meta_title": "Trådløs CarPlay Skjerm Guide | Azom",
            "meta_description": "Lær hvordan du monterer trådløs Apple CarPlay skjerm i eldre biler på 5 minutter uten verksted.",
            "focus_keyphrase": "trådløs carplay skjerm",
            "canonical": f"{origin}/guide/montera-carplay/",
            "robots": "index, follow",
            "og_title": "Monteringsguide CarPlay",
            "og_description": "Steg-for-steg montering",
            "schema_type": "Article",
            "internal_links": [
                {"href": f"{origin}/produkt/carplay-skarm-pro/", "anchor": "Azom CarPlay Skjerm Pro"},
            ],
            "images_total": 2,
            "images_missing_alt": 1,
            "before_sha256": canonical_sha256({"object_id": 12, "meta_title": "guide"}),
        },
        {
            "object_type": "page",
            "object_id": 15,
            "title": "Hjem – Azom",
            "slug": "",
            "url": f"{origin}/",
            "status": "publish",
            "word_count": 360,
            "excerpt": "Nordens enkleste plug & play CarPlay-løsninger.",
            "meta_title": "Azom – Trådløs Apple CarPlay & Android Auto",
            "meta_description": "Oppgrader bilen på 5 minutter med trådløs Apple CarPlay og Android Auto fra Azom. 2 års garanti og rask levering.",
            "focus_keyphrase": "azom carplay",
            "canonical": f"{origin}/",
            "robots": "index, follow",
            "og_title": "Azom CarPlay",
            "og_description": "Plug & play CarPlay",
            "schema_type": "Organization",
            "internal_links": [
                {"href": f"{origin}/produkt/carplay-skarm-pro/", "anchor": "CarPlay Skjerm Pro"},
                {"href": f"{origin}/guide/montera-carplay/", "anchor": "Monteringsguide"},
            ],
            "images_total": 1,
            "images_missing_alt": 0,
            "before_sha256": canonical_sha256({"object_id": 15, "meta_title": "home"}),
        },
    ]


def _fallback_mock_media(base_url: str) -> list[dict[str, Any]]:
    origin = base_url.rstrip("/") or "https://mock-shop.example.test"
    return [
        {
            "media_id": 301,
            "filename": "azom-carplay-skarm-pro.webp",
            "url": f"{origin}/wp-content/uploads/2026/11/azom-carplay-skarm-pro.webp",
            "mime_type": "image/webp",
            "alt_text": "Azom 9 tommers trådløs Apple CarPlay-skjerm montert på dashbord",
            "title": "Azom CarPlay Skjerm Pro",
            "caption": "",
            "description": "",
            "width": 1200,
            "height": 800,
            "filesize": 94_000,
            "attached_post_id": 501,
            "before_sha256": canonical_sha256({"media_id": 301, "alt_text": "ok"}),
        },
        {
            "media_id": 302,
            "filename": "IMG_4821.jpg",
            "url": f"{origin}/wp-content/uploads/2026/11/IMG_4821.jpg",
            "mime_type": "image/jpeg",
            "alt_text": "",
            "title": "CarPlay Pro i Volvo XC60",
            "caption": "",
            "description": "",
            "width": 0,
            "height": 0,
            "filesize": 640_000,
            "attached_post_id": 501,
            "before_sha256": canonical_sha256({"media_id": 302, "alt_text": ""}),
        },
        {
            "media_id": 303,
            "filename": "DSC_0094.png",
            "url": f"{origin}/wp-content/uploads/2026/11/DSC_0094.png",
            "mime_type": "image/png",
            "alt_text": "bild",
            "title": "CarPlay Adapter Mini USB-C",
            "caption": "",
            "description": "",
            "width": 1000,
            "height": 1000,
            "filesize": 310_000,
            "attached_post_id": 502,
            "before_sha256": canonical_sha256({"media_id": 303, "alt_text": "bild"}),
        },
    ]


class SeoStudioService:
    """High-level facade for SEO audits, SHA-256 fix plans, Change Vault writes, strategies, campaigns, and link packages."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        bridge_client_factory: Callable[[Site], Any] | None = None,
        wp_client_factory: Callable[..., Any] | None = None,
        woo_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.store = SeoStudioStore(data_dir=self.data_dir)
        self.ledger = ChangeLedger(data_dir=self.data_dir)
        self._bridge_client_factory = bridge_client_factory
        self._wp_client_factory = wp_client_factory
        self._woo_client_factory = woo_client_factory

    # --- Internal site/client resolvers ----------------------------------- #

    def _resolve_site(self, site_id: str, *, market: str = "no") -> Site:
        sid = str(site_id or "").strip().lower()
        sites = load_sites(data_dir=self.data_dir, apply_overlay=False)
        found = site_by_id(sites, sid) if sid else None
        if found is not None:
            return found
        if not sid and sites:
            return sites[0]
        if not is_mock_mode():
            if sid:
                raise ValueError(
                    f"Unknown site_id {site_id!r} in live mode; register the site in wp_sites.yaml."
                )
            raise ValueError("No WordPress sites configured in live mode.")
        # Synthetic fallback site for custom site IDs in mock/tests ONLY
        normalize_market(market)
        clean_sid = sid or "mock-shop"
        url = f"https://{clean_sid}.example.test"
        return Site(
            id=clean_sid,
            display_name=clean_sid,
            canonical_url=url,
            environment="staging" if "staging" in clean_sid else "production",
            business_owner="customer",
            hosting_provider="unknown",
            wordpress_version="6.7",
            php_version="8.3",
            woocommerce=True,
            critical_urls=(f"{url}/",),
            maintenance_window="not agreed",
            secret_ref=f"WP_SECRET_{clean_sid.upper().replace('-', '_')}",
            capabilities=(
                ("hosting_api", "unknown"),
                ("wp_cli", "unknown"),
                ("staging", "unknown"),
                ("backup", "unknown"),
                ("restore", "unknown"),
                ("uptime", "unknown"),
                ("security_scanner", "unknown"),
            ),
            alert_confirmations=2,
            check_interval_seconds=300,
            timeout_seconds=10.0,
            dedupe_window_seconds=30,
            retention_days=30,
            alert_owner="oscar",
            files_root="/var/www/mock",
            ssh_host=f"{clean_sid}.example.test",
        )

    def _get_bridge(self, site: Site) -> Any | None:
        if self._bridge_client_factory is not None:
            return self._bridge_client_factory(site)
        try:
            from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

            return bridge_client_for_site(site)
        except Exception:
            return None

    def _get_wp_client(self, site: Site, *, market: str = "no") -> Any | None:
        if self._wp_client_factory is not None:
            return self._wp_client_factory(base_url=site.canonical_url, domain=market)
        try:
            from ecom_ops.integrations.wordpress import wp_client_from_env

            return wp_client_from_env(
                base_url=site.canonical_url,
                domain=market,
                use_mock=is_mock_mode(),
            )
        except Exception:
            return None

    def _get_woo_client(self, site: Site, *, market: str = "no") -> Any | None:
        if self._woo_client_factory is not None:
            return self._woo_client_factory(base_url=site.canonical_url, domain=market)
        try:
            from ecom_ops.integrations.woocommerce import client_from_env

            return client_from_env(
                base_url=site.canonical_url,
                domain=market,
                use_mock=is_mock_mode(),
            )
        except Exception:
            return None

    def _load_entities_and_media(
        self,
        site: Site,
        *,
        market: str,
        limit: int,
        object_type: str = "all",
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Collect overview, entities, media, categories from Bridge with WP/Woo fallback."""
        sources: list[str] = []
        overview: dict[str, Any] = {
            "seo_plugin": "yoast",
            "seo_plugin_version": "",
            "blog_public": True,
            "permalink_structure": "/%postname%/",
            "home_url": site.canonical_url,
            "sitemap_urls": [f"{site.canonical_url.rstrip('/')}/sitemap_index.xml"],
            "robots_url": f"{site.canonical_url.rstrip('/')}/robots.txt",
            "multilingual": "wpml",
            "counts": {},
        }
        entities: list[dict[str, Any]] = []
        media: list[dict[str, Any]] = []
        categories: list[dict[str, Any]] = []

        bridge = self._get_bridge(site)
        if bridge is not None:
            if hasattr(bridge, "seo_overview"):
                try:
                    ov = bridge.seo_overview()
                    if isinstance(ov, dict) and ov.get("ok"):
                        overview.update(ov)
                        sources.append("azom_ops_bridge:seo_overview")
                except Exception:
                    pass
            if hasattr(bridge, "seo_entities"):
                try:
                    res = bridge.seo_entities(object_type=object_type, limit=limit, page=1)
                    if isinstance(res, dict) and res.get("ok"):
                        entities = list(res.get("entities") or [])
                        if res.get("seo_plugin"):
                            overview["seo_plugin"] = res["seo_plugin"]
                        sources.append("azom_ops_bridge:seo_entities")
                except Exception:
                    pass
            if hasattr(bridge, "seo_media_inventory"):
                try:
                    mres = bridge.seo_media_inventory(limit=max(50, limit), page=1)
                    if isinstance(mres, dict) and mres.get("ok"):
                        media = list(mres.get("media") or [])
                        sources.append("azom_ops_bridge:seo_media")
                except Exception:
                    pass

        # Fallback to Woo/WP REST or mock fixtures if bridge did not provide entities/media
        if not entities:
            if is_mock_mode():
                entities = _fallback_mock_entities(site.canonical_url)
                sources.append("mock_seo_entities")
            else:
                wp = self._get_wp_client(site, market=market)
                if wp is not None:
                    if object_type in {"all", "page"} and hasattr(wp, "list_pages"):
                        try:
                            for page in wp.list_pages(per_page=min(25, limit)):
                                raw_p = page.raw if isinstance(page.raw, dict) else {}
                                content_obj = raw_p.get("content")
                                rendered = (
                                    content_obj.get("rendered", "")
                                    if isinstance(content_obj, dict)
                                    else str(content_obj or "")
                                )
                                entities.append(
                                    {
                                        "object_type": "page",
                                        "object_id": page.id,
                                        "title": page.title,
                                        "slug": str(raw_p.get("slug") or ""),
                                        "url": str(page.link or ""),
                                        "status": page.status,
                                        "word_count": _strip_html_word_count(rendered),
                                        "meta_title": "",
                                        "meta_description": "",
                                        "focus_keyphrase": "",
                                        "canonical": "",
                                        "robots": "",
                                        "internal_links": [],
                                    }
                                )
                            sources.append("wp_rest:pages")
                        except Exception:
                            pass
                    if object_type in {"all", "post"} and hasattr(wp, "list_posts"):
                        try:
                            for post in wp.list_posts(per_page=min(25, limit)):
                                raw_p = post.raw if isinstance(post.raw, dict) else {}
                                content_obj = raw_p.get("content")
                                rendered = (
                                    content_obj.get("rendered", "")
                                    if isinstance(content_obj, dict)
                                    else str(content_obj or "")
                                )
                                entities.append(
                                    {
                                        "object_type": "post",
                                        "object_id": post.id,
                                        "title": post.title,
                                        "slug": str(raw_p.get("slug") or ""),
                                        "url": str(post.link or ""),
                                        "status": post.status,
                                        "word_count": _strip_html_word_count(rendered),
                                        "meta_title": "",
                                        "meta_description": "",
                                        "focus_keyphrase": "",
                                        "canonical": "",
                                        "robots": "",
                                        "internal_links": [],
                                    }
                                )
                            sources.append("wp_rest:posts")
                        except Exception:
                            pass
                if object_type in {"all", "product"}:
                    woo_fb = self._get_woo_client(site, market=market)
                    if woo_fb is not None and hasattr(woo_fb, "list_products"):
                        try:
                            for prod in woo_fb.list_products(per_page=min(25, limit)):
                                if not isinstance(prod, dict):
                                    continue
                                desc_html = (
                                    f"{prod.get('short_description') or ''} "
                                    f"{prod.get('description') or ''}"
                                )
                                entities.append(
                                    {
                                        "object_type": "product",
                                        "object_id": int(prod.get("id") or 0),
                                        "title": str(prod.get("name") or ""),
                                        "slug": str(prod.get("slug") or ""),
                                        "url": str(prod.get("permalink") or ""),
                                        "status": str(prod.get("status") or "publish"),
                                        "word_count": _strip_html_word_count(desc_html),
                                        "meta_title": "",
                                        "meta_description": "",
                                        "focus_keyphrase": "",
                                        "canonical": "",
                                        "robots": "",
                                        "internal_links": [],
                                    }
                                )
                            sources.append("woo_rest:products")
                        except Exception:
                            pass

        if not media:
            if is_mock_mode():
                media = _fallback_mock_media(site.canonical_url)
                sources.append("mock_seo_media")
            else:
                wp = self._get_wp_client(site, market=market)
                if wp is not None and hasattr(wp, "list_media"):
                    try:
                        raw_media = wp.list_media(per_page=min(50, limit))
                        for m in raw_media or []:
                            details = m.get("media_details") or {}
                            title_obj = m.get("title")
                            title_txt = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj or "")
                            media.append(
                                {
                                    "media_id": int(m.get("id") or 0),
                                    "filename": str(m.get("source_url") or "").rsplit("/", 1)[-1],
                                    "url": str(m.get("source_url") or ""),
                                    "mime_type": str(m.get("mime_type") or ""),
                                    "alt_text": str(m.get("alt_text") or ""),
                                    "title": title_txt,
                                    "width": int(details.get("width") or 0),
                                    "height": int(details.get("height") or 0),
                                    "filesize": int(details.get("filesize") or 0),
                                    "attached_post_id": m.get("post"),
                                }
                            )
                        sources.append("wp_rest:media")
                    except Exception:
                        pass

        # Optional WooCommerce product categories
        woo = self._get_woo_client(site, market=market)
        if woo is not None and hasattr(woo, "list_product_categories"):
            try:
                cats = woo.list_product_categories(per_page=50, page=1)
                if isinstance(cats, list):
                    categories = cats
                    sources.append("woo_rest:categories")
            except Exception:
                pass

        return overview, entities, media, categories, sources

    # --- 1. Audits -------------------------------------------------------- #

    def run_audit(
        self,
        *,
        site_id: str,
        actor: str,
        scope: str = "site",
        target_id: str | None = None,
        path: str = "/",
        market: str = "no",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run a full or scoped SEO audit and persist the ``seoa-*`` report."""
        actor_obj = require_seo_read(actor)
        clean_scope = str(scope or "site").strip().lower()
        if clean_scope not in VALID_AUDIT_SCOPES:
            raise ValueError(
                f"Invalid audit scope {scope!r}. Allowed: {sorted(VALID_AUDIT_SCOPES)}"
            )
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        clean_path = "/" + str(path or "/").lstrip("/")

        obj_filter = (
            "product"
            if clean_scope == "product"
            else ("page" if clean_scope == "page" else "all")
        )
        overview, entities, media, categories, data_sources = self._load_entities_and_media(
            site, market=mkt, limit=limit, object_type=obj_filter
        )

        # Filter to specific target_id when provided (fail explicit on unmatched target)
        target_not_found = False
        matched_target_url = ""
        if target_id is not None and str(target_id).strip():
            tid = str(target_id).strip()
            if clean_scope == "media":
                media = [m for m in media if str(m.get("media_id")) == tid]
                if not media:
                    target_not_found = True
            else:
                entities = [
                    e
                    for e in entities
                    if str(e.get("object_id")) == tid or str(e.get("slug")) == tid
                ]
                if not entities:
                    target_not_found = True
                else:
                    matched_target_url = str(entities[0].get("url") or "").strip()

        all_issues: list[dict[str, Any]] = []
        technical_data: dict[str, Any] = {}
        on_page_data: dict[str, Any] = {}
        media_data: dict[str, Any] = {}
        links_data: dict[str, Any] = {}
        cannibalization_data: list[dict[str, Any]] = []

        if target_not_found:
            all_issues.append(
                SeoIssue(
                    code="target_not_found",
                    severity="high",
                    category="technical",
                    title=f"Målobjekt '{target_id}' hittades inte för scope '{clean_scope}'",
                    detail=f"Inget matchande objekt med ID/slug '{target_id}' fanns på {site.id}.",
                    target_type=clean_scope if clean_scope in VALID_TARGET_TYPES else "site",
                    target_id=str(target_id or ""),
                    suggestion="Verifiera objekt-ID eller kör en fullständig 'site'-audit.",
                ).to_dict()
            )

        # Technical SEO (HTML, JSON-LD, robots, sitemap, blog_public, Nordic hreflang)
        if clean_scope in {"site", "technical", "page", "product"} and not target_not_found:
            origin = str(overview.get("home_url") or site.canonical_url).rstrip("/")
            if clean_scope in {"product", "page"} and matched_target_url and clean_path == "/":
                target_url = (
                    matched_target_url
                    if matched_target_url.startswith(("http://", "https://"))
                    else urljoin(f"{origin}/", matched_target_url.lstrip("/"))
                )
            else:
                target_url = f"{origin}{clean_path}"

            html_res = fetch_public_seo_resource(target_url, site_id=site.id, market=mkt)
            if html_res.get("ok"):
                html_analysis = analyze_html_seo(
                    html_res["text"],
                    url=html_res.get("final_url") or target_url,
                    response_headers=html_res.get("headers"),
                )
            else:
                err_msg = str(html_res.get("message") or f"HTTP {html_res.get('status', 0)}")
                html_analysis = {
                    "url": target_url,
                    "fetch_error": err_msg,
                    "status": int(html_res.get("status") or 0),
                    "issues": [
                        SeoIssue(
                            code="technical_fetch_failed",
                            severity="critical",
                            category="technical",
                            title="Kunde inte hämta sidans HTML för teknisk SEO-audit",
                            detail=f"Hämtning av {target_url} misslyckades: {err_msg}",
                            url=target_url,
                            suggestion="Kontrollera att webbplatsen svarar med HTTP 200 OK och inte blockerar granskning.",
                        ).to_dict()
                    ],
                }

            robots_url = str(overview.get("robots_url") or f"{origin}/robots.txt")
            robots_res = fetch_public_seo_resource(robots_url, site_id=site.id, market=mkt)
            if robots_res.get("ok"):
                robots_analysis = analyze_robots_txt(robots_res.get("text", ""), site_url=origin)
            else:
                r_err = str(robots_res.get("message") or f"HTTP {robots_res.get('status', 0)}")
                robots_analysis = {
                    "robots_url": robots_url,
                    "blocks_entire_site": False,
                    "blocks_assets": False,
                    "blocks_products": False,
                    "disallows_wildcard": [],
                    "allows_wildcard": [],
                    "sitemaps": [],
                    "fetch_error": r_err,
                    "issues": [
                        SeoIssue(
                            code="robots_fetch_failed",
                            severity="medium",
                            category="technical",
                            title="Kunde inte läsa robots.txt",
                            detail=f"Hämtning av {robots_url} misslyckades ({r_err}).",
                            url=robots_url,
                            suggestion="Publicera en giltig robots.txt (HTTP 200) som listar XML-sitemap.",
                        ).to_dict()
                    ],
                }

            sitemap_candidates = list(
                dict.fromkeys(
                    [
                        *(str(u).strip() for u in (overview.get("sitemap_urls") or []) if str(u).strip()),
                        *(str(u).strip() for u in (robots_analysis.get("sitemaps") or []) if str(u).strip()),
                        f"{origin}/sitemap_index.xml",
                        f"{origin}/wp-sitemap.xml",
                    ]
                )
            )
            primary_sm = sitemap_candidates[0]
            sitemap_analysis: dict[str, Any] | None = None
            last_sm_err = ""
            for sm_url in sitemap_candidates[:4]:
                sm_res = fetch_public_seo_resource(sm_url, site_id=site.id, market=mkt)
                if not sm_res.get("ok"):
                    last_sm_err = str(sm_res.get("message") or f"HTTP {sm_res.get('status', 0)}")
                    continue
                candidate_analysis = analyze_sitemap_xml(sm_res.get("text", ""), sitemap_url=sm_url)
                sitemap_analysis = candidate_analysis
                if candidate_analysis.get("valid_xml"):
                    break
            if sitemap_analysis is None:
                sitemap_analysis = {
                    "sitemap_url": primary_sm,
                    "kind": "empty",
                    "valid_xml": False,
                    "url_count": 0,
                    "child_sitemaps": [],
                    "urls": [],
                    "non_https_urls": [],
                    "duplicate_urls": [],
                    "fetch_error": last_sm_err or "sitemap fetch failed",
                    "issues": [
                        SeoIssue(
                            code="sitemap_fetch_failed",
                            severity="high",
                            category="technical",
                            title="XML-sitemap kunde inte hämtas",
                            detail=f"Ingen av sitemap-URL:erna ({', '.join(sitemap_candidates[:3])}) svarade med HTTP 200 ({last_sm_err}).",
                            url=primary_sm,
                            suggestion="Aktivera XML-sitemap i SEO-pluginet eller WordPress core (wp-sitemap.xml) och verifiera HTTP 200.",
                        ).to_dict()
                    ],
                }

            # Nordic hreflang cross-market check
            if is_mock_mode():
                fx = mock_technical_fixtures(site_id=site.id, market=mkt, path=clean_path, base_url=origin)
                nordic_analyzed = {
                    u: analyze_html_seo(h, url=u) for u, h in fx["nordic_pages"].items()
                }
            else:
                primary_key = str(html_analysis.get("url") or target_url)
                nordic_analyzed = {primary_key: html_analysis}
                seen_alt_urls: set[str] = {primary_key.rstrip("/").lower()}
                fetched_alts = 0
                for hl_entry in html_analysis.get("hreflang") or []:
                    if fetched_alts >= _MAX_LIVE_HREFLANG_FETCHES:
                        break
                    alt_url = str(hl_entry.get("href") or "").strip()
                    if not alt_url.startswith("https://"):
                        continue
                    norm_alt = alt_url.rstrip("/").lower()
                    if norm_alt in seen_alt_urls:
                        continue
                    seen_alt_urls.add(norm_alt)
                    fetched_alts += 1
                    alt_res = fetch_public_seo_resource(
                        alt_url,
                        use_mock=False,
                        site_id=site.id,
                        market=mkt,
                        timeout=8.0,
                    )
                    if alt_res.get("ok"):
                        alt_final = str(alt_res.get("final_url") or alt_url)
                        nordic_analyzed[alt_final] = analyze_html_seo(
                            alt_res["text"],
                            url=alt_final,
                            response_headers=alt_res.get("headers"),
                        )
            hreflang_audit = audit_nordic_hreflang(nordic_analyzed)

            tech_issues: list[dict[str, Any]] = []
            if overview.get("blog_public") is False:
                tech_issues.append(
                    SeoIssue(
                        code="wp_blog_public_disabled",
                        severity="critical",
                        category="technical",
                        title="WordPress 'blog_public' är avstängt (Discourage search engines)",
                        detail="Inställningen 'Be sökmotorer att inte indexera denna webbplats' är aktiv.",
                        url=origin,
                        suggestion="Aktivera sökmotorsynlighet i WordPress (Inställningar -> Läsning).",
                    ).to_dict()
                )
            tech_issues.extend(html_analysis.get("issues") or [])
            tech_issues.extend(robots_analysis.get("issues") or [])
            tech_issues.extend(sitemap_analysis.get("issues") or [])
            tech_issues.extend(hreflang_audit.get("issues") or [])

            technical_data = {
                "seo_plugin": overview.get("seo_plugin", "unknown"),
                "seo_plugin_version": overview.get("seo_plugin_version", ""),
                "blog_public": bool(overview.get("blog_public", True)),
                "permalink_structure": overview.get("permalink_structure", ""),
                "multilingual": overview.get("multilingual", ""),
                "html": html_analysis,
                "robots": robots_analysis,
                "sitemap": sitemap_analysis,
                "hreflang": hreflang_audit,
                "provenance": html_res.get("provenance", "mock"),
            }
            all_issues.extend(tech_issues)
            data_sources.append(f"technical_html:{html_res.get('provenance', 'mock')}")

        # On-page audit
        if clean_scope in {"site", "page", "product"}:
            on_page_data = audit_entities_on_page(
                entities,
                categories=categories if clean_scope == "site" else None,
                seo_plugin=str(overview.get("seo_plugin") or "yoast"),
            )
            all_issues.extend(on_page_data.get("issues") or [])

        # Media SEO audit
        if clean_scope in {"site", "media"}:
            media_data = audit_media_seo(media)
            all_issues.extend(media_data.get("issues") or [])

        # Internal link graph
        if clean_scope in {"site", "links"}:
            links_data = audit_internal_links(entities, base_url=site.canonical_url)
            all_issues.extend(links_data.get("issues") or [])

        # Keyword cannibalization
        if clean_scope in {"site", "cannibalization"}:
            cannibalization_data, cann_issues = detect_keyword_cannibalization(entities)
            all_issues.extend(cann_issues)

        # GSC quick-wins enrichment (Proposal 2)
        gsc_status_data: dict[str, Any] = {}
        gsc_quick_wins_data: dict[str, Any] = {}
        gsc_note_text = GSC_NOT_CONNECTED_NOTE
        if clean_scope in {"site", "gsc"}:
            gsc_status_data, gsc_quick_wins_data = self._fetch_gsc_quick_wins_internal(
                site,
                market=mkt,
                entities=entities,
                days=28,
            )
            mode = str(gsc_status_data.get("mode") or "mock")
            if gsc_status_data.get("connected") or mode == "mock":
                sd_cnt = int((gsc_quick_wins_data.get("summary") or {}).get("strike_distance_count", 0))
                lc_cnt = int((gsc_quick_wins_data.get("summary") or {}).get("low_ctr_count", 0))
                if mode == "live":
                    gsc_note_text = (
                        f"Google Search Console (GSC) ansluten (live): {sd_cnt} slagläges-sökord "
                        f"(pos 4–15) och {lc_cnt} låg-CTR-möjligheter analyserade."
                    )
                    data_sources.append("google_search_console:live")
                else:
                    gsc_note_text = (
                        f"{GSC_NOT_CONNECTED_NOTE} (Mock-läge: {sd_cnt} simulerade slagläges-sökord "
                        f"och {lc_cnt} låg-CTR-fynd tillgängliga för förhandsvisning.)"
                    )
                    data_sources.append("google_search_console:mock")

        # Nordic parity scope (Proposal 3)
        parity_data: dict[str, Any] = {}
        if clean_scope == "parity":
            parity_res = self._run_nordic_parity_internal(site)
            parity_data = parity_res
            all_issues.extend(parity_res.get("issues") or [])
            data_sources.append("nordic_parity_matrix")

        # Deep crawl scope (Proposal 3)
        deep_crawl_data: dict[str, Any] = {}
        if clean_scope == "deep_crawl":
            crawl_res = self._run_deep_crawl_internal(site, max_pages=min(max(1, limit), 10), market=mkt)
            deep_crawl_data = crawl_res
            all_issues.extend(crawl_res.get("issues") or [])
            data_sources.append("deep_sitemap_crawl")

        # Deduplicate & sort issues by severity
        all_issues.sort(key=lambda item: (_SEVERITY_ORDER.get(str(item.get("severity")), 9), str(item.get("code"))))
        score, grade = compute_seo_score(all_issues)

        sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for iss in all_issues:
            s = str(iss.get("severity") or "low").lower()
            if s in sev_counts:
                sev_counts[s] += 1

        summary = {
            "total_issues": len(all_issues),
            **sev_counts,
            "seo_plugin": overview.get("seo_plugin", "unknown"),
            "entities_audited": int(on_page_data.get("entities_audited", 0)),
            "media_audited": int(media_data.get("total_media", 0)),
            "missing_alt_count": int(media_data.get("missing_alt_count", 0)),
            "orphan_pages_count": int(links_data.get("orphan_count", 0)),
            "cannibalization_count": len(cannibalization_data),
            "gsc_strike_distance_count": int(
                (gsc_quick_wins_data.get("summary") or {}).get("strike_distance_count", 0)
            ),
            **({"target_not_found": True} if target_not_found else {}),
        }

        report = SeoAuditReport(
            audit_id=new_seo_id("seoa"),
            site_id=site.id,
            market=mkt,
            scope=clean_scope,
            score=score,
            grade=grade,
            summary=summary,
            issues=all_issues,
            technical=technical_data,
            on_page=on_page_data,
            media=media_data,
            links=links_data,
            cannibalization=cannibalization_data,
            data_sources=list(dict.fromkeys(data_sources)),
            gsc_note=gsc_note_text,
            gsc_status=gsc_status_data,
            gsc_quick_wins=gsc_quick_wins_data,
            parity=parity_data,
            deep_crawl=deep_crawl_data,
        )
        payload = report.to_dict()
        self.store.save_audit(payload)
        log_action(
            actor=actor_obj.name,
            action="seo_audit_run",
            target=site.id,
            target_id=report.audit_id,
            details={"scope": clean_scope, "market": mkt, "score": score, "issues": len(all_issues)},
        )
        return {"ok": True, **payload}

    def get_audit(self, audit_id: str) -> dict[str, Any] | None:
        return self.store.get_audit(audit_id)

    def list_audits(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_audits(site_id=site_id, limit=limit)

    # --- 2. SEO Fix Plans (Propose + SHA-256 Pinned Oscar Apply) ---------- #

    @staticmethod
    def _bridge_lookup_media(bridge: Any, target_id: int) -> dict[str, Any] | None:
        if bridge is None or not hasattr(bridge, "seo_media_inventory"):
            return None
        # Try single-target lookup first (Contract #2), fall back to paginated scan
        try:
            direct = bridge.seo_media_inventory(media_id=target_id, limit=1, page=1)
            if isinstance(direct, dict) and direct.get("ok"):
                for item in direct.get("media") or []:
                    if str(item.get("media_id")) == str(target_id):
                        return item
                if direct.get("total", 0) == 0:
                    return None
        except TypeError:
            pass
        except Exception:
            return None

        for page in range(1, _MAX_BRIDGE_LOOKUP_PAGES + 1):
            try:
                mres = bridge.seo_media_inventory(limit=100, page=page)
            except Exception:
                return None
            if not isinstance(mres, dict) or not mres.get("ok"):
                return None
            batch = mres.get("media") or []
            for item in batch:
                if str(item.get("media_id")) == str(target_id):
                    return item
            total = int(mres.get("total") or 0)
            if len(batch) < 100 or (total > 0 and page * 100 >= total):
                break
        return None

    @staticmethod
    def _bridge_lookup_entity(
        bridge: Any,
        *,
        target_type: str,
        target_id: int,
    ) -> tuple[dict[str, Any] | None, str]:
        if bridge is None or not hasattr(bridge, "seo_entities"):
            return None, "yoast"
        obj_filter = target_type if target_type in {"product", "page", "post"} else "all"
        # Try single-target lookup first (Contract #2), fall back to paginated scan
        try:
            direct = bridge.seo_entities(
                object_type=obj_filter, object_id=target_id, limit=1, page=1
            )
            if isinstance(direct, dict) and direct.get("ok"):
                seo_plugin = str(direct.get("seo_plugin") or "yoast")
                for ent in direct.get("entities") or []:
                    ent_type = str(ent.get("object_type") or "")
                    if str(ent.get("object_id")) == str(target_id) and (
                        target_type in {"site", "category"} or ent_type == target_type
                    ):
                        return ent, seo_plugin
                if direct.get("total", 0) == 0:
                    return None, seo_plugin
        except TypeError:
            pass
        except Exception:
            return None, "yoast"

        last_plugin = "yoast"
        for page in range(1, _MAX_BRIDGE_LOOKUP_PAGES + 1):
            try:
                eres = bridge.seo_entities(object_type=obj_filter, limit=100, page=page)
            except Exception:
                return None, last_plugin
            if not isinstance(eres, dict) or not eres.get("ok"):
                return None, last_plugin
            last_plugin = str(eres.get("seo_plugin") or last_plugin)
            batch = eres.get("entities") or []
            for ent in batch:
                ent_type = str(ent.get("object_type") or "")
                if str(ent.get("object_id")) == str(target_id) and (
                    target_type in {"site", "category"} or ent_type == target_type
                ):
                    return ent, last_plugin
            total = int(eres.get("total") or 0)
            if len(batch) < 100 or (total > 0 and page * 100 >= total):
                break
        return None, last_plugin

    def _capture_rest_content_fields(
        self,
        site: Site,
        *,
        target_type: str,
        target_id: int,
        market: str,
        proposed: dict[str, Any] | None,
        before: dict[str, Any],
    ) -> bool:
        """Populate REST content fields (``name``/``description``/``short_description``/``content``) when proposed or needed."""
        props = proposed or {}
        if target_type == "product" and (
            any(k in props for k in ("name", "description", "short_description")) or not before
        ):
            woo = self._get_woo_client(site, market=market)
            if woo is not None and hasattr(woo, "get_product"):
                try:
                    prod = woo.get_product(target_id)
                    if isinstance(prod, dict) and prod.get("id"):
                        before.setdefault("object_type", "product")
                        before.setdefault("object_id", int(prod["id"]))
                        before["name"] = str(prod.get("name") or before.get("title") or "")
                        before.setdefault("title", before["name"])
                        before.setdefault("slug", str(prod.get("slug") or ""))
                        before["description"] = str(prod.get("description") or "")
                        before["short_description"] = str(prod.get("short_description") or "")
                        return True
                except Exception:
                    pass
        elif target_type in {"page", "post"} and ("content" in props or not before):
            wp = self._get_wp_client(site, market=market)
            if wp is not None:
                try:
                    post_obj = (
                        wp.get_page(target_id)
                        if target_type == "page" and hasattr(wp, "get_page")
                        else (wp.get_post(target_id) if hasattr(wp, "get_post") else None)
                    )
                    if post_obj is not None and int(getattr(post_obj, "id", 0) or 0) > 0:
                        raw_p = post_obj.raw if isinstance(post_obj.raw, dict) else {}
                        content_obj = raw_p.get("content")
                        content_val = (
                            content_obj.get("raw") or content_obj.get("rendered") or ""
                            if isinstance(content_obj, dict)
                            else str(content_obj or "")
                        )
                        excerpt_obj = raw_p.get("excerpt")
                        excerpt_val = (
                            excerpt_obj.get("raw") or excerpt_obj.get("rendered") or ""
                            if isinstance(excerpt_obj, dict)
                            else str(excerpt_obj or "")
                        )
                        before.setdefault("object_type", target_type)
                        before.setdefault("object_id", int(post_obj.id))
                        before.setdefault("title", str(post_obj.title or ""))
                        before.setdefault("slug", str(raw_p.get("slug") or ""))
                        before.setdefault("excerpt", str(excerpt_val))
                        before["content"] = str(content_val)
                        return True
                except Exception:
                    pass
        return False

    def _fetch_current_target_state(
        self,
        site: Site,
        *,
        fix_type: str,
        target_type: str,
        target_id: str,
        market: str,
        proposed: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Read current before-state & before_sha256 for the target entity/media."""
        bridge = self._get_bridge(site)
        mock_mode = is_mock_mode()

        if fix_type == "media_alt" or target_type == "media":
            mid = int(target_id) if str(target_id).isdigit() else 0
            if mid > 0:
                item = self._bridge_lookup_media(bridge, mid)
                if item is not None:
                    before = {
                        "media_id": int(item["media_id"]),
                        "alt_text": str(item.get("alt_text") or ""),
                        "title": str(item.get("title") or ""),
                        "caption": str(item.get("caption") or ""),
                        "description": str(item.get("description") or ""),
                    }
                    bsha = str(item.get("before_sha256") or _media_snapshot_sha256(before))
                    return before, bsha

                # WP REST single-media lookup
                wp = self._get_wp_client(site, market=market)
                if wp is not None and hasattr(wp, "get_media"):
                    try:
                        m = wp.get_media(mid)
                        if isinstance(m, dict) and int(m.get("id") or 0) == mid:
                            def _rendered(val: Any) -> str:
                                if isinstance(val, dict):
                                    return str(val.get("raw") or val.get("rendered") or "")
                                return str(val or "")

                            before = {
                                "media_id": mid,
                                "alt_text": str(m.get("alt_text") or ""),
                                "title": _rendered(m.get("title")),
                                "caption": _rendered(m.get("caption")),
                                "description": _rendered(m.get("description")),
                            }
                            return before, _media_snapshot_sha256(before)
                    except Exception:
                        pass

            # Mock fixtures strictly gated on is_mock_mode()
            if not mock_mode:
                raise TargetNotFound(
                    f"TargetNotFound: media:{target_id} hittades inte på '{site.id}' i live-läge."
                )
            for item in _fallback_mock_media(site.canonical_url):
                if str(item.get("media_id")) == str(target_id):
                    before = {
                        "media_id": int(item["media_id"]),
                        "alt_text": str(item.get("alt_text") or ""),
                        "title": str(item.get("title") or ""),
                        "caption": str(item.get("caption") or ""),
                        "description": str(item.get("description") or ""),
                    }
                    return before, str(item.get("before_sha256") or _media_snapshot_sha256(before))
            before = {
                "media_id": mid if mid > 0 else target_id,
                "alt_text": "",
                "title": "",
                "caption": "",
                "description": "",
            }
            return before, _media_snapshot_sha256(before)

        # Entity SEO meta / product / page / post / schema
        obj_int = int(target_id) if str(target_id).isdigit() else 0
        if obj_int > 0 and target_type in {"product", "page", "post"}:
            ent, seo_plugin = self._bridge_lookup_entity(
                bridge, target_type=target_type, target_id=obj_int
            )
            if ent is not None:
                before = {
                    "seo_plugin": seo_plugin,
                    "object_type": str(ent.get("object_type") or target_type),
                    "object_id": int(ent["object_id"]),
                    "title": str(ent.get("title") or ""),
                    "slug": str(ent.get("slug") or ""),
                    "excerpt": str(ent.get("excerpt") or ""),
                    "meta_title": str(ent.get("meta_title") or ""),
                    "meta_description": str(ent.get("meta_description") or ""),
                    "focus_keyphrase": str(ent.get("focus_keyphrase") or ""),
                    "canonical": str(ent.get("canonical") or ""),
                    "robots": str(ent.get("robots") or ""),
                    "og_title": str(ent.get("og_title") or ""),
                    "og_description": str(ent.get("og_description") or ""),
                    "schema_type": str(ent.get("schema_type") or ""),
                }
                self._capture_rest_content_fields(
                    site,
                    target_type=target_type,
                    target_id=obj_int,
                    market=market,
                    proposed=proposed,
                    before=before,
                )
                bsha = str(ent.get("before_sha256") or _entity_snapshot_sha256(before))
                return before, bsha

            # Live/mock WP/Woo REST fallback lookup when bridge did not return entity
            rest_before: dict[str, Any] = {}
            if self._capture_rest_content_fields(
                site,
                target_type=target_type,
                target_id=obj_int,
                market=market,
                proposed=proposed,
                before=rest_before,
            ):
                rest_before.setdefault("seo_plugin", "yoast")
                for k in _ENTITY_SNAPSHOT_KEYS:
                    rest_before.setdefault(k, "")
                return rest_before, _entity_snapshot_sha256(rest_before)

        if not mock_mode:
            raise TargetNotFound(
                f"TargetNotFound: {target_type}:{target_id} hittades inte på '{site.id}' i live-läge."
            )

        for ent in _fallback_mock_entities(site.canonical_url):
            if str(ent.get("object_id")) == str(target_id):
                before = {
                    "seo_plugin": "yoast",
                    "object_type": str(ent.get("object_type") or target_type),
                    "object_id": int(ent["object_id"]),
                    "title": str(ent.get("title") or ""),
                    "slug": str(ent.get("slug") or ""),
                    "excerpt": str(ent.get("excerpt") or ""),
                    "meta_title": str(ent.get("meta_title") or ""),
                    "meta_description": str(ent.get("meta_description") or ""),
                    "focus_keyphrase": str(ent.get("focus_keyphrase") or ""),
                    "canonical": str(ent.get("canonical") or ""),
                    "robots": str(ent.get("robots") or ""),
                    "og_title": str(ent.get("og_title") or ""),
                    "og_description": str(ent.get("og_description") or ""),
                    "schema_type": str(ent.get("schema_type") or ""),
                }
                if obj_int > 0 and target_type in {"product", "page", "post"}:
                    self._capture_rest_content_fields(
                        site,
                        target_type=target_type,
                        target_id=obj_int,
                        market=market,
                        proposed=proposed,
                        before=before,
                    )
                return before, str(ent.get("before_sha256") or _entity_snapshot_sha256(before))

        before = {
            "seo_plugin": "yoast",
            "object_type": target_type,
            "object_id": obj_int if obj_int > 0 else target_id,
            "title": "",
            "slug": "",
            "excerpt": "",
            "meta_title": "",
            "meta_description": "",
            "focus_keyphrase": "",
            "canonical": "",
            "robots": "",
            "og_title": "",
            "og_description": "",
            "schema_type": "",
        }
        return before, _entity_snapshot_sha256(before)

    def propose_fix(
        self,
        *,
        site_id: str,
        actor: str,
        fix_type: str,
        target_type: str,
        target_id: str | int,
        proposed: dict[str, Any],
        summary: str = "",
        market: str = "no",
    ) -> dict[str, Any]:
        """Create an immutable, SHA-256-pinned SEO fix plan (``seom-*``) — never executes."""
        actor_obj = require_seo_propose(actor)
        clean_fix = str(fix_type or "").strip().lower()
        if clean_fix not in VALID_FIX_TYPES:
            raise ValueError(f"Invalid fix_type {fix_type!r}. Allowed: {sorted(VALID_FIX_TYPES)}")
        clean_target_type = str(target_type or "").strip().lower()
        if clean_target_type not in VALID_TARGET_TYPES:
            raise ValueError(
                f"Invalid target_type {target_type!r}. Allowed: {sorted(VALID_TARGET_TYPES)}"
            )
        if not isinstance(proposed, dict) or not proposed:
            raise ValueError("proposed must be a non-empty dict of fields to change")

        tid = str(target_id).strip()
        if not tid:
            raise ValueError("target_id must not be empty")
        if clean_target_type in _NUMERIC_TARGET_TYPES:
            if not tid.isdigit() or int(tid) <= 0:
                raise ValueError(
                    f"target_id must be a positive integer for target_type={clean_target_type!r}, got {target_id!r}"
                )

        allowed_fields = _allowed_proposed_fields(clean_fix, clean_target_type)
        disallowed = sorted(k for k in proposed if k not in allowed_fields)
        if disallowed:
            raise ValueError(
                f"Unsupported proposed field(s) {disallowed} for fix_type={clean_fix!r}, "
                f"target_type={clean_target_type!r}. Allowed: {sorted(allowed_fields)}"
            )
        if clean_fix == "schema_jsonld" and "schema_json" in proposed:
            sj = proposed["schema_json"]
            if not isinstance(sj, dict) or not str(sj.get("@type") or "").strip():
                raise ValueError("schema_json must be a JSON object with a non-empty '@type'")

        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)

        before, before_sha = self._fetch_current_target_state(
            site,
            fix_type=clean_fix,
            target_type=clean_target_type,
            target_id=tid,
            market=mkt,
            proposed=proposed,
        )
        now_dt = datetime.now(UTC)
        expires_dt = now_dt + timedelta(hours=FIX_PLAN_TTL_HOURS)

        clean_summary = (
            " ".join(str(summary or "").strip().split())
            or f"SEO-fix ({clean_fix}) på {clean_target_type} #{tid}"
        )
        plan_id = new_seo_id("seom")
        hash_material = {
            "plan_id": plan_id,
            "site_id": site.id,
            "market": mkt,
            "fix_type": clean_fix,
            "target_type": clean_target_type,
            "target_id": tid,
            "before_sha256": before_sha,
            "proposed": proposed,
        }
        plan_sha = canonical_sha256(hash_material)

        plan = SeoFixPlan(
            plan_id=plan_id,
            site_id=site.id,
            market=mkt,
            fix_type=clean_fix,
            target_type=clean_target_type,
            target_id=tid,
            summary=clean_summary,
            before=before,
            proposed=dict(proposed),
            before_sha256=before_sha,
            plan_sha256=plan_sha,
            status="pending",
            executed=False,
            approval_required=True,
            created_at=now_dt.isoformat(),
            expires_at=expires_dt.isoformat(),
            actor=actor_obj.name,
        )
        plan_dict = plan.to_dict()
        self.store.save_fix_plan(plan_dict)
        log_action(
            actor=actor_obj.name,
            action="seo_fix_proposed",
            target=site.id,
            target_id=plan_id,
            details={
                "fix_type": clean_fix,
                "target_type": clean_target_type,
                "target_id": tid,
                "plan_sha256": plan_sha,
            },
        )
        return {"ok": True, "plan": plan_dict}

    def get_fix_plan(self, plan_id: str) -> dict[str, Any] | None:
        return self.store.get_fix_plan(plan_id)

    def list_fix_plans(self, site_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.list_fix_plans(site_id=site_id, limit=limit)

    def apply_fix(
        self,
        *,
        site_id: str,
        plan_id: str,
        actor: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        """Apply a SHA-256-pinned SEO fix plan (Oscar ``WP_OPS_ADMIN`` ONLY) with Change Vault snapshot."""
        def _err(error_class: str, message: str) -> dict[str, Any]:
            return {
                "ok": False,
                "plan_id": str(plan_id or ""),
                "change_id": "",
                "executed": False,
                "live_calls": False,
                "error_class": error_class,
                "message": message,
            }

        try:
            actor_obj = require_seo_apply(actor)
        except AccessDenied as exc:
            log_action(
                actor=str(actor or "unknown"),
                action="seo_fix_apply_denied",
                target=site_id,
                target_id=plan_id,
                success=False,
                details={"reason": str(exc)},
            )
            return _err("AccessDenied", str(exc))

        allowed, gate_reason = seo_write_allowed()
        if not allowed:
            err_cls = (
                "SeoKillSwitchActive"
                if "kill" in gate_reason
                else "SeoWriteDisabled"
            )
            msg = (
                "SEO-skrivningar är stoppade av kill-switch (AZOM_SEO_KILL / AZOM_WP_DEPLOY_KILL)."
                if "kill" in gate_reason
                else "Live SEO-skrivning är avstängd (kräver AZOM_SEO_WRITE_ENABLED=1 i processmiljön)."
            )
            log_action(
                actor=actor_obj.name,
                action="seo_fix_apply_blocked",
                target=site_id,
                target_id=plan_id,
                success=False,
                details={"reason": gate_reason},
            )
            return _err(err_cls, msg)

        initial_plan = self.store.get_fix_plan(plan_id)
        if initial_plan is None:
            return _err("PlanNotFound", f"SEO-fixplan '{plan_id}' hittades inte.")
        if str(initial_plan.get("site_id", "")).lower() != str(site_id or "").strip().lower():
            return _err(
                "SiteMismatch",
                f"Planen tillhör '{initial_plan.get('site_id')}', inte '{site_id}'.",
            )

        try:
            site = self._resolve_site(
                str(initial_plan["site_id"]),
                market=str(initial_plan.get("market") or "no"),
            )
        except Exception as exc:
            return _err(type(exc).__name__, str(exc)[:200])

        with hold_site(site.id):
            plan = self.store.get_fix_plan(plan_id)
            if plan is None:
                return _err("PlanNotFound", f"SEO-fixplan '{plan_id}' hittades inte.")
            if plan.get("status") != "pending" or plan.get("executed"):
                return _err("PlanAlreadyApplied", f"Planen har redan status '{plan.get('status')}'.")

            # Strict fail-closed expiry check (reject missing, malformed, or timezone-naive)
            raw_exp = str(plan.get("expires_at") or "").strip()
            if not raw_exp:
                return _err("InvalidPlan", "SEO-fixplanen saknar utgångsdatum (expires_at).")
            try:
                exp_dt = datetime.fromisoformat(raw_exp)
            except ValueError:
                return _err("InvalidPlan", "SEO-fixplanen har ogiltigt utgångsdatum (expires_at).")
            if exp_dt.tzinfo is None or exp_dt.utcoffset() is None:
                return _err("InvalidPlan", "SEO-fixplanens utgångsdatum saknar tidszon (UTC krävs).")
            if datetime.now(UTC) > exp_dt.astimezone(UTC):
                expired_plan = {**plan, "status": "expired"}
                self.store.compare_and_swap_fix_plan(
                    plan_id, expected_status="pending", new_plan=expired_plan
                )
                return _err("PlanExpired", "SEO-fixplanen har löpt ut (12h TTL). Skapa en ny plan.")

            if not hmac.compare_digest(
                str(expected_sha256 or "").strip(), str(plan.get("plan_sha256") or "")
            ):
                return _err(
                    "PlanHashMismatch",
                    "SHA-256-hash matchar inte planen — applicering avbruten.",
                )

            fix_type = str(plan.get("fix_type") or "")
            target_type = str(plan.get("target_type") or "")
            target_id = str(plan.get("target_id") or "")
            proposed = dict(plan.get("proposed") or {})
            before = dict(plan.get("before") or {})
            before_sha = str(plan.get("before_sha256") or "")
            if not proposed:
                return _err("UnsupportedFields", "SEO-fixplanen innehåller inga fält att ändra.")

            live_calls = gate_reason == "live"
            bridge = self._get_bridge(site)
            plan_market = str(plan.get("market") or "no")

            # Pre-flight transport & field coverage check (never half-apply or fake executed=True)
            is_media = fix_type == "media_alt" or target_type == "media"
            media_fields: dict[str, Any] = {}
            bridge_fields: dict[str, Any] = {}
            bridge_covers_schema_json = False
            woo_fields: dict[str, Any] = {}
            wp_fields: dict[str, Any] = {}
            wp_client: Any = None
            woo_client: Any = None

            if is_media:
                if not target_id.isdigit() or int(target_id) <= 0:
                    return _err("UnsupportedFields", f"Ogiltigt numeriskt media-ID: {target_id!r}.")
                media_fields = {k: v for k, v in proposed.items() if k in _BRIDGE_MEDIA_FIELDS}
                extra_media = sorted(set(proposed) - set(media_fields))
                if extra_media or not media_fields:
                    return _err(
                        "UnsupportedFields",
                        f"Ej stödda mediafält i fixplan: {extra_media or list(proposed)}.",
                    )
                if not (bridge is not None and hasattr(bridge, "seo_update_media")):
                    wp_client = self._get_wp_client(site, market=plan_market)
                    if not (wp_client is not None and hasattr(wp_client, "update_media")):
                        return _err(
                            "NoTransportAvailable",
                            "Varken Azom Ops Bridge eller WP REST är tillgänglig för media_alt-skrivning.",
                        )
            else:
                if target_type not in {"product", "page", "post"} or not target_id.isdigit() or int(target_id) <= 0:
                    return _err(
                        "UnsupportedFields",
                        f"Automatisk skrivning stöds inte för target_type='{target_type}' (target_id='{target_id}').",
                    )
                has_bridge = bridge is not None and hasattr(bridge, "seo_update_meta")
                if has_bridge:
                    bridge_fields = {k: v for k, v in proposed.items() if k in _BRIDGE_META_FIELDS}
                    if (
                        "schema_type" not in bridge_fields
                        and fix_type == "schema_jsonld"
                        and isinstance(proposed.get("schema_json"), dict)
                        and str(proposed["schema_json"].get("@type") or "").strip()
                    ):
                        bridge_fields["schema_type"] = str(proposed["schema_json"]["@type"]).strip()
                        bridge_covers_schema_json = True

                remaining = {
                    k: v
                    for k, v in proposed.items()
                    if k not in bridge_fields and not (k == "schema_json" and bridge_covers_schema_json)
                }
                if remaining:
                    if target_type == "product":
                        woo_fields = {
                            k: v for k, v in remaining.items() if k in _WOO_PRODUCT_CONTENT_FIELDS
                        }
                        unmapped = sorted(set(remaining) - set(woo_fields))
                        if unmapped:
                            err_cls = (
                                "NoTransportAvailable"
                                if (not has_bridge and set(unmapped) <= (_BRIDGE_META_FIELDS | _SCHEMA_FIELDS))
                                else "UnsupportedFields"
                            )
                            return _err(
                                err_cls,
                                f"Fält saknar skrivtransport för product: {', '.join(unmapped)}.",
                            )
                        woo_client = self._get_woo_client(site, market=plan_market)
                        if woo_client is None or not hasattr(woo_client, "update_product"):
                            return _err(
                                "NoTransportAvailable",
                                "WooCommerce REST-klient är inte tillgänglig för produktskrivning.",
                            )
                    else:
                        wp_fields = {
                            k: v for k, v in remaining.items() if k in _WP_POST_CONTENT_FIELDS
                        }
                        unmapped = sorted(set(remaining) - set(wp_fields))
                        if unmapped:
                            err_cls = (
                                "NoTransportAvailable"
                                if (not has_bridge and set(unmapped) <= (_BRIDGE_META_FIELDS | _SCHEMA_FIELDS))
                                else "UnsupportedFields"
                            )
                            return _err(
                                err_cls,
                                f"Fält saknar skrivtransport för {target_type}: {', '.join(unmapped)}.",
                            )
                        wp_client = self._get_wp_client(site, market=plan_market)
                        needed_fn = "update_page" if target_type == "page" else "update_post"
                        if wp_client is None or not hasattr(wp_client, needed_fn):
                            return _err(
                                "NoTransportAvailable",
                                f"WordPress REST-klient är inte tillgänglig för {target_type}-skrivning.",
                            )

                if not bridge_fields and not woo_fields and not wp_fields:
                    return _err(
                        "NoTransportAvailable",
                        "Ingen skrivtransport kunde applicera de föreslagna fälten.",
                    )

            # Atomically claim the plan ("pending" -> "applying") before remote writes
            claiming_plan = {**plan, "status": "applying"}
            if not self.store.compare_and_swap_fix_plan(
                plan_id, expected_status="pending", new_plan=claiming_plan
            ):
                return _err("PlanAlreadyApplied", "Planen har redan påbörjats eller applicerats.")

            applied_fields: set[str] = set()
            transports_used: list[str] = []
            after_data: dict[str, Any] = {**before, **proposed}

            try:
                if is_media:
                    media_int = int(target_id)
                    if bridge is not None and hasattr(bridge, "seo_update_media"):
                        res = bridge.seo_update_media(
                            change_id=plan_id,
                            media_id=media_int,
                            fields=media_fields,
                            expected_before_sha256=before_sha,
                        )
                        if not isinstance(res, dict) or not res.get("ok", True):
                            raise RuntimeError(str((res or {}).get("error") or "Bridge media update failed"))
                        if res.get("after"):
                            after_data = {**after_data, **dict(res["after"])}
                        if res.get("before"):
                            before = {**before, **dict(res["before"])}
                        applied_fields.update(media_fields.keys())
                        transports_used.append("bridge")
                    else:
                        wp_client.update_media(
                            media_int,
                            alt_text=media_fields.get("alt_text"),
                            title=media_fields.get("title"),
                            caption=media_fields.get("caption"),
                            description=media_fields.get("description"),
                        )
                        applied_fields.update(media_fields.keys())
                        transports_used.append("wp_rest")
                else:
                    obj_int = int(target_id)
                    if bridge_fields:
                        res = bridge.seo_update_meta(
                            change_id=plan_id,
                            object_type=target_type,
                            object_id=obj_int,
                            fields=bridge_fields,
                            expected_before_sha256=before_sha,
                        )
                        if not isinstance(res, dict) or not res.get("ok", True):
                            raise RuntimeError(str((res or {}).get("error") or "Bridge meta update failed"))
                        if res.get("after"):
                            after_data = {**after_data, **dict(res["after"])}
                        if res.get("before"):
                            before = {**before, **dict(res["before"])}
                        applied_fields.update(bridge_fields.keys())
                        if bridge_covers_schema_json:
                            applied_fields.add("schema_json")
                        transports_used.append("bridge")

                    if woo_fields:
                        woo_client.update_product(obj_int, woo_fields)
                        after_data.update(woo_fields)
                        applied_fields.update(woo_fields.keys())
                        transports_used.append("woo_rest")

                    if wp_fields:
                        if target_type == "page":
                            wp_client.update_page(
                                obj_int,
                                title=wp_fields.get("title"),
                                content=wp_fields.get("content"),
                                excerpt=wp_fields.get("excerpt"),
                                slug=wp_fields.get("slug"),
                            )
                        else:
                            wp_client.update_post(
                                obj_int,
                                title=wp_fields.get("title"),
                                content=wp_fields.get("content"),
                                excerpt=wp_fields.get("excerpt"),
                                slug=wp_fields.get("slug"),
                            )
                        after_data.update(wp_fields)
                        applied_fields.update(wp_fields.keys())
                        transports_used.append("wp_rest")

                missing_applied = sorted(set(proposed) - applied_fields)
                if not applied_fields or missing_applied:
                    raise RuntimeError(
                        f"UnsupportedFields: inte alla föreslagna fält skrevs ({', '.join(missing_applied)})"
                    )
            except Exception as exc:
                self.store.compare_and_swap_fix_plan(
                    plan_id, expected_status="applying", new_plan=plan
                )
                log_action(
                    actor=actor_obj.name,
                    action="seo_fix_apply_failed",
                    target=site.id,
                    target_id=plan_id,
                    success=False,
                    details={"error": str(exc)[:200]},
                )
                return _err(type(exc).__name__, f"Kunde inte applicera SEO-fix: {str(exc)[:200]}")

            transport_used = "+".join(transports_used) if transports_used else "bridge"
            vault_category = "media_alt" if is_media else "seo_meta"
            int_tid = int(target_id) if target_id.isdigit() else target_id
            # Ensure rollback metadata fields exist inside before/after snapshots
            if vault_category == "media_alt":
                before.setdefault("media_id", int_tid)
                after_data.setdefault("media_id", int_tid)
            else:
                before.setdefault("object_type", target_type)
                before.setdefault("object_id", int_tid)
                after_data.setdefault("object_type", target_type)
                after_data.setdefault("object_id", int_tid)

            ledger_meta: dict[str, Any] = {
                "plan_id": plan_id,
                "plan_sha256": plan["plan_sha256"],
                "fix_type": fix_type,
                "target_type": target_type,
                "target_id": target_id,
                "market": plan_market,
                "domain": plan_market,
                "live_calls": live_calls,
                "applied_fields": sorted(applied_fields),
            }
            if vault_category == "media_alt":
                ledger_meta["media_id"] = int_tid
            else:
                ledger_meta["object_type"] = target_type
                ledger_meta["object_id"] = int_tid

            change_row = self.ledger.record_change(
                site_id=site.id,
                environment=site.environment,
                category=vault_category,
                transport=transport_used,
                actor=actor_obj.name,
                summary=plan["summary"],
                target_ref=f"{target_type}:{target_id}",
                before_data=before,
                after_data=after_data,
                metadata=ledger_meta,
            )
            change_id = str(change_row["change_id"])
            plan["status"] = "applied"
            plan["executed"] = True
            plan["applied_change_id"] = change_id
            plan["applied_at"] = utc_now_iso()
            self.store.compare_and_swap_fix_plan(
                plan_id, expected_status="applying", new_plan=plan
            )

            log_action(
                actor=actor_obj.name,
                action="seo_fix_applied",
                target=site.id,
                target_id=plan_id,
                success=True,
                details={
                    "change_id": change_id,
                    "category": vault_category,
                    "transport": transport_used,
                    "live_calls": live_calls,
                },
            )
            return {
                "ok": True,
                "plan_id": plan_id,
                "change_id": change_id,
                "executed": True,
                "live_calls": live_calls,
                "transport": transport_used,
                "error_class": "",
                "message": f"SEO-fix applicerad och sparad i Change Vault ({change_id}).",
            }

    # --- 2b. Batch Fix-Plan Workflows (seob-*) — Proposal 1 --------------- #

    def _record_change_with_optional_batch(
        self,
        *,
        batch_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call ChangeLedger.record_change, passing ``batch_id`` kwarg when supported."""
        meta = dict(kwargs.get("metadata") or {})
        if batch_id:
            meta["batch_id"] = batch_id
        kwargs["metadata"] = meta
        if batch_id:
            try:
                sig = inspect.signature(self.ledger.record_change)
                if "batch_id" in sig.parameters:
                    kwargs["batch_id"] = batch_id
            except (TypeError, ValueError):
                pass
        return self.ledger.record_change(**kwargs)

    def propose_batch_from_audit(
        self,
        site_id: str,
        audit_id: str,
        *,
        scope: str = "all",
        limit: int = 20,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Generate a SHA-256-pinned batch fix plan (``seob-*``) from an audit report — never applies."""
        actor_obj = require_seo_propose(actor)
        audit = self.store.get_audit(audit_id)
        if audit is None:
            raise ValueError(f"SEO audit not found: {audit_id!r}")
        if str(audit.get("site_id") or "").lower() != str(site_id or "").strip().lower():
            raise ValueError(
                f"Audit {audit_id!r} belongs to site {audit.get('site_id')!r}, not {site_id!r}"
            )

        mkt = normalize_market(str(audit.get("market") or "no"))
        site = self._resolve_site(site_id, market=mkt)
        _overview, entities, media, _cats, _sources = self._load_entities_and_media(
            site, market=mkt, limit=max(50, int(limit or 20))
        )
        drafted = draft_batch_fix_items(
            audit,
            entities,
            media,
            market=mkt,
            scope=scope,
            limit=limit,
        )

        pinned_items: list[dict[str, Any]] = []
        hash_items: list[dict[str, Any]] = []
        for idx, raw_item in enumerate(drafted, start=1):
            fix_type = str(raw_item["fix_type"]).strip().lower()
            target_type = str(raw_item["target_type"]).strip().lower()
            target_id = str(raw_item["target_id"]).strip()
            proposed = dict(raw_item.get("proposed") or {})
            if not proposed:
                continue
            allowed = _allowed_proposed_fields(fix_type, target_type)
            proposed = {k: v for k, v in proposed.items() if k in allowed}
            if not proposed:
                continue
            try:
                before, before_sha = self._fetch_current_target_state(
                    site,
                    fix_type=fix_type,
                    target_type=target_type,
                    target_id=target_id,
                    market=mkt,
                    proposed=proposed,
                )
            except TargetNotFound:
                continue

            pinned_items.append(
                {
                    "index": idx,
                    "fix_type": fix_type,
                    "target_type": target_type,
                    "target_id": target_id,
                    "url": str(raw_item.get("url") or ""),
                    "summary": str(raw_item.get("summary") or f"{fix_type} {target_type}:{target_id}"),
                    "issue_codes": list(raw_item.get("issue_codes") or []),
                    "before": before,
                    "proposed": proposed,
                    "before_sha256": before_sha,
                    "status": "pending",
                    "change_id": "",
                    "error": "",
                }
            )
            hash_items.append(
                {
                    "fix_type": fix_type,
                    "target_type": target_type,
                    "target_id": target_id,
                    "before_sha256": before_sha,
                    "proposed": proposed,
                }
            )

        if not pinned_items:
            raise ValueError(
                f"Inga åtgärdbara batch-objekt hittades i audit {audit_id!r} för scope={scope!r}."
            )

        now_dt = datetime.now(UTC)
        expires_dt = now_dt + timedelta(hours=FIX_PLAN_TTL_HOURS)
        batch_id = new_seo_id("seob")
        manifest_sha = canonical_sha256(
            {
                "batch_id": batch_id,
                "site_id": site.id,
                "audit_id": audit_id,
                "market": mkt,
                "scope": str(scope or "all").strip().lower(),
                "items": hash_items,
            }
        )
        batch = SeoFixBatch(
            batch_id=batch_id,
            site_id=site.id,
            audit_id=audit_id,
            market=mkt,
            scope=str(scope or "all").strip().lower(),
            summary=f"SEO-batch ({scope}, {len(pinned_items)} objekt) från {audit_id}",
            items=pinned_items,
            item_count=len(pinned_items),
            plan_sha256=manifest_sha,
            status="pending",
            executed=False,
            approval_required=True,
            created_at=now_dt.isoformat(),
            expires_at=expires_dt.isoformat(),
            actor=actor_obj.name,
        )
        batch_dict = batch.to_dict()
        self.store.save_fix_batch(batch_dict)
        log_action(
            actor=actor_obj.name,
            action="seo_batch_proposed",
            target=site.id,
            target_id=batch_id,
            details={
                "audit_id": audit_id,
                "scope": batch.scope,
                "item_count": len(pinned_items),
                "plan_sha256": manifest_sha,
            },
        )
        return {"ok": True, "batch": batch_dict}

    def list_fix_batches(self, site_id: str | None = None, *, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.list_fix_batches(site_id=site_id, limit=limit)

    def get_fix_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.store.get_fix_batch(batch_id)

    def apply_batch(
        self,
        site_id: str,
        batch_id: str,
        *,
        expected_sha256: str,
        actor: str = "oscar",
    ) -> dict[str, Any]:
        """Apply a SHA-256-pinned batch fix plan (Oscar ``WP_OPS_ADMIN`` ONLY) with per-item drift check & shared Change Vault ``batch_id``."""
        def _err(
            error_class: str,
            message: str,
            *,
            batch_obj: dict[str, Any] | None = None,
            applied_count: int = 0,
            failed_count: int = 0,
            change_ids: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "ok": False,
                "batch_id": str(batch_id or ""),
                "batch": batch_obj,
                "applied_count": applied_count,
                "failed_count": failed_count,
                "change_ids": list(change_ids or []),
                "executed": applied_count > 0,
                "error_class": error_class,
                "message": message,
            }

        try:
            actor_obj = require_seo_apply(actor)
        except AccessDenied as exc:
            log_action(
                actor=str(actor or "unknown"),
                action="seo_batch_apply_denied",
                target=site_id,
                target_id=batch_id,
                success=False,
                details={"reason": str(exc)},
            )
            return _err("AccessDenied", str(exc))

        allowed, gate_reason = seo_write_allowed()
        if not allowed:
            err_cls = "SeoKillSwitchActive" if "kill" in gate_reason else "SeoWriteDisabled"
            msg = (
                "SEO-skrivningar är stoppade av kill-switch (AZOM_SEO_KILL / AZOM_WP_DEPLOY_KILL)."
                if "kill" in gate_reason
                else "Live SEO-skrivning är avstängd (kräver AZOM_SEO_WRITE_ENABLED=1 i processmiljön)."
            )
            log_action(
                actor=actor_obj.name,
                action="seo_batch_apply_blocked",
                target=site_id,
                target_id=batch_id,
                success=False,
                details={"reason": gate_reason},
            )
            return _err(err_cls, msg)

        initial = self.store.get_fix_batch(batch_id)
        if initial is None:
            return _err("BatchNotFound", f"SEO-batch '{batch_id}' hittades inte.")
        if str(initial.get("site_id") or "").lower() != str(site_id or "").strip().lower():
            return _err(
                "SiteMismatch",
                f"Batchen tillhör '{initial.get('site_id')}', inte '{site_id}'.",
                batch_obj=initial,
            )

        try:
            site = self._resolve_site(
                str(initial["site_id"]),
                market=str(initial.get("market") or "no"),
            )
        except Exception as exc:
            return _err(type(exc).__name__, str(exc)[:200], batch_obj=initial)

        with hold_site(site.id):
            batch = self.store.get_fix_batch(batch_id)
            if batch is None:
                return _err("BatchNotFound", f"SEO-batch '{batch_id}' hittades inte.")
            if batch.get("status") != "pending" or batch.get("executed"):
                return _err(
                    "BatchAlreadyApplied",
                    f"Batchen har redan status '{batch.get('status')}'.",
                    batch_obj=batch,
                )

            raw_exp = str(batch.get("expires_at") or "").strip()
            if not raw_exp:
                return _err("InvalidBatch", "SEO-batchen saknar utgångsdatum.", batch_obj=batch)
            try:
                exp_dt = datetime.fromisoformat(raw_exp)
            except ValueError:
                return _err("InvalidBatch", "SEO-batchen har ogiltigt utgångsdatum.", batch_obj=batch)
            if exp_dt.tzinfo is None or exp_dt.utcoffset() is None:
                return _err("InvalidBatch", "SEO-batchens utgångsdatum saknar UTC-tidszon.", batch_obj=batch)
            if datetime.now(UTC) > exp_dt.astimezone(UTC):
                expired = {**batch, "status": "expired"}
                self.store.compare_and_swap_fix_batch(
                    batch_id, expected_status="pending", new_batch=expired
                )
                return _err(
                    "BatchExpired",
                    "SEO-batchen har löpt ut (12h TTL). Skapa en ny batch.",
                    batch_obj=expired,
                )

            if not hmac.compare_digest(
                str(expected_sha256 or "").strip(), str(batch.get("plan_sha256") or "")
            ):
                return _err(
                    "PlanHashMismatch",
                    "SHA-256-hash matchar inte batchen — applicering avbruten.",
                    batch_obj=batch,
                )

            claiming = {**batch, "status": "applying"}
            if not self.store.compare_and_swap_fix_batch(
                batch_id, expected_status="pending", new_batch=claiming
            ):
                return _err(
                    "BatchAlreadyApplied",
                    "Batchen har redan påbörjats eller applicerats.",
                    batch_obj=self.store.get_fix_batch(batch_id),
                )

            live_calls = gate_reason == "live"
            bridge = self._get_bridge(site)
            batch_market = str(batch.get("market") or "no")
            applied_count = 0
            failed_count = 0
            change_ids: list[str] = []
            updated_items: list[dict[str, Any]] = []

            for raw_item in list(batch.get("items") or []):
                item = dict(raw_item)
                fix_type = str(item.get("fix_type") or "")
                target_type = str(item.get("target_type") or "")
                target_id = str(item.get("target_id") or "")
                proposed = dict(item.get("proposed") or {})
                before = dict(item.get("before") or {})
                expected_before_sha = str(item.get("before_sha256") or "")
                is_media = fix_type == "media_alt" or target_type == "media"

                try:
                    # Per-item drift detection before mutation
                    _cur_before, cur_sha = self._fetch_current_target_state(
                        site,
                        fix_type=fix_type,
                        target_type=target_type,
                        target_id=target_id,
                        market=batch_market,
                        proposed=proposed,
                    )
                    if expected_before_sha and not hmac.compare_digest(cur_sha, expected_before_sha):
                        raise RuntimeError("DriftDetected: målobjektet ändrades sedan batchen skapades")

                    after_data = {**before, **proposed}
                    transport_used = "bridge"

                    if is_media:
                        media_int = int(target_id)
                        media_fields = {k: v for k, v in proposed.items() if k in _BRIDGE_MEDIA_FIELDS}
                        if not media_fields:
                            raise RuntimeError("UnsupportedFields: inga giltiga mediafält")
                        if bridge is not None and hasattr(bridge, "seo_update_media"):
                            res = bridge.seo_update_media(
                                change_id=f"{batch_id}:{target_id}",
                                media_id=media_int,
                                fields=media_fields,
                                expected_before_sha256=expected_before_sha,
                            )
                            if not isinstance(res, dict) or not res.get("ok", True):
                                raise RuntimeError(str((res or {}).get("error") or "Bridge media update failed"))
                            if res.get("after"):
                                after_data = {**after_data, **dict(res["after"])}
                            if res.get("before"):
                                before = {**before, **dict(res["before"])}
                        else:
                            wp_client = self._get_wp_client(site, market=batch_market)
                            if wp_client is None or not hasattr(wp_client, "update_media"):
                                raise RuntimeError("NoTransportAvailable: saknar media-skrivtransport")
                            wp_client.update_media(
                                media_int,
                                alt_text=media_fields.get("alt_text"),
                                title=media_fields.get("title"),
                                caption=media_fields.get("caption"),
                                description=media_fields.get("description"),
                            )
                            transport_used = "wp_rest"
                    else:
                        obj_int = int(target_id)
                        bridge_fields = {k: v for k, v in proposed.items() if k in _BRIDGE_META_FIELDS}
                        if not bridge_fields:
                            raise RuntimeError("UnsupportedFields: inga giltiga metafält")
                        if bridge is None or not hasattr(bridge, "seo_update_meta"):
                            raise RuntimeError("NoTransportAvailable: saknar Azom Ops Bridge för seo_meta")
                        res = bridge.seo_update_meta(
                            change_id=f"{batch_id}:{target_id}",
                            object_type=target_type,
                            object_id=obj_int,
                            fields=bridge_fields,
                            expected_before_sha256=expected_before_sha,
                        )
                        if not isinstance(res, dict) or not res.get("ok", True):
                            raise RuntimeError(str((res or {}).get("error") or "Bridge meta update failed"))
                        if res.get("after"):
                            after_data = {**after_data, **dict(res["after"])}
                        if res.get("before"):
                            before = {**before, **dict(res["before"])}

                    vault_category = "media_alt" if is_media else "seo_meta"
                    int_tid = int(target_id) if target_id.isdigit() else target_id
                    if vault_category == "media_alt":
                        before.setdefault("media_id", int_tid)
                        after_data.setdefault("media_id", int_tid)
                    else:
                        before.setdefault("object_type", target_type)
                        before.setdefault("object_id", int_tid)
                        after_data.setdefault("object_type", target_type)
                        after_data.setdefault("object_id", int_tid)

                    ledger_meta: dict[str, Any] = {
                        "batch_id": batch_id,
                        "audit_id": str(batch.get("audit_id") or ""),
                        "plan_sha256": str(batch.get("plan_sha256") or ""),
                        "fix_type": fix_type,
                        "target_type": target_type,
                        "target_id": target_id,
                        "market": batch_market,
                        "domain": batch_market,
                        "live_calls": live_calls,
                        "applied_fields": sorted(proposed.keys()),
                    }
                    if vault_category == "media_alt":
                        ledger_meta["media_id"] = int_tid
                    else:
                        ledger_meta["object_type"] = target_type
                        ledger_meta["object_id"] = int_tid

                    chg = self._record_change_with_optional_batch(
                        batch_id=batch_id,
                        site_id=site.id,
                        environment=site.environment,
                        category=vault_category,
                        transport=transport_used,
                        actor=actor_obj.name,
                        summary=str(item.get("summary") or f"Batch {batch_id} {target_type}:{target_id}"),
                        target_ref=f"{target_type}:{target_id}",
                        before_data=before,
                        after_data=after_data,
                        metadata=ledger_meta,
                    )
                    cid = str(chg["change_id"])
                    change_ids.append(cid)
                    applied_count += 1
                    item["status"] = "applied"
                    item["change_id"] = cid
                    item["error"] = ""
                except Exception as exc:
                    failed_count += 1
                    item["status"] = "failed"
                    item["error"] = str(exc)[:200]

                updated_items.append(item)

            final_status = (
                "applied"
                if (applied_count > 0 and failed_count == 0)
                else ("partial" if applied_count > 0 else "failed")
            )
            batch["items"] = updated_items
            batch["status"] = final_status
            batch["executed"] = applied_count > 0
            batch["applied_count"] = applied_count
            batch["failed_count"] = failed_count
            batch["applied_change_ids"] = change_ids
            batch["applied_at"] = utc_now_iso()
            self.store.compare_and_swap_fix_batch(
                batch_id, expected_status="applying", new_batch=batch
            )

            log_action(
                actor=actor_obj.name,
                action="seo_batch_applied",
                target=site.id,
                target_id=batch_id,
                success=failed_count == 0 and applied_count > 0,
                details={
                    "status": final_status,
                    "applied_count": applied_count,
                    "failed_count": failed_count,
                    "change_ids": change_ids,
                    "live_calls": live_calls,
                },
            )
            ok_flag = applied_count > 0 and failed_count == 0
            return {
                "ok": ok_flag,
                "batch_id": batch_id,
                "batch": batch,
                "applied_count": applied_count,
                "failed_count": failed_count,
                "change_ids": change_ids,
                "executed": applied_count > 0,
                "live_calls": live_calls,
                "error_class": "" if ok_flag else ("PartialBatchFailure" if applied_count > 0 else "BatchApplyFailed"),
                "message": (
                    f"Batch {batch_id} applicerad ({applied_count} ändringar i Change Vault)."
                    if ok_flag
                    else f"Batch {batch_id}: {applied_count} applicerade, {failed_count} misslyckades."
                ),
            }

    # --- 2c. GSC Quick-Wins, Deep Crawl, Nordic Parity & Audit Diff ------- #

    def _fetch_gsc_quick_wins_internal(
        self,
        site: Site,
        *,
        market: str = "no",
        entities: list[dict[str, Any]] | None = None,
        days: int = 28,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Lazily load ``ecom_ops.integrations.gsc`` and compute quick-wins (mock-safe)."""
        mock = is_mock_mode()
        status_info: dict[str, Any]
        rows: list[dict[str, Any]] = []

        try:
            from ecom_ops.integrations import gsc as gsc_mod

            status_info = dict(gsc_mod.gsc_status()) if hasattr(gsc_mod, "gsc_status") else {}
            client = gsc_mod.gsc_client_from_env(use_mock=mock)
            resp = client.query_search_analytics(
                site_url=site.canonical_url,
                market=market,
                days=int(days),
                row_limit=100,
            )
            if isinstance(resp, dict) and resp.get("ok"):
                rows = list(resp.get("rows") or [])
                status_info.setdefault("mode", resp.get("mode", "mock" if mock else "live"))
                status_info.setdefault("property_url", resp.get("property_url", site.canonical_url))
                status_info.setdefault("connected", status_info.get("mode") == "live")
            else:
                status_info.setdefault("mode", "mock" if mock else "live")
                status_info.setdefault("connected", False)
                if isinstance(resp, dict) and resp.get("error"):
                    status_info["error"] = str(resp["error"])
        except Exception as exc:
            if mock:
                origin = site.canonical_url.rstrip("/") or "https://mock-shop.example.test"
                status_info = {
                    "connected": False,
                    "mode": "mock",
                    "property_url": f"{origin}/",
                    "note": "Mock GSC-data (fallback)",
                }
                rows = [
                    {
                        "query": "trådløs carplay skjerm",
                        "page": f"{origin}/produkt/carplay-skarm-pro/",
                        "clicks": 38,
                        "impressions": 1450,
                        "ctr": 0.0262,
                        "position": 6.4,
                    },
                    {
                        "query": "trådløs carplay skjerm",
                        "page": f"{origin}/guide/montera-carplay/",
                        "clicks": 11,
                        "impressions": 620,
                        "ctr": 0.0177,
                        "position": 9.8,
                    },
                    {
                        "query": "apple carplay adapter bil",
                        "page": f"{origin}/produkt/carplay-adapter-mini/",
                        "clicks": 14,
                        "impressions": 890,
                        "ctr": 0.0157,
                        "position": 5.2,
                    },
                    {
                        "query": "montere carplay i eldre bil",
                        "page": f"{origin}/guide/montera-carplay/",
                        "clicks": 22,
                        "impressions": 1100,
                        "ctr": 0.02,
                        "position": 8.1,
                    },
                ]
            else:
                status_info = {
                    "connected": False,
                    "mode": "unavailable",
                    "property_url": site.canonical_url,
                    "error": str(exc)[:160],
                }
                rows = []

        analysis = analyze_gsc_quick_wins(rows, entities=entities)
        return status_info, analysis

    def get_gsc_quick_wins(
        self,
        site_id: str,
        *,
        days: int = 28,
        actor: str = "jonatan",
        market: str = "no",
    ) -> dict[str, Any]:
        """Return read-only GSC strike-distance, low-CTR, and query cannibalization opportunities."""
        require_seo_read(actor)
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        _ov, entities, _media, _cats, _src = self._load_entities_and_media(
            site, market=mkt, limit=50
        )
        gsc_status_data, analysis = self._fetch_gsc_quick_wins_internal(
            site, market=mkt, entities=entities, days=days
        )
        return {
            "ok": True,
            "site_id": site.id,
            "days": int(days),
            "gsc_status": gsc_status_data,
            "strike_distance": analysis["strike_distance"],
            "low_ctr": analysis["low_ctr"],
            "cannibalization": analysis["cannibalization"],
            "summary": analysis["summary"],
        }

    def _run_deep_crawl_internal(
        self,
        site: Site,
        *,
        max_pages: int = 10,
        market: str = "no",
        fetcher: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        origin = site.canonical_url.rstrip("/")
        sitemap_urls: list[str] = []
        sm_issues: list[dict[str, Any]] = []
        for candidate in (f"{origin}/sitemap_index.xml", f"{origin}/wp-sitemap.xml"):
            if fetcher is not None:
                sm_res = fetcher(candidate)
            else:
                sm_res = fetch_public_seo_resource(
                    candidate,
                    use_mock=is_mock_mode(),
                    site_id=site.id,
                    market=market,
                )
            if isinstance(sm_res, dict) and sm_res.get("ok") and sm_res.get("text"):
                expanded = expand_sitemap_index(
                    str(sm_res["text"]),
                    max_children=5,
                    fetcher=fetcher,
                    use_mock=is_mock_mode(),
                    site_id=site.id,
                    market=market,
                    sitemap_url=candidate,
                )
                sm_issues.extend(expanded.get("issues") or [])
                if expanded.get("urls"):
                    sitemap_urls = list(expanded["urls"])
                    break

        if not sitemap_urls:
            sitemap_urls = [f"{origin}/"]

        crawl_res = crawl_site_pages(
            sitemap_urls,
            max_pages=max_pages,
            fetcher=fetcher,
            use_mock=is_mock_mode(),
            site_id=site.id,
            market=market,
        )
        all_issues = sm_issues + list(crawl_res.get("issues") or [])
        summary = dict(crawl_res.get("summary") or {})
        summary["sitemap_urls_found"] = len(sitemap_urls)
        summary["total_issues"] = len(all_issues)
        return {
            "ok": True,
            "site_id": site.id,
            "sitemap_urls": sitemap_urls,
            "crawled": crawl_res["crawled"],
            "issues": all_issues,
            "summary": summary,
        }

    def run_deep_crawl(
        self,
        site_id: str,
        *,
        max_pages: int = 10,
        actor: str = "jonatan",
        market: str = "no",
        fetcher: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Expand XML sitemap index and crawl up to ``max_pages`` URLs for status, redirect, and noindex/canonical issues."""
        actor_obj = require_seo_read(actor)
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        res = self._run_deep_crawl_internal(site, max_pages=max_pages, market=mkt, fetcher=fetcher)
        log_action(
            actor=actor_obj.name,
            action="seo_deep_crawl_run",
            target=site.id,
            details={"crawled": len(res["crawled"]), "issues": len(res["issues"])},
        )
        return res

    def _run_nordic_parity_internal(
        self,
        site: Site,
        *,
        market_pages: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pages_map: dict[str, list[dict[str, Any]]]
        if market_pages is not None:
            result = audit_nordic_parity_matrix(market_pages)
            return {"ok": True, "site_id": site.id, **result}

        if is_mock_mode():
            fx_home = mock_technical_fixtures(site_id=site.id, market="no", path="/")
            fx_prod = mock_technical_fixtures(
                site_id=site.id, market="no", path="/produkt/carplay-skarm-pro/"
            )
            pages_map = {
                "se": [
                    {
                        "key": "/",
                        "url": "https://azom.se/",
                        "html": fx_home["nordic_pages"]["https://azom.se/"],
                        "price_currency": "SEK",
                    },
                    {
                        "key": "/produkt/carplay-skarm-pro",
                        "url": "https://azom.se/produkt/carplay-skarm-pro/",
                        "title": "Azom CarPlay Skärm Pro – Köp online",
                        "price_currency": "SEK",
                        "html": fx_prod["nordic_pages"]["https://azom.se/produkt/carplay-skarm-pro/"],
                    },
                ],
                "no": [
                    {
                        "key": "/",
                        "url": "https://azom.no/",
                        "html": fx_home["nordic_pages"]["https://azom.no/"],
                        "price_currency": "NOK",
                    },
                    {
                        "key": "/produkt/carplay-skarm-pro",
                        "url": "https://azom.no/produkt/carplay-skarm-pro/",
                        "title": "Azom CarPlay Skjerm Pro – Rask levering",
                        "text": "Köp trådløs CarPlay med fraktfritt tilbud.",
                        "price_currency": "NOK",
                    },
                ],
                "dk": [
                    {
                        "key": "/",
                        "url": "https://azom.dk/",
                        "html": fx_home["nordic_pages"]["https://azom.dk/"],
                        "price_currency": "DKK",
                    },
                ],
            }
        else:
            # Live mode: fetch homepage + hreflang alternates across configured/linked Nordic markets
            pages_map = {}
            home_url = f"{site.canonical_url.rstrip('/')}/"
            home_res = fetch_public_seo_resource(home_url, use_mock=False, site_id=site.id)
            if home_res.get("ok"):
                parsed = analyze_html_seo(
                    home_res["text"],
                    url=home_res.get("final_url") or home_url,
                    response_headers=home_res.get("headers"),
                )
                lang_low = str(parsed.get("html_lang") or "").lower()
                primary_mkt = "se" if lang_low.startswith("sv") else ("dk" if lang_low.startswith("da") else "no")
                pages_map.setdefault(primary_mkt, []).append(
                    {"key": "/", "url": home_res.get("final_url") or home_url, "html": home_res["text"]}
                )
                for hl in (parsed.get("hreflang") or [])[:5]:
                    hl_code = str(hl.get("hreflang") or "").lower()
                    hl_url = str(hl.get("href") or "").strip()
                    if not hl_url.startswith("https://") or hl_code == "x-default":
                        continue
                    target_mkt = "se" if hl_code.startswith("sv") else ("dk" if hl_code.startswith("da") else ("no" if hl_code.startswith(("nb", "nn", "no")) else ""))
                    if not target_mkt or target_mkt in pages_map:
                        continue
                    alt_res = fetch_public_seo_resource(hl_url, use_mock=False, site_id=site.id, market=target_mkt)
                    if alt_res.get("ok"):
                        pages_map.setdefault(target_mkt, []).append(
                            {"key": "/", "url": alt_res.get("final_url") or hl_url, "html": alt_res["text"]}
                        )

        result = audit_nordic_parity_matrix(pages_map)
        return {"ok": True, "site_id": site.id, **result}

    def run_nordic_parity(
        self,
        site_id: str,
        *,
        actor: str = "jonatan",
        market_pages: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Audit SE/NO/DK market parity (translations, Swedish leaks, currency, hreflang)."""
        actor_obj = require_seo_read(actor)
        site = self._resolve_site(site_id)
        res = self._run_nordic_parity_internal(site, market_pages=market_pages)
        log_action(
            actor=actor_obj.name,
            action="seo_nordic_parity_run",
            target=site.id,
            details={"markets": res["markets"], "issues": len(res["issues"])},
        )
        return res

    @staticmethod
    def _issue_fingerprint(issue: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(issue.get("code") or ""),
            str(issue.get("target_type") or ""),
            str(issue.get("target_id") or ""),
            str(issue.get("url") or "").rstrip("/"),
        )

    def diff_audits(
        self,
        site_id: str,
        *,
        latest_audit_id: str | None = None,
        previous_audit_id: str | None = None,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Compare two persisted audits for a site and report score delta, new issues, and resolved issues."""
        require_seo_read(actor)
        clean_sid = str(site_id or "").strip().lower()

        latest: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None

        if latest_audit_id:
            latest = self.store.get_audit(latest_audit_id)
        if previous_audit_id:
            previous = self.store.get_audit(previous_audit_id)

        if latest is None or previous is None:
            history = self.store.list_audits(site_id=clean_sid, limit=30)
            if latest is None:
                site_scoped = [a for a in history if a.get("scope") == "site"]
                latest = site_scoped[0] if site_scoped else (history[0] if history else None)
            if latest is not None and previous is None:
                lid = latest.get("audit_id")
                lscope = latest.get("scope")
                same_scope = [
                    a for a in history if a.get("audit_id") != lid and a.get("scope") == lscope
                ]
                previous = same_scope[0] if same_scope else None

        if latest is None:
            return {
                "ok": True,
                "has_previous": False,
                "latest_id": "",
                "previous_id": None,
                "score_delta": 0,
                "new_issues": [],
                "resolved_issues": [],
                "summary": {
                    "latest_score": None,
                    "previous_score": None,
                    "score_delta": 0,
                    "new_issues_count": 0,
                    "resolved_issues_count": 0,
                },
            }

        if previous is None:
            return {
                "ok": True,
                "has_previous": False,
                "latest_id": str(latest["audit_id"]),
                "previous_id": None,
                "score_delta": 0,
                "new_issues": [],
                "resolved_issues": [],
                "summary": {
                    "latest_score": int(latest.get("score") or 0),
                    "latest_grade": str(latest.get("grade") or ""),
                    "previous_score": None,
                    "score_delta": 0,
                    "new_issues_count": 0,
                    "resolved_issues_count": 0,
                },
            }

        latest_map = {
            self._issue_fingerprint(iss): iss
            for iss in (latest.get("issues") or [])
            if isinstance(iss, dict)
        }
        prev_map = {
            self._issue_fingerprint(iss): iss
            for iss in (previous.get("issues") or [])
            if isinstance(iss, dict)
        }
        new_issues = [iss for fp, iss in latest_map.items() if fp not in prev_map]
        resolved_issues = [iss for fp, iss in prev_map.items() if fp not in latest_map]
        score_delta = int(latest.get("score") or 0) - int(previous.get("score") or 0)

        return {
            "ok": True,
            "has_previous": True,
            "latest_id": str(latest["audit_id"]),
            "previous_id": str(previous["audit_id"]),
            "score_delta": score_delta,
            "new_issues": new_issues,
            "resolved_issues": resolved_issues,
            "summary": {
                "latest_score": int(latest.get("score") or 0),
                "latest_grade": str(latest.get("grade") or ""),
                "previous_score": int(previous.get("score") or 0),
                "previous_grade": str(previous.get("grade") or ""),
                "score_delta": score_delta,
                "new_issues_count": len(new_issues),
                "resolved_issues_count": len(resolved_issues),
            },
        }

    # --- 3. Strategies ---------------------------------------------------- #

    def create_strategy(
        self,
        *,
        site_id: str,
        actor: str,
        title: str,
        market: str = "no",
        focus_topics: list[str] | None = None,
        goals: str = "",
        research_brief_id: str | None = None,
        serp_research: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_obj = require_seo_propose(actor)
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        recent_audits = self.store.list_audits(site_id=site.id, limit=1)
        latest_audit = recent_audits[0] if recent_audits else None
        gsc_qw = (latest_audit or {}).get("gsc_quick_wins") or None
        if not gsc_qw:
            _, gsc_qw = self._fetch_gsc_quick_wins_internal(site, market=mkt, days=28)

        strategy = build_strategy(
            site_id=site.id,
            actor=actor_obj.name,
            title=title,
            market=mkt,
            focus_topics=focus_topics,
            goals=goals,
            latest_audit=latest_audit,
            research_brief_id=research_brief_id,
            data_dir=self.data_dir,
            gsc_quick_wins=gsc_qw,
            serp_research=serp_research,
        )
        self.store.save_strategy(strategy)
        log_action(
            actor=actor_obj.name,
            action="seo_strategy_created",
            target=site.id,
            target_id=strategy["strategy_id"],
            details={"market": mkt, "title": strategy["title"]},
        )
        return {"ok": True, "strategy": strategy}

    def list_strategies(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_strategies(site_id=site_id, limit=limit)

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        return self.store.get_strategy(strategy_id)

    # --- 4. Campaigns ----------------------------------------------------- #

    def create_campaign(
        self,
        *,
        site_id: str,
        actor: str,
        name: str,
        market: str = "no",
        strategy_id: str | None = None,
        target_keywords: list[str] | None = None,
        target_urls: list[str] | None = None,
        campaign_type: str = "commercial",
        objective: str = "",
        research_brief_id: str | None = None,
        serp_research: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_obj = require_seo_propose(actor)
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        recent_audits = self.store.list_audits(site_id=site.id, limit=1)
        gsc_qw = ((recent_audits[0] if recent_audits else None) or {}).get("gsc_quick_wins") or None
        if not gsc_qw:
            _, gsc_qw = self._fetch_gsc_quick_wins_internal(site, market=mkt, days=28)
        campaign = build_campaign(
            site_id=site.id,
            actor=actor_obj.name,
            name=name,
            market=mkt,
            strategy_id=strategy_id,
            target_keywords=target_keywords,
            target_urls=target_urls,
            campaign_type=campaign_type,
            objective=objective,
            research_brief_id=research_brief_id,
            data_dir=self.data_dir,
            gsc_quick_wins=gsc_qw,
            serp_research=serp_research,
        )
        self.store.save_campaign(campaign)
        log_action(
            actor=actor_obj.name,
            action="seo_campaign_created",
            target=site.id,
            target_id=campaign["campaign_id"],
            details={"market": mkt, "name": campaign["name"], "strategy_id": strategy_id},
        )
        return {"ok": True, "campaign": campaign}

    def list_campaigns(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_campaigns(site_id=site_id, limit=limit)

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        return self.store.get_campaign(campaign_id)

    # --- 5. Link Packages (Länkpaket) ------------------------------------- #

    def _compute_cumulative_anchor_mix_for_site(
        self,
        site_id: str,
        *,
        current_targets: list[dict[str, Any]] | None = None,
        exclude_package_id: str | None = None,
        market: str = "no",
        base_url: str = "",
    ) -> dict[str, Any]:
        all_pkgs = self.store.list_link_packages(site_id=site_id, limit=200)
        hist = [
            p
            for p in all_pkgs
            if p.get("package_id") != exclude_package_id
        ]
        return evaluate_cumulative_anchor_mix(
            current_targets or [],
            hist,
            base_url=base_url,
            market=market,
        )

    def evaluate_site_cumulative_anchor_mix(
        self,
        site_id: str,
        *,
        include_package_id: str | None = None,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Evaluate cumulative anchor-text profile across all ordered/delivered packages (+ optional draft)."""
        require_seo_read(actor)
        site = self._resolve_site(site_id)
        cur_targets: list[dict[str, Any]] = []
        mkt = "no"
        if include_package_id:
            inc = self.store.get_link_package(include_package_id)
            if inc is not None:
                cur_targets = list(inc.get("targets") or [])
                mkt = str(inc.get("market") or "no")
        return self._compute_cumulative_anchor_mix_for_site(
            site.id,
            current_targets=cur_targets,
            exclude_package_id=include_package_id,
            market=mkt,
            base_url=site.canonical_url,
        )

    def create_link_package(
        self,
        *,
        site_id: str,
        actor: str,
        name: str,
        market: str = "no",
        campaign_id: str | None = None,
        budget_sek: float = 15000.0,
        link_count: int = 6,
        targets: list[dict[str, Any]] | None = None,
        notes: str = "",
        recommended_vendors: list[dict[str, Any]] | None = None,
        selected_vendor_id: str | None = None,
        vendor_vetting_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_obj = require_link_package_draft(actor)
        budget, count = validate_link_package_inputs(budget_sek, link_count)
        mkt = normalize_market(market)
        site = self._resolve_site(site_id, market=mkt)
        pkg = build_link_package(
            site_id=site.id,
            actor=actor_obj.name,
            name=name,
            market=mkt,
            campaign_id=campaign_id,
            budget_sek=budget,
            link_count=count,
            targets=targets,
            notes=notes,
            base_url=site.canonical_url,
            recommended_vendors=recommended_vendors,
            selected_vendor_id=selected_vendor_id,
            vendor_vetting_summary=vendor_vetting_summary,
        )
        cum_mix = self._compute_cumulative_anchor_mix_for_site(
            site.id,
            current_targets=list(pkg.get("targets") or []),
            exclude_package_id=pkg["package_id"],
            market=mkt,
            base_url=site.canonical_url,
        )
        pkg["cumulative_anchor_mix"] = cum_mix
        self.store.save_link_package(pkg)
        log_action(
            actor=actor_obj.name,
            action="seo_link_package_created",
            target=site.id,
            target_id=pkg["package_id"],
            details={
                "market": mkt,
                "budget_sek": pkg["budget_sek"],
                "link_count": pkg["link_count"],
                "guardrail_blocked": pkg["guardrail_blocked"],
            },
        )
        return {"ok": True, "package": pkg, "cumulative_anchor_mix": cum_mix}

    def list_link_packages(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_link_packages(site_id=site_id, limit=limit)

    def get_link_package(self, package_id: str) -> dict[str, Any] | None:
        pkg = self.store.get_link_package(package_id)
        if pkg is None:
            return None
        sid = str(pkg.get("site_id") or "")
        if sid:
            pkg["cumulative_anchor_mix"] = self._compute_cumulative_anchor_mix_for_site(
                sid,
                current_targets=list(pkg.get("targets") or []),
                exclude_package_id=package_id,
                market=str(pkg.get("market") or "no"),
            )
        return pkg

    def update_link_package_draft(
        self,
        package_id: str,
        *,
        name: str | None = None,
        budget_sek: float | None = None,
        targets: list[dict[str, Any]] | None = None,
        notes: str | None = None,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Edit a link-package draft (only allowed when ``status == 'draft'``) and re-evaluate anchor mixes."""
        try:
            actor_obj = require_link_package_draft(actor)
        except AccessDenied as exc:
            return {
                "ok": False,
                "link_package": None,
                "cumulative_anchor_mix": None,
                "error_class": "AccessDenied",
                "message": str(exc),
            }

        initial_pkg = self.store.get_link_package(package_id)
        if initial_pkg is None:
            return {
                "ok": False,
                "link_package": None,
                "cumulative_anchor_mix": None,
                "error_class": "PackageNotFound",
                "message": f"Länkpaket '{package_id}' hittades inte.",
            }

        lock_site_id = str(initial_pkg.get("site_id") or "default").strip().lower()
        with hold_site(lock_site_id):
            pkg = self.store.get_link_package(package_id)
            if pkg is None:
                return {
                    "ok": False,
                    "link_package": None,
                    "cumulative_anchor_mix": None,
                    "error_class": "PackageNotFound",
                    "message": f"Länkpaket '{package_id}' hittades inte.",
                }
            if str(pkg.get("status") or "").lower() != "draft":
                return {
                    "ok": False,
                    "link_package": pkg,
                    "cumulative_anchor_mix": pkg.get("cumulative_anchor_mix"),
                    "error_class": "InvalidStatus",
                    "message": f"Endast utkast (draft) kan redigeras — nuvarande status är '{pkg.get('status')}'.",
                }

            mkt = normalize_market(str(pkg.get("market") or "no"))
            new_name = " ".join(str(name).strip().split()) if name is not None and str(name).strip() else str(pkg.get("name") or "")
            new_notes = str(notes).strip() if notes is not None else str(pkg.get("notes") or "")
            raw_targets = targets if targets is not None else list(pkg.get("targets") or [])
            new_budget_raw = budget_sek if budget_sek is not None else float(pkg.get("budget_sek") or 15000.0)
            new_budget, new_count = validate_link_package_inputs(new_budget_raw, len(raw_targets))

            avg_cost = round(new_budget / new_count, 2)
            total_weeks = max(4, min(12, math.ceil(new_count / 1.5) * 2))
            existing_targets = list(pkg.get("targets") or [])
            normalized_targets: list[dict[str, Any]] = []
            drip_buckets: dict[int, list[int]] = {}
            for idx, row in enumerate(raw_targets, start=1):
                if not isinstance(row, dict):
                    continue
                prev = existing_targets[idx - 1] if idx - 1 < len(existing_targets) else {}
                t_url = str(row.get("target_url") or row.get("url") or "").strip()
                t_anchor = str(row.get("anchor_text") or row.get("anchor") or "").strip()
                t_kw = str(row.get("keyword") or row.get("primary_keyword") or prev.get("keyword") or "").strip()
                t_type = str(row.get("anchor_type") or "").strip().lower() or classify_anchor(
                    t_anchor, target_url=t_url, keyword=t_kw, market=mkt
                )
                week = int(
                    row.get("target_week")
                    or (1 if new_count <= 1 else 1 + math.floor(((idx - 1) / (new_count - 1)) * (total_weeks - 1)))
                )
                week = max(1, min(total_weeks, week))
                drip_buckets.setdefault(week, []).append(idx)
                item = {
                    **prev,
                    **row,
                    "index": idx,
                    "target_url": t_url,
                    "anchor_text": t_anchor,
                    "anchor_type": t_type,
                    "keyword": t_kw,
                    "target_week": week,
                    "estimated_cost_sek": avg_cost,
                }
                item.setdefault("article_angle", str(prev.get("article_angle") or "Redaktionell nordisk köpguide"))
                item.setdefault("min_dr", int(prev.get("min_dr") or 30))
                item.setdefault("language", str(prev.get("language") or {"no": "nb-NO", "se": "sv-SE", "dk": "da-DK"}[mkt]))
                item.setdefault("tld_preference", str(prev.get("tld_preference") or f".{mkt}"))
                item.setdefault("rel_guidance", str(prev.get("rel_guidance") or "dofollow"))
                item.setdefault("placement_type", str(prev.get("placement_type") or "editorial_guest_article"))
                normalized_targets.append(item)

            mix_report = evaluate_anchor_mix(
                normalized_targets,
                market=mkt,
                link_count=len(normalized_targets),
                notes=new_notes,
                name=new_name,
            )
            cum_mix = self._compute_cumulative_anchor_mix_for_site(
                lock_site_id,
                current_targets=normalized_targets,
                exclude_package_id=package_id,
                market=mkt,
            )

            now = utc_now_iso()
            pkg["name"] = new_name
            pkg["budget_sek"] = round(new_budget, 2)
            pkg["link_count"] = len(normalized_targets)
            pkg["targets"] = normalized_targets
            pkg["notes"] = new_notes
            pkg["anchor_mix"] = mix_report.to_dict()
            pkg["cumulative_anchor_mix"] = cum_mix
            pkg["guardrail_warnings"] = list(dict.fromkeys([*mix_report.warnings, *cum_mix.get("warnings", [])]))
            pkg["blocking_reasons"] = list(dict.fromkeys([*mix_report.blocking_reasons, *cum_mix.get("blocking_reasons", [])]))
            pkg["guardrail_blocked"] = bool(mix_report.guardrail_blocked or cum_mix.get("guardrail_blocked"))
            pkg["drip_plan"] = [
                {
                    "week": wk,
                    "links_scheduled": len(idxs),
                    "placement_indices": idxs,
                    "velocity_note": "Jämn, naturlig publiceringstakt utan länkspikar.",
                }
                for wk, idxs in sorted(drip_buckets.items())
            ]
            pkg["cost_breakdown"] = {
                "total_budget_sek": round(new_budget, 2),
                "link_count": len(normalized_targets),
                "avg_cost_per_link_sek": avg_cost,
                "currency": "SEK",
                "note": "Endast estimat och beställningsunderlag — inga automatiska köp eller betalningar utförs.",
            }
            pkg["updated_at"] = now
            history = list(pkg.get("history") or [])
            history.append(
                {
                    "from_status": "draft",
                    "to_status": "draft",
                    "actor": actor_obj.name,
                    "note": "Utkast uppdaterat i SEO Studio",
                    "at": now,
                }
            )
            pkg["history"] = history
            _fn, _ct, md_body = render_link_package_brief(pkg, fmt="md")
            pkg["brief_markdown"] = md_body

            if not self.store.compare_and_swap_link_package(
                package_id, expected_status="draft", new_pkg=pkg
            ):
                return {
                    "ok": False,
                    "link_package": self.store.get_link_package(package_id),
                    "cumulative_anchor_mix": cum_mix,
                    "error_class": "ConcurrentModification",
                    "message": "Länkpaketet ändrades samtidigt av en annan begäran.",
                }

            log_action(
                actor=actor_obj.name,
                action="seo_link_package_draft_updated",
                target=lock_site_id,
                target_id=package_id,
                success=True,
                details={"link_count": pkg["link_count"], "guardrail_blocked": pkg["guardrail_blocked"]},
            )
            return {
                "ok": True,
                "link_package": pkg,
                "package": pkg,
                "cumulative_anchor_mix": cum_mix,
            }

    def record_link_delivery(
        self,
        package_id: str,
        *,
        target_index: int,
        published_url: str,
        published_at: str = "",
        actor: str = "oscar",
    ) -> dict[str, Any]:
        """Record a live published URL for a placement target (Oscar ``MARKETING_MUTATE`` ONLY)."""
        try:
            actor_obj = require_link_package_approve(actor)
        except AccessDenied as exc:
            return {
                "ok": False,
                "link_package": None,
                "error_class": "AccessDenied",
                "message": str(exc),
            }

        clean_url = str(published_url or "").strip()
        if not clean_url.startswith(("https://", "http://")):
            return {
                "ok": False,
                "link_package": None,
                "error_class": "InvalidUrl",
                "message": "published_url måste börja med https:// eller http://.",
            }

        initial = self.store.get_link_package(package_id)
        if initial is None:
            return {
                "ok": False,
                "link_package": None,
                "error_class": "PackageNotFound",
                "message": f"Länkpaket '{package_id}' hittades inte.",
            }

        lock_site_id = str(initial.get("site_id") or "default").strip().lower()
        with hold_site(lock_site_id):
            pkg = self.store.get_link_package(package_id)
            if pkg is None:
                return {
                    "ok": False,
                    "link_package": None,
                    "error_class": "PackageNotFound",
                    "message": f"Länkpaket '{package_id}' hittades inte.",
                }
            cur_status = str(pkg.get("status") or "draft").lower()
            idx_int = int(target_index)
            targets = list(pkg.get("targets") or [])
            matched_t: dict[str, Any] | None = None
            for t in targets:
                if int(t.get("index") or 0) == idx_int:
                    matched_t = t
                    break
            if matched_t is None:
                return {
                    "ok": False,
                    "link_package": pkg,
                    "error_class": "TargetNotFound",
                    "message": f"Placering #{idx_int} hittades inte i länkpaket '{package_id}'.",
                }

            now = utc_now_iso()
            matched_t["published_url"] = clean_url
            matched_t["published_at"] = str(published_at or "").strip() or now
            pkg["targets"] = targets
            pkg["updated_at"] = now
            history = list(pkg.get("history") or [])
            history.append(
                {
                    "from_status": cur_status,
                    "to_status": cur_status,
                    "actor": actor_obj.name,
                    "note": f"Leverans registrerad för placering #{idx_int}: {clean_url}",
                    "at": now,
                }
            )
            pkg["history"] = history
            self.store.compare_and_swap_link_package(
                package_id, expected_status=cur_status, new_pkg=pkg
            )
            log_action(
                actor=actor_obj.name,
                action="seo_link_delivery_recorded",
                target=lock_site_id,
                target_id=package_id,
                success=True,
                details={"target_index": idx_int, "published_url": clean_url},
            )
            return {"ok": True, "link_package": pkg, "package": pkg}

    def verify_link_package(
        self,
        package_id: str,
        *,
        actor: str = "jonatan",
        fetcher: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Verify all targets in a link package that have a recorded ``published_url`` and persist report."""
        actor_obj = require_seo_read(actor)
        pkg = self.store.get_link_package(package_id)
        if pkg is None:
            return {
                "ok": False,
                "package_id": package_id,
                "verified_count": 0,
                "issue_count": 0,
                "results": [],
                "error_class": "PackageNotFound",
                "message": f"Länkpaket '{package_id}' hittades inte.",
            }

        results: list[dict[str, Any]] = []
        verified_count = 0
        issue_count = 0
        targets = list(pkg.get("targets") or [])

        for t in targets:
            pub_url = str(t.get("published_url") or "").strip()
            if not pub_url:
                continue
            v = verify_backlink_placement(
                pub_url,
                expected_target_url=str(t.get("target_url") or ""),
                expected_anchor=str(t.get("anchor_text") or ""),
                fetcher=fetcher,
                use_mock=is_mock_mode(),
            )
            entry = {"target_index": int(t.get("index") or 0), **v}
            t["verification"] = entry
            results.append(entry)
            if v.get("verified"):
                verified_count += 1
            if v.get("issues"):
                issue_count += len(v["issues"])

        now = utc_now_iso()
        pkg["targets"] = targets
        pkg["verification"] = {
            "checked_at": now,
            "checked_by": actor_obj.name,
            "checked_count": len(results),
            "verified_count": verified_count,
            "issue_count": issue_count,
            "results": results,
        }
        pkg["updated_at"] = now
        self.store.save_link_package(pkg)
        log_action(
            actor=actor_obj.name,
            action="seo_link_package_verified",
            target=str(pkg.get("site_id") or ""),
            target_id=package_id,
            success=True,
            details={"checked": len(results), "verified": verified_count, "issues": issue_count},
        )
        return {
            "ok": True,
            "package_id": package_id,
            "verified_count": verified_count,
            "issue_count": issue_count,
            "results": results,
        }

    def transition_link_package(
        self,
        *,
        package_id: str,
        actor: str,
        new_status: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Advance a link-package order brief through the strict HITL approval state machine."""
        target_status = str(new_status or "").strip().lower()
        if target_status not in LINK_PACKAGE_STATUSES:
            return {
                "ok": False,
                "package": None,
                "error_class": "InvalidStatus",
                "message": f"Ogiltig status '{new_status}'. Tillåtna: {', '.join(LINK_PACKAGE_STATUSES)}.",
            }

        try:
            if target_status in _OSCAR_ONLY_LINK_STATUSES:
                actor_obj = require_link_package_approve(actor)
            else:
                actor_obj = require_link_package_draft(actor)
        except AccessDenied as exc:
            log_action(
                actor=str(actor or "unknown"),
                action="seo_link_package_transition_denied",
                target="seo_link_package",
                target_id=package_id,
                success=False,
                details={"new_status": target_status, "reason": str(exc)},
            )
            return {
                "ok": False,
                "package": None,
                "error_class": "AccessDenied",
                "message": str(exc),
            }

        initial_pkg = self.store.get_link_package(package_id)
        if initial_pkg is None:
            return {
                "ok": False,
                "package": None,
                "error_class": "PackageNotFound",
                "message": f"Länkpaket '{package_id}' hittades inte.",
            }

        lock_site_id = str(initial_pkg.get("site_id") or "default").strip().lower()
        with hold_site(lock_site_id):
            pkg = self.store.get_link_package(package_id)
            if pkg is None:
                return {
                    "ok": False,
                    "package": None,
                    "error_class": "PackageNotFound",
                    "message": f"Länkpaket '{package_id}' hittades inte.",
                }

            cur_status = str(pkg.get("status") or "draft").lower()
            allowed_next = _ALLOWED_LINK_TRANSITIONS.get(cur_status, frozenset())
            if target_status not in allowed_next:
                return {
                    "ok": False,
                    "package": pkg,
                    "error_class": "InvalidTransition",
                    "message": f"Kan inte byta status från '{cur_status}' till '{target_status}'.",
                }

            if target_status in {"ready_for_review", "approved_for_manual_order"}:
                fresh_mix = evaluate_anchor_mix(
                    list(pkg.get("targets") or []),
                    market=str(pkg.get("market") or "no"),
                    link_count=len(pkg.get("targets") or []),
                    notes=str(pkg.get("notes") or ""),
                    name=str(pkg.get("name") or ""),
                )
                pkg["anchor_mix"] = fresh_mix.to_dict()
                pkg["guardrail_warnings"] = list(fresh_mix.warnings)
                pkg["guardrail_blocked"] = bool(fresh_mix.guardrail_blocked)
                pkg["blocking_reasons"] = list(fresh_mix.blocking_reasons)
                if fresh_mix.guardrail_blocked:
                    reasons = "; ".join(
                        pkg.get("blocking_reasons")
                        or ["Ankartext-mix eller anti-spam-guardrail bruten."]
                    )
                    log_action(
                        actor=actor_obj.name,
                        action="seo_link_package_guardrail_blocked",
                        target=pkg["site_id"],
                        target_id=package_id,
                        success=False,
                        details={"new_status": target_status, "reasons": reasons},
                    )
                    return {
                        "ok": False,
                        "package": pkg,
                        "error_class": "GuardrailBlocked",
                        "message": f"Länkpaketet är blockerat av anti-spam/ankartext-guardrail: {reasons}",
                    }

            now = utc_now_iso()
            pkg["status"] = target_status
            pkg["updated_at"] = now
            history = list(pkg.get("history") or [])
            history.append(
                {
                    "from_status": cur_status,
                    "to_status": target_status,
                    "actor": actor_obj.name,
                    "note": str(note or "").strip(),
                    "at": now,
                }
            )
            pkg["history"] = history
            _fn, _ct, md_body = render_link_package_brief(pkg, fmt="md")
            pkg["brief_markdown"] = md_body
            if not self.store.compare_and_swap_link_package(
                package_id, expected_status=cur_status, new_pkg=pkg
            ):
                return {
                    "ok": False,
                    "package": self.store.get_link_package(package_id),
                    "error_class": "ConcurrentModification",
                    "message": "Länkpaketet ändrades samtidigt av en annan begäran — försök igen.",
                }

            log_action(
                actor=actor_obj.name,
                action="seo_link_package_transition",
                target=pkg["site_id"],
                target_id=package_id,
                success=True,
                details={
                    "from_status": cur_status,
                    "to_status": target_status,
                    "note": str(note or "")[:160],
                },
            )
            return {
                "ok": True,
                "package": pkg,
                "error_class": "",
                "message": f"Status uppdaterad: {cur_status} -> {target_status}.",
            }

    def export_link_package(
        self,
        package_id: str,
        *,
        fmt: str = "md",
        actor: str | None = None,
    ) -> tuple[str, str, str]:
        """Export a link-package supplier brief as ``(filename, content_type, body)``."""
        if actor is not None:
            require_seo_read(actor)
        pkg = self.store.get_link_package(package_id)
        if pkg is None:
            raise KeyError(f"Link package not found: {package_id}")
        return render_link_package_brief(pkg, fmt=fmt)

    # --- 6. Vetted Nordic Link-Shop Selector & Autonomous SEO Autopilot --- #

    def vet_link_vendors(
        self,
        *,
        market: str = "no",
        budget_sek: float = 15000.0,
        link_count: int = 6,
        goal: str = "organic_growth",
        include_web_research: bool = True,
        actor: str = "jonatan",
        brand_url_generic_pct: float = 70.0,
        anchor_guardrail_blocked: bool = False,
    ) -> dict[str, Any]:
        """Vet and rank top 2–3 white-hat Nordic SEO link shops (read-only; zero purchases)."""
        actor_obj = require_seo_read(actor)
        budget, count = validate_link_package_inputs(budget_sek, link_count)
        mkt = normalize_market(market)
        res = vet_link_vendors_via_web(
            mkt,
            actor=actor_obj.name,
            budget_sek=budget,
            link_count=count,
            goal=goal,
            brand_url_generic_pct=brand_url_generic_pct,
            anchor_guardrail_blocked=anchor_guardrail_blocked,
            include_web_research=include_web_research,
            data_dir=self.data_dir,
        )
        log_action(
            actor=actor_obj.name,
            action="seo_link_vendors_vetted",
            target=mkt,
            details={
                "budget_sek": budget,
                "link_count": count,
                "selected_vendor_id": res.get("selected_vendor_id"),
                "recommended_tier": res.get("recommended_tier"),
                "web_vetting_used": res.get("web_vetting_used"),
            },
        )
        return res

    def select_link_package_vendor(
        self,
        package_id: str,
        *,
        vendor_id: str,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Select a vetted white-hat link shop on a ``draft`` link package (refuses non-draft & rejected shops)."""
        try:
            actor_obj = require_link_package_draft(actor)
        except AccessDenied as exc:
            return {
                "ok": False,
                "link_package": None,
                "error_class": "AccessDenied",
                "message": str(exc),
            }

        initial_pkg = self.store.get_link_package(package_id)
        if initial_pkg is None:
            return {
                "ok": False,
                "link_package": None,
                "error_class": "PackageNotFound",
                "message": f"Länkpaket '{package_id}' hittades inte.",
            }

        lock_site_id = str(initial_pkg.get("site_id") or "default").strip().lower()
        with hold_site(lock_site_id):
            pkg = self.store.get_link_package(package_id)
            if pkg is None:
                return {
                    "ok": False,
                    "link_package": None,
                    "error_class": "PackageNotFound",
                    "message": f"Länkpaket '{package_id}' hittades inte.",
                }
            if str(pkg.get("status") or "").lower() != "draft":
                return {
                    "ok": False,
                    "link_package": pkg,
                    "error_class": "InvalidStatus",
                    "message": (
                        f"Leverantör kan endast ändras i status 'draft' — nuvarande status är '{pkg.get('status')}'."
                    ),
                }

            mkt = normalize_market(str(pkg.get("market") or "no"))
            clean_vid = str(vendor_id or "").strip().lower()
            curated = get_vendor(clean_vid)
            existing_recs = [v for v in (pkg.get("recommended_vendors") or []) if isinstance(v, dict)]
            rec_match = next(
                (v for v in existing_recs if str(v.get("vendor_id") or "").strip().lower() == clean_vid),
                None,
            )
            if curated is None and rec_match is None:
                return {
                    "ok": False,
                    "link_package": pkg,
                    "error_class": "VendorNotFound",
                    "message": f"Okänd eller ej granskad länkleverantör: '{vendor_id}'.",
                }

            vet_target = curated if curated is not None else rec_match
            vet = vet_vendor_candidate(vet_target, market=mkt)
            if vet["hard_reject"] or not vet["eligible"]:
                return {
                    "ok": False,
                    "link_package": pkg,
                    "error_class": "VendorRejected",
                    "message": (
                        f"Leverantören '{vendor_id}' är inte godkänd av anti-PBN-granskningen: "
                        f"{'; '.join(vet.get('hard_reject_reasons') or ['poäng < 70'])}"
                    ),
                }

            resolved_vid = str(vet["vendor_id"])
            if not any(str(v.get("vendor_id") or "").lower() == resolved_vid for v in existing_recs):
                # Populate recommended_vendors with matched candidates including this vetted shop
                matched = vet_link_vendors_via_web(
                    mkt,
                    actor=actor_obj.name,
                    budget_sek=float(pkg.get("budget_sek") or 15000.0),
                    link_count=int(pkg.get("link_count") or 6),
                    include_web_research=False,
                    data_dir=self.data_dir,
                )
                cands = list(matched.get("candidates") or [])
                if not any(str(v.get("vendor_id") or "").lower() == resolved_vid for v in cands) and curated is not None:
                    cands.append(
                        {
                            "vendor_id": curated.vendor_id,
                            "name": curated.name,
                            "domain": curated.domain,
                            "url": curated.url,
                            "business_model": curated.business_model,
                            "rank": len(cands) + 1,
                            "rank_score": float(vet["vetting_score"]),
                            "fit_score": float(vet["vetting_score"]),
                            "vetting_score": vet["vetting_score"],
                            "vetting_status": vet["vetting_status"],
                            "estimated_unit_sek_range": list(curated.typical_price_per_link_sek),
                            "guarantee_months": curated.guarantee_months,
                            "order_workflow_notes": curated.order_workflow_notes,
                            "provenance": "curated_catalog",
                        }
                    )
                pkg["recommended_vendors"] = cands
                pkg["vendor_vetting_summary"] = {
                    **(pkg.get("vendor_vetting_summary") or {}),
                    "recommended_tier": matched.get("recommended_tier", ""),
                    "disclosure_checklist": list(
                        matched.get("disclosure_checklist") or NORDIC_DISCLOSURE_CHECKLIST[mkt]
                    ),
                }

            now = utc_now_iso()
            pkg["selected_vendor_id"] = resolved_vid
            pkg["updated_at"] = now
            history = list(pkg.get("history") or [])
            history.append(
                {
                    "from_status": "draft",
                    "to_status": "draft",
                    "actor": actor_obj.name,
                    "note": f"Valde granskad länkleverantör: {vet['name']} ({resolved_vid})",
                    "at": now,
                }
            )
            pkg["history"] = history
            _fn, _ct, md_body = render_link_package_brief(pkg, fmt="md")
            pkg["brief_markdown"] = md_body

            if not self.store.compare_and_swap_link_package(
                package_id, expected_status="draft", new_pkg=pkg
            ):
                return {
                    "ok": False,
                    "link_package": self.store.get_link_package(package_id),
                    "error_class": "ConcurrentModification",
                    "message": "Länkpaketet ändrades samtidigt av en annan begäran.",
                }

            log_action(
                actor=actor_obj.name,
                action="seo_link_package_vendor_selected",
                target=lock_site_id,
                target_id=package_id,
                success=True,
                details={"vendor_id": resolved_vid, "vetting_score": vet["vetting_score"]},
            )
            return {"ok": True, "link_package": pkg, "package": pkg}

    def run_seo_autopilot(
        self,
        site_id: str,
        *,
        topic: str = "",
        market: str | None = None,
        budget_sek: float = 15000.0,
        link_count: int = 6,
        include_web_research: bool = True,
        include_vendor_vetting: bool = True,
        include_batch_fixes: bool = False,
        start_deep_research: bool = False,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        """Run the end-to-end autonomous SEO pipeline (strictly HITL; zero silent writes or purchases)."""
        return run_autonomous_seo_pipeline(
            self,
            site_id=site_id,
            actor=actor,
            market=market,
            topic=topic,
            budget_sek=budget_sek,
            link_count=link_count,
            include_web_research=include_web_research,
            include_vendor_vetting=include_vendor_vetting,
            include_batch_fixes=include_batch_fixes,
            start_deep_research=start_deep_research,
        )


__all__ = ["SeoStudioService", "TargetNotFound"]
