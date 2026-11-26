"""Compatibility facade for site-bound homepage review context and steps.

Canonical implementation lives in ``ecom_ops.wp_ops.review.page.context`` and
``ecom_ops.wp_ops.review.page.steps``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.page.context import (
    _EXCERPT,
    _MAX_FILE,
    _MAX_FILES,
    _SLUG,
    ReviewContext,
    _read,
    not_run_rendered_dom,
    prepare_context,
)
from ecom_ops.wp_ops.review.page.steps import (
    step_content,
    step_home,
    step_preview,
    step_sources,
)

__all__ = [
    "ReviewContext",
    "_EXCERPT",
    "_MAX_FILE",
    "_MAX_FILES",
    "_SLUG",
    "_read",
    "not_run_rendered_dom",
    "prepare_context",
    "step_content",
    "step_home",
    "step_preview",
    "step_sources",
]
