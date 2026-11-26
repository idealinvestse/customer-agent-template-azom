"""Compatibility facade for WordPress operations dashboard routes.

The stateless route modules live under ``infrastructure/dashboard/wp/``.
"""

from __future__ import annotations

from wp import register_wp_routes
from wp.common import (
    _KINDS,
    _PLAN_ID,
    _REVIEW_ID,
    _SITE_ID,
)
from wp.common import (
    can_drill as _can_drill,
)
from wp.common import (
    can_edit as _can_edit,
)
from wp.common import (
    can_read_wp as _can_read_wp,
)
from wp.common import (
    can_run as _can_run,
)
from wp.common import (
    can_save_ssh as _can_save_ssh,
)
from wp.common import (
    file_admin as _file_admin,
)
from wp.common import (
    file_block as _file_block,
)
from wp.common import (
    is_denied as _denied,
)
from wp.common import (
    redirect_site as _redirect_site,
)
from wp.common import (
    review_http as _review_http,
)
from wp.common import (
    review_ops as _review_ops,
)
from wp.common import (
    safe_http_url as _safe_http_url,
)
from wp.common import (
    safe_review_id as _safe_review_id,
)
from wp.common import (
    safe_variant_group as _safe_variant_group,
)
from wp.common import (
    sanitize_draft_page as _sanitize_draft_page,
)
from wp.common import (
    scrub_posted as _scrub_posted,
)
from wp.preview_routes import (
    harden_dashboard_preview_html as _harden_preview_html,
)
from wp.preview_routes import (
    preview_csp_for_site as _preview_csp_for_site,
)
from wp.review_routes import (
    build_live_overlay_comparison as _build_live_overlay_comparison,
)
from wp.review_routes import (
    render_review as _render_review,
)
from wp.review_routes import (
    site_review_plans as _site_review_plans,
)

__all__ = [
    "_KINDS",
    "_PLAN_ID",
    "_REVIEW_ID",
    "_SITE_ID",
    "_build_live_overlay_comparison",
    "_can_drill",
    "_can_edit",
    "_can_read_wp",
    "_can_run",
    "_can_save_ssh",
    "_denied",
    "_file_admin",
    "_file_block",
    "_harden_preview_html",
    "_preview_csp_for_site",
    "_redirect_site",
    "_render_review",
    "_review_http",
    "_review_ops",
    "_safe_http_url",
    "_safe_review_id",
    "_safe_variant_group",
    "_sanitize_draft_page",
    "_scrub_posted",
    "_site_review_plans",
    "register_wp_routes",
]
