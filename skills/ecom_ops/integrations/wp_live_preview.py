"""Compatibility facade for read-only live page fetch and isolated overlay browser preview.

Delegates implementation to ``ecom_ops.integrations.wp.*`` while preserving all
public/test-imported symbols and monkeypatch hooks on this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp import http_guard as _http_guard
from ecom_ops.integrations.wp import preview_capture as _preview_capture
from ecom_ops.integrations.wp.http_guard import (
    _MAX_HTML_BYTES,
    _PREVIEW_USER_AGENT,
    _REFUSED_PATH_TOKENS,
    _SITE_BLOCKED_SUBSTRINGS,
    _STATIC_ASSET_HOSTS,
    _TRACKER_HOSTS,
    BLOCKED_WP_PATHS,
    STATIC_ASSET_HOSTS,
    _allowed_site_hosts,
    _AllowlistedSiteRedirectHandler,
    _blocked_site_path_reason,
    _host_matches_suffix,
    _host_resolves_public,
    _is_ip_literal,
    _safe_https_parsed,
    _unquote_iteratively,
    classify_request,
    clear_live_html_cache,
    fetch_site_rest_json,
    validate_preview_path,
)
from ecom_ops.integrations.wp.png_composite import (
    _MINI_FONT_5X7,
    _compose_with_stdlib_png,
    _decode_png_rgb,
    _encode_png_rgb,
    build_comparison_composite,
)
from ecom_ops.integrations.wp.preview_capture import (
    _MAX_FULL_PAGE_HEIGHT,
    _PREFERRED_SHOT_ORDER,
    FOCUS_SECTIONS,
    PREVIEW_VIEWPORTS,
    _compute_focus_clip,
    _finalize_summary,
    _order_screenshots,
    live_preview_enabled,
    validate_focus_options,
)
from ecom_ops.integrations.wp.preview_scripts import (
    dom_probe_script as _dom_probe_script,
)
from ecom_ops.integrations.wp.preview_scripts import (
    focus_and_highlight_script as _focus_and_highlight_script,
)
from ecom_ops.integrations.wp.preview_scripts import (
    inspect_element_script as _inspect_element_script,
)
from ecom_ops.integrations.wp.preview_scripts import (
    reveal_region_script as _reveal_region_script,
)
from ecom_ops.integrations.wp.preview_scripts import (
    scroll_sweep_script as _scroll_sweep_script,
)
from ecom_ops.integrations.wp.sandbox_html import (
    _safe_js_json,
    _sandbox_base_href,
    _strip_sandbox_scripts_and_trackers,
    build_interactive_sandbox_html,
    build_sandbox_guard_script,
)
from ecom_ops.wp_ops.models import Site


def fetch_live_html(
    site: Site,
    path: str = "/",
    *,
    timeout: float = 15.0,
    max_bytes: int = _MAX_HTML_BYTES,
    use_cache: bool = False,
) -> dict[str, Any]:
    """Fetch public live HTML via HTTPS GET with no cookies and no site credentials."""
    return _http_guard.fetch_live_html(
        site,
        path,
        timeout=timeout,
        max_bytes=max_bytes,
        use_cache=use_cache,
        public_host_check=_host_resolves_public,
    )


def capture_preview(
    site: Site,
    *,
    path: str = "/",
    artifact_dir: Path,
    revisions: list[tuple[str, str]],
    highlight_token: str | None = None,
    focus_section: str | None = None,
    focus_selector: str | None = None,
    focus_text: str | None = None,
    full_page: bool = False,
    browser_binary: str | None = None,
    timeout_s: float = 45.0,
    target_url: str | None = None,
    preview_kind_override: str | None = None,
    extra_preview_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render live / overlay HTML revisions in headless Chrome with read-only Fetch interception."""
    return _preview_capture.capture_preview(
        site,
        path=path,
        artifact_dir=artifact_dir,
        revisions=revisions,
        highlight_token=highlight_token,
        focus_section=focus_section,
        focus_selector=focus_selector,
        focus_text=focus_text,
        full_page=full_page,
        browser_binary=browser_binary,
        timeout_s=timeout_s,
        target_url=target_url,
        preview_kind_override=preview_kind_override,
        extra_preview_meta=extra_preview_meta,
    )


def inspect_element(
    site: Site,
    *,
    path: str = "/",
    html_override: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    browser_binary: str | None = None,
    timeout_s: float = 30.0,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Inspect rendered DOM, computed styles, matched CSS rules, and layout health via read-only CDP."""
    return _preview_capture.inspect_element(
        site,
        path=path,
        html_override=html_override,
        selector=selector,
        text=text,
        browser_binary=browser_binary,
        timeout_s=timeout_s,
        target_url=target_url,
        fetch_html_fn=fetch_live_html,
    )


__all__ = [
    "BLOCKED_WP_PATHS",
    "FOCUS_SECTIONS",
    "PREVIEW_VIEWPORTS",
    "STATIC_ASSET_HOSTS",
    "_AllowlistedSiteRedirectHandler",
    "_MAX_FULL_PAGE_HEIGHT",
    "_MAX_HTML_BYTES",
    "_MINI_FONT_5X7",
    "_PREFERRED_SHOT_ORDER",
    "_PREVIEW_USER_AGENT",
    "_REFUSED_PATH_TOKENS",
    "_SITE_BLOCKED_SUBSTRINGS",
    "_STATIC_ASSET_HOSTS",
    "_TRACKER_HOSTS",
    "_allowed_site_hosts",
    "_blocked_site_path_reason",
    "_compose_with_stdlib_png",
    "_compute_focus_clip",
    "_decode_png_rgb",
    "_dom_probe_script",
    "_encode_png_rgb",
    "_finalize_summary",
    "_focus_and_highlight_script",
    "_host_matches_suffix",
    "_host_resolves_public",
    "_inspect_element_script",
    "_is_ip_literal",
    "_order_screenshots",
    "_reveal_region_script",
    "_safe_https_parsed",
    "_safe_js_json",
    "_sandbox_base_href",
    "_scroll_sweep_script",
    "_strip_sandbox_scripts_and_trackers",
    "_unquote_iteratively",
    "build_comparison_composite",
    "build_interactive_sandbox_html",
    "build_sandbox_guard_script",
    "capture_preview",
    "classify_request",
    "clear_live_html_cache",
    "fetch_live_html",
    "fetch_site_rest_json",
    "inspect_element",
    "live_preview_enabled",
    "validate_focus_options",
    "validate_preview_path",
]
