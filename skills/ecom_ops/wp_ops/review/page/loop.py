"""Task-owned homepage review loop.

The four review tools run in order. Order tools, catalog page lookups,
arbitrary URLs, shell, PHP, and WP-CLI are refused and not executed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.review.page.context import (
    ReviewContext,
    not_run_rendered_dom,
    prepare_context,
)
from ecom_ops.wp_ops.review.page.formatters import (
    _STEP_LABELS,
    _format,
    _layers,
    _report,
)
from ecom_ops.wp_ops.review.page.steps import (
    step_content,
    step_home,
    step_preview,
    step_sources,
)

if TYPE_CHECKING:
    from ecom_ops.bot.progress_events import ProgressCallback

PAGE_REVIEW_TOOLS = (
    "wp_page_review_home",
    "wp_page_review_content",
    "wp_page_review_sources",
    "wp_page_review_preview",
)
PAGE_REVIEW_MAX_CALLS = 4
PAGE_REVIEW_MAX_SECONDS = 45.0
ORDER_TOOL_NAMES = frozenset(
    {
        "lookup_order",
        "list_recent_orders",
        "list_products",
        "show_product",
        "list_pages",
        "show_page",
        "list_posts",
        "show_post",
        "catalog",
    }
)
_STEPS = {
    "wp_page_review_home": step_home,
    "wp_page_review_content": step_content,
    "wp_page_review_sources": step_sources,
    "wp_page_review_preview": step_preview,
}
_STEER_NOTICE = "Följdfrågan stannar i sidgranskningen. Orderverktyg kördes inte."


@dataclass
class PageReviewOutcome:
    text: str
    steps: tuple[dict[str, Any], ...]
    refused: tuple[dict[str, Any], ...] = ()
    tool_digest: str = ""
    order_tools_called: tuple[str, ...] = ()
    wrote: bool = False
    layers: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[str, ...] = ()


def _arm_io_timeout(ctx: Any, remaining: float) -> None:
    """Give the in-flight REST call the time still left in this review."""
    ctx.io_timeout = remaining
    client = getattr(ctx, "client", None)
    if client is None or not hasattr(client, "timeout"):
        return
    try:
        current = float(client.timeout)
    except (TypeError, ValueError):
        current = remaining
    client.timeout = min(current, remaining)


def _refused(name: str, reason: str = "tool_refused") -> dict[str, Any]:
    return {
        "tool": str(name)[:80],
        "status": "not_run",
        "availability": "unavailable",
        "reason": reason,
        "executed": False,
        "wrote": False,
    }


def execute_page_review_tool(
    name: str,
    arguments: dict[str, Any] | None,
    ctx: ReviewContext,
) -> dict[str, Any]:
    """Run one named review tool, or refuse anything else without IO."""
    if name not in PAGE_REVIEW_TOOLS or name in ORDER_TOOL_NAMES:
        return _refused(name)
    if not isinstance(arguments, dict) or set(arguments) - {"site_id"}:
        return _refused(name, reason="argument_refused")
    requested = arguments.get("site_id")
    if requested is not None and requested != ctx.site_id:
        return _refused(name, reason="site_mismatch")
    return _STEPS[name](ctx)


def _refuse_attempt(name: str, arguments: dict[str, Any], ctx: ReviewContext) -> dict[str, Any]:
    """Refuse a model-supplied call. The task steps still run afterwards."""
    if name in ORDER_TOOL_NAMES or name not in PAGE_REVIEW_TOOLS:
        return _refused(name)
    if not isinstance(arguments, dict) or set(arguments) - {"site_id"}:
        return _refused(name, reason="argument_refused")
    if arguments.get("site_id") not in {None, ctx.site_id}:
        return _refused(name, reason="site_mismatch")
    return _refused(name, reason="task_owns_the_call")


def run_bounded_page_review_loop(
    *,
    site_id: str,
    actor: str,
    client: Any = None,
    transport: Any = None,
    registry_path: Path | None = None,
    data_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
    on_checkpoint: Callable[[], list[str]] | None = None,
    attempted: list[tuple[str, dict[str, Any]]] | None = None,
    clock: Callable[[], float] | None = None,
    max_calls: int = PAGE_REVIEW_MAX_CALLS,
    max_seconds: float = PAGE_REVIEW_MAX_SECONDS,
    target_page: str | None = None,
) -> PageReviewOutcome:
    """Run the homepage review. The task owns the tool order."""
    from ecom_ops.bot.progress_events import ProgressEvent, emit_progress

    now = clock or time.monotonic
    started = now()
    root = data_dir_path(data_dir)
    ctx = prepare_context(
        site_id=site_id,
        actor=actor,
        client=client,
        transport=transport,
        registry_path=registry_path,
        data_dir=root,
        target_page=target_page,
    )
    refused: list[dict[str, Any]] = []
    for name, arguments in (attempted or [])[:PAGE_REVIEW_MAX_CALLS]:
        refused.append(_refuse_attempt(str(name), arguments, ctx))
    steps: list[dict[str, Any]] = []
    calls = 0
    steer_events = 0
    step_total = len(PAGE_REVIEW_TOOLS)
    for step_index, name in enumerate(PAGE_REVIEW_TOOLS, start=1):
        if on_checkpoint is not None:
            steers = on_checkpoint() or []
            if steers:
                steer_events += 1
                emit_progress(
                    on_progress,
                    ProgressEvent(
                        step_id=f"page-review-steer-{steer_events}",
                        tool="page_review",
                        label="Följdfråga mottagen",
                        phase="steer",
                        detail=ctx.site_id or site_id or None,
                        outcome=_STEER_NOTICE,
                        legacy_text=_STEER_NOTICE,
                    ),
                )
        step_id = f"page-review-{step_index}-{name}"
        target_site = ctx.site_id or site_id or None
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=name,
                label=_STEP_LABELS.get(name, name),
                phase="started",
                step_index=step_index,
                step_total=step_total,
                detail=target_site,
            ),
        )
        step_started = time.monotonic()
        elapsed = now() - started
        if calls >= max_calls or elapsed > max_seconds:
            reason = "call_budget" if calls >= max_calls else "time_budget"
            row: dict[str, Any] = {
                "tool": name,
                "site_id": ctx.site_id,
                "status": "not_run",
                "availability": "unavailable",
                "reason": reason,
                "executed": False,
                "wrote": False,
                "detail": f"{reason} unavailable",
                "findings": [],
            }
            if name == "wp_page_review_preview":
                row["rendered_dom"] = not_run_rendered_dom()
        else:
            _arm_io_timeout(ctx, max(0.0, max_seconds - elapsed))
            try:
                row = _STEPS[name](ctx)
            except TimeoutError:
                row = {
                    "tool": name,
                    "site_id": ctx.site_id,
                    "status": "not_run",
                    "availability": "unavailable",
                    "reason": "io_timeout",
                    "executed": False,
                    "wrote": False,
                    "detail": "io_timeout unavailable",
                    "findings": [],
                }
            calls += 1
        elapsed_ms = int((time.monotonic() - step_started) * 1000)
        row["site_id"] = ctx.site_id or site_id
        steps.append(row)
        _report(
            on_progress,
            row,
            step_id=step_id,
            step_index=step_index,
            step_total=step_total,
            elapsed_ms=elapsed_ms,
        )
    text = _format(ctx.site_id or site_id, steps)
    attachments: list[str] = []
    preview_row = next((row for row in steps if row.get("tool") == "wp_page_review_preview"), None)
    if isinstance(preview_row, dict) and isinstance(preview_row.get("rendered_dom"), dict):
        rdom = preview_row["rendered_dom"]
        for key in ("desktop_screenshot", "mobile_screenshot"):
            val = rdom.get(key)
            if isinstance(val, str) and val.strip() and val not in attachments:
                attachments.append(val)
    return PageReviewOutcome(
        text=text,
        steps=tuple(steps),
        refused=tuple(refused),
        wrote=any(bool(row.get("wrote")) for row in steps),
        layers=_layers(steps),
        attachments=tuple(attachments),
    )


__all__ = [
    "ORDER_TOOL_NAMES",
    "PAGE_REVIEW_MAX_CALLS",
    "PAGE_REVIEW_MAX_SECONDS",
    "PAGE_REVIEW_TOOLS",
    "PageReviewOutcome",
    "execute_page_review_tool",
    "run_bounded_page_review_loop",
]
