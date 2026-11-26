"""Compatibility facade for WordPress review template knowledge.

Canonical implementation lives in ``ecom_ops.wp_ops.review.knowledge``.
This module has no IO and does not decide who may read a site.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.knowledge import (
    BLOCK_CANDIDATES,
    CLASSIC_POSTS,
    ENQUEUE,
    HOOKS,
    TEMPLATE_CONTEXTS,
    hierarchy_candidates,
    template_candidates,
)

__all__ = [
    "BLOCK_CANDIDATES",
    "CLASSIC_POSTS",
    "ENQUEUE",
    "HOOKS",
    "TEMPLATE_CONTEXTS",
    "hierarchy_candidates",
    "template_candidates",
]
