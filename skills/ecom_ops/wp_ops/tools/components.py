"""Component inspection, tree walking, search, code index, hierarchy, intel, and playbook tools."""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.code_index import index_sources
from ecom_ops.wp_ops.credentials import bind_credential, redact_secret_text
from ecom_ops.wp_ops.file_changes import (
    _MAX_BYTES,
    _SKIP_DIRS,
    _directory,
    search_texts,
    sensitive_relative,
)
from ecom_ops.wp_ops.file_changes import (
    rest_reads_allowed as _default_rest_reads_allowed,
)
from ecom_ops.wp_ops.files import (
    LocalTree,
    PathRejected,
    TransportError,
    component_dir,
    components_parent,
    resolve_file,
)
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS, get_playbook
from ecom_ops.wp_ops.review_knowledge import TEMPLATE_CONTEXTS, template_candidates
from ecom_ops.wp_ops.review_workspace import ReviewError, site_plugin_slug
from ecom_ops.wp_ops.tools.registry import count_outcome, read_budget_outcome, register_tool
from ecom_ops.wp_ops.tools.types import (
    CODE_INDEX_EXTENSIONS,
    FILE_PATH_RE,
    FILE_READ_DEFAULT,
    FILE_READ_MAX,
    HOOK_RE,
    INTEL_SLUG_RE,
    SITE_ID_RE,
    SLUG_RE,
    WC_TESTED_RE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import (
    error_payload,
    parse_line_range,
    safe_failure,
    short_text,
    utf8_window,
)
from ecom_ops.wp_ops.tools.workspace_io import authorize_editor_site
from ecom_ops.wp_ops.wporg_intel import fetch_component_intel

_COMPONENT_KINDS = frozenset({"theme", "plugin", "mu_plugin"})
_DEFAULT_CRITICAL_PLUGIN_SLUGS = frozenset(
    {
        "woocommerce",
        "klarna-checkout-for-woocommerce",
        "klarna-payments-for-woocommerce",
        "woo-vipps",
        "woocommerce-gateway-stripe",
        "svea-checkout-for-woocommerce",
        "azom-ops-bridge",
    }
)
_REQUIRES_PLUGINS_RE = re.compile(r"(?im)^\s*(?:\*\s*)?Requires\s+Plugins:\s*([^\r\n*]+)")
_HOOK_PRIORITY_RE = re.compile(
    r"""\b(add_action|add_filter|do_action|apply_filters)\s*\(\s*(['"])([A-Za-z0-9_.:/-]{1,100})\2(?:\s*,\s*([^,\)\r\n]{1,80}))?(?:\s*,\s*(-?\d+))?"""
)


def _critical_plugin_slugs() -> frozenset[str]:
    try:
        from ecom_ops.wp_ops.plugin_policy import CRITICAL_PLUGIN_SLUGS

        return frozenset(CRITICAL_PLUGIN_SLUGS)
    except Exception:
        return _DEFAULT_CRITICAL_PLUGIN_SLUGS


def _parse_requires_plugins(body: str) -> list[str]:
    if not body:
        return []
    match = _REQUIRES_PLUGINS_RE.search(body[:8192])
    if not match:
        return []
    out: list[str] = []
    for raw_part in match.group(1).split(","):
        slug = raw_part.strip().lower()
        if slug and INTEL_SLUG_RE.fullmatch(slug) and slug not in out:
            out.append(slug)
        if len(out) >= 16:
            break
    return out


def _extract_hook_priorities(body: str) -> list[dict[str, Any]]:
    if not body:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int | None]] = set()
    for kind_fn, _quote, hook_name, raw_cb, raw_prio in _HOOK_PRIORITY_RE.findall(body):
        cb = (raw_cb or "").strip().strip("'\"")[:80] if raw_cb else ""
        if kind_fn in {"add_action", "add_filter"}:
            prio: int | None = int(raw_prio) if raw_prio is not None and raw_prio != "" else 10
        else:
            prio = None
        key = (kind_fn, hook_name, cb, prio)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind_fn, "hook": hook_name, "callback": cb, "priority": prio})
        if len(out) >= 30:
            break
    return out


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get("ecom_ops.wp_ops.agent_tools")
    if facade is not None:
        val = getattr(facade, name, None)
        if val is not None:
            return val
    return default


def _rest_reads_allowed() -> bool:
    return bool(_facade_attr("rest_reads_allowed", _default_rest_reads_allowed)())


def has_site_rest_credentials(site: Site | None) -> bool:
    if site is None:
        return False
    try:
        bound = bind_credential(site)
        return bound.present and ":" in bound.value
    except Exception:
        return False


def same_configured_host(canonical_url: str) -> bool:
    configured = os.environ.get("WP_BASE_URL") or os.environ.get("WOO_BASE_URL") or ""
    left = (urlparse(canonical_url).hostname or "").lower()
    right = (urlparse(configured).hostname or "").lower()
    return bool(left and right and left == right)


def component_rest_source(canonical_url: str, *, site: Site | None = None) -> str:
    """Live component reads only when mock is explicitly off and host or credentials match."""
    if not _rest_reads_allowed():
        return "not_called"
    if has_site_rest_credentials(site) or same_configured_host(canonical_url):
        return "live"
    return "not_bound"


def load_live_theme_rows(canonical_url: str, *, site: Site | None = None) -> list[dict[str, str]]:
    if not _rest_reads_allowed():
        return []
    src_fn = _facade_attr("component_rest_source", component_rest_source)
    if not (has_site_rest_credentials(site) or src_fn(canonical_url) == "live"):
        return []
    try:
        from ecom_ops.integrations.wp_component_reads import load_theme_rows

        return load_theme_rows(site=site)
    except Exception:
        return []


def load_live_plugin_rows(canonical_url: str, *, site: Site | None = None) -> list[dict[str, str]]:
    if not _rest_reads_allowed():
        return []
    src_fn = _facade_attr("component_rest_source", component_rest_source)
    if not (has_site_rest_credentials(site) or src_fn(canonical_url) == "live"):
        return []
    try:
        from ecom_ops.integrations.wp_component_reads import load_plugin_rows

        return load_plugin_rows(site=site)
    except Exception:
        return []


def _call_component_rest_source(canonical_url: str, *, site: Site | None = None) -> str:
    fn = _facade_attr("component_rest_source", component_rest_source)
    try:
        return str(fn(canonical_url, site=site))
    except TypeError:
        return str(fn(canonical_url))


def _call_load_live_theme_rows(canonical_url: str, *, site: Site | None = None) -> list[dict[str, str]]:
    fn = _facade_attr("load_live_theme_rows", load_live_theme_rows)
    try:
        return list(fn(canonical_url, site=site))
    except TypeError:
        return list(fn(canonical_url))


def _call_load_live_plugin_rows(canonical_url: str, *, site: Site | None = None) -> list[dict[str, str]]:
    fn = _facade_attr("load_live_plugin_rows", load_live_plugin_rows)
    try:
        return list(fn(canonical_url, site=site))
    except TypeError:
        return list(fn(canonical_url))


def active_live_theme(canonical_url: str, *, site: Site | None = None) -> tuple[dict[str, str] | None, bool]:
    """Return ``(active_theme_row, live_called)`` using facade hooks for monkeypatch safety."""
    if not (_rest_reads_allowed() and isinstance(canonical_url, str) and _call_component_rest_source(canonical_url, site=site) == "live"):
        return None, False
    for row in _call_load_live_theme_rows(canonical_url, site=site):
        if isinstance(row, dict) and row.get("status") == "active":
            return row, True
    return None, True


def open_transport_batch(transport: Any) -> Any:
    """Return a batch context manager when supported by ``transport``, else ``nullcontext``."""
    if hasattr(transport, "open_batch") and callable(transport.open_batch):
        return transport.open_batch()
    return contextlib.nullcontext()


def _list_dir_entries(transport: Any, directory: str) -> list[tuple[str, bool]]:
    if hasattr(transport, "list_entries") and callable(transport.list_entries):
        return list(transport.list_entries(directory))
    return [(name, Path(name).suffix == "") for name in transport.list_dir(directory)]


def walk_component_tree(
    transport: Any,
    base_dir: str,
    *,
    max_depth: int,
    max_entries: int,
    max_woo_overrides: int = 0,
) -> tuple[list[str], list[dict[str, str]], list[str], bool, bool]:
    """Bounded BFS component tree walker using ``transport.list_entries()`` when available."""
    try:
        root_entries = _list_dir_entries(transport, base_dir)
    except (PathRejected, TransportError):
        return [], [], [], False, False
    root_names = [name for name, _is_dir in root_entries]
    root_readable = bool(root_names)
    entries: list[dict[str, str]] = []
    woo_overrides: list[str] = []
    truncated = False
    queue: list[tuple[str, str, int, list[tuple[str, bool]] | None]] = [("", base_dir, 1, list(root_entries))]
    woo_visited = False
    while queue:
        prefix, current_dir, depth, preloaded = queue.pop(0)
        if prefix == "woocommerce":
            woo_visited = True
        if preloaded is not None:
            dir_entries = preloaded
        else:
            try:
                dir_entries = _list_dir_entries(transport, current_dir)
            except (PathRejected, TransportError):
                continue
        for name, is_dir in dir_entries:
            if name in {".", ".."} or name.lower() in _SKIP_DIRS or sensitive_relative(name):
                continue
            rel = f"{prefix}/{name}" if prefix else name
            if sensitive_relative(rel):
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append({"path": rel, "kind": "directory" if is_dir else "file"})
            if max_woo_overrides > 0 and not is_dir and rel.startswith("woocommerce/"):
                if len(woo_overrides) < max_woo_overrides:
                    woo_overrides.append(rel)
                else:
                    truncated = True
            if is_dir and depth < max_depth:
                queue.append((rel, f"{current_dir}/{name}", depth + 1, None))
        if truncated:
            break
    if (
        max_woo_overrides > 0
        and any(name == "woocommerce" and is_dir for name, is_dir in root_entries)
        and (not woo_visited or (not truncated and len(woo_overrides) < max_woo_overrides))
    ):
        woo_seen = set(woo_overrides)
        woo_queue: list[tuple[str, str, int]] = [("woocommerce", f"{base_dir}/woocommerce", 2)]
        woo_steps = 0
        while woo_queue and len(woo_overrides) < max_woo_overrides and woo_steps < 40:
            woo_steps += 1
            w_prefix, w_dir, w_depth = woo_queue.pop(0)
            try:
                w_entries = _list_dir_entries(transport, w_dir)
            except (PathRejected, TransportError):
                continue
            for name, is_dir in w_entries:
                if name in {".", ".."} or name.lower() in _SKIP_DIRS or sensitive_relative(name):
                    continue
                rel = f"{w_prefix}/{name}"
                if sensitive_relative(rel):
                    continue
                if is_dir:
                    if w_depth < 4:
                        woo_queue.append((rel, f"{w_dir}/{name}", w_depth + 1))
                elif rel not in woo_seen:
                    if len(woo_overrides) >= max_woo_overrides:
                        truncated = True
                        break
                    woo_seen.add(rel)
                    woo_overrides.append(rel)
    return root_names, entries, woo_overrides, root_readable, truncated


def parse_style_headers(style_text: str) -> dict[str, str]:
    head = (style_text or "")[:8192]
    out: dict[str, str] = {}
    theme_match = re.search(r"^[ \t/*#]*Theme\s+Name\s*:\s*([^\r\n*]+)", head, re.I | re.M)
    if theme_match:
        val = theme_match.group(1).strip()
        if val:
            out["theme_name"] = val
    tpl_match = re.search(
        r"^[ \t/*#]*Template\s*:\s*([a-z0-9][a-z0-9_-]{0,60})\b",
        head,
        re.I | re.M,
    )
    if tpl_match:
        tpl = tpl_match.group(1).strip().lower()
        if SLUG_RE.fullmatch(tpl):
            out["template"] = tpl
    return out


def scan_local_themes(tools: Any, site_obj: Site) -> dict[str, dict[str, str]]:
    editor = tools.service._file_editor()
    transport = editor._transport(site_obj)
    if not isinstance(transport, LocalTree):
        return {}
    try:
        parent_dir = components_parent(site_obj.files_root, "theme")
        slugs = [name for name in transport.list_dir(parent_dir) if SLUG_RE.fullmatch(name)]
    except (PathRejected, TransportError):
        return {}
    themes: dict[str, dict[str, str]] = {}
    for slug in slugs:
        headers: dict[str, str] = {}
        try:
            style_path = resolve_file(site_obj.files_root, "theme", slug, "style.css")
            raw_bytes = transport.read_bytes(style_path)
            if len(raw_bytes) <= _MAX_BYTES and b"\x00" not in raw_bytes:
                headers = parse_style_headers(raw_bytes.decode("utf-8", errors="replace"))
        except (PathRejected, TransportError):
            headers = {}
        themes[slug] = headers
    return themes


def known_parent_slug(tools: Any, site_id: str, actor: str) -> str | None:
    shown = tools.service.show_site(site_id, actor=actor)
    if not shown.ok:
        return None
    site = (shown.data or {}).get("site") or {}
    canonical = site.get("canonical_url") if isinstance(site, dict) else ""
    site_model = tools._site_model(site_id)
    active_row, _live = active_live_theme(canonical, site=site_model)
    if active_row is not None:
        template = active_row.get("template") or ""
        stylesheet = active_row.get("stylesheet") or ""
        if template and stylesheet and template != stylesheet:
            return template
    _editor, site_obj, denied = authorize_editor_site(tools.service, site_id, actor, write=False)
    if denied is not None or site_obj is None:
        return None
    local_themes = scan_local_themes(tools, site_obj)
    parents = {
        meta["template"]
        for slug_name, meta in local_themes.items()
        if meta.get("template") and meta["template"] != slug_name
    }
    if len(parents) == 1:
        return next(iter(parents))
    return None


def is_parent_theme_on_site(tools: Any, site_obj: Site, slug: str, *, site_id: str, actor: str) -> bool:
    if not slug or not SLUG_RE.fullmatch(slug):
        return False
    if known_parent_slug(tools, site_id, actor) == slug:
        return True
    local_themes = scan_local_themes(tools, site_obj)
    return any(other_slug != slug and meta.get("template") == slug for other_slug, meta in local_themes.items())


def resolve_active_theme(
    tools: Any,
    site_id: str,
    actor: str,
    site_obj: Site,
    explicit_slug: str | None = None,
) -> tuple[str | None, str | None, bool]:
    parent_theme: str | None = None
    active_theme: str | None = None
    active_row, live_calls = active_live_theme(site_obj.canonical_url, site=site_obj)
    if active_row is not None:
        stylesheet = active_row.get("stylesheet") or ""
        template = active_row.get("template") or ""
        if isinstance(stylesheet, str) and stylesheet:
            active_theme = stylesheet
        if isinstance(template, str) and template and template != stylesheet:
            parent_theme = template
    if explicit_slug:
        if active_theme == explicit_slug:
            return explicit_slug, parent_theme, live_calls
        local_themes = scan_local_themes(tools, site_obj)
        if explicit_slug in local_themes:
            tpl = local_themes[explicit_slug].get("template") or None
            if tpl and tpl != explicit_slug:
                parent_theme = tpl
        return explicit_slug, parent_theme, live_calls
    if active_theme:
        return active_theme, parent_theme, live_calls
    local_themes = scan_local_themes(tools, site_obj)
    for slug_name, meta in local_themes.items():
        if meta.get("template") and meta["template"] != slug_name:
            return slug_name, meta["template"], live_calls
    for slug_name, meta in local_themes.items():
        if meta.get("theme_name"):
            return slug_name, None, live_calls
    listed = tools.service.list_components(site_id, "theme", actor=actor)
    slugs = (listed.data or {}).get("slugs") or [] if listed.ok else []
    return (slugs[0] if slugs else None), None, live_calls


def read_installed_index_files(
    site_obj: Site,
    kind: str,
    slug: str,
    relative: str,
    editor: Any,
) -> tuple[dict[str, str], bool]:
    transport = editor._transport(site_obj)
    files: dict[str, str] = {}
    total_bytes = 0
    truncated = False
    has_entries = hasattr(transport, "list_entries") and callable(transport.list_entries)
    with open_transport_batch(transport):
        if relative and Path(relative).suffix:
            try:
                single_abs = resolve_file(site_obj.files_root, kind, slug, relative)
                raw_bytes = transport.read_bytes(single_abs)
                if len(raw_bytes) <= _MAX_BYTES and b"\x00" not in raw_bytes:
                    decoded = raw_bytes.decode("utf-8")
                    files[relative.strip("/")] = editor._scrub(site_obj, decoded)
                    return files, False
            except (PathRejected, TransportError, UnicodeDecodeError):
                pass

        start_dir = _directory(site_obj.files_root, kind, slug, relative)
        base_prefix = relative.strip("/")
        stack = [(base_prefix, start_dir)]
        while stack and len(files) < 120 and total_bytes < 768 * 1024:
            prefix, current = stack.pop(0)
            try:
                if has_entries:
                    dir_entries = list(transport.list_entries(current))
                else:
                    dir_entries = [
                        (item_name, Path(item_name).suffix.lower() not in CODE_INDEX_EXTENSIONS)
                        for item_name in transport.list_dir(current)
                    ]
            except (PathRejected, TransportError):
                continue
            for item_name, is_dir in dir_entries:
                if item_name in {".", ".."} or sensitive_relative(item_name) or item_name.lower() in _SKIP_DIRS:
                    continue
                rel = f"{prefix}/{item_name}" if prefix else item_name
                if sensitive_relative(rel):
                    continue
                if is_dir:
                    try:
                        stack.append((rel, _directory(site_obj.files_root, kind, slug, rel)))
                    except PathRejected:
                        continue
                    continue
                if Path(item_name).suffix.lower() not in CODE_INDEX_EXTENSIONS:
                    continue
                if len(files) >= 120 or total_bytes >= 768 * 1024:
                    truncated = True
                    break
                try:
                    raw_bytes = transport.read_bytes(resolve_file(site_obj.files_root, kind, slug, rel))
                except (PathRejected, TransportError):
                    continue
                if len(raw_bytes) > _MAX_BYTES or b"\x00" in raw_bytes:
                    continue
                try:
                    body = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                files[rel] = editor._scrub(site_obj, body)
                total_bytes += len(raw_bytes)
            if truncated:
                break
    return files, truncated


# --- Outcome formatters ---

def _component_context_outcome(data: dict[str, Any], _reads: int | None) -> str:
    wp = short_text(data.get("wordpress_version"), 16)
    php = short_text(data.get("php_version"), 16)
    bits = [bit for bit in (f"WP {wp}" if wp else "", f"PHP {php}" if php else "") if bit]
    return " · ".join(bits) if bits else "kontext hämtad"


def _theme_overview_outcome(data: dict[str, Any], _reads: int | None) -> str:
    theme = short_text(data.get("active_theme"), 32)
    kind = short_text(data.get("theme_kind"), 16)
    bits = [bit for bit in (theme or "", kind or "") if bit]
    return " · ".join(bits) if bits else "temaöversikt hämtad"


def _plugin_overview_outcome(data: dict[str, Any], _reads: int | None) -> str:
    slug = short_text(data.get("slug"), 32)
    return f"plugin {slug} läst" if slug else "pluginöversikt hämtad"


def _hierarchy_outcome(data: dict[str, Any], _reads: int | None) -> str:
    fm = short_text(data.get("first_match"), 40)
    return f"första träff: {fm}" if fm else "mallhierarki kontrollerad"


def _intel_outcome(data: dict[str, Any], _reads: int | None) -> str:
    ver = short_text(data.get("latest_version") or data.get("version"), 20)
    return f"senaste: {ver}" if ver else "komponentmetadata hämtad"


def _playbook_outcome(data: dict[str, Any], _reads: int | None) -> str:
    topic = short_text(data.get("topic"), 32)
    return f"guide: {topic}" if topic else "åtgärdsguide hämtad"


# --- Tool handlers (12 tools) ---

@register_tool(
    name="wp_read_theme_plugin_file",
    group="file",
    label="Läs tema-/pluginfil",
    short_label="Temafilsuppslag",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "kind": {"type": "string", "enum": ["theme", "plugin", "mu_plugin"]},
            "slug": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,60}$"},
            "path": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,219}$"},
            "offset": {"type": "integer", "minimum": 0, "maximum": 65536},
            "limit": {"type": "integer", "minimum": 1, "maximum": FILE_READ_MAX},
            "start_line": {"type": "integer", "minimum": 1, "maximum": 50000},
            "end_line": {"type": "integer", "minimum": 1, "maximum": 50000},
        },
        "required": ["kind", "slug", "path"],
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("läsning", fallback="fil läst", with_truncated=True),
    in_wp_schemas=True,
    in_core_file_tools=True,
    read_budget=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_read_theme_plugin_file(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    kind = arguments.get("kind")
    slug = arguments.get("slug")
    relative = arguments.get("path")
    if (
        not isinstance(kind, str)
        or kind not in _COMPONENT_KINDS
        or not isinstance(slug, str)
        or not SLUG_RE.fullmatch(slug)
        or not isinstance(relative, str)
        or len(relative) > 220
    ):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    has_offset = "offset" in arguments
    has_start = "start_line" in arguments
    has_end = "end_line" in arguments
    if has_offset and (has_start or has_end):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    start_line, end_line, range_ok = parse_line_range(arguments)
    if not range_ok:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if not has_start:
        start_line = None
    if not has_end:
        end_line = None
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", FILE_READ_DEFAULT)
    if type(offset) is not int or type(limit) is not int:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if offset < 0 or offset > 65536 or limit < 1 or limit > FILE_READ_MAX:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    read = tools.service.read_file(
        site_id,
        kind,
        slug,
        relative,
        actor=ctx.actor,
        admin_only=True,
    )
    if not read.ok:
        failure = safe_failure(read)
        if failure.get("error_class") == "FileMissing":
            failure["recovery_tool"] = "wp_search_component"
            failure["hint"] = (
                f"Filen '{relative}' finns inte i {kind} '{slug}'. "
                f"Använd wp_search_component(site_id='{site_id}', kind='{kind}', slug='{slug}', query=...) "
                f"eller wp_list_component_tree(site_id='{site_id}', kind='{kind}', slug='{slug}') "
                "för att hitta rätt mallfil."
            )
        return ToolOutput(failure, site_id)
    data = read.data or {}
    body = data.get("content")
    if not isinstance(body, str):
        return ToolOutput(error_payload("FileUnavailable"), site_id)
    body = redact_secret_text(body, redact_whole_lines=True)
    lines = body.splitlines()
    total_lines = len(lines)
    live = data.get("live_calls") is True
    source_name = "sftp" if data.get("ssh") is True else "mock"
    if start_line is not None or end_line is not None:
        s_line = start_line if start_line is not None else 1
        e_line = end_line if end_line is not None else min(total_lines, s_line + 119)
        out_lines: list[str] = []
        used_bytes = 0
        actual_end = total_lines if s_line > total_lines else s_line - 1
        for idx in range(s_line, min(e_line, total_lines) + 1):
            formatted = f"{idx}: {lines[idx - 1]}"
            chunk = formatted.encode("utf-8")
            sep_bytes = 1 if out_lines else 0
            if out_lines and used_bytes + sep_bytes + len(chunk) > limit:
                break
            if not out_lines and len(chunk) > limit:
                formatted, _ = utf8_window(chunk, 0, limit)
                out_lines.append(formatted)
                actual_end = idx
                break
            out_lines.append(formatted)
            used_bytes += sep_bytes + len(chunk)
            actual_end = idx
        next_line = actual_end + 1 if actual_end < total_lines else None
        return ToolOutput(
            {
                "ok": True,
                "status": "read",
                "site_id": site_id,
                "path": relative,
                "source": source_name,
                "content": "\n".join(out_lines),
                "start_line": s_line,
                "end_line": actual_end,
                "total_lines": total_lines,
                "next_line": next_line,
                "truncated": next_line is not None,
                "sha256": data.get("digest"),
                "ssh": source_name == "sftp",
                "live_calls": live,
            },
            site_id=site_id,
        )
    encoded_body = body.encode()
    preview, next_offset = utf8_window(encoded_body, offset, limit)
    return ToolOutput(
        {
            "ok": True,
            "status": "read",
            "site_id": site_id,
            "path": relative,
            "source": source_name,
            "content": preview,
            "offset": offset,
            "next_offset": next_offset,
            "total_lines": total_lines,
            "truncated": next_offset is not None,
            "sha256": data.get("digest"),
            "ssh": source_name == "sftp",
            "live_calls": live,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_component_context",
    group="file",
    label="Hämta komponentkontext",
    short_label="Komponentkontext",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string"}},
        "additionalProperties": False,
    },
    outcome=_component_context_outcome,
    in_file_schemas=True,
    in_core_file_tools=True,
    capability_group="drift_analysis",
)
def handle_component_context(tools: Any, ctx: ToolContext, _arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    shown = tools.service.show_site(site_id, actor=ctx.actor)
    if not shown.ok:
        return ToolOutput(safe_failure(shown), site_id)
    site = dict((shown.data or {}).get("site") or {})
    if not isinstance(site, dict):
        return ToolOutput(error_payload("SiteUnavailable"), site_id)
    canonical = site.get("canonical_url") if isinstance(site.get("canonical_url"), str) else ""
    for key in list(site):
        if key.startswith("ssh_") or key in {"secret_ref", "files_root", "host_public_key"}:
            site.pop(key, None)
    php = site.get("php_version") if isinstance(site.get("php_version"), str) else ""
    php_known = bool(php) and php not in {"unknown", "unconfigured"}
    wordpress = site.get("wordpress_version") if isinstance(site.get("wordpress_version"), str) else ""
    site_model = tools._site_model(site_id)
    source = _call_component_rest_source(canonical, site=site_model)
    themes: dict[str, Any] = {"source": source, "items": None}
    plugins: dict[str, Any] = {"source": source, "items": None}
    if source == "live":
        themes = {"source": "live", "items": _call_load_live_theme_rows(canonical, site=site_model)}
        plugins = {"source": "live", "items": _call_load_live_plugin_rows(canonical, site=site_model)}
    theme_relationship = "unknown"
    theme_items = themes.get("items")
    if isinstance(theme_items, list):
        for row in theme_items:
            if not isinstance(row, dict) or row.get("status") != "active":
                continue
            template = row.get("template") or ""
            stylesheet = row.get("stylesheet") or ""
            if isinstance(template, str) and isinstance(stylesheet, str) and template and stylesheet:
                theme_relationship = "child" if template != stylesheet else "no_child"
                break
    return ToolOutput(
        {
            "ok": True,
            "status": "context",
            "site_id": site_id,
            "environment": site.get("environment"),
            "wordpress_version": wordpress or None,
            "version_source": "registry" if wordpress and wordpress not in {"unknown", "unconfigured"} else "unknown",
            "php_version": php if php_known else None,
            "php_source": "registry" if php_known else "unknown",
            "woocommerce": site.get("woocommerce"),
            "woo_version": site.get("woo_version") or None,
            "capabilities": site.get("capabilities") if isinstance(site.get("capabilities"), dict) else {},
            "themes": themes,
            "plugins": plugins,
            "system_status": {"source": "not_called"},
            "theme_relationship": theme_relationship,
            "live_calls": source == "live",
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_list_components",
    group="file",
    label="Lista komponenter",
    short_label="Komponentlista",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["theme", "plugin", "mu_plugin"]},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    outcome=count_outcome("slugs", "komponenter", "komponenter listade"),
    in_file_schemas=True,
    in_core_file_tools=True,
    capability_group="drift_analysis",
)
def handle_list_components(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    kind = arguments.get("kind")
    if not isinstance(kind, str) or kind not in _COMPONENT_KINDS:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    listed = tools.service.list_components(site_id, kind, actor=ctx.actor)
    if listed.ok and isinstance(listed.data, dict):
        payload = dict(listed.data)
        payload.setdefault("ok", True)
        payload.setdefault("status", "listed")
        listed = WpOpsResult(True, listed.message, payload)
    return tools._file_result(listed, site_id)


@register_tool(
    name="wp_theme_overview",
    group="file",
    label="Temaöversikt",
    short_label="Temaöversikt",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "slug": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=_theme_overview_outcome,
    in_file_schemas=True,
    in_core_file_tools=True,
    capability_group="drift_analysis",
)
def handle_theme_overview(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    shown = tools.service.show_site(site_id, actor=ctx.actor)
    if not shown.ok:
        return ToolOutput(safe_failure(shown), site_id)
    slug_arg = arguments.get("slug")
    if "slug" in arguments and (not isinstance(slug_arg, str) or not SLUG_RE.fullmatch(slug_arg)):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    site_dict = (shown.data or {}).get("site") or {}
    canonical = site_dict.get("canonical_url") if isinstance(site_dict, dict) else ""
    site_model = tools._site_model(site_id)
    active_theme: str | None = None
    parent_theme: str | None = None
    theme_relationship = "unknown"
    version: str | None = None
    active_row, live_calls = active_live_theme(canonical, site=site_model)
    if active_row is not None:
        stylesheet = active_row.get("stylesheet") or ""
        template = active_row.get("template") or ""
        if isinstance(stylesheet, str) and stylesheet:
            active_theme = stylesheet
        if isinstance(template, str) and isinstance(stylesheet, str) and template and stylesheet:
            if template != stylesheet:
                theme_relationship = "child"
                parent_theme = template
            else:
                theme_relationship = "no_child"
        ver = active_row.get("version") or ""
        if isinstance(ver, str) and ver:
            version = ver
    chosen_slug = slug_arg or active_theme
    if not chosen_slug:
        listed = tools.service.list_components(site_id, "theme", actor=ctx.actor)
        if not listed.ok:
            return ToolOutput(safe_failure(listed), site_id)
        slugs = (listed.data or {}).get("slugs") or []
        if not slugs:
            return ToolOutput(error_payload("ComponentNotFound"), site_id)
        chosen_slug = slugs[0]
        if active_theme is None:
            active_theme = chosen_slug
    elif active_theme is None:
        active_theme = chosen_slug

    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    try:
        base_dir = component_dir(site_obj.files_root, "theme", chosen_slug)
    except PathRejected:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    transport = editor._transport(site_obj)
    ssh = editor._ssh(site_obj)
    with open_transport_batch(transport):
        root_names, entries, woo_overrides, root_readable, truncated = walk_component_tree(
            transport,
            base_dir,
            max_depth=3,
            max_entries=120,
            max_woo_overrides=40,
        )
    has_theme_json = "theme.json" in root_names
    has_html_templates = any(
        row["kind"] == "file"
        and row["path"].startswith("templates/")
        and row["path"].lower().endswith(".html")
        for row in entries
    )
    if has_theme_json or has_html_templates:
        theme_kind = "block"
    elif root_readable:
        theme_kind = "classic"
    else:
        theme_kind = "unknown"
    return ToolOutput(
        {
            "ok": True,
            "status": "overview",
            "site_id": site_id,
            "environment": site_obj.environment,
            "active_theme": active_theme,
            "parent_theme": parent_theme,
            "theme_relationship": theme_relationship,
            "theme_kind": theme_kind,
            "version": version,
            "entries": entries,
            "woocommerce_overrides": woo_overrides,
            "truncated": truncated,
            "ssh": ssh,
            "live_calls": live_calls or ssh,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_plugin_overview",
    group="file",
    label="Pluginöversikt",
    short_label="Pluginöversikt",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "slug": {"type": "string"},
        },
        "required": ["slug"],
        "additionalProperties": False,
    },
    outcome=_plugin_overview_outcome,
    in_file_schemas=True,
    in_core_file_tools=True,
    capability_group="drift_analysis",
)
def handle_plugin_overview(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    shown = tools.service.show_site(site_id, actor=ctx.actor)
    if not shown.ok:
        return ToolOutput(safe_failure(shown), site_id)
    editor, site_obj, denied = authorize_editor_site(
        tools.service,
        site_id,
        ctx.actor,
        write=False,
        extra_permission=Permission.WP_OPS_ADMIN,
        action="wp_ops_file_read",
    )
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    slug = arguments.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    try:
        base_dir = component_dir(site_obj.files_root, "plugin", slug)
    except PathRejected:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    site_dict = (shown.data or {}).get("site") or {}
    canonical = site_dict.get("canonical_url") if isinstance(site_dict, dict) else ""
    live_calls = False
    rest_meta: dict[str, str | None] = {
        "version": None,
        "status": None,
        "requires_wp": None,
        "requires_php": None,
    }
    if _rest_reads_allowed() and isinstance(canonical, str) and _call_component_rest_source(canonical, site=site_obj) == "live":
        live_calls = True
        for row in _call_load_live_plugin_rows(canonical, site=site_obj):
            plugin_path = row.get("plugin") or ""
            row_slug = plugin_path.split("/", 1)[0]
            if row_slug == slug or plugin_path == f"{slug}.php":
                rest_meta = {
                    "version": row.get("version") or None,
                    "status": row.get("status") or None,
                    "requires_wp": row.get("requires_wp") or None,
                    "requires_php": row.get("requires_php") or None,
                }
                break
    transport = editor._transport(site_obj)
    ssh = editor._ssh(site_obj)
    main_file: str | None = None
    main_body = ""
    with open_transport_batch(transport):
        root_names, entries, _, _, truncated = walk_component_tree(
            transport,
            base_dir,
            max_depth=2,
            max_entries=80,
            max_woo_overrides=0,
        )
        php_files = [name for name in root_names if name.lower().endswith(".php") and not sensitive_relative(name)]
        preferred = f"{slug}.php"
        candidates = ([preferred] if preferred in php_files else []) + [name for name in php_files if name != preferred][:9]
        fallback_name: str | None = None
        fallback_body = ""
        for fname in candidates:
            try:
                raw_bytes = transport.read_bytes(f"{base_dir}/{fname}")
            except (PathRejected, TransportError):
                continue
            if len(raw_bytes) > 65536 or b"\x00" in raw_bytes:
                continue
            try:
                decoded = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if fname == preferred:
                main_file = fname
                main_body = decoded
                break
            if fallback_name is None:
                fallback_name = fname
                fallback_body = decoded
            if re.search(r"(?im)^\s*(?:\*\s*)?Plugin\s+Name:", decoded):
                main_file = fname
                main_body = decoded
                break
        if main_file is None and fallback_name is not None:
            main_file = fallback_name
            main_body = fallback_body
    scrubbed_body = editor._scrub(site_obj, main_body) if main_body else ""
    if scrubbed_body:
        scrubbed_body = redact_secret_text(scrubbed_body, redact_whole_lines=True)
    has_hpos = bool(
        scrubbed_body and "declare_compatibility" in scrubbed_body and "custom_order_tables" in scrubbed_body
    )
    wc_match = WC_TESTED_RE.search(scrubbed_body) if scrubbed_body else None
    wc_tested = wc_match.group(1).strip()[:32] if wc_match and wc_match.group(1).strip() else None
    requires_plugins = _parse_requires_plugins(scrubbed_body)
    hooks: list[str] = []
    seen_hooks: set[str] = set()
    if scrubbed_body:
        for _, hook_name in HOOK_RE.findall(scrubbed_body):
            if hook_name not in seen_hooks:
                seen_hooks.add(hook_name)
                hooks.append(hook_name)
                if len(hooks) >= 30:
                    break
    hook_priorities = _extract_hook_priorities(scrubbed_body)
    auto_update: bool | None = None
    update_available: bool | None = None
    new_version: str | None = None
    is_critical = slug.lower() in _critical_plugin_slugs()
    inv_fn = getattr(tools.service, "plugin_inventory", None)
    if callable(inv_fn):
        try:
            inv_res = inv_fn(site_id, actor=ctx.actor, include_updates=True, include_wporg=False)
            if inv_res.ok and isinstance(inv_res.data, dict):
                for prow in inv_res.data.get("plugins") or []:
                    if not isinstance(prow, dict):
                        continue
                    prow_slug = str(prow.get("slug") or "")
                    if prow_slug != slug:
                        continue
                    if isinstance(prow.get("auto_update"), bool):
                        auto_update = prow["auto_update"]
                    if isinstance(prow.get("update_available"), bool):
                        update_available = prow["update_available"]
                    if isinstance(prow.get("new_version"), str) and prow["new_version"]:
                        new_version = short_text(prow["new_version"], 32)
                    if prow.get("is_critical") is True:
                        is_critical = True
                    inv_req = prow.get("requires_plugins")
                    if isinstance(inv_req, list):
                        for req_slug in inv_req:
                            if isinstance(req_slug, str) and req_slug.strip() and req_slug.strip() not in requires_plugins:
                                requires_plugins.append(req_slug.strip())
                    if not rest_meta["version"] and isinstance(prow.get("version"), str) and prow["version"]:
                        rest_meta["version"] = short_text(prow["version"], 32)
                    if not rest_meta["requires_wp"] and isinstance(prow.get("requires_wp"), str) and prow["requires_wp"]:
                        rest_meta["requires_wp"] = short_text(prow["requires_wp"], 24)
                    if not rest_meta["requires_php"] and isinstance(prow.get("requires_php"), str) and prow["requires_php"]:
                        rest_meta["requires_php"] = short_text(prow["requires_php"], 24)
                    break
        except Exception:
            pass
    try:
        recommended: str | None = site_plugin_slug(site_id)
    except ReviewError:
        recommended = None
    policy = (
        "site_plugin_target"
        if recommended is not None and slug == recommended
        else "read_only_extend_via_site_plugin"
    )
    return ToolOutput(
        {
            "ok": True,
            "status": "overview",
            "site_id": site_id,
            "environment": site_obj.environment,
            "slug": slug,
            "main_file": main_file,
            "version": rest_meta["version"],
            "plugin_status": rest_meta["status"],
            "requires_wp": rest_meta["requires_wp"],
            "requires_php": rest_meta["requires_php"],
            "requires_plugins": requires_plugins,
            "auto_update": auto_update,
            "update_available": update_available,
            "new_version": new_version,
            "is_critical": is_critical,
            "has_woo_hpos_declaration": has_hpos,
            "wc_tested_up_to": wc_tested,
            "hooks": hooks,
            "hook_priorities": hook_priorities,
            "entries": entries,
            "truncated": truncated,
            "third_party_edit_policy": policy,
            "recommended_site_plugin": recommended,
            "ssh": ssh,
            "live_calls": live_calls or ssh,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_list_component_tree",
    group="file",
    label="Lista katalogstruktur",
    short_label="Filträd",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["theme", "plugin", "mu_plugin"]},
            "slug": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["kind", "slug"],
        "additionalProperties": False,
    },
    outcome=count_outcome("entries", "katalogposter", "katalog listad", truncated_suffix=True),
    in_file_schemas=True,
    in_core_file_tools=True,
    capability_group="drift_analysis",
)
def handle_list_component_tree(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    kind = arguments.get("kind")
    slug = arguments.get("slug")
    relative = arguments.get("path", "")
    if (
        not isinstance(kind, str)
        or kind not in _COMPONENT_KINDS
        or not isinstance(slug, str)
        or not SLUG_RE.fullmatch(slug)
        or not isinstance(relative, str)
        or (relative and FILE_PATH_RE.fullmatch(relative) is None)
    ):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    listed = tools.service.list_component_tree(site_id, kind, slug, relative, actor=ctx.actor)
    return tools._file_result(listed, site_id)


@register_tool(
    name="wp_search_component",
    group="file",
    label="Sök i komponent",
    short_label="Filsökning",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "kind": {"type": "string"},
            "slug": {"type": "string"},
            "path": {"type": "string"},
            "plan_id": {"type": "string"},
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["literal", "regex"]},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 3},
            "extensions": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "case_insensitive": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    outcome=count_outcome("hits", "sökträffar", "sökning klar", truncated_suffix=True),
    in_file_schemas=True,
    in_core_file_tools=True,
    search_budget=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_search_component(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    query = arguments.get("query")
    if not isinstance(query, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    relative = arguments.get("path", "")
    if not isinstance(relative, str) or (relative and FILE_PATH_RE.fullmatch(relative) is None):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    extensions = arguments.get("extensions")
    if "extensions" in arguments and (
        not isinstance(extensions, list)
        or not extensions
        or len(extensions) > 6
        or any(not isinstance(item, str) for item in extensions)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    case_insensitive = arguments.get("case_insensitive", True)
    if type(case_insensitive) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    mode = arguments.get("mode", "literal")
    if not isinstance(mode, str) or mode not in {"literal", "regex"}:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    context_lines = arguments.get("context_lines", 0)
    if type(context_lines) is not int or context_lines < 0 or context_lines > 3:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    plan_id = arguments.get("plan_id")
    kind = arguments.get("kind")
    slug = arguments.get("slug")
    if plan_id is not None:
        if "kind" in arguments or "slug" in arguments:
            err = error_payload("InvalidSearch")
            err["hint"] = (
                "Kombinera inte plan_id med kind/slug i wp_search_component. "
                "Skicka bara plan_id för att söka i arbetsytans work/, "
                "eller utelämna plan_id och skicka kind + slug för att söka i ett installerat tema/plugin (t.ex. föräldratemat)."
            )
            return ToolOutput(err, site_id)
        _editor, site_obj, denied = authorize_editor_site(
            tools.service,
            site_id,
            ctx.actor,
            write=False,
            extra_permission=Permission.WP_OPS_ADMIN,
            action="wp_ops_file_read",
        )
        if denied is not None:
            return tools._file_result(denied, site_id)
        assert site_obj is not None
        ops = tools._review_plans()
        status_res = ops.workspace_status(site_id, str(plan_id), actor=ctx.actor)
        if not status_res.ok:
            return tools._file_result(status_res, site_id)
        if sensitive_relative(relative):
            return ToolOutput(error_payload("PathRejected"), site_id)
        files_dict, _ = tools._read_workspace_text_files(
            site_id,
            str(plan_id),
            rel_prefix=relative,
            site_obj=site_obj,
        )
        bound = bind_credential(site_obj)
        extra_secrets = [bound.value] if bound.present else []
        try:
            found = search_texts(
                files_dict,
                query,
                mode=mode,
                context_lines=context_lines,
                case_insensitive=case_insensitive,
                extensions=extensions,
                path_prefix=relative or None,
                max_matches=20,
                extra_secrets=extra_secrets,
            )
        except ValueError:
            err = error_payload("InvalidSearch")
            err["hint"] = (
                "Ogiltig sökning: query måste vara 2–80 tecken (och giltig regex vid mode='regex'). "
                "För att söka i ett installerat tema/föräldratema: utelämna plan_id och ange kind='theme' + slug."
            )
            return ToolOutput(err, site_id)
        payload: dict[str, Any] = {
            "ok": True,
            "status": "searched",
            "site_id": site_id,
            "plan_id": str(plan_id),
            "source_scope": "workspace",
            **found,
            "ssh": False,
            "live_calls": False,
        }
        if not found.get("hits"):
            plan_row = ops.get(str(plan_id), site_id=site_id)
            if isinstance(plan_row, dict) and plan_row.get("kind") == "theme":
                parent_slug = tools._workspace_parent_slug(site_id, str(plan_id), actor=ctx.actor)
                if parent_slug:
                    payload["parent_theme_slug"] = parent_slug
                    payload["hint"] = (
                        "Inga träffar i arbetsytans work/. Om mallfilen ligger i föräldratemat, "
                        f"sök i föräldratemat med wp_search_component(site_id='{site_id}', kind='theme', "
                        f"slug='{parent_slug}', query=...) utan plan_id."
                    )
        return ToolOutput(payload, site_id=site_id)

    if "kind" not in arguments or "slug" not in arguments:
        err = error_payload("InvalidToolArguments")
        err["hint"] = (
            "Ange antingen plan_id (för att söka i arbetsytan) eller både kind och slug "
            "(för att söka i ett installerat tema/plugin/MU-plugin)."
        )
        return ToolOutput(err, site_id)
    if (
        not isinstance(kind, str)
        or kind not in _COMPONENT_KINDS
        or not isinstance(slug, str)
        or not SLUG_RE.fullmatch(slug)
    ):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    found_res = tools.service.search_component(
        site_id,
        kind,
        slug,
        relative,
        query,
        actor=ctx.actor,
        extensions=extensions,
        case_insensitive=case_insensitive,
        mode=mode,
        context_lines=context_lines,
    )
    out = tools._file_result(found_res, site_id)
    if not found_res.ok and out.data.get("error_class") == "InvalidSearch":
        data = dict(out.data)
        data["hint"] = (
            "Ogiltig sökning: query måste vara 2–80 tecken (och giltig regex vid mode='regex'). "
            "Kombinera inte plan_id med kind/slug."
        )
        return ToolOutput(data, site_id=out.site_id)
    return out


@register_tool(
    name="wp_code_index",
    group="file",
    label="Indexera källkod",
    short_label="Kodindexering",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["theme", "plugin", "mu_plugin"]},
            "slug": {"type": "string"},
            "path": {"type": "string"},
            "plan_id": {"type": "string"},
            "query": {"type": "string"},
            "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
        "additionalProperties": False,
    },
    outcome=count_outcome("entries", "symboler indexerade", "kodindex klart"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    search_budget=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_code_index(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    editor, site_obj, denied = authorize_editor_site(
        tools.service,
        site_id,
        ctx.actor,
        write=False,
        extra_permission=Permission.WP_OPS_ADMIN,
        action="wp_ops_file_read",
    )
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None

    plan_id = arguments.get("plan_id")
    kind = arguments.get("kind")
    slug = arguments.get("slug")
    relative = arguments.get("path", "")
    query = arguments.get("query")
    symbols = arguments.get("symbols")

    if "kind" in arguments and (not isinstance(kind, str) or kind not in _COMPONENT_KINDS):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if "slug" in arguments and (not isinstance(slug, str) or not SLUG_RE.fullmatch(slug)):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    if plan_id is None and (not isinstance(kind, str) or not isinstance(slug, str)):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if not isinstance(relative, str) or (relative and FILE_PATH_RE.fullmatch(relative) is None):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    if sensitive_relative(relative):
        return ToolOutput(error_payload("PathRejected"), site_id)
    if "query" in arguments and (not isinstance(query, str) or len(query) > 80):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if "symbols" in arguments and (
        not isinstance(symbols, list)
        or not symbols
        or len(symbols) > 12
        or any(not isinstance(item, str) or not item.strip() for item in symbols)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    if plan_id is not None:
        ops = tools._review_plans()
        status_res = ops.workspace_status(site_id, str(plan_id), actor=ctx.actor)
        if not status_res.ok:
            return tools._file_result(status_res, site_id)
        source_files, files_truncated = tools._read_workspace_text_files(
            site_id,
            str(plan_id),
            rel_prefix=relative,
            allowed_exts=CODE_INDEX_EXTENSIONS,
            site_obj=site_obj,
        )
        ssh = False
    else:
        try:
            source_files, files_truncated = read_installed_index_files(
                site_obj, str(kind), str(slug), relative, editor
            )
        except PathRejected:
            return ToolOutput(error_payload("PathRejected"), site_id)
        ssh = editor._ssh(site_obj)

    indexed = index_sources(source_files, query=query, symbols=symbols, max_results=60)
    for entry in indexed.get("entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("signature"), str):
            entry["signature"] = tools._scrub_bounded(site_obj, entry["signature"], max_len=160)

    return ToolOutput(
        {
            "ok": True,
            "status": "indexed",
            "site_id": site_id,
            "source_scope": "workspace" if plan_id is not None else "installed",
            "plan_id": plan_id,
            "kind": kind,
            "slug": slug,
            "path": relative,
            "files_truncated": files_truncated,
            **indexed,
            "ssh": ssh,
            "live_calls": ssh,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_template_hierarchy",
    group="file",
    label="Kontrollera mallhierarki",
    short_label="Mallhierarki",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "context": {"type": "string", "enum": list(TEMPLATE_CONTEXTS)},
            "slug": {"type": "string"},
            "post_type": {"type": "string"},
            "taxonomy": {"type": "string"},
            "theme_slug": {"type": "string"},
        },
        "required": ["context"],
        "additionalProperties": False,
    },
    outcome=_hierarchy_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    capability_group="drift_analysis",
)
def handle_template_hierarchy(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None

    context = arguments.get("context")
    if not isinstance(context, str) or not context.strip():
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    for opt_key in ("slug", "post_type", "taxonomy", "theme_slug"):
        if opt_key in arguments and arguments[opt_key] is not None and not isinstance(arguments[opt_key], str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    explicit_theme = arguments.get("theme_slug")
    if explicit_theme is not None and explicit_theme != "" and SLUG_RE.fullmatch(explicit_theme) is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    target_theme, parent_theme, live_calls = resolve_active_theme(
        tools,
        site_id,
        ctx.actor,
        site_obj,
        explicit_slug=explicit_theme or None,
    )
    try:
        hierarchy = template_candidates(
            context,
            slug=arguments.get("slug"),
            post_type=arguments.get("post_type"),
            taxonomy=arguments.get("taxonomy"),
            theme_slug=target_theme,
        )
    except ValueError:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    existing_set: set[str] = set()
    theme_kind = "unknown"
    if target_theme and SLUG_RE.fullmatch(target_theme):
        transport = editor._transport(site_obj)
        with open_transport_batch(transport):
            try:
                base_dir = component_dir(site_obj.files_root, "theme", target_theme)
            except PathRejected:
                base_dir = ""
            if base_dir:
                try:
                    root_names = set(transport.list_dir(base_dir))
                except (PathRejected, TransportError):
                    root_names = set()
                for name in root_names:
                    if "." in name and not sensitive_relative(name):
                        existing_set.add(name)
                if "templates" in root_names:
                    try:
                        for name in transport.list_dir(f"{base_dir}/templates"):
                            if "." in name and not sensitive_relative(name):
                                existing_set.add(f"templates/{name}")
                    except (PathRejected, TransportError):
                        pass
                if "woocommerce" in root_names:
                    for sub in ("", "single-product", "cart", "checkout", "myaccount", "loop", "global"):
                        sub_dir = f"{base_dir}/woocommerce/{sub}" if sub else f"{base_dir}/woocommerce"
                        prefix = f"woocommerce/{sub}" if sub else "woocommerce"
                        try:
                            for name in transport.list_dir(sub_dir):
                                if "." in name and not sensitive_relative(name):
                                    existing_set.add(f"{prefix}/{name}")
                        except (PathRejected, TransportError):
                            continue
                has_html = any(p.startswith("templates/") and p.lower().endswith(".html") for p in existing_set)
                if "theme.json" in root_names or has_html:
                    theme_kind = "block"
                elif root_names:
                    theme_kind = "classic"

    ordered_all = list(hierarchy.get("woo_candidates") or []) + (
        list(hierarchy.get("block_candidates") or [])
        if theme_kind == "block"
        else list(hierarchy.get("candidates") or [])
    )
    existing_candidates = [cand for cand in ordered_all if cand in existing_set]
    ssh = editor._ssh(site_obj)
    return ToolOutput(
        {
            "ok": True,
            "status": "hierarchy",
            "site_id": site_id,
            "theme_slug": target_theme,
            "parent_theme": parent_theme,
            "theme_kind": theme_kind,
            "existing_candidates": existing_candidates,
            "first_match": existing_candidates[0] if existing_candidates else None,
            **hierarchy,
            "ssh": ssh,
            "live_calls": live_calls or ssh,
        },
        site_id=site_id,
    )


@register_tool(
    name="wc_template_overrides_audit",
    group="file",
    label="Granska Woo-malloverrides",
    short_label="WooCommerce-mallgranskning",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "theme_slug": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("override-granskning", fallback="override-granskning klar", parenthesized=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    read_budget=True,
    capability_group="drift_analysis",
)
def handle_woo_overrides_audit(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    from ecom_ops.wp_ops.review_workspace import read_theme_woo_files
    from ecom_ops.wp_ops.woo_templates import audit_overrides as run_override_audit

    site_id = str(ctx.site_id)
    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    explicit_slug = arguments.get("theme_slug")
    if "theme_slug" in arguments and (not isinstance(explicit_slug, str) or not SLUG_RE.fullmatch(explicit_slug)):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    target_slug, _parent, live_calls = resolve_active_theme(
        tools,
        site_id,
        ctx.actor,
        site_obj,
        explicit_slug=explicit_slug,
    )
    if not target_slug:
        return ToolOutput(error_payload("ComponentNotFound"), site_id)
    ssh = editor._ssh(site_obj)
    transport = editor._transport(site_obj)
    try:
        scanned = read_theme_woo_files(transport, site_obj.files_root, target_slug)
    except ReviewError as exc:
        return tools._file_result(editor._fail(exc.error_class, exc.error_class, ssh=ssh), site_id)
    report = run_override_audit(
        scanned["woo_files"],
        woo_version=site_obj.woo_version or None,
    )
    payload = {
        "ok": True,
        "status": "audited",
        "site_id": site_obj.id,
        "slug": target_slug,
        "has_woocommerce_dir": bool(scanned["has_woocommerce_dir"]),
        "root_wc_templates": list(scanned["root_wc_templates"]),
        "skipped_files": int(scanned.get("skipped_files") or 0),
        **report,
        "ssh": ssh,
        "live_calls": bool(ssh or live_calls),
    }
    return tools._file_result(WpOpsResult(True, "woo overrides audited", payload), site_id)


@register_tool(
    name="wp_component_intel",
    group="file",
    label="Hämta komponentmetadata",
    short_label="Komponentinformation",
    schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["theme", "plugin"]},
            "slug": {"type": "string"},
            "installed_version": {"type": "string"},
        },
        "required": ["kind", "slug"],
        "additionalProperties": False,
    },
    outcome=_intel_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    capability_group="drift_analysis",
)
def handle_component_intel(_tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WP_OPS_READ)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied)

    kind = arguments.get("kind")
    slug = arguments.get("slug")
    installed_version = arguments.get("installed_version")
    if kind not in {"theme", "plugin"} or not isinstance(slug, str) or not INTEL_SLUG_RE.fullmatch(slug):
        return ToolOutput(error_payload("InvalidToolArguments"))
    if "installed_version" in arguments and (
        not isinstance(installed_version, str) or len(installed_version) > 40
    ):
        return ToolOutput(error_payload("InvalidToolArguments"))

    try:
        intel = fetch_component_intel(kind, slug, installed_version=installed_version)
    except ValueError:
        return ToolOutput(error_payload("InvalidToolArguments"))
    err = intel.get("error_class")
    return ToolOutput(
        {
            "ok": not bool(err),
            "status": "unknown" if err else (intel.get("source") or "ok"),
            "live_calls": bool(intel.get("intel_enabled") and not err),
            **intel,
        }
    )


@register_tool(
    name="wp_playbook",
    group="file",
    label="Hämta åtgärdsguide",
    short_label="WP/Woo-playbook",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "enum": list(PLAYBOOK_TOPICS)},
        },
        "required": ["topic"],
        "additionalProperties": False,
    },
    outcome=_playbook_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    capability_group="drift_analysis",
)
def handle_playbook(_tools: Any, _ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    topic = arguments.get("topic")
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 80:
        return ToolOutput(error_payload("InvalidToolArguments"))
    playbook = get_playbook(topic)
    if not playbook.get("ok"):
        return ToolOutput(
            {
                "ok": False,
                "status": "unknown",
                "error_class": "UnknownPlaybook",
                "live_calls": False,
                **playbook,
            }
        )
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "live_calls": False,
            **playbook,
        }
    )


__all__ = [
    "active_live_theme",
    "component_rest_source",
    "handle_code_index",
    "handle_component_context",
    "handle_component_intel",
    "handle_list_component_tree",
    "handle_list_components",
    "handle_playbook",
    "handle_plugin_overview",
    "handle_read_theme_plugin_file",
    "handle_search_component",
    "handle_template_hierarchy",
    "handle_theme_overview",
    "handle_woo_overrides_audit",
    "has_site_rest_credentials",
    "is_parent_theme_on_site",
    "known_parent_slug",
    "load_live_plugin_rows",
    "load_live_theme_rows",
    "open_transport_batch",
    "parse_style_headers",
    "read_installed_index_files",
    "resolve_active_theme",
    "same_configured_host",
    "scan_local_themes",
    "walk_component_tree",
]
