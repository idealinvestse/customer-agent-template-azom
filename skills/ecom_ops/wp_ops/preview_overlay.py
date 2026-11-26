"""Compatibility facade for HTML preview overlay helpers.

Canonical implementation lives in ``ecom_ops.wp_ops.review.preview.*``,
``ecom_ops.wp_ops.html_sanitize``, and ``ecom_ops.wp_ops.file_policy``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.file_policy import (
    normalize_css_for_check as _normalize_css_for_security,
)
from ecom_ops.wp_ops.html_sanitize import (
    _is_safe_url,
    _neutralize_overlay_fragment,
    _strip_tag_event_handlers,
    sanitize_page_draft_html,
)
from ecom_ops.wp_ops.html_sanitize import (
    sanitize_rich_html as _sanitize_rich_html,
)
from ecom_ops.wp_ops.review.preview.diff_overlay import (
    _expand_php_i18n_literals,
    apply_overlay,
)
from ecom_ops.wp_ops.review.preview.product_overlay import (
    apply_product_edits_overlay,
)
from ecom_ops.wp_ops.review.preview.shell import (
    OverlayHunk,
    OverlayResult,
    SiteShell,
    compose_standalone_page,
    distinctive_token,
    extract_site_shell,
    strip_css_header,
)

__all__ = [
    "OverlayHunk",
    "OverlayResult",
    "SiteShell",
    "_expand_php_i18n_literals",
    "_is_safe_url",
    "_neutralize_overlay_fragment",
    "_normalize_css_for_security",
    "_sanitize_rich_html",
    "_strip_tag_event_handlers",
    "apply_overlay",
    "apply_product_edits_overlay",
    "compose_standalone_page",
    "distinctive_token",
    "extract_site_shell",
    "sanitize_page_draft_html",
    "strip_css_header",
]
