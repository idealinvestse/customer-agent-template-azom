"""WordPress review catalog for classic themes, block themes, and WooCommerce overrides.

This module has no IO and does not decide who may read a site.
``hierarchy_candidates`` is the only candidate order.
"""

from __future__ import annotations

CLASSIC_POSTS = ("front-page.php", "home.php", "index.php")
BLOCK_CANDIDATES = (
    "templates/front-page.html",
    "templates/home.html",
    "templates/index.html",
)
HOOKS = ("template_include", "template_redirect", "pre_get_posts")
ENQUEUE = ("wp_enqueue_script", "wp_enqueue_style")


def hierarchy_candidates(
    *,
    show_on_front: str,
    block_theme: bool,
    slug: str = "",
    page_id: str = "",
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
    safe_slug = str(slug or "").strip().lower()
    safe_id = str(page_id or "").strip()
    if safe_slug:
        candidates.append(f"page-{safe_slug}.php")
    if safe_id:
        candidates.append(f"page-{safe_id}.php")
    candidates.extend(("page.php", "singular.php", "index.php"))
    return tuple(candidates)
