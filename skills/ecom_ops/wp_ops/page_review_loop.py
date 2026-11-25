"""Task-owned homepage review loop.

The four review tools run in order. Order tools, catalog page lookups,
arbitrary URLs, shell, PHP, and WP-CLI are refused and not executed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.page_review import (
    ReviewContext,
    prepare_context,
    step_content,
    step_home,
    step_preview,
    step_sources,
)

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


@dataclass
class PageReviewOutcome:
    text: str
    steps: tuple[dict[str, Any], ...]
    refused: tuple[dict[str, Any], ...] = ()
    tool_digest: str = ""
    order_tools_called: tuple[str, ...] = ()
    wrote: bool = False
    layers: dict[str, Any] = field(default_factory=dict)


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


def _refused(name: str, reason: str = "tool_refused") -> dict[str, Any]:
    return {
        "tool": str(name)[:80],
        "status": "not_run",
        "availability": "unavailable",
        "reason": reason,
        "executed": False,
        "wrote": False,
    }


def run_bounded_page_review_loop(
    *,
    site_id: str,
    actor: str,
    client: Any = None,
    transport: Any = None,
    registry_path: Path | None = None,
    data_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
    attempted: list[tuple[str, dict[str, Any]]] | None = None,
    clock: Callable[[], float] | None = None,
    max_calls: int = PAGE_REVIEW_MAX_CALLS,
    max_seconds: float = PAGE_REVIEW_MAX_SECONDS,
    target_page: str | None = None,
) -> PageReviewOutcome:
    """Run the homepage review. The task owns the tool order."""
    now = clock or time.monotonic
    started = now()
    root = data_dir if data_dir is not None else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
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
    for name in PAGE_REVIEW_TOOLS:
        elapsed = now() - started
        if calls >= max_calls or elapsed > max_seconds:
            reason = "call_budget" if calls >= max_calls else "time_budget"
            row = {
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
                row["rendered_dom"] = {
                    "status": "not_run",
                    "availability": "unavailable",
                    "desktop_screenshot": None,
                    "mobile_screenshot": None,
                    "console_errors": None,
                    "network_errors": None,
                    "verification": "not_run",
                }
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
        row["site_id"] = ctx.site_id or site_id
        steps.append(row)
        _report(on_progress, row)
    text = _format(ctx.site_id or site_id, steps)
    return PageReviewOutcome(
        text=text,
        steps=tuple(steps),
        refused=tuple(refused),
        wrote=any(bool(row.get("wrote")) for row in steps),
        layers=_layers(steps),
    )


def _refuse_attempt(name: str, arguments: dict[str, Any], ctx: ReviewContext) -> dict[str, Any]:
    """Refuse a model-supplied call. The task steps still run afterwards."""
    if name in ORDER_TOOL_NAMES or name not in PAGE_REVIEW_TOOLS:
        return _refused(name)
    if not isinstance(arguments, dict) or set(arguments) - {"site_id"}:
        return _refused(name, reason="argument_refused")
    if arguments.get("site_id") not in {None, ctx.site_id}:
        return _refused(name, reason="site_mismatch")
    return _refused(name, reason="task_owns_the_call")


def _report(on_progress: Callable[[str], None] | None, row: dict[str, Any]) -> None:
    if on_progress is None:
        return
    status = str(row.get("status") or "")
    availability = str(row.get("availability") or "")
    if status == "not_run":
        tail = "not_run/unavailable" if availability in {"", "unavailable"} else f"not_run/{availability}"
    else:
        tail = str(row.get("detail") or status)
    line = " ".join(part for part in (str(row.get("tool") or ""), str(row.get("site_id") or ""), tail) if part)
    try:
        on_progress(line[:180])
    except Exception:
        return


def _layers(steps: list[dict[str, Any]]) -> dict[str, Any]:
    database: dict[str, Any] = {"status": "not_run", "availability": "unavailable"}
    source: dict[str, Any] = {"status": "not_run", "availability": "unavailable"}
    rendered: dict[str, Any] = {
        "status": "not_run",
        "availability": "unavailable",
        "desktop_screenshot": None,
        "mobile_screenshot": None,
        "console_errors": None,
        "network_errors": None,
    }
    for row in steps:
        if row.get("tool") == "wp_page_review_content" and isinstance(row.get("database"), dict):
            database = dict(row["database"])
            database["status"] = row.get("status")
        elif row.get("tool") == "wp_page_review_content":
            database = {"status": row.get("status"), "availability": row.get("availability")}
        if row.get("tool") == "wp_page_review_sources":
            source = {
                "status": row.get("status"),
                "availability": row.get("availability"),
                "detail": row.get("detail"),
            }
        if isinstance(row.get("rendered_dom"), dict):
            rendered = dict(row["rendered_dom"])
    return {"database": database, "source_file": source, "rendered_dom": rendered}


def _format(site_id: str, steps: list[dict[str, Any]]) -> str:
    lines = [
        f"Sidgranskning för {site_id or 'ingen sajt'}.",
        "Orderverktyg kördes inte.",
        "",
        "Lager: databas, källfil, renderad DOM.",
    ]
    for row in steps:
        status = row.get("status")
        if status == "not_run":
            availability = row.get("availability") or "unavailable"
            label = "not_run/unavailable" if availability == "unavailable" else f"not_run/{availability}"
        else:
            label = str(status)
        reason = str(row.get("reason") or "")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"{row.get('tool')} — {row.get('site_id')} — {label}{suffix}")
    database = _layers(steps)["database"]
    lines.append("")
    lines.append(
        "Databas: editorinnehåll "
        + ("lästes" if database.get("editor_raw_present") else "saknas eller lästes inte")
        + ". Lagrat rendered-HTML är inte DOM."
    )
    lines.append("Källfil: " + _source_line(steps))
    rendered = _layers(steps)["rendered_dom"]
    lines.append(
        "Renderad DOM: not_run/unavailable. Desktop-bild: saknas. Mobilbild: saknas. "
        "Konsol: not_run/unavailable. Nätverk: not_run/unavailable."
        if rendered.get("status") != "read"
        else "Renderad DOM lästes."
    )
    lines.append("")
    lines.append("Fynd:")
    any_finding = False
    for row in steps:
        for finding in row.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            any_finding = True
            lines.append(_finding_line(finding))
    if not any_finding:
        lines.append("Inga fil- eller DOM-rader. Kontroller som inte kördes är not_run/unavailable.")
    text = "\n".join(lines)
    return text[:6000]


def _source_line(steps: list[dict[str, Any]]) -> str:
    for row in steps:
        if row.get("tool") != "wp_page_review_sources":
            continue
        if row.get("status") == "read":
            return str(row.get("detail") or "läst")
        return "not_run/unavailable"
    return "not_run/unavailable"


def _finding_line(finding: dict[str, Any]) -> str:
    layer = str(finding.get("layer") or "")
    path = finding.get("path") or "saknar sökväg"
    line = finding.get("line")
    where = f"{path}:{line}" if isinstance(line, int) else str(path)
    excerpt = finding.get("excerpt")
    tail = f" — {excerpt}" if isinstance(excerpt, str) and excerpt else ""
    return (
        f"- {layer} {where} risk={finding.get('risk')} "
        f"security={finding.get('security')} verification={finding.get('verification')}{tail}"
    )
