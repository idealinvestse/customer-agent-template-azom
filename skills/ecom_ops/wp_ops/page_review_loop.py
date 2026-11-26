"""Compatibility facade for the task-owned homepage review loop.

Canonical implementation lives in ``ecom_ops.wp_ops.review.page.loop`` and
``ecom_ops.wp_ops.review.page.formatters``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.page.formatters import (
    _FAILED_REASONS,
    _STEP_LABELS,
    _finding_line,
    _format,
    _layers,
    _report,
    _source_line,
    _step_outcome,
)
from ecom_ops.wp_ops.review.page.loop import (
    ORDER_TOOL_NAMES,
    PAGE_REVIEW_MAX_CALLS,
    PAGE_REVIEW_MAX_SECONDS,
    PAGE_REVIEW_TOOLS,
    PageReviewOutcome,
    execute_page_review_tool,
    run_bounded_page_review_loop,
)

__all__ = [
    "ORDER_TOOL_NAMES",
    "PAGE_REVIEW_MAX_CALLS",
    "PAGE_REVIEW_MAX_SECONDS",
    "PAGE_REVIEW_TOOLS",
    "PageReviewOutcome",
    "_FAILED_REASONS",
    "_STEP_LABELS",
    "_finding_line",
    "_format",
    "_layers",
    "_report",
    "_source_line",
    "_step_outcome",
    "execute_page_review_tool",
    "run_bounded_page_review_loop",
]
