"""WordPress review catalog for classic themes, block themes, and WooCommerce overrides.

This module has no IO and does not decide who may read a site.
``hierarchy_candidates`` is the only candidate order.
"""

from __future__ import annotations

import re
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
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


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

    classic: list[str] = []
    block: list[str] = []
    woo: list[str] = []
    note_parts: list[str] = []

    if ctx == "front_page":
        classic.append("front-page.php")
        block.append("templates/front-page.html")
        if safe_slug:
            classic.extend([f"page-{safe_slug}.php", "page.php", "singular.php"])
            block.extend(
                [
                    f"templates/page-{safe_slug}.html",
                    "templates/page.html",
                    "templates/singular.html",
                ]
            )
        classic.extend(["home.php", "index.php"])
        block.extend(["templates/home.html", "templates/index.html"])
        note_parts.append("front-page overrides home/page when present.")
    elif ctx == "home":
        classic = ["home.php", "index.php"]
        block = ["templates/home.html", "templates/index.html"]
        note_parts.append("Posts index falls back from home to index.")
    elif ctx == "single":
        if safe_post_type and safe_slug:
            classic.extend([f"single-{safe_post_type}-{safe_slug}.php", f"single-{safe_post_type}.php"])
            block.extend(
                [
                    f"templates/single-{safe_post_type}-{safe_slug}.html",
                    f"templates/single-{safe_post_type}.html",
                ]
            )
        elif safe_post_type:
            classic.append(f"single-{safe_post_type}.php")
            block.append(f"templates/single-{safe_post_type}.html")
        elif safe_slug:
            classic.extend([f"single-post-{safe_slug}.php", "single-post.php"])
            block.extend([f"templates/single-post-{safe_slug}.html", "templates/single-post.html"])
        classic.extend(["single.php", "singular.php", "index.php"])
        block.extend(["templates/single.html", "templates/singular.html", "templates/index.html"])
        if safe_post_type == "product":
            woo.extend(["woocommerce/single-product.php", "woocommerce/content-single-product.php"])
        note_parts.append("Single post hierarchy ordered from post_type+slug to index.")
    elif ctx == "page":
        if safe_slug:
            classic.append(f"page-{safe_slug}.php")
            block.append(f"templates/page-{safe_slug}.html")
        classic.extend(["page.php", "singular.php", "index.php"])
        block.extend(["templates/page.html", "templates/singular.html", "templates/index.html"])
        note_parts.append("Custom page template assignments in DB require file verification.")
    elif ctx == "singular":
        classic = ["singular.php", "index.php"]
        block = ["templates/singular.html", "templates/index.html"]
        note_parts.append("Generic singular fallback before index.")
    elif ctx == "archive":
        if safe_post_type:
            classic.append(f"archive-{safe_post_type}.php")
            block.append(f"templates/archive-{safe_post_type}.html")
        classic.extend(["archive.php", "index.php"])
        block.extend(["templates/archive.html", "templates/index.html"])
        if safe_post_type == "product":
            woo.extend(["woocommerce/archive-product.php", "woocommerce/content-product.php"])
        note_parts.append("Archive hierarchy checks post_type archive before generic archive.")
    elif ctx == "category":
        if safe_slug:
            classic.append(f"category-{safe_slug}.php")
            block.append(f"templates/category-{safe_slug}.html")
        classic.extend(["category.php", "archive.php", "index.php"])
        block.extend(["templates/category.html", "templates/archive.html", "templates/index.html"])
        note_parts.append("Category archive hierarchy.")
    elif ctx == "tag":
        if safe_slug:
            classic.append(f"tag-{safe_slug}.php")
            block.append(f"templates/tag-{safe_slug}.html")
        classic.extend(["tag.php", "archive.php", "index.php"])
        block.extend(["templates/tag.html", "templates/archive.html", "templates/index.html"])
        note_parts.append("Tag archive hierarchy.")
    elif ctx == "taxonomy":
        if safe_taxonomy and safe_slug:
            classic.extend([f"taxonomy-{safe_taxonomy}-{safe_slug}.php", f"taxonomy-{safe_taxonomy}.php"])
            block.extend(
                [
                    f"templates/taxonomy-{safe_taxonomy}-{safe_slug}.html",
                    f"templates/taxonomy-{safe_taxonomy}.html",
                ]
            )
        elif safe_taxonomy:
            classic.append(f"taxonomy-{safe_taxonomy}.php")
            block.append(f"templates/taxonomy-{safe_taxonomy}.html")
        classic.extend(["taxonomy.php", "archive.php", "index.php"])
        block.extend(["templates/taxonomy.html", "templates/archive.html", "templates/index.html"])
        if safe_taxonomy == "product_cat":
            woo.extend(
                [
                    "woocommerce/taxonomy-product-cat.php",
                    "woocommerce/taxonomy-product_cat.php",
                    "woocommerce/archive-product.php",
                ]
            )
        elif safe_taxonomy == "product_tag":
            woo.extend(
                [
                    "woocommerce/taxonomy-product-tag.php",
                    "woocommerce/taxonomy-product_tag.php",
                    "woocommerce/archive-product.php",
                ]
            )
        note_parts.append("Custom taxonomy hierarchy ordered by taxonomy+term to archive.")
    elif ctx == "author":
        if safe_slug:
            classic.append(f"author-{safe_slug}.php")
            block.append(f"templates/author-{safe_slug}.html")
        classic.extend(["author.php", "archive.php", "index.php"])
        block.extend(["templates/author.html", "templates/archive.html", "templates/index.html"])
        note_parts.append("Author archive hierarchy.")
    elif ctx == "date":
        classic = ["date.php", "archive.php", "index.php"]
        block = ["templates/date.html", "templates/archive.html", "templates/index.html"]
        note_parts.append("Date archive hierarchy.")
    elif ctx == "search":
        classic = ["search.php", "index.php"]
        block = ["templates/search.html", "templates/index.html"]
        note_parts.append("Search results hierarchy.")
    elif ctx == "404":
        classic = ["404.php", "index.php"]
        block = ["templates/404.html", "templates/index.html"]
        note_parts.append("Not-found 404 hierarchy.")
    elif ctx == "attachment":
        if safe_slug:
            classic.append(f"attachment-{safe_slug}.php")
            block.append(f"templates/attachment-{safe_slug}.html")
        classic.extend(["attachment.php", "single.php", "singular.php", "index.php"])
        block.extend(
            [
                "templates/attachment.html",
                "templates/single.html",
                "templates/singular.html",
                "templates/index.html",
            ]
        )
        note_parts.append("Attachment hierarchy falls back through single/singular to index.")
    elif ctx == "woo_shop":
        classic = ["woocommerce.php", "archive-product.php", "archive.php", "index.php"]
        block = ["templates/archive-product.html", "templates/archive.html", "templates/index.html"]
        woo = [
            "woocommerce/archive-product.php",
            "woocommerce/content-product.php",
            "woocommerce/loop/loop-start.php",
        ]
        note_parts.append("WooCommerce shop prefers theme woocommerce/archive-product.php override.")
    elif ctx == "woo_product":
        if safe_slug:
            classic.append(f"single-product-{safe_slug}.php")
            block.append(f"templates/single-product-{safe_slug}.html")
        classic.extend(["woocommerce.php", "single-product.php", "single.php", "singular.php", "index.php"])
        block.extend(
            [
                "templates/single-product.html",
                "templates/single.html",
                "templates/singular.html",
                "templates/index.html",
            ]
        )
        woo = ["woocommerce/single-product.php", "woocommerce/content-single-product.php"]
        note_parts.append("WooCommerce product page uses woocommerce/single-product.php override when present.")
    elif ctx == "woo_category":
        if safe_slug:
            classic.append(f"taxonomy-product_cat-{safe_slug}.php")
            block.append(f"templates/taxonomy-product_cat-{safe_slug}.html")
        classic.extend(
            [
                "taxonomy-product_cat.php",
                "woocommerce.php",
                "archive-product.php",
                "taxonomy.php",
                "archive.php",
                "index.php",
            ]
        )
        block.extend(
            [
                "templates/taxonomy-product_cat.html",
                "templates/archive-product.html",
                "templates/taxonomy.html",
                "templates/archive.html",
                "templates/index.html",
            ]
        )
        woo = [
            "woocommerce/taxonomy-product-cat.php",
            "woocommerce/taxonomy-product_cat.php",
            "woocommerce/archive-product.php",
        ]
        note_parts.append("WooCommerce product category archive hierarchy.")
    elif ctx == "woo_tag":
        if safe_slug:
            classic.append(f"taxonomy-product_tag-{safe_slug}.php")
            block.append(f"templates/taxonomy-product_tag-{safe_slug}.html")
        classic.extend(
            [
                "taxonomy-product_tag.php",
                "woocommerce.php",
                "archive-product.php",
                "taxonomy.php",
                "archive.php",
                "index.php",
            ]
        )
        block.extend(
            [
                "templates/taxonomy-product_tag.html",
                "templates/archive-product.html",
                "templates/taxonomy.html",
                "templates/archive.html",
                "templates/index.html",
            ]
        )
        woo = [
            "woocommerce/taxonomy-product-tag.php",
            "woocommerce/taxonomy-product_tag.php",
            "woocommerce/archive-product.php",
        ]
        note_parts.append("WooCommerce product tag archive hierarchy.")
    elif ctx == "woo_cart":
        if safe_slug:
            classic.append(f"page-{safe_slug}.php")
            block.append(f"templates/page-{safe_slug}.html")
        classic.extend(["page-cart.php", "page.php", "singular.php", "index.php"])
        block.extend(
            [
                "templates/page-cart.html",
                "templates/cart.html",
                "templates/page.html",
                "templates/singular.html",
                "templates/index.html",
            ]
        )
        woo = [
            "woocommerce/cart/cart.php",
            "woocommerce/cart/cart-totals.php",
            "woocommerce/cart/cart-empty.php",
        ]
        note_parts.append("Cart renders via page wrapper plus WooCommerce cart block or woocommerce/cart/*.php overrides.")
    elif ctx == "woo_checkout":
        if safe_slug:
            classic.append(f"page-{safe_slug}.php")
            block.append(f"templates/page-{safe_slug}.html")
        classic.extend(["page-checkout.php", "page.php", "singular.php", "index.php"])
        block.extend(
            [
                "templates/page-checkout.html",
                "templates/checkout.html",
                "templates/page.html",
                "templates/singular.html",
                "templates/index.html",
            ]
        )
        woo = [
            "woocommerce/checkout/form-checkout.php",
            "woocommerce/checkout/review-order.php",
            "woocommerce/checkout/thankyou.php",
            "woocommerce/checkout/payment.php",
        ]
        note_parts.append(
            "Checkout renders via page wrapper plus WooCommerce Checkout block or woocommerce/checkout/*.php overrides."
        )
    elif ctx == "woo_account":
        if safe_slug:
            classic.append(f"page-{safe_slug}.php")
            block.append(f"templates/page-{safe_slug}.html")
        classic.extend(["page-my-account.php", "page.php", "singular.php", "index.php"])
        block.extend(
            [
                "templates/page-my-account.html",
                "templates/page.html",
                "templates/singular.html",
                "templates/index.html",
            ]
        )
        woo = [
            "woocommerce/myaccount/my-account.php",
            "woocommerce/myaccount/dashboard.php",
            "woocommerce/myaccount/orders.php",
            "woocommerce/myaccount/form-login.php",
        ]
        note_parts.append("My Account renders via page wrapper and woocommerce/myaccount/*.php overrides.")

    if safe_theme:
        note_parts.append(f"Evaluated for theme '{safe_theme}'.")

    return {
        "context": ctx,
        "candidates": _dedup(classic),
        "block_candidates": _dedup(block),
        "woo_candidates": _dedup(woo),
        "notes": " ".join(note_parts),
    }
