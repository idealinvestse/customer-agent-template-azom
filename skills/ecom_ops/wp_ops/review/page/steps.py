"""Site-bound homepage review steps: home, content, sources, and preview.

Database content, source files, and rendered DOM stay strictly separate.
The model does not choose a URL, a shell command, or a production write.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlparse

from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.files import PathRejected, component_dir
from ecom_ops.wp_ops.review.knowledge import ENQUEUE, HOOKS, hierarchy_candidates
from ecom_ops.wp_ops.review.page.context import (
    _MAX_FILES,
    _SLUG,
    ReviewContext,
    _component,
    _evidence_line,
    _finding,
    _line_of,
    _line_text,
    _list,
    _not_run,
    _read_relative,
    _safe_relative,
    _theme_kind,
    not_run_rendered_dom,
)

_ALLOWED_SOURCE_SUFFIXES = (".php", ".css", ".js", ".json", ".html")
_CORE_THEME_FILES = ("functions.php", "header.php", "footer.php", "style.css", "theme.json")
_THEME_SOURCE_FOLDERS = ("templates", "parts", "styles", "assets", "woocommerce")


def _resolve_target_page(ctx: ReviewContext, tool: str, target: str) -> dict[str, Any] | None:
    if target.isdigit():
        try:
            ctx.page = ctx.client.get_page(target, context="edit")
        except TimeoutError:
            return _not_run(tool, ctx.site_id, "io_timeout")
        except Exception:
            return _not_run(tool, ctx.site_id, "target_page_not_found")
    else:
        try:
            pages = ctx.client.list_pages(per_page=20, search=target)
        except TimeoutError:
            return _not_run(tool, ctx.site_id, "io_timeout")
        except Exception:
            return _not_run(tool, ctx.site_id, "target_page_not_found")
        target_lower = target.lower()
        matched = None
        for candidate in pages if isinstance(pages, list) else []:
            c_raw = candidate.raw if isinstance(getattr(candidate, "raw", None), dict) else {}
            c_slug = str(c_raw.get("slug") or "").strip().lower()
            if c_slug == target_lower or str(getattr(candidate, "id", "")) == target:
                matched = candidate
                break
        if matched is None:
            return _not_run(tool, ctx.site_id, "target_page_not_found")
        try:
            ctx.page = ctx.client.get_page(matched.id, context="edit")
        except TimeoutError:
            return _not_run(tool, ctx.site_id, "io_timeout")
        except Exception:
            return _not_run(tool, ctx.site_id, "target_page_not_found")
    if ctx.page is None:
        return _not_run(tool, ctx.site_id, "target_page_not_found")
    return None


def _record_page_slug(ctx: ReviewContext) -> None:
    raw = ctx.page.raw if isinstance(getattr(ctx.page, "raw", None), dict) else {}
    slug = str(raw.get("slug") or "").strip().lower()
    if _SLUG.fullmatch(slug):
        ctx.page_slug = slug


def _resolve_homepage_front(
    ctx: ReviewContext,
    tool: str,
) -> tuple[list[Any], str, int, dict[str, Any] | None]:
    target = str(ctx.target_page or "").strip()
    if target:
        try:
            themes = ctx.client.list_themes()
        except TimeoutError:
            return [], "", 0, _not_run(tool, ctx.site_id, "io_timeout")
        except Exception:
            return [], "", 0, _not_run(tool, ctx.site_id, "rest_failed")
        if not isinstance(themes, list):
            return [], "", 0, _not_run(tool, ctx.site_id, "rest_failed")
        failure = _resolve_target_page(ctx, tool, target)
        if failure is not None:
            return [], "", 0, failure
        page_id = int(ctx.page.id)
        ctx.show_on_front = "page"
        ctx.page_id = page_id
        ctx.page_for_posts = 0
        _record_page_slug(ctx)
        return themes, "page", page_id, None

    try:
        settings = ctx.client.get_settings()
        themes = ctx.client.list_themes()
    except TimeoutError:
        return [], "", 0, _not_run(tool, ctx.site_id, "io_timeout")
    except Exception:
        return [], "", 0, _not_run(tool, ctx.site_id, "rest_failed")
    if not isinstance(settings, dict) or not isinstance(themes, list):
        return [], "", 0, _not_run(tool, ctx.site_id, "rest_failed")
    front = str(settings.get("show_on_front") or "").strip().lower()
    if front not in {"page", "posts"}:
        return [], "", 0, _not_run(tool, ctx.site_id, "settings_incomplete")
    page_id = 0
    if front == "page":
        try:
            page_id = int(settings.get("page_on_front") or 0)
        except (TypeError, ValueError):
            page_id = 0
        if page_id <= 0:
            return [], "", 0, _not_run(tool, ctx.site_id, "page_on_front_missing")
    try:
        page_for_posts = int(settings.get("page_for_posts") or 0)
    except (TypeError, ValueError):
        page_for_posts = 0
    ctx.show_on_front = front
    ctx.page_id = page_id
    ctx.page_for_posts = max(0, page_for_posts)
    if front == "page":
        try:
            ctx.page = ctx.client.get_page(page_id, context="edit")
        except TimeoutError:
            return [], "", 0, _not_run(tool, ctx.site_id, "io_timeout")
        except Exception:
            ctx.page = None
        else:
            _record_page_slug(ctx)
    return themes, front, page_id, None


def _select_active_theme(themes: list[Any]) -> tuple[str, str]:
    active = [
        row for row in themes if isinstance(row, dict) and str(row.get("status") or "") == "active"
    ]
    if len(active) != 1:
        return "", ""
    stylesheet = str(active[0].get("stylesheet") or "").strip().lower()
    template = str(active[0].get("template") or "").strip().lower()
    return stylesheet, template


def _read_site_editor_templates(ctx: ReviewContext, *, block: bool) -> tuple[str, list[str]]:
    if not block:
        return "unavailable", []
    list_tpl = getattr(ctx.client, "list_templates", None)
    if not callable(list_tpl):
        return "unavailable", []
    try:
        tpl_rows = list_tpl()
    except Exception:
        tpl_rows = []
    if not isinstance(tpl_rows, list) or not tpl_rows:
        return "unavailable", []
    templates = [
        str(row.get("slug") or row.get("id"))[:80]
        for row in tpl_rows[:20]
        if isinstance(row, dict) and (row.get("slug") or row.get("id"))
    ]
    return ("read", templates) if templates else ("unavailable", [])


def _scan_hierarchy_candidates(
    ctx: ReviewContext,
    root: str | None,
    stylesheet: str,
    candidates: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    present: list[dict[str, Any]] = []
    if root is None or ctx.transport is None or not candidates:
        return present, findings

    hierarchy_blocked = False
    for relative in candidates:
        if hierarchy_blocked:
            break
        absolute = root + "/" + relative
        text = _read_relative(ctx, root, relative, role="stylesheet")
        role = "stylesheet"
        path = f"themes/{stylesheet}/{relative}"
        if text is not None:
            mark = "read"
        elif absolute in ctx.clipped:
            mark = "clipped"
        elif absolute in ctx.capped:
            mark = "capped"
        else:
            mark = "missing"
        if text is None and mark not in {"clipped", "capped"} and ctx.relationship == "child":
            parent = _component(ctx, ctx.template)
            if parent is not None:
                parent_absolute = parent + "/" + relative
                parent_text = _read_relative(ctx, parent, relative, role="parent_read_only")
                if parent_text is not None:
                    text = parent_text
                    role = "parent_read_only"
                    path = f"themes/{ctx.template}/{relative}"
                    mark = "read"
                elif parent_absolute in ctx.clipped:
                    path = f"themes/{ctx.template}/{relative}"
                    mark = "clipped"
                elif parent_absolute in ctx.capped:
                    path = f"themes/{ctx.template}/{relative}"
                    mark = "capped"
        present.append({"path": path, "present": text is not None, "role": role, "mark": mark})
        if mark in {"clipped", "capped"}:
            hierarchy_blocked = True
            continue
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
    return present, findings


def _pick_template_winner(
    present: list[dict[str, Any]],
    assigned_template: str,
    *,
    relationship: str,
) -> tuple[str | None, str | None, str, str, str]:
    selected = None
    stopped_on_clip = False
    for row in present:
        if row.get("mark") in {"clipped", "capped"}:
            stopped_on_clip = True
            break
        if row.get("present"):
            selected = row
            break
    assignment_state = "absent"
    if assigned_template and assigned_template.lower() != "default" and not stopped_on_clip:
        assignment_state = "unverified"
        leaf = assigned_template.replace("\\", "/").rsplit("/", 1)[-1].lower()
        winner = str((selected or {}).get("path") or "").lower()
        if selected and selected.get("present") and leaf and winner.endswith("/" + leaf):
            assignment_state = "verified"
    if selected is None:
        return None, None, "unresolved", assignment_state, "unavailable"

    role = selected.get("role")
    if role == "parent_read_only":
        template_origin = "parent_theme"
    elif relationship == "child":
        template_origin = "child_theme"
    else:
        template_origin = "theme"
    selected_template = str(selected.get("path") or "")
    template_path = selected_template or None
    selection = "candidate" if assignment_state == "unverified" else "verified"
    return selected_template, template_path, template_origin, assignment_state, selection


def step_home(ctx: ReviewContext) -> dict[str, Any]:
    tool = "wp_page_review_home"
    if not ctx.allowed:
        return _not_run(tool, ctx.site_id, ctx.reason or "actor_denied")
    if ctx.site is None:
        return _not_run(tool, ctx.site_id, ctx.reason or "site_unselected")
    if ctx.client is None:
        return _not_run(tool, ctx.site_id, "not_bound", "not_bound")

    themes, front, page_id, failure = _resolve_homepage_front(ctx, tool)
    if failure is not None:
        return failure

    stylesheet, template = _select_active_theme(themes)
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
            "selected_template": None,
            "selected_template_path": None,
            "selection": "unavailable",
            "site_editor": "unavailable",
            "site_editor_templates": [],
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
    ctx.theme_kind = "unknown" if root is None or ctx.transport is None else _theme_kind(ctx, root)
    page_raw = ctx.page.raw if ctx.page is not None and isinstance(getattr(ctx.page, "raw", None), dict) else {}
    assigned_template = str(page_raw.get("template") or "").strip()
    block = ctx.theme_kind == "block"
    site_editor, site_editor_templates = _read_site_editor_templates(ctx, block=block)

    candidates: tuple[str, ...] = (
        ()
        if ctx.theme_kind == "unknown"
        else hierarchy_candidates(
            show_on_front=front,
            block_theme=block,
            slug=ctx.page_slug,
            page_id=str(page_id) if page_id else "",
            assigned_template=assigned_template,
        )
    )
    present, findings = _scan_hierarchy_candidates(ctx, root, stylesheet, candidates)
    selected_template, template_path, template_origin, assignment_state, selection = (
        _pick_template_winner(present, assigned_template, relationship=ctx.relationship)
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
        "homepage_mode": "static_page" if front == "page" else "latest_posts",
        "layer": "database",
        "show_on_front": front,
        "page_id": page_id or None,
        "page_for_posts": ctx.page_for_posts or None,
        "front_page": {
            "id": page_id or None,
            "slug": ctx.page_slug or None,
            "title": str(getattr(ctx.page, "title", "") or "") or None,
            "status": str(getattr(ctx.page, "status", "") or "") or None,
            "template": assigned_template or None,
            "template_origin": template_origin,
        }
        if front == "page"
        else None,
        "selected_template": selected_template,
        "selected_template_path": template_path,
        "template_assignment": assigned_template or None,
        "assignment_state": assignment_state,
        "selection": selection,
        "site_editor": site_editor,
        "site_editor_templates": site_editor_templates,
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
            "homepage_mode": "latest_posts",
            "layer": "database",
            "show_on_front": "posts",
            "page_on_front": None,
            "page_for_posts": ctx.page_for_posts or None,
            "front_page": None,
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


def _collect_theme_files(ctx: ReviewContext, root: str, names: list[str]) -> list[str]:
    theme_files = [name for name in names if name.lower().endswith(_ALLOWED_SOURCE_SUFFIXES)]
    theme_files.extend(_CORE_THEME_FILES)
    for folder in _THEME_SOURCE_FOLDERS:
        children = _list(ctx.transport, root + "/" + folder)
        if children is None:
            continue
        theme_files.extend(
            f"{folder}/{name}"
            for name in children
            if name.lower().endswith(_ALLOWED_SOURCE_SUFFIXES)
        )
    return theme_files


def _collect_active_plugins(
    ctx: ReviewContext,
    candidates: list[tuple[str, str, str]],
) -> list[str] | None:
    try:
        plugins = ctx.client.list_plugins()
    except Exception:
        return None
    if not isinstance(plugins, list):
        return None
    active_plugins: list[str] = []
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("status") != "active":
            continue
        plugin_path = str(plugin.get("plugin") or "")
        slug, separator, main_file = plugin_path.partition("/")
        if not separator or not _SLUG.fullmatch(slug):
            continue
        active_plugins.append(slug)
        if _safe_relative(main_file) and main_file.lower().endswith(".php"):
            candidates.append(("plugin", slug, main_file))
        else:
            try:
                plugin_root = component_dir(ctx.site.files_root, "plugin", slug)
            except PathRejected:
                continue
            main_candidates = _list(ctx.transport, plugin_root) or []
            candidates.extend(
                ("plugin", slug, name)
                for name in main_candidates
                if name.lower().endswith(".php")
            )
        if len(active_plugins) >= 4:
            break
    return active_plugins


def _scan_source_inventory(
    ctx: ReviewContext,
    root: str,
    candidates: list[tuple[str, str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inventory: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for kind, slug, relative in candidates:
        key = (kind, slug, relative)
        if key in seen or len(seen) >= _MAX_FILES:
            continue
        seen.add(key)
        component_kind = "plugin" if kind == "plugin" else "theme"
        try:
            component_root = component_dir(ctx.site.files_root, component_kind, slug)
        except PathRejected:
            continue
        role = (
            "parent_read_only"
            if kind == "parent_theme"
            else "plugin_read_only"
            if kind == "plugin"
            else "theme"
        )
        child_absolute = root + "/" + relative
        if kind == "parent_theme" and child_absolute in ctx.clipped | ctx.capped:
            continue
        text = _read_relative(ctx, component_root, relative, role=role)
        if text is None:
            continue
        component_name = "plugins" if component_kind == "plugin" else "themes"
        path = f"{component_name}/{slug}/{relative}"
        inventory.append({"path": path, "kind": kind, "role": role, "read": True})
        cited_hook = False
        for needle in (*HOOKS, *ENQUEUE):
            line = _line_of(text, needle)
            if line is None:
                continue
            snippet = _line_text(text, line)
            if snippet:
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
            if cited is not None:
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
    return inventory, findings


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

    theme_files = _collect_theme_files(ctx, root, names)
    candidates: list[tuple[str, str, str]] = [
        ("theme", ctx.stylesheet, relative) for relative in theme_files
    ]
    if ctx.relationship == "child" and ctx.template:
        candidates.extend(("parent_theme", ctx.template, relative) for relative in theme_files)

    active_plugins = _collect_active_plugins(ctx, candidates)
    inventory, findings = _scan_source_inventory(ctx, root, candidates)
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
        "active_theme": ctx.stylesheet,
        "parent_theme": ctx.template if ctx.relationship == "child" else None,
        "active_plugins": active_plugins,
        "plugin_inventory_status": "read" if active_plugins is not None else "unavailable",
        "source_inventory": inventory,
        "woo_plugin_edit_target": False,
        "findings": findings,
    }


def _preview_target_path(ctx: ReviewContext) -> str:
    if not ctx.target_page or ctx.page is None:
        return "/"
    link = ""
    raw = getattr(ctx.page, "raw", None)
    if isinstance(raw, dict):
        link = str(raw.get("link") or "")
    if not link:
        link = str(getattr(ctx.page, "link", "") or "")
    if link:
        parsed = urlparse(link)
        if parsed.path and parsed.path.startswith("/"):
            return parsed.path
    if ctx.page_slug:
        return f"/{ctx.page_slug}"
    return "/"


def _preview_not_run_row(
    ctx: ReviewContext,
    reason: str,
    *,
    include_placeholder_finding: bool,
) -> dict[str, Any]:
    row = _not_run("wp_page_review_preview", ctx.site_id, reason)
    row["layer"] = "rendered_dom"
    row["rendered_dom"] = not_run_rendered_dom()
    if include_placeholder_finding:
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


def _collect_preview_findings(
    preview_path: str,
    vp_desktop: dict[str, Any],
    vp_mobile: dict[str, Any],
    *,
    include_evidence: bool,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    console_errors: list[str] = []
    network_errors: list[dict[str, Any]] = []
    for vp in (vp_desktop, vp_mobile):
        for err_msg in vp.get("console_errors") or []:
            if err_msg not in console_errors:
                console_errors.append(str(err_msg))
        for net_err in vp.get("network_errors") or []:
            if net_err not in network_errors:
                network_errors.append(net_err)
    console_errors = console_errors[:10]
    network_errors = network_errors[:10]

    findings: list[dict[str, Any]] = []
    for err_msg in console_errors[:3]:
        findings.append(
            {
                "layer": "rendered_dom",
                "path": preview_path,
                "line": None,
                "excerpt": str(err_msg)[:120] if include_evidence else None,
                "dom": "console_error",
                "risk": "low",
                "security": "untrusted_data",
                "verification": "observed",
            }
        )
    for net_err in network_errors[:3]:
        findings.append(
            {
                "layer": "rendered_dom",
                "path": preview_path,
                "line": None,
                "excerpt": (
                    str(net_err.get("url") or net_err.get("error_text") or "")[:120]
                    if include_evidence
                    else None
                ),
                "dom": f"network_{net_err.get('status') or 'error'}",
                "risk": "low",
                "security": "untrusted_data",
                "verification": "observed",
            }
        )
    if not findings:
        findings.append(
            {
                "layer": "rendered_dom",
                "path": preview_path,
                "line": None,
                "excerpt": None,
                "dom": f"http_{vp_desktop.get('http_status') or 200}",
                "risk": "low",
                "security": "untrusted_data",
                "verification": "observed",
            }
        )
    return console_errors, network_errors, findings


def step_preview(ctx: ReviewContext) -> dict[str, Any]:
    """Run read-only live browser preview when enabled; otherwise report not_run."""
    from ecom_ops.integrations.wp_live_preview import (
        capture_preview,
        fetch_live_html,
        live_preview_enabled,
        validate_preview_path,
    )

    if not live_preview_enabled() or not ctx.allowed or ctx.site is None:
        return _preview_not_run_row(ctx, "browser_not_run", include_placeholder_finding=True)

    data_root = data_dir_path(ctx.data_dir)
    artifact_dir = (
        data_root / "wp_workspaces" / ctx.site_id / f"pv-{secrets.token_hex(6)}" / "artifacts"
    )
    try:
        preview_path = validate_preview_path(_preview_target_path(ctx))
        fetched = fetch_live_html(ctx.site, preview_path)
        rendered = capture_preview(
            ctx.site,
            path=preview_path,
            artifact_dir=artifact_dir,
            revisions=[("live", str(fetched.get("html") or ""))],
            target_url=str(fetched.get("final_url") or "") or None,
        )
    except Exception as exc:
        err = getattr(exc, "error_class", None) or type(exc).__name__ or "LivePreviewFailed"
        return _preview_not_run_row(ctx, str(err)[:40], include_placeholder_finding=False)

    live_revs = (rendered.get("revisions") or {}).get("live") or {}
    vp_desktop = live_revs.get("desktop") or {}
    vp_mobile = live_revs.get("mobile") or {}
    console_errors, network_errors, findings = _collect_preview_findings(
        preview_path,
        vp_desktop,
        vp_mobile,
        include_evidence=ctx.include_evidence,
    )
    return {
        "tool": "wp_page_review_preview",
        "site_id": ctx.site_id,
        "status": "read",
        "availability": "available",
        "reason": "",
        "executed": True,
        "wrote": False,
        "detail": f"live:{preview_path}",
        "layer": "rendered_dom",
        "rendered_dom": {
            "status": "read",
            "availability": "available",
            "desktop_screenshot": vp_desktop.get("screenshot_path"),
            "mobile_screenshot": vp_mobile.get("screenshot_path"),
            "console_errors": console_errors,
            "network_errors": network_errors,
            "verification": "observed",
        },
        "findings": findings,
    }


__all__ = [
    "step_content",
    "step_home",
    "step_preview",
    "step_sources",
]
