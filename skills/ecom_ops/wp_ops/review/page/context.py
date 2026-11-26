"""Shared context, safe file reading, and evidence helpers for page reviews.

Permission checks stay here, not in the review skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
from ecom_ops.wp_ops.file_changes import sensitive_relative
from ecom_ops.wp_ops.files import PathRejected, component_dir
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review.knowledge import ENQUEUE, HOOKS
from ecom_ops.wp_ops.review.risk import review_risk

# Allow underscores alongside hyphens so theme/plugin slugs like storefront_child match.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
_MAX_FILE = 65536
_MAX_FILES = 32
_EXCERPT = 120


@dataclass
class ReviewContext:
    site_id: str
    actor_name: str
    include_evidence: bool = False
    allowed: bool = False
    site: Any = None
    client: Any = None
    transport: Any = None
    reason: str = ""
    availability: str = "unavailable"
    show_on_front: str = ""
    page_id: int = 0
    page_for_posts: int = 0
    page_slug: str = ""
    stylesheet: str = ""
    template: str = ""
    relationship: str = "unknown"
    theme_kind: str = "unknown"
    texts: dict[str, str] = field(default_factory=dict)
    file_roles: dict[str, str] = field(default_factory=dict)
    clipped: set[str] = field(default_factory=set)
    capped: set[str] = field(default_factory=set)
    page: Any = None
    io_timeout: float | None = None
    target_page: str | None = None
    data_dir: Path | None = None


def not_run_rendered_dom(*, include_verification: bool = True) -> dict[str, Any]:
    """Return the canonical not_run/unavailable rendered_dom payload."""
    payload: dict[str, Any] = {
        "status": "not_run",
        "availability": "unavailable",
        "desktop_screenshot": None,
        "mobile_screenshot": None,
        "console_errors": None,
        "network_errors": None,
    }
    if include_verification:
        payload["verification"] = "not_run"
    return payload


def _not_run(tool: str, site_id: str, reason: str, availability: str = "unavailable") -> dict[str, Any]:
    return {
        "tool": tool,
        "site_id": site_id,
        "status": "not_run",
        "availability": availability,
        "reason": reason,
        "executed": False,
        "wrote": False,
        "detail": f"{reason} {availability}".strip(),
        "findings": [],
    }


def _finding(
    ctx: ReviewContext,
    *,
    layer: str,
    path: str,
    line: int | None,
    text: str,
    verification: str,
) -> dict[str, Any]:
    shown = line if ctx.include_evidence else None
    excerpt = None
    if ctx.include_evidence and text:
        excerpt = " ".join(text.split())[:_EXCERPT]
    return {
        "layer": layer,
        "path": path,
        "line": shown,
        "excerpt": excerpt,
        "risk": review_risk(path, text[:500] if text else ""),
        "security": "untrusted_data",
        "verification": verification,
    }


def _line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return None


def _line_text(text: str, line: int) -> str:
    rows = text.splitlines()
    if line < 1 or line > len(rows):
        return ""
    return rows[line - 1].strip()


def _evidence_line(path: str, text: str) -> tuple[int, str] | None:
    """Return one real source line. The excerpt is that line, not the file start."""
    for needle in (*HOOKS, *ENQUEUE):
        line = _line_of(text, needle)
        if line is None:
            continue
        snippet = _line_text(text, line)
        if snippet:
            return line, snippet
    if review_risk(path, "") != "high":
        for number, row in enumerate(text.splitlines(), start=1):
            snippet = row.strip()
            if snippet and review_risk(path, snippet) == "high":
                return number, snippet
    for number, row in enumerate(text.splitlines(), start=1):
        snippet = row.strip()
        if snippet:
            return number, snippet
    return None


def _list(transport: Any, absolute: str) -> list[str] | None:
    try:
        names = transport.list_dir(absolute)
    except Exception:
        return None
    if not isinstance(names, list):
        return None
    clean: list[str] = []
    for name in names:
        text = str(name)
        if text in {".", ".."} or "/" in text or "\\" in text or text.startswith("."):
            continue
        if sensitive_relative(text):
            continue
        clean.append(text)
        if len(clean) >= 256:
            break
    return clean


def _read(ctx: ReviewContext, absolute: str, _relative: str) -> str | None:
    """Cache by absolute path. Parent and child themes share relative names."""
    cached = ctx.texts.get(absolute)
    if isinstance(cached, str):
        return cached
    if len(ctx.texts) >= _MAX_FILES:
        ctx.capped.add(absolute)
        return None
    try:
        data = ctx.transport.read_bytes(absolute)
    except Exception:
        return None
    if not isinstance(data, (bytes, bytearray)):
        return None
    if len(data) > _MAX_FILE:
        ctx.clipped.add(absolute)
        return None
    text = bytes(data).decode("utf-8", errors="replace")
    ctx.texts[absolute] = text
    return text


def _component(ctx: ReviewContext, slug: str) -> str | None:
    if ctx.site is None or not _SLUG.fullmatch(slug):
        return None
    try:
        return component_dir(ctx.site.files_root, "theme", slug)
    except PathRejected:
        return None


def _theme_kind(ctx: ReviewContext, root: str) -> str:
    names = _list(ctx.transport, root)
    if not names:
        return "unknown"
    if "theme.json" in names:
        return "block"
    templates = _list(ctx.transport, root + "/templates")
    if templates and any(name.endswith(".html") for name in templates):
        return "block"
    return "classic"


def _safe_relative(relative: str) -> bool:
    parts = [part for part in str(relative or "").split("/") if part != ""]
    if not parts or relative.startswith("/") or ".." in parts:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)


def _read_relative(ctx: ReviewContext, root: str, relative: str, *, role: str) -> str | None:
    if ctx.transport is None or not _safe_relative(relative):
        return None
    text = _read(ctx, root + "/" + relative, relative)
    if text is not None:
        ctx.file_roles[relative] = role
    return text


def prepare_context(
    *,
    site_id: str,
    actor: str,
    client: Any = None,
    transport: Any = None,
    registry_path: Path | None = None,
    data_dir: Path | None = None,
    target_page: str | None = None,
) -> ReviewContext:
    clean_target = str(target_page or "").strip().lower() or None
    ctx = ReviewContext(
        site_id=str(site_id or "").strip(),
        actor_name=actor,
        target_page=clean_target,
        data_dir=Path(data_dir) if data_dir is not None else None,
    )
    try:
        person = resolve_actor(actor)
    except AccessDenied:
        ctx.reason = "actor_denied"
        return ctx
    if not person.has(Permission.WP_OPS_READ):
        ctx.reason = "actor_denied"
        return ctx
    ctx.allowed = True
    ctx.include_evidence = person.has(Permission.WP_OPS_ADMIN)
    if not ctx.site_id:
        ctx.reason = "site_unselected"
        return ctx
    sites = load_sites(
        registry_path,
        data_dir=data_dir,
        apply_overlay=registry_path is None,
    )
    site = site_by_id(sites, ctx.site_id)
    if site is None:
        ctx.reason = "unknown_site"
        return ctx
    ctx.site = site
    ctx.transport = transport
    if transport is None and data_dir is not None:
        from ecom_ops.integrations.wp_files_ssh import build_file_transport

        ctx.transport = build_file_transport(site, data_dir)
    ctx.client = client
    if ctx.client is None:
        ctx.reason = "not_bound"
        ctx.availability = "not_bound"
    return ctx


__all__ = [
    "ReviewContext",
    "_EXCERPT",
    "_MAX_FILE",
    "_MAX_FILES",
    "_SLUG",
    "_component",
    "_evidence_line",
    "_finding",
    "_line_of",
    "_line_text",
    "_list",
    "_not_run",
    "_read",
    "_read_relative",
    "_safe_relative",
    "_theme_kind",
    "not_run_rendered_dom",
    "prepare_context",
]
