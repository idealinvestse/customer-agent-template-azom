"""Compatibility facade for homepage source-review intent detection.

Canonical implementation lives in ``ecom_ops.wp_ops.review.page.intent``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.page.intent import (
    extract_page_review_target,
    is_page_review,
)

__all__ = [
    "extract_page_review_target",
    "is_page_review",
]
