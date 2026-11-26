"""Risk classification and approval guards for WordPress review workspaces."""

from __future__ import annotations

import re

from ecom_ops.wp_ops.file_policy import (
    is_safe_pure_css_file,
    normalize_css_for_check,
    risk_for,
)

ORDER_RISK_RE = re.compile(
    r"(?i)(?<![a-z])(?:shipping|stock|inventory|orders?|frakt|lager)(?![a-z])"
)
_ORDER_PARTS = frozenset({"shipping", "stock", "inventory", "order", "orders", "frakt", "lager"})
_PAGE_DRAFT_UNSAFE_RE = re.compile(
    r"<\?|<script\b|<iframe\b|<svg\b|<object\b|<embed\b|</style|"
    r"javascript:|vbscript:|expression\s*\(|url\s*\(|image-set\s*\(|"
    r"@import\b|behavior\s*:|-moz-binding\s*:|\bon[a-z0-9_-]+\s*=",
    re.I,
)


def review_risk(relative: str, text: str) -> str:
    """High-risk classifier for the review chain. ``risk_for`` stays unchanged."""
    clean = str(relative or "").strip().replace("\\", "/")
    if clean == "page_draft.json":
        raw_text = text or ""
        norm_text = normalize_css_for_check(raw_text)
        return (
            "high"
            if (_PAGE_DRAFT_UNSAFE_RE.search(raw_text) or _PAGE_DRAFT_UNSAFE_RE.search(norm_text))
            else "low"
        )
    if risk_for(relative, text) == "high":
        return "high"
    parts = [part.lower() for part in clean.split("/")]
    if any(part in _ORDER_PARTS for part in parts):
        return "high"
    if is_safe_pure_css_file(relative, text):
        return "low"
    if ORDER_RISK_RE.search(relative or "") or ORDER_RISK_RE.search(text or ""):
        return "high"
    return "low"


def tree_risk(slug: str, files: list[tuple[str, bytes]], summary: str = "") -> str:
    """Return ``'high'`` if the workspace slug, summary, or any file is high risk."""
    if review_risk(slug, summary) == "high":
        return "high"
    for rel, data in files:
        if review_risk(rel, data.decode("utf-8", errors="replace")) == "high":
            return "high"
    return "low"


def text_is_review_approval(text: str) -> bool:
    """Free text and ``action:yes`` never approve a review plan."""
    del text
    return False


__all__ = [
    "ORDER_RISK_RE",
    "review_risk",
    "text_is_review_approval",
    "tree_risk",
]
