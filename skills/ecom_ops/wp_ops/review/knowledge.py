"""WordPress review catalog for classic themes, block themes, and WooCommerce overrides.

This module has no IO and does not decide who may read a site.
``hierarchy_candidates`` is the only candidate order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CLASSIC_POSTS = ("front-page.php", "home.php", "index.php")
_ASSIGNED_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_WP_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
BLOCK_CANDIDATES = (
    "templates/front-page.html",
    "templates/home.html",
    "templates/index.html",
)
HOOKS = ("template_include", "template_redirect", "pre_get_posts")
ENQUEUE = ("wp_enqueue_script", "wp_enqueue_style")
TEMPLATE_CONTEXTS: tuple[str, ...] = (
    "front_page",
    "home",
    "single",
    "page",
    "singular",
    "archive",
    "category",
    "tag",
    "taxonomy",
    "author",
    "date",
    "search",
    "404",
    "attachment",
    "woo_shop",
    "woo_product",
    "woo_category",
    "woo_tag",
    "woo_cart",
    "woo_checkout",
    "woo_account",
)
_SUPPORTED_CONTEXTS = frozenset(TEMPLATE_CONTEXTS)


@dataclass(frozen=True)
class _TemplateRule:
    classic: tuple[str, ...]
    block: tuple[str, ...]
    woo: tuple[str, ...] = ()
    note: str = ""
    slug_prefix: str | None = None
    post_type_prefix: str | None = None
    woo_by_post_type: tuple[tuple[str, tuple[str, ...]], ...] = ()
    woo_by_taxonomy: tuple[tuple[str, tuple[str, ...]], ...] = ()


_WOO_CAT_OVERRIDES = (
    "woocommerce/taxonomy-product-cat.php",
    "woocommerce/taxonomy-product_cat.php",
    "woocommerce/archive-product.php",
)
_WOO_TAG_OVERRIDES = (
    "woocommerce/taxonomy-product-tag.php",
    "woocommerce/taxonomy-product_tag.php",
    "woocommerce/archive-product.php",
)
_WOO_PRODUCT_OVERRIDES = (
    "woocommerce/single-product.php",
    "woocommerce/content-single-product.php",
)
_WOO_ARCHIVE_OVERRIDES = (
    "woocommerce/archive-product.php",
    "woocommerce/content-product.php",
)

_TEMPLATE_RULES: dict[str, _TemplateRule] = {
    "front_page": _TemplateRule(
        classic=("front-page.php", "home.php", "index.php"),
        block=("templates/front-page.html", "templates/home.html", "templates/index.html"),
        note="front-page overrides home/page when present.",
    ),
    "home": _TemplateRule(
        classic=("home.php", "index.php"),
        block=("templates/home.html", "templates/index.html"),
        note="Posts index falls back from home to index.",
    ),
    "single": _TemplateRule(
        classic=("single.php", "singular.php", "index.php"),
        block=("templates/single.html", "templates/singular.html", "templates/index.html"),
        note="Single post hierarchy ordered from post_type+slug to index.",
        post_type_prefix="single",
        woo_by_post_type=(("product", _WOO_PRODUCT_OVERRIDES),),
    ),
    "page": _TemplateRule(
        classic=("page.php", "singular.php", "index.php"),
        block=("templates/page.html", "templates/singular.html", "templates/index.html"),
        note="Custom page template assignments in DB require file verification.",
        slug_prefix="page",
    ),
    "singular": _TemplateRule(
        classic=("singular.php", "index.php"),
        block=("templates/singular.html", "templates/index.html"),
        note="Generic singular fallback before index.",
    ),
    "archive": _TemplateRule(
        classic=("archive.php", "index.php"),
        block=("templates/archive.html", "templates/index.html"),
        note="Archive hierarchy checks post_type archive before generic archive.",
        post_type_prefix="archive",
        woo_by_post_type=(("product", _WOO_ARCHIVE_OVERRIDES),),
    ),
    "category": _TemplateRule(
        classic=("category.php", "archive.php", "index.php"),
        block=("templates/category.html", "templates/archive.html", "templates/index.html"),
        note="Category archive hierarchy.",
        slug_prefix="category",
    ),
    "tag": _TemplateRule(
        classic=("tag.php", "archive.php", "index.php"),
        block=("templates/tag.html", "templates/archive.html", "templates/index.html"),
        note="Tag archive hierarchy.",
        slug_prefix="tag",
    ),
    "taxonomy": _TemplateRule(
        classic=("taxonomy.php", "archive.php", "index.php"),
        block=("templates/taxonomy.html", "templates/archive.html", "templates/index.html"),
        note="Custom taxonomy hierarchy ordered by taxonomy+term to archive.",
        woo_by_taxonomy=(
            ("product_cat", _WOO_CAT_OVERRIDES),
            ("product_tag", _WOO_TAG_OVERRIDES),
        ),
    ),
    "author": _TemplateRule(
        classic=("author.php", "archive.php", "index.php"),
        block=("templates/author.html", "templates/archive.html", "templates/index.html"),
        note="Author archive hierarchy.",
        slug_prefix="author",
    ),
    "date": _TemplateRule(
        classic=("date.php", "archive.php", "index.php"),
        block=("templates/date.html", "templates/archive.html", "templates/index.html"),
        note="Date archive hierarchy.",
    ),
    "search": _TemplateRule(
        classic=("search.php", "index.php"),
        block=("templates/search.html", "templates/index.html"),
        note="Search results hierarchy.",
    ),
    "404": _TemplateRule(
        classic=("404.php", "index.php"),
        block=("templates/404.html", "templates/index.html"),
        note="Not-found 404 hierarchy.",
    ),
    "attachment": _TemplateRule(
        classic=("attachment.php", "single.php", "singular.php", "index.php"),
        block=(
            "templates/attachment.html",
            "templates/single.html",
            "templates/singular.html",
            "templates/index.html",
        ),
        note="Attachment hierarchy falls back through single/singular to index.",
        slug_prefix="attachment",
    ),
    "woo_shop": _TemplateRule(
        classic=("woocommerce.php", "archive-product.php", "archive.php", "index.php"),
        block=("templates/archive-product.html", "templates/archive.html", "templates/index.html"),
        woo=(*_WOO_ARCHIVE_OVERRIDES, "woocommerce/loop/loop-start.php"),
        note="WooCommerce shop prefers theme woocommerce/archive-product.php override.",
    ),
    "woo_product": _TemplateRule(
        classic=("woocommerce.php", "single-product.php", "single.php", "singular.php", "index.php"),
        block=(
            "templates/single-product.html",
            "templates/single.html",
            "templates/singular.html",
            "templates/index.html",
        ),
        woo=_WOO_PRODUCT_OVERRIDES,
        note="WooCommerce product page uses woocommerce/single-product.php override when present.",
        slug_prefix="single-product",
    ),
    "woo_category": _TemplateRule(
        classic=(
            "taxonomy-product_cat.php",
            "woocommerce.php",
            "archive-product.php",
            "taxonomy.php",
            "archive.php",
            "index.php",
        ),
        block=(
            "templates/taxonomy-product_cat.html",
            "templates/archive-product.html",
            "templates/taxonomy.html",
            "templates/archive.html",
            "templates/index.html",
        ),
        woo=_WOO_CAT_OVERRIDES,
        note="WooCommerce product category archive hierarchy.",
        slug_prefix="taxonomy-product_cat",
    ),
    "woo_tag": _TemplateRule(
        classic=(
            "taxonomy-product_tag.php",
            "woocommerce.php",
            "archive-product.php",
            "taxonomy.php",
            "archive.php",
            "index.php",
        ),
        block=(
            "templates/taxonomy-product_tag.html",
            "templates/archive-product.html",
            "templates/taxonomy.html",
            "templates/archive.html",
            "templates/index.html",
        ),
        woo=_WOO_TAG_OVERRIDES,
        note="WooCommerce product tag archive hierarchy.",
        slug_prefix="taxonomy-product_tag",
    ),
    "woo_cart": _TemplateRule(
        classic=("page-cart.php", "page.php", "singular.php", "index.php"),
        block=(
            "templates/page-cart.html",
            "templates/cart.html",
            "templates/page.html",
            "templates/singular.html",
            "templates/index.html",
        ),
        woo=(
            "woocommerce/cart/cart.php",
            "woocommerce/cart/cart-totals.php",
            "woocommerce/cart/cart-empty.php",
        ),
        note="Cart renders via page wrapper plus WooCommerce cart block or woocommerce/cart/*.php overrides.",
        slug_prefix="page",
    ),
    "woo_checkout": _TemplateRule(
        classic=("page-checkout.php", "page.php", "singular.php", "index.php"),
        block=(
            "templates/page-checkout.html",
            "templates/checkout.html",
            "templates/page.html",
            "templates/singular.html",
            "templates/index.html",
        ),
        woo=(
            "woocommerce/checkout/form-checkout.php",
            "woocommerce/checkout/review-order.php",
            "woocommerce/checkout/thankyou.php",
            "woocommerce/checkout/payment.php",
        ),
        note="Checkout renders via page wrapper plus WooCommerce Checkout block or woocommerce/checkout/*.php overrides.",
        slug_prefix="page",
    ),
    "woo_account": _TemplateRule(
        classic=("page-my-account.php", "page.php", "singular.php", "index.php"),
        block=(
            "templates/page-my-account.html",
            "templates/page.html",
            "templates/singular.html",
            "templates/index.html",
        ),
        woo=(
            "woocommerce/myaccount/my-account.php",
            "woocommerce/myaccount/dashboard.php",
            "woocommerce/myaccount/orders.php",
            "woocommerce/myaccount/form-login.php",
        ),
        note="My Account renders via page wrapper and woocommerce/myaccount/*.php overrides.",
        slug_prefix="page",
    ),
}


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


def _clean_wp_token(value: str | None, *, field: str) -> str | None:
    """Validate and normalize a WordPress slug, post_type, taxonomy, or theme_slug."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    if not _WP_TOKEN.fullmatch(cleaned):
        raise ValueError(f"Invalid {field}: {value!r}")
    return cleaned


def _normalize_context(context: str) -> str:
    cleaned = str(context or "").strip().lower().replace("-", "_")
    if cleaned not in _SUPPORTED_CONTEXTS:
        raise ValueError(f"Unsupported template context: {context!r}")
    return cleaned


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _dynamic_prefixes(
    ctx: str,
    rule: _TemplateRule,
    *,
    slug: str | None,
    post_type: str | None,
    taxonomy: str | None,
) -> tuple[list[str], list[str]]:
    classic_pre: list[str] = []
    block_pre: list[str] = []
    if ctx == "single":
        if post_type and slug:
            names = (f"single-{post_type}-{slug}", f"single-{post_type}")
        elif post_type:
            names = (f"single-{post_type}",)
        elif slug:
            names = (f"single-post-{slug}", "single-post")
        else:
            names = ()
        classic_pre.extend(f"{name}.php" for name in names)
        block_pre.extend(f"templates/{name}.html" for name in names)
    elif ctx == "taxonomy":
        if taxonomy and slug:
            names = (f"taxonomy-{taxonomy}-{slug}", f"taxonomy-{taxonomy}")
        elif taxonomy:
            names = (f"taxonomy-{taxonomy}",)
        else:
            names = ()
        classic_pre.extend(f"{name}.php" for name in names)
        block_pre.extend(f"templates/{name}.html" for name in names)
    elif rule.post_type_prefix and post_type:
        name = f"{rule.post_type_prefix}-{post_type}"
        classic_pre.append(f"{name}.php")
        block_pre.append(f"templates/{name}.html")
    elif rule.slug_prefix and slug:
        name = f"{rule.slug_prefix}-{slug}"
        classic_pre.append(f"{name}.php")
        block_pre.append(f"templates/{name}.html")
    return classic_pre, block_pre


def template_candidates(
    context: str,
    slug: str | None = None,
    post_type: str | None = None,
    taxonomy: str | None = None,
    theme_slug: str | None = None,
) -> dict[str, Any]:
    """Return ordered classic PHP, block HTML, and WooCommerce override candidates."""
    ctx = _normalize_context(context)
    safe_slug = _clean_wp_token(slug, field="slug")
    safe_post_type = _clean_wp_token(post_type, field="post_type")
    safe_taxonomy = _clean_wp_token(taxonomy, field="taxonomy")
    safe_theme = _clean_wp_token(theme_slug, field="theme_slug")

    rule = _TEMPLATE_RULES[ctx]
    if ctx == "front_page" and safe_slug:
        classic = [
            "front-page.php",
            f"page-{safe_slug}.php",
            "page.php",
            "singular.php",
            "home.php",
            "index.php",
        ]
        block = [
            "templates/front-page.html",
            f"templates/page-{safe_slug}.html",
            "templates/page.html",
            "templates/singular.html",
            "templates/home.html",
            "templates/index.html",
        ]
    else:
        classic_pre, block_pre = _dynamic_prefixes(
            ctx,
            rule,
            slug=safe_slug,
            post_type=safe_post_type,
            taxonomy=safe_taxonomy,
        )
        classic = [*classic_pre, *rule.classic]
        block = [*block_pre, *rule.block]

    woo = list(rule.woo)
    for key, extras in rule.woo_by_post_type:
        if safe_post_type == key:
            woo.extend(extras)
    for key, extras in rule.woo_by_taxonomy:
        if safe_taxonomy == key:
            woo.extend(extras)

    note_parts = [rule.note] if rule.note else []
    if safe_theme:
        note_parts.append(f"Evaluated for theme '{safe_theme}'.")

    return {
        "context": ctx,
        "candidates": _dedup(classic),
        "block_candidates": _dedup(block),
        "woo_candidates": _dedup(woo),
        "notes": " ".join(note_parts),
    }


__all__ = [
    "BLOCK_CANDIDATES",
    "CLASSIC_POSTS",
    "ENQUEUE",
    "HOOKS",
    "TEMPLATE_CONTEXTS",
    "hierarchy_candidates",
    "template_candidates",
]
