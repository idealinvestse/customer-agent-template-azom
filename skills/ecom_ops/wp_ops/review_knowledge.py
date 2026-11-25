"""WordPress review catalog for classic themes, block themes, and WooCommerce overrides.

This module has no IO and does not decide who may read a site.
``hierarchy_candidates`` is the only candidate order.
"""

from __future__ import annotations

import re

CLASSIC_POSTS = ("front-page.php", "home.php", "index.php")
_ASSIGNED_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
BLOCK_CANDIDATES = (
    "templates/front-page.html",
    "templates/home.html",
    "templates/index.html",
)
HOOKS = ("template_include", "template_redirect", "pre_get_posts")
ENQUEUE = ("wp_enqueue_script", "wp_enqueue_style")


def _assigned_relative(value: str) -> str:
    """Return one safe theme-relative PHP template, or an empty string."""
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.lower() == "default" or text.startswith("/"):
        return ""
    parts = [part for part in text.split("/") if part != ""]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    if any(_ASSIGNED_PART.fullmatch(part) is None for part in parts):
        return ""
    if not parts[-1].lower().endswith(".php"):
        return ""
    return "/".join(parts)


def hierarchy_candidates(
    *,
    show_on_front: str,
    block_theme: bool,
    slug: str = "",
    page_id: str = "",
    assigned_template: str = "",
) -> tuple[str, ...]:
    """Return the template candidates for one known front page.

    An unknown ``show_on_front`` returns an empty tuple. The caller must not
    invent a page or a theme file to fill that gap.
    """
    if block_theme:
        return BLOCK_CANDIDATES
    front = str(show_on_front or "").strip().lower()
    if front == "posts":
        return CLASSIC_POSTS
    if front != "page":
        return ()
    candidates = ["front-page.php"]
    assigned = _assigned_relative(assigned_template)
    if assigned and assigned not in candidates:
        candidates.append(assigned)
    safe_slug = str(slug or "").strip().lower()
    safe_id = str(page_id or "").strip()
    if safe_slug and f"page-{safe_slug}.php" not in candidates:
        candidates.append(f"page-{safe_slug}.php")
    if safe_id and f"page-{safe_id}.php" not in candidates:
        candidates.append(f"page-{safe_id}.php")
    for name in ("page.php", "singular.php", "index.php"):
        if name not in candidates:
            candidates.append(name)
    return tuple(candidates)
