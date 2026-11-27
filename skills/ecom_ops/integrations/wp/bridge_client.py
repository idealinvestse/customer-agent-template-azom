"""Authenticated HMAC-SHA256 client for the ``azom-ops-bridge`` WordPress plugin.

Default posture is mock-first and fail-closed:
- In ``AZOM_USE_MOCK=1``, uses deterministic ``InMemoryWpBridgeTransport``.
- Live egress requires ``AZOM_USE_MOCK=0``, ``AZOM_WP_BRIDGE_ENABLED=1``,
  ``AZOM_WP_DEPLOY_KILL`` unset, and a per-site ``WP_BRIDGE_SECRET_<SITE_ID>``
  of at least 32 characters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

from ecom_ops.integrations._http_guard import pinned_https_request
from ecom_ops.runtime_env import env_flag
from ecom_ops.security import SecurityError
from ecom_ops.wp_ops.config import is_mock_mode
from ecom_ops.wp_ops.models import Site

_MIN_SECRET_LEN = 32
_BLOCKED_DROPIN_FILES = frozenset(
    {
        "advanced-cache.php",
        "db.php",
        "db-error.php",
        "install.php",
        "maintenance.php",
        "object-cache.php",
        "php-error.php",
        "fatal-error-handler.php",
        "sunrise.php",
        "blog-deleted.php",
        "blog-inactive.php",
        "blog-suspended.php",
    }
)
_BRIDGE_PLUGIN_FILE = "azom-ops-bridge/azom-ops-bridge.php"
_BRIDGE_SLUG = "azom-ops-bridge"
_SEO_ALLOWED_OBJECT_TYPES = frozenset({"product", "page", "post"})
_SEO_ALLOWED_META_FIELDS = frozenset(
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
_SEO_ALLOWED_MEDIA_FIELDS = frozenset({"alt_text", "title", "caption", "description"})


def _canonical_seo_sha256(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plugin_slug_from_file(plugin_file: str) -> str:
    clean = str(plugin_file or "").replace("\\", "/").strip("/")
    if "/" in clean:
        return clean.split("/", 1)[0]
    return clean[:-4] if clean.lower().endswith(".php") else clean


def _is_bridge_plugin(plugin_file: str) -> bool:
    slug = _plugin_slug_from_file(plugin_file).lower()
    return slug == _BRIDGE_SLUG or str(plugin_file or "").strip().lower() == _BRIDGE_PLUGIN_FILE


def _bridge_error(status: int, message: str, response: dict[str, Any] | None = None) -> SecurityError:
    err = SecurityError(f"Bridge error ({status}): {message}")
    err.bridge_response = response if response is not None else {"ok": False, "error": message}
    return err


def bridge_enabled() -> bool:
    """True only when live mode is active, ``AZOM_WP_BRIDGE_ENABLED=1``, and kill-switch is off."""
    if is_mock_mode():
        return False
    if env_flag("AZOM_WP_DEPLOY_KILL"):
        return False
    return env_flag("AZOM_WP_BRIDGE_ENABLED")


def bridge_secret_env_name(site_id: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(site_id or "").upper()).strip("_")
    return f"WP_BRIDGE_SECRET_{clean}"


def resolve_bridge_secret(site: Site) -> tuple[str, str]:
    """Return ``(env_name, secret_value)`` for ``site`` without falling back to global WP creds."""
    name = bridge_secret_env_name(site.id)
    val = os.environ.get(name, "").strip()
    return name, val


def canonical_signed_target(route: str, params: dict[str, Any] | None = None) -> str:
    """Return canonical ``route`` or ``route?sorted_query`` matching PHP ``PHP_QUERY_RFC3986``."""
    clean_route = str(route or "")
    if not params:
        return clean_route
    items = sorted(
        (str(k), "" if v is None else str(v))
        for k, v in params.items()
        if str(k) not in {"rest_route", "_method", "_envelope"}
    )
    if not items:
        return clean_route
    qs = urlencode(items, quote_via=quote)
    return f"{clean_route}?{qs}" if qs else clean_route


def sign_bridge_request(
    *,
    secret: str,
    method: str,
    route: str,
    params: dict[str, Any] | None = None,
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Compute canonical HMAC-SHA256 headers matching ``Azom_Ops_Bridge_Auth``."""
    clean_secret = str(secret or "").strip()
    if len(clean_secret) < _MIN_SECRET_LEN:
        raise SecurityError("Bridge secret missing or shorter than 32 characters")
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    req_nonce = nonce or secrets.token_hex(16)
    verb = method.upper()
    signed_target = canonical_signed_target(route, params)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{ts}\n{req_nonce}\n{verb}\n{signed_target}\n{body_hash}"
    sig = hmac.new(
        clean_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Azom-Timestamp": ts,
        "X-Azom-Nonce": req_nonce,
        "X-Azom-Signature": sig,
        "Accept": "application/json",
    }
    if verb in {"POST", "PUT", "PATCH"} or body:
        headers["Content-Type"] = "application/json"
    return headers


class BridgeTransport(Protocol):
    is_mock: bool

    def call(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class InMemoryWpBridgeTransport:
    """Deterministic offline mock transport for ``azom-ops-bridge``."""

    is_mock: bool = True

    def __init__(self, site_id: str = "mock-shop", base_url: str = "https://mock.local") -> None:
        self.site_id = site_id
        self.base_url = base_url.rstrip("/")
        self.maintenance_active = False
        self.cache_flushes = 0
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.files: dict[str, bytes] = {
            "theme/twentytwentyfive/style.css": b"/* mock theme */\n",
            "mu_plugin/azom-mu-loader/azom-mu-loader.php": b"<?php\n// Mock MU loader\n",
        }
        self.snapshots: list[str] = []
        self.plugins: dict[str, dict[str, Any]] = {
            "woocommerce/woocommerce.php": {
                "slug": "woocommerce",
                "plugin_file": "woocommerce/woocommerce.php",
                "name": "WooCommerce",
                "version": "9.4.2",
                "active": True,
                "network_active": False,
                "update_available": True,
                "new_version": "9.5.1",
                "auto_update": False,
                "requires_wp": "6.5",
                "requires_php": "7.4",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "9.5",
                "hpos_compatible": True,
                "is_paused": False,
                "is_critical": True,
                "is_bridge": False,
                "requires_plugins": [],
            },
            "klarna-checkout-for-woocommerce/klarna-checkout-for-woocommerce.php": {
                "slug": "klarna-checkout-for-woocommerce",
                "plugin_file": "klarna-checkout-for-woocommerce/klarna-checkout-for-woocommerce.php",
                "name": "Klarna Checkout for WooCommerce",
                "version": "2.12.0",
                "active": True,
                "network_active": False,
                "update_available": True,
                "new_version": "2.12.2",
                "auto_update": False,
                "requires_wp": "6.2",
                "requires_php": "7.4",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "9.4",
                "hpos_compatible": True,
                "is_paused": False,
                "is_critical": True,
                "is_bridge": False,
                "requires_plugins": ["woocommerce"],
            },
            "azom-ops-bridge/azom-ops-bridge.php": {
                "slug": "azom-ops-bridge",
                "plugin_file": "azom-ops-bridge/azom-ops-bridge.php",
                "name": "Azom Ops Bridge",
                "version": "1.2.0",
                "active": True,
                "network_active": False,
                "update_available": False,
                "new_version": None,
                "auto_update": False,
                "requires_wp": "6.4",
                "requires_php": "8.1",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "9.6",
                "hpos_compatible": True,
                "is_paused": False,
                "is_critical": True,
                "is_bridge": True,
                "requires_plugins": [],
            },
            "litespeed-cache/litespeed-cache.php": {
                "slug": "litespeed-cache",
                "plugin_file": "litespeed-cache/litespeed-cache.php",
                "name": "LiteSpeed Cache",
                "version": "6.5.0",
                "active": True,
                "network_active": False,
                "update_available": True,
                "new_version": "6.5.2",
                "auto_update": True,
                "requires_wp": "5.3",
                "requires_php": "7.2",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "",
                "hpos_compatible": True,
                "is_paused": False,
                "is_critical": False,
                "is_bridge": False,
                "requires_plugins": [],
            },
            "wordpress-seo/wp-seo.php": {
                "slug": "wordpress-seo",
                "plugin_file": "wordpress-seo/wp-seo.php",
                "name": "Yoast SEO",
                "version": "23.4",
                "active": True,
                "network_active": False,
                "update_available": True,
                "new_version": "24.0",
                "auto_update": False,
                "requires_wp": "6.5",
                "requires_php": "7.4",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "",
                "hpos_compatible": None,
                "is_paused": False,
                "is_critical": False,
                "is_bridge": False,
                "requires_plugins": [],
            },
            "contact-form-7/wp-contact-form-7.php": {
                "slug": "contact-form-7",
                "plugin_file": "contact-form-7/wp-contact-form-7.php",
                "name": "Contact Form 7",
                "version": "5.9.8",
                "active": False,
                "network_active": False,
                "update_available": True,
                "new_version": "5.9.9",
                "auto_update": False,
                "requires_wp": "6.4",
                "requires_php": "7.4",
                "tested_wp": "6.7.1",
                "wc_tested_up_to": "",
                "hpos_compatible": None,
                "is_paused": False,
                "is_critical": False,
                "is_bridge": False,
                "requires_plugins": [],
            },
        }
        self.mu_plugins: list[dict[str, Any]] = [
            {
                "slug": "azom-mu-loader",
                "file": "azom-mu-loader.php",
                "name": "Azom MU Loader",
                "version": "1.0.0",
                "is_loader": True,
                "sha256": hashlib.sha256(b"<?php\n// Mock MU loader\n").hexdigest(),
                "load_order": 1,
            },
            {
                "slug": "azom-hardening",
                "file": "azom-hardening.php",
                "name": "Azom Hardening",
                "version": "1.0.0",
                "is_loader": False,
                "sha256": hashlib.sha256(b"<?php\n// Mock MU hardening\n").hexdigest(),
                "load_order": 2,
            },
        ]
        self.dropins: list[dict[str, Any]] = [
            {
                "file": "object-cache.php",
                "name": "Redis Object Cache Drop-In",
                "description": "External object cache drop-in.",
                "read_only": True,
            },
            {
                "file": "advanced-cache.php",
                "name": "LiteSpeed Advanced Cache",
                "description": "Page cache drop-in.",
                "read_only": True,
            },
        ]
        self.constants: dict[str, Any] = {
            "disallow_file_mods": False,
            "automatic_updater_disabled": False,
            "wp_auto_update_core": "minor",
        }
        self.autoupdate_filters: list[dict[str, Any]] = []
        self.paused_plugins: list[dict[str, Any]] = []
        self.debug_log_lines: list[str] = [
            "[27-Nov-2026 08:00:01 UTC] PHP Notice:  Mock notice in <WP_CONTENT>/plugins/wordpress-seo/wp-seo.php on line 42",
        ]
        self.hook_map: dict[str, list[dict[str, Any]]] = {
            "auto_update_plugin": [],
            "woocommerce_before_calculate_totals": [
                {"priority": 10, "callback": "WC_Cart::calculate_totals", "accepted_args": 1},
            ],
            "woocommerce_checkout_process": [
                {"priority": 10, "callback": "Klarna_Checkout::validate_checkout", "accepted_args": 1},
            ],
        }
        self.plugin_backups: dict[str, dict[str, Any]] = {}
        self.fail_probe_for_plugins: set[str] = set()
        self.fail_canary_paths_for_plugins: dict[str, dict[str, dict[str, Any]]] = {}
        self.seo_plugin = "yoast"
        self.seo_plugin_version = "23.4"
        self.blog_public = True
        self.permalink_structure = "/%postname%/"
        self.seo_entities_store: dict[tuple[str, int], dict[str, Any]] = {
            ("product", 501): {
                "object_type": "product",
                "object_id": 501,
                "title": "Azom CarPlay Trådlös Adapter Pro",
                "slug": "carplay-adapter-pro",
                "url": f"{self.base_url}/produkt/carplay-adapter-pro/",
                "status": "publish",
                "word_count": 320,
                "excerpt": "Trådlös Apple CarPlay adapter med snabb 5 GHz anslutning för nordiska bilmodeller.",
                "meta_title": "Azom CarPlay Trådlös Adapter Pro — Bästa Trådlösa Apple CarPlay Adaptern för Bil i Norden 2026",
                "meta_description": "Uppgradera bilen med Azom CarPlay Trådlös Adapter Pro. Plug-and-play på 10 sekunder, 5 GHz WiFi och fri frakt i Sverige och Norge.",
                "focus_keyphrase": "trådlös carplay adapter",
                "canonical": f"{self.base_url}/produkt/carplay-adapter-pro/",
                "robots": "index,follow",
                "og_title": "Azom CarPlay Trådlös Adapter Pro",
                "og_description": "Trådlös Apple CarPlay adapter med snabb 5 GHz anslutning.",
                "schema_type": "Product",
                "internal_links": [
                    "/carplay-kompatibilitet/",
                    "/blogg/basta-tradlos-carplay-adapter/",
                ],
                "images_total": 3,
                "images_missing_alt": 1,
            },
            ("product", 502): {
                "object_type": "product",
                "object_id": 502,
                "title": "Azom CarPlay Mini Dongle",
                "slug": "carplay-mini-dongle",
                "url": f"{self.base_url}/produkt/carplay-mini-dongle/",
                "status": "publish",
                "word_count": 48,
                "excerpt": "Kompakt CarPlay dongle.",
                "meta_title": "Azom CarPlay Trådlös Adapter Pro — Bästa Trådlösa Apple CarPlay Adaptern för Bil i Norden 2026",
                "meta_description": "",
                "focus_keyphrase": "trådlös carplay adapter",
                "canonical": f"{self.base_url}/produkt/carplay-mini-dongle/",
                "robots": "index,follow",
                "og_title": "",
                "og_description": "",
                "schema_type": "Product",
                "internal_links": [],
                "images_total": 2,
                "images_missing_alt": 2,
            },
            ("product", 503): {
                "object_type": "product",
                "object_id": 503,
                "title": "Azom Android Auto & CarPlay AI Box",
                "slug": "android-auto-carplay-ai-box",
                "url": f"{self.base_url}/produkt/android-auto-carplay-ai-box/",
                "status": "publish",
                "word_count": 265,
                "excerpt": "Kombinerad trådlös Android Auto och Apple CarPlay AI Box med streamingstöd.",
                "meta_title": "Azom Android Auto & CarPlay AI Box | Trådlös Bilunderhållning",
                "meta_description": "Få både trådlös Android Auto och Apple CarPlay i samma bilbox. Snabb installation via USB och 2 års nordisk garanti.",
                "focus_keyphrase": "android auto carplay box",
                "canonical": f"{self.base_url}/produkt/android-auto-carplay-ai-box/",
                "robots": "index,follow",
                "og_title": "Azom Android Auto & CarPlay AI Box",
                "og_description": "Trådlös Android Auto och Apple CarPlay AI Box.",
                "schema_type": "Product",
                "internal_links": ["/produkt/carplay-adapter-pro/"],
                "images_total": 4,
                "images_missing_alt": 1,
            },
            ("page", 2): {
                "object_type": "page",
                "object_id": 2,
                "title": "Om Azom — Nordisk Bilstereo & CarPlay",
                "slug": "om-oss",
                "url": f"{self.base_url}/om-oss/",
                "status": "publish",
                "word_count": 185,
                "excerpt": "Läs mer om Azom och vår passion för smart bilteknik i Norden.",
                "meta_title": "Om Azom",
                "meta_description": "",
                "focus_keyphrase": "azom carplay sverige",
                "canonical": f"{self.base_url}/om-oss/",
                "robots": "index,follow",
                "og_title": "Om Azom",
                "og_description": "",
                "schema_type": "AboutPage",
                "internal_links": ["/produkt/carplay-adapter-pro/", "/carplay-kompatibilitet/"],
                "images_total": 1,
                "images_missing_alt": 1,
            },
            ("page", 12): {
                "object_type": "page",
                "object_id": 12,
                "title": "Kompatibilitetsguide för Apple CarPlay",
                "slug": "carplay-kompatibilitet",
                "url": f"{self.base_url}/carplay-kompatibilitet/",
                "status": "publish",
                "word_count": 620,
                "excerpt": "Kontrollera om din bilmodell stöder trådlös Apple CarPlay och Android Auto.",
                "meta_title": "Kompatibilitetsguide för Apple CarPlay — Vilka bilar passar? | Azom",
                "meta_description": "Se vilka bilmärken som fungerar med Azom CarPlay.",
                "focus_keyphrase": "apple carplay kompatibilitet",
                "canonical": f"{self.base_url}/carplay-kompatibilitet/",
                "robots": "index,follow",
                "og_title": "Kompatibilitetsguide för Apple CarPlay",
                "og_description": "Se vilka bilar som fungerar med trådlös CarPlay.",
                "schema_type": "FAQPage",
                "internal_links": [
                    "/produkt/carplay-adapter-pro/",
                    "/produkt/carplay-mini-dongle/",
                    "/produkt/android-auto-carplay-ai-box/",
                ],
                "images_total": 2,
                "images_missing_alt": 0,
            },
            ("post", 1): {
                "object_type": "post",
                "object_id": 1,
                "title": "Bästa trådlös CarPlay adapter 2026 — Test & Guide",
                "slug": "basta-tradlos-carplay-adapter",
                "url": f"{self.base_url}/blogg/basta-tradlos-carplay-adapter/",
                "status": "publish",
                "word_count": 940,
                "excerpt": "Vi jämför årets bästa trådlösa CarPlay-adaptrar för Volvo, VW, Audi och Tesla.",
                "meta_title": "Bästa Trådlös CarPlay Adapter 2026 — Stort Test & Köpguide",
                "meta_description": "Jämför uppstartstid, stabilitet och kompatibilitet för trådlösa CarPlay-adaptrar 2026. Hitta rätt modell för din bil hos Azom.",
                "focus_keyphrase": "trådlös carplay adapter",
                "canonical": f"{self.base_url}/blogg/basta-tradlos-carplay-adapter/",
                "robots": "index,follow",
                "og_title": "Bästa Trådlös CarPlay Adapter 2026",
                "og_description": "Stort test av trådlösa Apple CarPlay-adaptrar.",
                "schema_type": "Article",
                "internal_links": [
                    "/produkt/carplay-adapter-pro/",
                    "/produkt/carplay-mini-dongle/",
                    "/carplay-kompatibilitet/",
                ],
                "images_total": 3,
                "images_missing_alt": 2,
            },
            ("post", 14): {
                "object_type": "post",
                "object_id": 14,
                "title": "Monteringsguide Volvo XC60 CarPlay",
                "slug": "monteringsguide-volvo-xc60-carplay",
                "url": f"{self.base_url}/blogg/monteringsguide-volvo-xc60-carplay/",
                "status": "publish",
                "word_count": 135,
                "excerpt": "",
                "meta_title": "Volvo XC60 CarPlay",
                "meta_description": "",
                "focus_keyphrase": "volvo xc60 carplay",
                "canonical": f"{self.base_url}/blogg/monteringsguide-volvo-xc60-carplay/",
                "robots": "index,follow",
                "og_title": "",
                "og_description": "",
                "schema_type": "Article",
                "internal_links": ["/produkt/carplay-adapter-pro/"],
                "images_total": 2,
                "images_missing_alt": 2,
            },
        }
        self.seo_media_store: dict[int, dict[str, Any]] = {
            10: {
                "media_id": 10,
                "filename": "azom-logo.png",
                "url": f"{self.base_url}/wp-content/uploads/2026/01/azom-logo.png",
                "mime_type": "image/png",
                "alt_text": "Azom logotyp",
                "title": "Azom logotyp",
                "caption": "",
                "description": "Officiell Azom varumärkeslogotyp",
                "width": 512,
                "height": 128,
                "filesize": 24576,
                "attached_post_id": 2,
            },
            101: {
                "media_id": 101,
                "filename": "IMG_4821.jpg",
                "url": f"{self.base_url}/wp-content/uploads/2026/05/IMG_4821.jpg",
                "mime_type": "image/jpeg",
                "alt_text": "",
                "title": "IMG_4821",
                "caption": "",
                "description": "",
                "width": 2400,
                "height": 1600,
                "filesize": 1850000,
                "attached_post_id": 501,
            },
            102: {
                "media_id": 102,
                "filename": "DSC00412.jpg",
                "url": f"{self.base_url}/wp-content/uploads/2026/05/DSC00412.jpg",
                "mime_type": "image/jpeg",
                "alt_text": "image",
                "title": "DSC00412",
                "caption": "",
                "description": "",
                "width": 1920,
                "height": 1080,
                "filesize": 920000,
                "attached_post_id": 502,
            },
            103: {
                "media_id": 103,
                "filename": "azom-carplay-adapter-pro-dashboard.webp",
                "url": f"{self.base_url}/wp-content/uploads/2026/06/azom-carplay-adapter-pro-dashboard.webp",
                "mime_type": "image/webp",
                "alt_text": "Azom CarPlay Pro trådlös adapter monterad i bilens instrumentpanel",
                "title": "Azom CarPlay Adapter Pro i bil",
                "caption": "Trådlös anslutning utan kabeltrassel",
                "description": "Produktbild av Azom CarPlay Trådlös Adapter Pro",
                "width": 1200,
                "height": 800,
                "filesize": 115000,
                "attached_post_id": 501,
            },
            104: {
                "media_id": 104,
                "filename": "Screenshot_2026_volvo.png",
                "url": f"{self.base_url}/wp-content/uploads/2026/07/Screenshot_2026_volvo.png",
                "mime_type": "image/png",
                "alt_text": "",
                "title": "Screenshot_2026_volvo",
                "caption": "",
                "description": "",
                "width": 1600,
                "height": 900,
                "filesize": 1240000,
                "attached_post_id": 14,
            },
        }

    @staticmethod
    def _seo_entity_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": str(row.get("title") or ""),
            "slug": str(row.get("slug") or ""),
            "excerpt": str(row.get("excerpt") or ""),
            "meta_title": str(row.get("meta_title") or ""),
            "meta_description": str(row.get("meta_description") or ""),
            "focus_keyphrase": str(row.get("focus_keyphrase") or ""),
            "canonical": str(row.get("canonical") or ""),
            "robots": str(row.get("robots") or "index,follow"),
            "og_title": str(row.get("og_title") or ""),
            "og_description": str(row.get("og_description") or ""),
            "schema_type": str(row.get("schema_type") or ""),
        }

    @staticmethod
    def _seo_media_snapshot(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "alt_text": str(row.get("alt_text") or ""),
            "title": str(row.get("title") or ""),
            "caption": str(row.get("caption") or ""),
            "description": str(row.get("description") or ""),
        }

    def _mock_health_payload(self) -> dict[str, Any]:
        active = [
            {"plugin": row["plugin_file"], "name": row["name"], "version": row["version"]}
            for row in self.plugins.values()
            if row.get("active")
        ]
        return {
            "ok": True,
            "bridge_version": "1.2.0",
            "wordpress_version": "6.7.1",
            "php_version": "8.3.12",
            "woocommerce_version": "9.4.2",
            "hpos_enabled": True,
            "theme": {
                "stylesheet": "twentytwentyfive",
                "template": "twentytwentyfive",
                "name": "Twenty Twenty-Five",
                "version": "1.0",
                "is_child": False,
                "parent": "",
                "is_block": True,
            },
            "active_plugins": active,
            "mu_plugins_count": len(self.mu_plugins),
            "dropins_count": len(self.dropins),
            "paused_plugins_count": len(self.paused_plugins),
            "cache_plugins": ["litespeed"],
            "multilingual": "wpml",
            "maintenance_active": self.maintenance_active,
            "opcache_enabled": True,
            "memory_limit": "512M",
            "site_url": self.base_url,
            "home_url": self.base_url,
        }

    def _validate_mu_or_component_path(self, comp_kind: str, slug: str, rel: str) -> str:
        clean_rel = str(rel or "").replace("\\", "/").lstrip("/")
        if not clean_rel or ".." in clean_rel or "\x00" in clean_rel:
            raise _bridge_error(400, "Invalid relative path")
        ext = clean_rel.rsplit(".", 1)[-1].lower() if "." in clean_rel else ""
        if ext not in {"php", "css", "scss", "js", "json", "txt", "md", "html", "pot", "po"}:
            raise _bridge_error(400, "File extension not allowed")
        if comp_kind == "mu_plugin":
            leaf = clean_rel.rsplit("/", 1)[-1].lower()
            if leaf in _BLOCKED_DROPIN_FILES:
                raise _bridge_error(400, "WordPress drop-ins are read-only and cannot be modified.")
            if slug and slug not in {"_root", "mu-plugins"}:
                if slug.lower() in {"woocommerce", "azom-ops-bridge"} or ".." in slug:
                    raise _bridge_error(400, "Blocked or invalid slug")
                return f"mu_plugin/{slug}/{clean_rel}"
            return f"mu_plugin/{clean_rel}"
        if not slug or slug.lower() in {"woocommerce", "azom-ops-bridge"} or ".." in slug:
            raise _bridge_error(400, "Blocked or invalid slug")
        return f"{comp_kind}/{slug}/{clean_rel}"

    def call(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        verb = method.upper()
        self.calls.append((verb, route, payload or params))

        if verb == "GET" and route == "/azom-ops/v1/health":
            return self._mock_health_payload()

        if verb == "GET" and route == "/azom-ops/v1/revisions":
            post_id = int((params or {}).get("post_id") or 0)
            revs = (
                [
                    {
                        "id": post_id * 10 + 1,
                        "parent": post_id,
                        "author": 1,
                        "modified_gmt": "2026-11-27T01:00:00",
                        "title": f"Revision for {post_id}",
                    }
                ]
                if post_id > 0
                else []
            )
            return {
                "ok": True,
                "post_id": post_id,
                "revisions": revs,
                "snapshots": list(self.snapshots),
            }

        if verb == "GET" and route == "/azom-ops/v1/translations":
            post_id = (params or {}).get("post_id")
            tr_map: dict[str, Any] = {}
            if post_id:
                pid = int(post_id)
                tr_map = {
                    "nb": {"id": pid, "status": "publish", "title": "Produkt NO"},
                    "sv": {"id": pid + 1000, "status": "publish", "title": "Produkt SV"},
                    "da": {"id": pid + 2000, "status": "draft", "title": "Produkt DK"},
                }
            return {
                "ok": True,
                "provider": "wpml",
                "default_lang": "nb",
                "languages": [
                    {"code": "nb", "name": "Norsk bokmål", "locale": "nb_NO", "active": True},
                    {"code": "sv", "name": "Svenska", "locale": "sv_SE", "active": False},
                    {"code": "da", "name": "Dansk", "locale": "da_DK", "active": False},
                ],
                "post_id": int(post_id) if post_id else None,
                "translations": tr_map,
            }

        if verb == "GET" and route == "/azom-ops/v1/snapshot":
            q = params or {}
            kind = str(q.get("kind") or "settings")
            if kind in {"theme_file", "plugin_file", "mu_plugin_file", "mu_plugin"}:
                if kind == "theme_file":
                    comp_kind = "theme"
                elif kind == "plugin_file":
                    comp_kind = "plugin"
                else:
                    comp_kind = "mu_plugin"
                key = self._validate_mu_or_component_path(
                    comp_kind,
                    str(q.get("slug") or ""),
                    str(q.get("path") or ""),
                )
                raw = self.files.get(key, b"")
                return {
                    "ok": True,
                    "kind": kind,
                    "exists": key in self.files,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "content": base64.b64encode(raw).decode("ascii"),
                }
            if kind in {"page", "post"}:
                pid = int(q.get("id") or 0)
                if pid <= 0:
                    raise SecurityError("Bridge error (404): Not found")
                pdata = {
                    "id": pid,
                    "title": f"Mock {kind} {pid}",
                    "content": "",
                    "status": "publish",
                    "modified_gmt": "2026-11-27 00:00:00",
                }
                return {
                    "ok": True,
                    "kind": kind,
                    "data": pdata,
                    "sha256": _canonical_seo_sha256(pdata),
                }
            sdata = {"title": "Azom Mock", "description": "Nordisk e-handel"}
            return {
                "ok": True,
                "kind": kind,
                "data": sdata,
                "sha256": _canonical_seo_sha256(sdata),
            }

        if verb == "POST" and route == "/azom-ops/v1/apply":
            body = payload or {}
            comp_kind = str(body.get("kind") or "theme")
            if comp_kind not in {"theme", "plugin", "mu_plugin"}:
                raise _bridge_error(400, "Unsupported component kind")
            slug = str(body.get("slug") or "")
            cid = str(body.get("change_id") or "")
            entries = list(body.get("files") or [])
            staged: dict[str, bytes | None] = {}
            for item in entries:
                rel = str(item.get("path") or "").lstrip("/")
                key = self._validate_mu_or_component_path(comp_kind, slug, rel)
                op = str(item.get("op") or "updated")
                expected_sha = str(item.get("expected_before_sha256") or "")
                prev = self.files.get(key)
                if op == "created" and prev is not None:
                    raise _bridge_error(409, "Target file already exists")
                if expected_sha:
                    actual_sha = hashlib.sha256(prev if prev is not None else b"").hexdigest()
                    if actual_sha != expected_sha:
                        raise _bridge_error(409, "Stale target digest mismatch")
                if op == "deleted":
                    staged[key] = None
                else:
                    raw_bytes = base64.b64decode(str(item.get("content_b64") or ""))
                    if len(raw_bytes) > 65536:
                        raise _bridge_error(400, "File exceeds max limit")
                    staged[key] = raw_bytes
            for key, raw_bytes in staged.items():
                if raw_bytes is None:
                    self.files.pop(key, None)
                else:
                    self.files[key] = raw_bytes
            self.snapshots.insert(0, f"snap-{cid}.json")
            self.cache_flushes += 1
            return {
                "ok": True,
                "change_id": cid,
                "files_written": len(entries),
                "http_status": 200,
            }

        if verb == "POST" and route == "/azom-ops/v1/rollback":
            body = payload or {}
            comp_kind = str(body.get("kind") or "theme")
            if comp_kind not in {"theme", "plugin", "mu_plugin"}:
                raise _bridge_error(400, "Unsupported component kind")
            slug = str(body.get("slug") or "")
            entries = list(body.get("files") or [])
            for item in entries:
                rel = str(item.get("path") or "").lstrip("/")
                key = self._validate_mu_or_component_path(comp_kind, slug, rel)
                if item.get("op") == "deleted":
                    self.files.pop(key, None)
                else:
                    self.files[key] = base64.b64decode(str(item.get("content_b64") or ""))
            self.cache_flushes += 1
            return {"ok": True, "restored_files": len(entries)}

        if verb == "POST" and route == "/azom-ops/v1/cache/flush":
            self.cache_flushes += 1
            return {"ok": True, "flushed": ["wp_object_cache", "wc_product_transients", "litespeed"]}

        if verb == "POST" and route == "/azom-ops/v1/maintenance":
            enabled = bool((payload or {}).get("enabled"))
            self.maintenance_active = enabled
            duration = min(900, max(30, int((payload or {}).get("duration_seconds") or 300)))
            return (
                {"ok": True, "enabled": True, "duration_seconds": duration}
                if enabled
                else {"ok": True, "enabled": False}
            )

        if verb == "GET" and route == "/azom-ops/v1/plugins/inventory":
            q = params or {}
            include_updates = str(q.get("include_updates", "1")) not in {"0", "false", "False", "no"}
            paused_files = {str(p.get("plugin_file") or "") for p in self.paused_plugins}
            plugins_out: list[dict[str, Any]] = []
            for row in self.plugins.values():
                item = dict(row)
                item["is_paused"] = item["plugin_file"] in paused_files or bool(item.get("is_paused"))
                if not include_updates:
                    item["update_available"] = False
                    item["new_version"] = None
                plugins_out.append(item)
            return {
                "ok": True,
                "plugins": plugins_out,
                "mu_plugins": [dict(m) for m in self.mu_plugins],
                "dropins": [dict(d) for d in self.dropins],
                "constants": dict(self.constants),
                "autoupdate_filters": [dict(f) for f in self.autoupdate_filters],
                "wp_version": "6.7.1",
                "php_version": "8.3.12",
            }

        if verb == "POST" and route == "/azom-ops/v1/plugins/update":
            body = payload or {}
            change_id = str(body.get("change_id") or "").strip()
            plugin_file = str(body.get("plugin_file") or "").strip()
            if not change_id or len(change_id) < 4:
                raise _bridge_error(400, "Invalid change_id")
            if not plugin_file or ".." in plugin_file:
                raise _bridge_error(400, "Invalid plugin_file")
            if _is_bridge_plugin(plugin_file):
                raise _bridge_error(403, "Self-update of azom-ops-bridge is refused.")
            if self.constants.get("disallow_file_mods"):
                raise _bridge_error(403, "DISALLOW_FILE_MODS is enabled on this site.")
            row = self.plugins.get(plugin_file)
            if row is None:
                raise _bridge_error(404, f"Plugin not installed: {plugin_file}")
            slug = str(row["slug"])
            from_version = str(row["version"])
            target_version = body.get("target_version")
            next_version = str(target_version or row.get("new_version") or from_version)
            if target_version and row.get("new_version") and str(target_version) != str(row["new_version"]):
                raise _bridge_error(409, "Requested target_version does not match available update version.")

            backup_before = bool(body.get("backup_before", True))
            probe_health = bool(body.get("probe_health", True))
            raw_canary = body.get("canary_paths")
            if raw_canary is None:
                canary_paths = ["/"]
            else:
                if not isinstance(raw_canary, list):
                    raise _bridge_error(400, "canary_paths must be an array of relative paths.")
                canary_paths = []
                for entry in raw_canary:
                    if not isinstance(entry, str):
                        raise _bridge_error(400, "canary_paths entries must be strings.")
                    clean_p = entry.strip()
                    if (
                        not clean_p
                        or not clean_p.startswith("/")
                        or ".." in clean_p
                        or "//" in clean_p
                        or "://" in clean_p
                        or "?" in clean_p
                        or "#" in clean_p
                        or "\x00" in clean_p
                        or len(clean_p) > 120
                        or not re.fullmatch(r"/(?:[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?)?", clean_p)
                    ):
                        raise _bridge_error(400, f"Invalid canary_paths entry: {clean_p}")
                    low_p = clean_p.lower()
                    if any(
                        low_p == blk or low_p.startswith(blk + "/") or low_p.startswith(blk + ".")
                        for blk in ("/wp-admin", "/wp-login", "/wp-json", "/xmlrpc")
                    ):
                        raise _bridge_error(400, f"Blocked administrative path in canary_paths: {clean_p}")
                    if clean_p not in canary_paths:
                        canary_paths.append(clean_p)
                        if len(canary_paths) >= 6:
                            break
                if not canary_paths:
                    canary_paths = ["/"]

            backup_id = ""
            backup_sha256 = ""
            if backup_before:
                backup_id = f"pbackup-{change_id}-{slug}.zip"
                raw_payload = f"{plugin_file}:{from_version}".encode()
                backup_sha256 = hashlib.sha256(raw_payload).hexdigest()
                self.plugin_backups[backup_id] = {
                    "backup_id": backup_id,
                    "plugin_file": plugin_file,
                    "slug": slug,
                    "version": from_version,
                    "sha256": backup_sha256,
                    "bytes": len(raw_payload),
                    "modified_gmt": "2026-11-27T08:00:00Z",
                }
            self.cache_flushes += 1
            canary_results: list[dict[str, Any]] = []
            if probe_health:
                per_path_failures = self.fail_canary_paths_for_plugins.get(plugin_file, {})
                failed_status = 200
                any_failed = False
                for idx_c, cpath in enumerate(canary_paths):
                    if cpath in per_path_failures:
                        spec = per_path_failures[cpath]
                        st = int(spec.get("status", 500))
                        fatal = bool(spec.get("fatal_detected", False))
                        path_ok = (200 <= st < 400) and (not fatal)
                    elif plugin_file in self.fail_probe_for_plugins and idx_c == 0:
                        st = 500
                        fatal = False
                        path_ok = False
                    else:
                        st = 200
                        fatal = False
                        path_ok = True
                    canary_results.append(
                        {
                            "path": cpath,
                            "status": st,
                            "ok": path_ok,
                            "fatal_detected": fatal,
                        }
                    )
                    if not path_ok:
                        any_failed = True
                        failed_status = st if st >= 400 else 500
                if any_failed:
                    return {
                        "ok": False,
                        "plugin_file": plugin_file,
                        "slug": slug,
                        "from_version": from_version,
                        "to_version": from_version,
                        "backup_id": backup_id,
                        "backup_sha256": backup_sha256,
                        "http_status": failed_status,
                        "auto_reverted": backup_before,
                        "canary_results": canary_results,
                        "error": "Post-update canary health probe failed; reverted automatically.",
                    }
            row["version"] = next_version
            if row.get("new_version") == next_version:
                row["update_available"] = False
                row["new_version"] = None
            return {
                "ok": True,
                "plugin_file": plugin_file,
                "slug": slug,
                "from_version": from_version,
                "to_version": next_version,
                "backup_id": backup_id,
                "backup_sha256": backup_sha256,
                "http_status": 200,
                "auto_reverted": False,
                "canary_results": canary_results,
                "error": None,
            }

        if verb == "POST" and route == "/azom-ops/v1/plugins/rollback":
            body = payload or {}
            change_id = str(body.get("change_id") or "").strip()
            plugin_file = str(body.get("plugin_file") or "").strip()
            backup_id = str(body.get("backup_id") or "").strip()
            if not change_id or len(change_id) < 4:
                raise _bridge_error(400, "Invalid change_id")
            if not plugin_file or ".." in plugin_file:
                raise _bridge_error(400, "Invalid plugin_file")
            if _is_bridge_plugin(plugin_file):
                raise _bridge_error(403, "Rollback of azom-ops-bridge is refused.")
            if not backup_id.startswith("pbackup-") or not backup_id.endswith(".zip") or "/" in backup_id or "\\" in backup_id or ".." in backup_id:
                raise _bridge_error(400, "Invalid backup_id format.")
            backup = self.plugin_backups.get(backup_id)
            if backup is None:
                raise _bridge_error(404, f"Plugin backup not found: {backup_id}")
            slug = _plugin_slug_from_file(plugin_file)
            if backup["slug"] != slug:
                raise _bridge_error(400, "Backup archive does not belong to target plugin.")
            row = self.plugins.get(plugin_file)
            restored_version = str(backup["version"])
            if row is not None:
                prev_ver = str(row["version"])
                row["version"] = restored_version
                if prev_ver != restored_version:
                    row["update_available"] = True
                    row["new_version"] = prev_ver
            self.cache_flushes += 1
            return {
                "ok": True,
                "plugin_file": plugin_file,
                "slug": slug,
                "restored_version": restored_version,
                "backup_id": backup_id,
                "http_status": 200,
            }

        if verb == "POST" and route == "/azom-ops/v1/plugins/autoupdate":
            body = payload or {}
            changes = body.get("changes")
            if not isinstance(changes, list) or not changes:
                raise _bridge_error(400, "Missing changes array")
            for item in changes:
                if not isinstance(item, dict):
                    raise _bridge_error(400, "Invalid change entry")
                pf = str(item.get("plugin_file") or "").strip()
                if not pf or ".." in pf:
                    raise _bridge_error(400, "Invalid plugin_file")
                if _is_bridge_plugin(pf):
                    raise _bridge_error(403, "Auto-update mutation of azom-ops-bridge is refused.")
                if not isinstance(item.get("enabled"), bool):
                    raise _bridge_error(400, "enabled must be a boolean")
            before = sorted(pf for pf, row in self.plugins.items() if row.get("auto_update"))
            current_set = set(before)
            applied: list[dict[str, Any]] = []
            for item in changes:
                pf = str(item["plugin_file"]).strip()
                enabled = bool(item["enabled"])
                had = pf in current_set
                if enabled:
                    current_set.add(pf)
                else:
                    current_set.discard(pf)
                if pf in self.plugins:
                    self.plugins[pf]["auto_update"] = enabled
                applied.append({"plugin_file": pf, "enabled": enabled, "changed": had != enabled})
            after = sorted(current_set)
            warnings: list[str] = []
            if self.constants.get("automatic_updater_disabled"):
                warnings.append("AUTOMATIC_UPDATER_DISABLED is enabled; WordPress background auto-updates will not run.")
            if self.constants.get("disallow_file_mods"):
                warnings.append("DISALLOW_FILE_MODS is enabled; WordPress cannot apply auto-updates.")
            if self.autoupdate_filters:
                warnings.append("Active auto_update_plugin filter callbacks may override auto_update_plugins option.")
            return {
                "ok": True,
                "before": before,
                "after": after,
                "applied": applied,
                "filter_warnings": warnings,
            }

        if verb == "POST" and route == "/azom-ops/v1/plugins/toggle":
            body = payload or {}
            plugin_file = str(body.get("plugin_file") or "").strip()
            if not plugin_file or ".." in plugin_file:
                raise _bridge_error(400, "Invalid plugin_file")
            if _is_bridge_plugin(plugin_file):
                raise _bridge_error(403, "Toggling azom-ops-bridge is refused.")
            if not isinstance(body.get("active"), bool):
                raise _bridge_error(400, "active must be a boolean")
            row = self.plugins.get(plugin_file)
            if row is None:
                raise _bridge_error(404, f"Plugin not installed: {plugin_file}")
            want_active = bool(body["active"])
            probe_health = bool(body.get("probe_health", True))
            was_active = bool(row["active"])
            self.cache_flushes += 1
            if probe_health and plugin_file in self.fail_probe_for_plugins:
                return {
                    "ok": False,
                    "plugin_file": plugin_file,
                    "active": was_active,
                    "auto_reverted": True,
                    "http_status": 500,
                }
            row["active"] = want_active
            return {
                "ok": True,
                "plugin_file": plugin_file,
                "active": want_active,
                "auto_reverted": False,
                "http_status": 200,
            }

        if verb == "GET" and route == "/azom-ops/v1/plugins/diagnostics":
            q = params or {}
            log_lines = max(0, min(300, int(q.get("log_lines") if q.get("log_lines") is not None else 80)))
            hooks_raw = str(q.get("hooks") or "").strip()
            requested_hooks = (
                [h.strip() for h in hooks_raw.split(",") if h.strip()]
                if hooks_raw
                else list(self.hook_map.keys())
            )
            hook_out: dict[str, list[dict[str, Any]]] = {}
            for hook_name in requested_hooks:
                if hook_name == "auto_update_plugin" and self.autoupdate_filters:
                    hook_out[hook_name] = [dict(f) for f in self.autoupdate_filters]
                else:
                    hook_out[hook_name] = [dict(entry) for entry in self.hook_map.get(hook_name, [])]
            backups_out = [
                {
                    "backup_id": b["backup_id"],
                    "slug": b["slug"],
                    "bytes": b["bytes"],
                    "modified_gmt": b["modified_gmt"],
                }
                for b in self.plugin_backups.values()
            ]
            return {
                "ok": True,
                "paused_plugins": [dict(p) for p in self.paused_plugins],
                "debug_log_tail": list(self.debug_log_lines[-log_lines:]) if log_lines > 0 else [],
                "hook_map": hook_out,
                "available_backups": backups_out,
                "bridge_health": self._mock_health_payload(),
            }

        if verb == "GET" and route == "/azom-ops/v1/seo/overview":
            home = self.base_url.rstrip("/")
            sitemaps: list[str] = []
            if self.seo_plugin in {"yoast", "rankmath"}:
                sitemaps.append(f"{home}/sitemap_index.xml")
            elif self.seo_plugin in {"aioseo", "seopress"}:
                sitemaps.append(f"{home}/sitemap.xml")
            sitemaps.append(f"{home}/wp-sitemap.xml")
            counts = {
                "products": sum(1 for (otype, _) in self.seo_entities_store if otype == "product"),
                "pages": sum(1 for (otype, _) in self.seo_entities_store if otype == "page"),
                "posts": sum(1 for (otype, _) in self.seo_entities_store if otype == "post"),
                "media": len(self.seo_media_store),
            }
            return {
                "ok": True,
                "seo_plugin": self.seo_plugin,
                "seo_plugin_version": self.seo_plugin_version,
                "blog_public": bool(self.blog_public),
                "permalink_structure": self.permalink_structure,
                "home_url": home,
                "sitemap_urls": sitemaps,
                "robots_url": f"{home}/robots.txt",
                "multilingual": "wpml",
                "counts": counts,
            }

        if verb == "GET" and route == "/azom-ops/v1/seo/entities":
            q = params or {}
            obj_type = str(q.get("object_type") or "all").strip().lower()
            if obj_type != "all" and obj_type not in _SEO_ALLOWED_OBJECT_TYPES:
                raise _bridge_error(400, f"Unsupported object_type: {obj_type}")
            raw_oid = q.get("object_id")
            if raw_oid is not None and str(raw_oid).strip() != "":
                try:
                    target_oid = int(raw_oid)
                except (TypeError, ValueError):
                    target_oid = 0
                if target_oid <= 0:
                    raise _bridge_error(400, "Invalid object_id")
                rows = [
                    {
                        **dict(row),
                        "internal_links": list(row.get("internal_links") or []),
                        "before_sha256": _canonical_seo_sha256(self._seo_entity_snapshot(row)),
                    }
                    for (otype, oid), row in sorted(
                        self.seo_entities_store.items(), key=lambda kv: (kv[0][0], kv[0][1])
                    )
                    if oid == target_oid and (obj_type == "all" or otype == obj_type)
                ]
                return {
                    "ok": True,
                    "seo_plugin": self.seo_plugin,
                    "entities": rows,
                    "total": len(rows),
                }
            limit = max(1, min(100, int(q.get("limit") or 50)))
            page = max(1, int(q.get("page") or 1))
            rows = [
                {
                    **dict(row),
                    "internal_links": list(row.get("internal_links") or []),
                    "before_sha256": _canonical_seo_sha256(self._seo_entity_snapshot(row)),
                }
                for (otype, _oid), row in sorted(self.seo_entities_store.items(), key=lambda kv: (kv[0][0], kv[0][1]))
                if obj_type == "all" or otype == obj_type
            ]
            total = len(rows)
            start = (page - 1) * limit
            return {
                "ok": True,
                "seo_plugin": self.seo_plugin,
                "entities": rows[start : start + limit],
                "total": total,
            }

        if verb == "POST" and route == "/azom-ops/v1/seo/meta":
            body = payload or {}
            change_id = str(body.get("change_id") or "").strip()
            if not change_id:
                raise _bridge_error(400, "Invalid change_id")
            obj_type = str(body.get("object_type") or "").strip().lower()
            if obj_type not in _SEO_ALLOWED_OBJECT_TYPES:
                raise _bridge_error(400, "Unsupported object_type")
            try:
                obj_id = int(body.get("object_id") or 0)
            except (TypeError, ValueError):
                obj_id = 0
            if obj_id <= 0:
                raise _bridge_error(400, "Invalid object_id")
            fields = body.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise _bridge_error(400, "Missing fields object")
            disallowed = [k for k in fields if str(k) not in _SEO_ALLOWED_META_FIELDS]
            if disallowed:
                raise _bridge_error(400, f"Disallowed SEO meta field: {disallowed[0]}")
            # Validate all fields before any mutation (parity with PHP class-seo.php)
            sanitized_fields: dict[str, str] = {}
            for k, v in fields.items():
                key_s = str(k)
                val_s = "" if v is None else str(v)
                if key_s == "slug":
                    clean_slug = re.sub(r"[^a-z0-9-]+", "-", val_s.strip().lower()).strip("-")
                    if not clean_slug:
                        raise _bridge_error(400, "Invalid slug")
                    sanitized_fields[key_s] = clean_slug
                else:
                    sanitized_fields[key_s] = val_s
            row = self.seo_entities_store.get((obj_type, obj_id))
            if row is None:
                raise _bridge_error(404, f"Target entity not found: {obj_type}:{obj_id}")
            before = self._seo_entity_snapshot(row)
            before_sha = _canonical_seo_sha256(before)
            expected_sha = str(body.get("expected_before_sha256") or "").strip()
            if expected_sha and expected_sha != before_sha:
                raise _bridge_error(
                    409,
                    "Stale SEO meta digest mismatch",
                    {"ok": False, "error": "Stale SEO meta digest mismatch", "before_sha256": before_sha},
                )
            for k, v in sanitized_fields.items():
                row[k] = v
            self.cache_flushes += 1
            after = self._seo_entity_snapshot(row)
            after_sha = _canonical_seo_sha256(after)
            return {
                "ok": True,
                "change_id": change_id,
                "seo_plugin": self.seo_plugin,
                "object_type": obj_type,
                "object_id": obj_id,
                "before": before,
                "after": after,
                "before_sha256": before_sha,
                "after_sha256": after_sha,
            }

        if verb == "GET" and route == "/azom-ops/v1/seo/media":
            q = params or {}
            raw_mid = q.get("media_id")
            if raw_mid is not None and str(raw_mid).strip() != "":
                try:
                    target_mid = int(raw_mid)
                except (TypeError, ValueError):
                    target_mid = 0
                if target_mid <= 0:
                    raise _bridge_error(400, "Invalid media_id")
                row = self.seo_media_store.get(target_mid)
                rows = (
                    [{**dict(row), "before_sha256": _canonical_seo_sha256(self._seo_media_snapshot(row))}]
                    if row is not None
                    else []
                )
                return {
                    "ok": True,
                    "media": rows,
                    "total": len(rows),
                }
            limit = max(1, min(100, int(q.get("limit") or 100)))
            page = max(1, int(q.get("page") or 1))
            rows = [
                {
                    **dict(row),
                    "before_sha256": _canonical_seo_sha256(self._seo_media_snapshot(row)),
                }
                for _mid, row in sorted(self.seo_media_store.items())
            ]
            total = len(rows)
            start = (page - 1) * limit
            return {
                "ok": True,
                "media": rows[start : start + limit],
                "total": total,
            }

        if verb == "POST" and route == "/azom-ops/v1/seo/media":
            body = payload or {}
            change_id = str(body.get("change_id") or "").strip()
            if not change_id:
                raise _bridge_error(400, "Invalid change_id")
            try:
                media_id = int(body.get("media_id") or 0)
            except (TypeError, ValueError):
                media_id = 0
            if media_id <= 0:
                raise _bridge_error(400, "Invalid media_id")
            fields = body.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise _bridge_error(400, "Missing fields object")
            disallowed = [k for k in fields if str(k) not in _SEO_ALLOWED_MEDIA_FIELDS]
            if disallowed:
                raise _bridge_error(400, f"Disallowed media field: {disallowed[0]}")
            row = self.seo_media_store.get(media_id)
            if row is None:
                raise _bridge_error(404, f"Media attachment not found: {media_id}")
            before = self._seo_media_snapshot(row)
            before_sha = _canonical_seo_sha256(before)
            expected_sha = str(body.get("expected_before_sha256") or "").strip()
            if expected_sha and expected_sha != before_sha:
                raise _bridge_error(
                    409,
                    "Stale media digest mismatch",
                    {"ok": False, "error": "Stale media digest mismatch", "before_sha256": before_sha},
                )
            for k, v in fields.items():
                row[str(k)] = "" if v is None else str(v)
            self.cache_flushes += 1
            after = self._seo_media_snapshot(row)
            after_sha = _canonical_seo_sha256(after)
            return {
                "ok": True,
                "change_id": change_id,
                "media_id": media_id,
                "before": before,
                "after": after,
                "before_sha256": before_sha,
                "after_sha256": after_sha,
            }

        raise SecurityError(f"Unhandled mock bridge route: {verb} {route}")


class PinnedBridgeHttpTransport:
    """DNS-pinned, HMAC-signed HTTPS transport to live ``azom-ops-bridge``."""

    is_mock: bool = False

    def __init__(self, *, base_url: str, secret: str, timeout: float = 12.0) -> None:
        parsed = urlsplit(str(base_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise SecurityError("Bridge base_url must be a clean https:// origin")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        self.secret = str(secret or "").strip()
        if len(self.secret) < _MIN_SECRET_LEN:
            raise SecurityError("Bridge secret missing or shorter than 32 characters")
        self.timeout = float(timeout)

    def call(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        verb = method.upper()
        body_bytes = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else b""
        )
        headers = sign_bridge_request(
            secret=self.secret,
            method=verb,
            route=route,
            params=params,
            body=body_bytes,
        )
        signed_target = canonical_signed_target(route, params)
        url = f"{self.base_url}/wp-json{signed_target}"
        try:
            status, _resp_headers, raw, _final_url = pinned_https_request(
                url,
                method=verb,
                headers=headers,
                body=body_bytes if (verb in {"POST", "PUT", "PATCH"} or body_bytes) else None,
                connect_timeout=5.0,
                read_timeout=self.timeout,
                max_redirects=0,
                allowed_auth_origins=(self.base_url,),
            )
        except Exception as exc:
            from ecom_ops.wp_ops.credentials import redact_secret_text

            raise SecurityError(redact_secret_text(f"Bridge transport error: {exc}")) from exc
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise SecurityError("Bridge returned non-JSON response") from exc
        if status >= 400:
            from ecom_ops.wp_ops.credentials import redact_secret_text

            err_msg = decoded.get("error") or decoded.get("message") or f"HTTP {status}"
            extra = " (auto_reverted=true)" if decoded.get("auto_reverted") is True else ""
            err = SecurityError(redact_secret_text(f"Bridge error ({status}){extra}: {err_msg}"))
            err.bridge_response = decoded
            raise err
        if not isinstance(decoded, dict):
            raise SecurityError("Bridge returned unexpected response shape")
        return decoded


class WpBridgeClient:
    """High-level client for ``azom-ops-bridge`` WordPress plugin endpoints."""

    def __init__(self, *, site_id: str, transport: BridgeTransport) -> None:
        self.site_id = site_id
        self.transport = transport

    @property
    def is_live(self) -> bool:
        return not getattr(self.transport, "is_mock", True)

    def health(self) -> dict[str, Any]:
        return self.transport.call("GET", "/azom-ops/v1/health")

    def revisions(self, *, post_id: int | None = None) -> dict[str, Any]:
        params = {"post_id": int(post_id)} if post_id else None
        return self.transport.call("GET", "/azom-ops/v1/revisions", params=params)

    def translations(self, *, post_id: int | None = None) -> dict[str, Any]:
        params = {"post_id": int(post_id)} if post_id else None
        return self.transport.call("GET", "/azom-ops/v1/translations", params=params)

    def get_snapshot(
        self,
        *,
        kind: str,
        slug: str = "",
        path: str = "",
        item_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"kind": kind}
        if slug:
            params["slug"] = slug
        if path:
            params["path"] = path
        if item_id:
            params["id"] = int(item_id)
        return self.transport.call("GET", "/azom-ops/v1/snapshot", params=params)

    def apply_files(
        self,
        *,
        change_id: str,
        kind: str,
        slug: str,
        entries: list[dict[str, Any]],
        files: dict[str, bytes],
        expected_before_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload_files: list[dict[str, Any]] = []
        hashes = expected_before_hashes or {}
        for item in entries:
            rel = str(item.get("path") or "")
            op = str(item.get("op") or "updated")
            row: dict[str, Any] = {"path": rel, "op": op}
            if rel in hashes:
                row["expected_before_sha256"] = hashes[rel]
            if op != "deleted":
                raw = files.get(rel, b"")
                row["content_b64"] = base64.b64encode(raw).decode("ascii")
            payload_files.append(row)
        return self.transport.call(
            "POST",
            "/azom-ops/v1/apply",
            payload={
                "change_id": change_id,
                "kind": kind,
                "slug": slug,
                "files": payload_files,
            },
        )

    def rollback(
        self,
        *,
        change_id: str,
        kind: str,
        slug: str,
        entries: list[dict[str, Any]],
        files: dict[str, bytes],
    ) -> dict[str, Any]:
        payload_files: list[dict[str, Any]] = []
        for item in entries:
            rel = str(item.get("path") or "")
            op = str(item.get("op") or "updated")
            row: dict[str, Any] = {"path": rel, "op": op}
            if op != "deleted":
                raw = files.get(rel, b"")
                row["content_b64"] = base64.b64encode(raw).decode("ascii")
            payload_files.append(row)
        return self.transport.call(
            "POST",
            "/azom-ops/v1/rollback",
            payload={
                "change_id": change_id,
                "kind": kind,
                "slug": slug,
                "files": payload_files,
            },
        )

    def flush_cache(self) -> dict[str, Any]:
        return self.transport.call("POST", "/azom-ops/v1/cache/flush", payload={})

    def set_maintenance(self, *, enabled: bool, duration_seconds: int = 300) -> dict[str, Any]:
        return self.transport.call(
            "POST",
            "/azom-ops/v1/maintenance",
            payload={"enabled": bool(enabled), "duration_seconds": int(duration_seconds)},
        )

    def plugin_inventory(self, *, include_updates: bool = True) -> dict[str, Any]:
        return self.transport.call(
            "GET",
            "/azom-ops/v1/plugins/inventory",
            params={"include_updates": "1" if include_updates else "0"},
        )

    def update_plugin(
        self,
        *,
        change_id: str,
        plugin_file: str,
        target_version: str | None = None,
        backup_before: bool = True,
        probe_health: bool = True,
        canary_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "change_id": change_id,
            "plugin_file": plugin_file,
            "backup_before": bool(backup_before),
            "probe_health": bool(probe_health),
        }
        if target_version is not None:
            payload["target_version"] = str(target_version)
        if canary_paths is not None:
            payload["canary_paths"] = [str(p) for p in canary_paths]
        try:
            return self.transport.call("POST", "/azom-ops/v1/plugins/update", payload=payload)
        except SecurityError as exc:
            resp = getattr(exc, "bridge_response", None)
            if isinstance(resp, dict) and "auto_reverted" in resp:
                return resp
            raise

    def rollback_plugin(
        self,
        *,
        change_id: str,
        plugin_file: str,
        backup_id: str,
        probe_health: bool = True,
    ) -> dict[str, Any]:
        return self.transport.call(
            "POST",
            "/azom-ops/v1/plugins/rollback",
            payload={
                "change_id": change_id,
                "plugin_file": plugin_file,
                "backup_id": backup_id,
                "probe_health": bool(probe_health),
            },
        )

    def set_plugin_autoupdates(self, *, changes: list[dict[str, Any]]) -> dict[str, Any]:
        return self.transport.call(
            "POST",
            "/azom-ops/v1/plugins/autoupdate",
            payload={"changes": list(changes)},
        )

    def toggle_plugin(
        self,
        *,
        plugin_file: str,
        active: bool,
        probe_health: bool = True,
    ) -> dict[str, Any]:
        try:
            return self.transport.call(
                "POST",
                "/azom-ops/v1/plugins/toggle",
                payload={
                    "plugin_file": plugin_file,
                    "active": bool(active),
                    "probe_health": bool(probe_health),
                },
            )
        except SecurityError as exc:
            resp = getattr(exc, "bridge_response", None)
            if isinstance(resp, dict) and "auto_reverted" in resp:
                return resp
            raise

    def plugin_diagnostics(
        self,
        *,
        hooks: list[str] | None = None,
        log_lines: int = 80,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"log_lines": int(log_lines)}
        if hooks:
            params["hooks"] = ",".join(str(h).strip() for h in hooks if str(h).strip())
        return self.transport.call("GET", "/azom-ops/v1/plugins/diagnostics", params=params)

    def seo_overview(self) -> dict[str, Any]:
        """Return detected SEO plugin, indexing flags, sitemaps, and entity counts."""
        return self.transport.call("GET", "/azom-ops/v1/seo/overview")

    def seo_entities(
        self,
        *,
        object_type: str = "all",
        limit: int = 50,
        page: int = 1,
        object_id: int | None = None,
    ) -> dict[str, Any]:
        """List normalized SEO metadata, links, and image counts for site entities."""
        params: dict[str, Any] = {
            "object_type": str(object_type or "all").strip().lower(),
            "limit": max(1, min(100, int(limit))),
            "page": max(1, int(page)),
        }
        if object_id is not None:
            params["object_id"] = int(object_id)
        return self.transport.call("GET", "/azom-ops/v1/seo/entities", params=params)

    def seo_update_meta(
        self,
        *,
        change_id: str,
        object_type: str,
        object_id: int,
        fields: dict[str, Any],
        expected_before_sha256: str = "",
    ) -> dict[str, Any]:
        """Update allowlisted SEO meta fields with optimistic SHA-256 locking."""
        payload: dict[str, Any] = {
            "change_id": str(change_id),
            "object_type": str(object_type).strip().lower(),
            "object_id": int(object_id),
            "fields": dict(fields),
        }
        if expected_before_sha256:
            payload["expected_before_sha256"] = str(expected_before_sha256).strip()
        return self.transport.call("POST", "/azom-ops/v1/seo/meta", payload=payload)

    def seo_media_inventory(
        self,
        *,
        limit: int = 100,
        page: int = 1,
        media_id: int | None = None,
    ) -> dict[str, Any]:
        """List media attachment SEO inventory (alt_text, filename, dimensions, filesize)."""
        params: dict[str, Any] = {
            "limit": max(1, min(100, int(limit))),
            "page": max(1, int(page)),
        }
        if media_id is not None:
            params["media_id"] = int(media_id)
        return self.transport.call("GET", "/azom-ops/v1/seo/media", params=params)

    def seo_update_media(
        self,
        *,
        change_id: str,
        media_id: int,
        fields: dict[str, Any],
        expected_before_sha256: str = "",
    ) -> dict[str, Any]:
        """Update media alt_text/title/caption/description with optimistic SHA-256 locking."""
        payload: dict[str, Any] = {
            "change_id": str(change_id),
            "media_id": int(media_id),
            "fields": dict(fields),
        }
        if expected_before_sha256:
            payload["expected_before_sha256"] = str(expected_before_sha256).strip()
        return self.transport.call("POST", "/azom-ops/v1/seo/media", payload=payload)


def bridge_client_for_site(site: Site, *, force_mock: bool | None = None) -> WpBridgeClient | None:
    """Return a ``WpBridgeClient`` for ``site`` (mock when mock mode, else only if enabled+secret valid)."""
    use_mock = is_mock_mode() if force_mock is None else bool(force_mock)
    if use_mock:
        return WpBridgeClient(
            site_id=site.id,
            transport=InMemoryWpBridgeTransport(site_id=site.id, base_url=site.canonical_url),
        )
    if not bridge_enabled():
        return None
    _name, secret = resolve_bridge_secret(site)
    if len(secret) < _MIN_SECRET_LEN:
        return None
    return WpBridgeClient(
        site_id=site.id,
        transport=PinnedBridgeHttpTransport(base_url=site.canonical_url, secret=secret),
    )


__all__ = [
    "BridgeTransport",
    "InMemoryWpBridgeTransport",
    "PinnedBridgeHttpTransport",
    "WpBridgeClient",
    "bridge_client_for_site",
    "bridge_enabled",
    "bridge_secret_env_name",
    "resolve_bridge_secret",
    "sign_bridge_request",
]
