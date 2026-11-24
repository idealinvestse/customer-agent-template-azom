"""Site-bound homepage review. Database content, source files, and DOM stay separate.

The model does not choose a URL, a shell command, or a production write.
Permission checks stay here, not in the review skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
from ecom_ops.wp_ops.file_changes import rest_reads_allowed, sensitive_relative
from ecom_ops.wp_ops.files import PathRejected, component_dir
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review_knowledge import ENQUEUE, HOOKS, hierarchy_candidates
from ecom_ops.wp_ops.review_plan import review_risk

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
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
    page_slug: str = ""
    stylesheet: str = ""
    template: str = ""
    relationship: str = "unknown"
    theme_kind: str = "unknown"
    texts: dict[str, str] = field(default_factory=dict)
    file_roles: dict[str, str] = field(default_factory=dict)
    page: Any = None


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
        return None
    try:
        data = ctx.transport.read_bytes(absolute)
    except Exception:
        return None
    if not isinstance(data, (bytes, bytearray)) or len(data) > _MAX_FILE:
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


def prepare_context(
    *,
    site_id: str,
    actor: str,
    client: Any = None,
    transport: Any = None,
    registry_path: Path | None = None,
    data_dir: Path | None = None,
) -> ReviewContext:
    ctx = ReviewContext(site_id=str(site_id or "").strip(), actor_name=actor)
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
    ctx.client = client if client is not None else _live_client(site.canonical_url)
    if ctx.client is None:
        ctx.reason = "not_bound"
        ctx.availability = "not_bound"
    return ctx


def _live_client(canonical_url: str) -> Any:
    """Open the configured WordPress client only when the host is already bound."""
    from ecom_ops.wp_ops.agent_tools import component_rest_source

    if component_rest_source(canonical_url) != "live" or not rest_reads_allowed():
        return None
    from ecom_ops.integrations.wordpress import wp_client_from_env

    try:
        return wp_client_from_env(use_mock=False)
    except Exception:
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


def step_home(ctx: ReviewContext) -> dict[str, Any]:
    tool = "wp_page_review_home"
    if not ctx.allowed:
        return _not_run(tool, ctx.site_id, ctx.reason or "actor_denied")
    if ctx.site is None:
        return _not_run(tool, ctx.site_id, ctx.reason or "site_unselected")
    if ctx.client is None:
        return _not_run(tool, ctx.site_id, "not_bound", "not_bound")
    try:
        settings = ctx.client.get_settings()
        themes = ctx.client.list_themes()
    except Exception:
        return _not_run(tool, ctx.site_id, "rest_failed")
    if not isinstance(settings, dict) or not isinstance(themes, list):
        return _not_run(tool, ctx.site_id, "rest_failed")
    front = str(settings.get("show_on_front") or "").strip().lower()
    if front not in {"page", "posts"}:
        return _not_run(tool, ctx.site_id, "settings_incomplete")
    page_id = 0
    if front == "page":
        try:
            page_id = int(settings.get("page_on_front") or 0)
        except (TypeError, ValueError):
            page_id = 0
        if page_id <= 0:
            return _not_run(tool, ctx.site_id, "page_on_front_missing")
    ctx.show_on_front = front
    ctx.page_id = page_id
    if front == "page":
        try:
            ctx.page = ctx.client.get_page(page_id, context="edit")
        except Exception:
            ctx.page = None
        else:
            raw = ctx.page.raw if isinstance(getattr(ctx.page, "raw", None), dict) else {}
            slug = str(raw.get("slug") or "").strip().lower()
            if _SLUG.fullmatch(slug):
                ctx.page_slug = slug
    active = [
        row for row in themes if isinstance(row, dict) and str(row.get("status") or "") == "active"
    ]
    stylesheet = str(active[0].get("stylesheet") or "").strip().lower() if len(active) == 1 else ""
    template = str(active[0].get("template") or "").strip().lower() if len(active) == 1 else ""
    if not _SLUG.fullmatch(stylesheet):
        return {
            "tool": tool,
            "site_id": ctx.site_id,
            "status": "read",
            "availability": "available",
            "reason": "active_theme_unknown",
            "executed": True,
            "wrote": False,
            "detail": f"front:{front}",
            "layer": "database",
            "show_on_front": front,
            "page_id": page_id or None,
            "stylesheet": None,
            "template": None,
            "theme_relationship": "unknown",
            "theme_kind": "unknown",
            "candidates": [],
            "candidate_hits": [],
            "findings": [],
        }
    ctx.stylesheet = stylesheet
    if template and _SLUG.fullmatch(template):
        ctx.template = template
        ctx.relationship = "child" if template != stylesheet else "no_child"
    else:
        ctx.relationship = "unknown"
    root = _component(ctx, stylesheet)
    if root is None or ctx.transport is None:
        ctx.theme_kind = "unknown"
    else:
        ctx.theme_kind = _theme_kind(ctx, root)
    block = ctx.theme_kind == "block"
    if ctx.theme_kind == "unknown":
        candidates: tuple[str, ...] = ()
    else:
        candidates = hierarchy_candidates(
            show_on_front=front,
            block_theme=block,
            slug=ctx.page_slug,
            page_id=str(page_id) if page_id else "",
        )
    findings = []
    present: list[dict[str, Any]] = []
    if root is not None and ctx.transport is not None and candidates:
        for relative in candidates:
            text = _read_relative(ctx, root, relative, role="stylesheet")
            role = "stylesheet"
            path = f"themes/{stylesheet}/{relative}"
            if text is None and ctx.relationship == "child":
                parent = _component(ctx, ctx.template)
                if parent is not None:
                    text = _read_relative(ctx, parent, relative, role="parent_read_only")
                    role = "parent_read_only"
                    path = f"themes/{ctx.template}/{relative}"
            row = {"path": path, "present": text is not None, "role": role}
            present.append(row)
            if text is None:
                continue
            cited = _evidence_line(path, text)
            if cited is None:
                continue
            line, snippet = cited
            findings.append(
                _finding(
                    ctx,
                    layer="source_file",
                    path=path,
                    line=line,
                    text=snippet,
                    verification="read",
                )
            )
    return {
        "tool": tool,
        "site_id": ctx.site_id,
        "status": "read",
        "availability": "available",
        "reason": "",
        "executed": True,
        "wrote": False,
        "detail": f"front:{front}",
        "layer": "database",
        "show_on_front": front,
        "page_id": page_id or None,
        "stylesheet": stylesheet,
        "template": ctx.template or None,
        "theme_relationship": ctx.relationship,
        "theme_kind": ctx.theme_kind,
        "candidates": [row["path"] for row in present],
        "candidate_hits": present,
        "findings": findings,
    }


def step_content(ctx: ReviewContext) -> dict[str, Any]:
    tool = "wp_page_review_content"
    if not ctx.allowed or ctx.site is None:
        return _not_run(tool, ctx.site_id, ctx.reason or "site_unselected")
    if ctx.client is None or not ctx.show_on_front:
        availability = "not_bound" if ctx.client is None else "unavailable"
        reason = "not_bound" if ctx.client is None else "homepage_not_resolved"
        return _not_run(tool, ctx.site_id, reason, availability)
    if ctx.show_on_front == "page" and ctx.page is None:
        return _not_run(tool, ctx.site_id, "page_read_failed")
    if ctx.show_on_front == "posts":
        return {
            "tool": tool,
            "site_id": ctx.site_id,
            "status": "read",
            "availability": "available",
            "reason": "",
            "executed": True,
            "wrote": False,
            "detail": "posts-index",
            "layer": "database",
            "database": {
                "kind": "posts_index",
                "editor_raw_present": False,
                "stored_rendered_present": False,
                "stored_rendered_is_dom": False,
            },
            "findings": [
                _finding(
                    ctx,
                    layer="database",
                    path="settings/page_on_front",
                    line=None,
                    text="",
                    verification="read",
                )
            ],
        }
    post = ctx.page
    raw = post.raw if isinstance(getattr(post, "raw", None), dict) else {}
    slug = str(raw.get("slug") or "").strip().lower()
    if _SLUG.fullmatch(slug):
        ctx.page_slug = slug
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    editor = content.get("raw") if isinstance(content.get("raw"), str) else ""
    stored = content.get("rendered") if isinstance(content.get("rendered"), str) else ""
    path = f"pages/{ctx.page_id}"
    return {
        "tool": tool,
        "site_id": ctx.site_id,
        "status": "read",
        "availability": "available",
        "reason": "",
        "executed": True,
        "wrote": False,
        "detail": f"page:{ctx.page_id}",
        "layer": "database",
        "database": {
            "kind": "page",
            "page_id": ctx.page_id,
            "editor_raw_present": bool(editor),
            "stored_rendered_present": bool(stored),
            "stored_rendered_is_dom": False,
        },
        "findings": [
            _finding(
                ctx,
                layer="database",
                path=path,
                line=None,
                text=editor or stored,
                verification="read",
            )
        ],
    }


def step_sources(ctx: ReviewContext) -> dict[str, Any]:
    tool = "wp_page_review_sources"
    if not ctx.allowed or ctx.site is None:
        return _not_run(tool, ctx.site_id, ctx.reason or "site_unselected")
    if not ctx.stylesheet or ctx.transport is None:
        return _not_run(tool, ctx.site_id, "active_theme_unknown")
    root = _component(ctx, ctx.stylesheet)
    if root is None:
        return _not_run(tool, ctx.site_id, "active_theme_unknown")
    names = _list(ctx.transport, root)
    if names is None:
        return _not_run(tool, ctx.site_id, "source_unavailable")
    findings = []
    interesting = [name for name in names if name.endswith(".php")]
    interesting.extend(("functions.php", "header.php", "footer.php"))
    woo = _list(ctx.transport, root + "/woocommerce") or []
    for name in woo:
        if name.endswith(".php"):
            interesting.append(f"woocommerce/{name}")
    seen: set[str] = set()
    for relative in interesting:
        if relative in seen:
            continue
        seen.add(relative)
        text = _read_relative(ctx, root, relative, role="stylesheet")
        if text is None:
            continue
        path = f"themes/{ctx.stylesheet}/{relative}"
        cited_hook = False
        for needle in (*HOOKS, *ENQUEUE):
            line = _line_of(text, needle)
            if line is None:
                continue
            snippet = _line_text(text, line)
            if not snippet:
                continue
            cited_hook = True
            findings.append(
                _finding(
                    ctx,
                    layer="source_file",
                    path=path,
                    line=line,
                    text=snippet,
                    verification="read",
                )
            )
        if relative.startswith("woocommerce/") and not cited_hook:
            cited = _evidence_line(path, text)
            if cited is None:
                continue
            line, snippet = cited
            findings.append(
                _finding(
                    ctx,
                    layer="source_file",
                    path=path,
                    line=line,
                    text=snippet,
                    verification="read",
                )
            )
    return {
        "tool": tool,
        "site_id": ctx.site_id,
        "status": "read",
        "availability": "available",
        "reason": "",
        "executed": True,
        "wrote": False,
        "detail": f"themes/{ctx.stylesheet}",
        "layer": "source_file",
        "theme_relationship": ctx.relationship,
        "woo_plugin_edit_target": False,
        "findings": findings,
    }


def step_preview(ctx: ReviewContext) -> dict[str, Any]:
    """No browser is wired. This never invents a screenshot or fetches a URL."""
    row = _not_run("wp_page_review_preview", ctx.site_id, "browser_not_run")
    row["layer"] = "rendered_dom"
    row["rendered_dom"] = {
        "status": "not_run",
        "availability": "unavailable",
        "desktop_screenshot": None,
        "mobile_screenshot": None,
        "console_errors": None,
        "network_errors": None,
        "verification": "not_run",
    }
    row["findings"] = [
        {
            "layer": "rendered_dom",
            "path": None,
            "line": None,
            "excerpt": None,
            "dom": None,
            "risk": "unknown",
            "security": "not_observed",
            "verification": "not_run",
        }
    ]
    return row
