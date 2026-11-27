"""Review workspace lifecycle/editing/checks tools and single-file (fe-*) plan tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission, resolve_actor
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.file_changes import _MAX_BYTES, sensitive_relative
from ecom_ops.wp_ops.files import (
    LocalTree,
    PathRejected,
    TransportError,
    component_dir,
    resolve_file,
)
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.review_workspace import (
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
    iter_files,
    site_plugin_slug,
    workspace_root,
)
from ecom_ops.wp_ops.static_checks import run_static_checks
from ecom_ops.wp_ops.tools.components import (
    _call_component_rest_source,
    _call_load_live_plugin_rows,
    _rest_reads_allowed,
    active_live_theme,
    is_parent_theme_on_site,
    known_parent_slug,
    parse_style_headers,
    scan_local_themes,
)
from ecom_ops.wp_ops.tools.registry import (
    count_outcome,
    plan_id_outcome,
    read_budget_outcome,
    register_tool,
    status_outcome,
)
from ecom_ops.wp_ops.tools.types import (
    CODE_INDEX_EXTENSIONS,
    FILE_PATH_RE,
    FILE_SHA_RE,
    REVIEW_PLAN_RE,
    SCAFFOLD_ONLY_FILES,
    SITE_ID_RE,
    SLUG_RE,
    VARIANT_GROUP_RE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import (
    error_payload,
    parse_line_range,
    safe_failure,
    short_text,
)
from ecom_ops.wp_ops.tools.workspace_io import authorize_editor_site, resolve_sandbox_preview_meta


def workspace_file_list(data_dir: Path, site_id: str, plan_id: str, *, limit: int = 30) -> list[str]:
    try:
        ws_root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return []
    files: list[str] = []
    for rel, _raw in iter_files(ws_root / "work"):
        if sensitive_relative(rel):
            continue
        files.append(rel)
        if len(files) >= limit:
            break
    return files


def workspace_parent_slug(tools: Any, site_id: str, plan_id: str, *, actor: str) -> str | None:
    try:
        ws_root = workspace_root(tools.service.data_dir, site_id, plan_id)
    except ReviewError:
        ws_root = None
    if ws_root is not None:
        for folder in ("work", "base"):
            style_path = ws_root / folder / "style.css"
            if style_path.is_file() and not style_path.is_symlink():
                try:
                    headers = parse_style_headers(style_path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    headers = {}
                tpl = headers.get("template")
                if tpl and SLUG_RE.fullmatch(tpl):
                    return tpl
    return known_parent_slug(tools, site_id, actor)


def enrich_workspace_missing(
    tools: Any,
    out: ToolOutput,
    site_id: str,
    plan_id: str,
    rel_path: str,
    *,
    actor: str,
    ops: Any,
) -> ToolOutput:
    if out.data.get("error_class") != "FileMissing":
        return out
    data = dict(out.data)
    ws_files = workspace_file_list(tools.service.data_dir, site_id, plan_id)
    data["workspace_files"] = ws_files
    plan_row = ops.get(plan_id, site_id=site_id)
    is_theme = isinstance(plan_row, dict) and plan_row.get("kind") == "theme"
    parent_slug = workspace_parent_slug(tools, site_id, plan_id, actor=actor) if is_theme else None
    if parent_slug:
        data["parent_theme_slug"] = parent_slug
        data["recovery_tool"] = "wp_workspace_copy_from_parent"
        data["hint"] = (
            f"Filen '{rel_path}' finns inte i arbetsytans work/ "
            f"(nuvarande filer: {', '.join(ws_files) or 'inga'}). "
            f"Kopiera den från föräldratemat '{parent_slug}' med "
            f"wp_workspace_copy_from_parent(site_id='{site_id}', plan_id='{plan_id}', path='{rel_path}') först."
        )
    else:
        data["recovery_tool"] = "wp_workspace_list_files"
        data["hint"] = (
            f"Filen '{rel_path}' finns inte i arbetsytans work/ "
            f"(nuvarande filer: {', '.join(ws_files) or 'inga'}). "
            "Lista filer med wp_workspace_list_files eller skapa filen med wp_workspace_create_file."
        )
    return ToolOutput(data, site_id=out.site_id)


def read_workspace_text_files(
    tools: Any,
    site_id: str,
    plan_id: str,
    *,
    rel_prefix: str = "",
    allowed_exts: frozenset[str] | None = None,
    site_obj: Site | None = None,
) -> tuple[dict[str, str], bool]:
    try:
        ws_root = workspace_root(tools.service.data_dir, site_id, plan_id)
    except ReviewError:
        return {}, False
    clean_prefix = rel_prefix.strip().replace("\\", "/").strip("/")
    files: dict[str, str] = {}
    total_bytes = 0
    truncated = False
    editor = tools.service._file_editor() if site_obj is not None else None
    for rel, raw_bytes in iter_files(ws_root / "work"):
        if sensitive_relative(rel):
            continue
        if clean_prefix and rel != clean_prefix and not rel.startswith(clean_prefix + "/"):
            continue
        if allowed_exts is not None and Path(rel).suffix.lower() not in allowed_exts:
            continue
        if len(raw_bytes) > _MAX_BYTES or b"\x00" in raw_bytes:
            continue
        if len(files) >= 200 or total_bytes + len(raw_bytes) > 1024 * 1024:
            truncated = True
            break
        try:
            body = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if editor is not None and site_obj is not None:
            body = editor._scrub(site_obj, body)
        files[rel] = body
        total_bytes += len(raw_bytes)
    return files, truncated


def verified_theme_pair(tools: Any, site_id: str, actor: str, requested_slug: str = "") -> VerifiedTheme | None:
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
        if (
            isinstance(template, str)
            and isinstance(stylesheet, str)
            and template
            and stylesheet
            and (not requested_slug or requested_slug in {stylesheet, template})
        ):
            return VerifiedTheme(stylesheet=stylesheet, template=template)

    if not requested_slug or not SLUG_RE.fullmatch(requested_slug):
        return None
    _editor, site_obj, denied = authorize_editor_site(tools.service, site_id, actor, write=False)
    if denied is not None or site_obj is None:
        return None
    local_themes = scan_local_themes(tools, site_obj)
    if not local_themes:
        return None
    for other_slug, meta in local_themes.items():
        if other_slug != requested_slug and meta.get("template") == requested_slug:
            return VerifiedTheme(stylesheet=other_slug, template=requested_slug)
    if requested_slug not in local_themes:
        return None
    meta = local_themes[requested_slug]
    tpl = meta.get("template") or ""
    if tpl and tpl != requested_slug:
        return VerifiedTheme(stylesheet=requested_slug, template=tpl)
    if meta.get("theme_name"):
        return VerifiedTheme(stylesheet=requested_slug, template=requested_slug)
    return None


def verified_plugin(tools: Any, site_id: str, actor: str, requested_slug: str) -> VerifiedPlugin | None:
    if not SLUG_RE.fullmatch(requested_slug) or requested_slug == "woocommerce":
        return None
    shown = tools.service.show_site(site_id, actor=actor)
    if not shown.ok:
        return None
    site = (shown.data or {}).get("site") or {}
    canonical = site.get("canonical_url") if isinstance(site, dict) else ""
    site_model = tools._site_model(site_id)
    if _rest_reads_allowed() and isinstance(canonical, str) and _call_component_rest_source(canonical, site=site_model) == "live":
        for row in _call_load_live_plugin_rows(canonical, site=site_model):
            plugin_path = row.get("plugin") or ""
            slug = plugin_path.split("/", 1)[0]
            if row.get("status") == "active" and slug == requested_slug:
                return VerifiedPlugin(slug=slug)

    try:
        reserved_site_plugin = site_plugin_slug(site_id)
    except ReviewError:
        return None
    if requested_slug != reserved_site_plugin:
        return None
    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, actor, write=False)
    if denied is not None or site_obj is None:
        return None
    transport = editor._transport(site_obj)
    if not isinstance(transport, LocalTree):
        return None
    try:
        plugin_path = component_dir(site_obj.files_root, "plugin", requested_slug)
        names = [name for name in transport.list_dir(plugin_path) if name not in {".", ".."} and not sensitive_relative(name)]
    except (PathRejected, TransportError):
        return None
    if names:
        return VerifiedPlugin(slug=requested_slug)
    return None


# --- Outcome formatters ---

def _workspace_status_outcome(data: dict[str, Any], _reads: int | None) -> str:
    pid = short_text(data.get("plan_id"), 32)
    st = short_text(data.get("status"), 24)
    if pid and st:
        return f"{pid} ({st})"
    return f"status: {st}" if st else "arbetsytans status hämtad"


def _bulk_replace_outcome(data: dict[str, Any], _reads: int | None) -> str:
    if data.get("dry_run") is True:
        return "torrkörning klar"
    files = data.get("files")
    if isinstance(files, list):
        return f"{len(files)} filer ändrade"
    return "massersättning klar"


def _run_checks_outcome(data: dict[str, Any], _reads: int | None) -> str:
    st = short_text(data.get("status"), 24) or "klar"
    ready = " · staging klar" if data.get("staging_ready") is True else ""
    return f"kontroller: {st}{ready}"


# --- Single-file fe-* plan tools (8 tools) ---

_PROPOSE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "site_id": {"type": "string"},
        "kind": {"type": "string"},
        "slug": {"type": "string"},
        "path": {"type": "string"},
        "new_text": {"type": "string"},
        "base_sha256": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["kind", "slug", "path", "new_text", "base_sha256"],
    "additionalProperties": False,
}

_FE_PLAN_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"site_id": {"type": "string"}, "plan_id": {"type": "string"}},
    "required": ["plan_id"],
    "additionalProperties": False,
}


def _propose_file_common(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, is_new: bool) -> ToolOutput:
    site_id = str(ctx.site_id)
    kind = arguments.get("kind")
    slug = arguments.get("slug")
    relative = arguments.get("path", "")
    if (
        not isinstance(kind, str)
        or kind not in {"theme", "plugin"}
        or not isinstance(slug, str)
        or not SLUG_RE.fullmatch(slug)
        or not isinstance(relative, str)
        or (relative and FILE_PATH_RE.fullmatch(relative) is None)
    ):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    new_text = arguments.get("new_text")
    base = arguments.get("base_sha256")
    summary = arguments.get("summary", "")
    if not isinstance(new_text, str) or not isinstance(base, str) or not FILE_SHA_RE.fullmatch(base):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if not isinstance(summary, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    parent = known_parent_slug(tools, site_id, ctx.actor) if kind == "theme" else None
    if is_new:
        proposed = tools.service.propose_new_file(
            site_id, kind, slug, relative, new_text, base, summary, actor=ctx.actor, parent_slug=parent
        )
    else:
        proposed = tools.service.propose_file_diff(
            site_id, kind, slug, relative, new_text, base, summary, actor=ctx.actor, parent_slug=parent
        )
    return tools._file_result(proposed, site_id)


@register_tool(
    name="wp_propose_file_diff",
    group="file",
    label="Skapa filändringsplan",
    short_label="Diffplan",
    schema=_PROPOSE_FILE_SCHEMA,
    outcome=plan_id_outcome("plan {pid} skapad", "filplan skapad", nested_plan=True),
    in_file_schemas=True,
    in_core_file_tools=True,
    deep_phase=True,
    capability_group="fe_plan",
)
def handle_propose_file_diff(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    return _propose_file_common(tools, ctx, arguments, is_new=False)


@register_tool(
    name="wp_propose_new_file",
    group="file",
    label="Skapa ny-fil-plan",
    short_label="Ny filplan",
    schema=_PROPOSE_FILE_SCHEMA,
    outcome=plan_id_outcome("plan {pid} skapad", "filplan skapad", nested_plan=True),
    in_file_schemas=True,
    in_core_file_tools=True,
    deep_phase=True,
    capability_group="fe_plan",
)
def handle_propose_new_file(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    return _propose_file_common(tools, ctx, arguments, is_new=True)


@register_tool(
    name="wp_validate_plan",
    group="file",
    label="Validera filplan",
    short_label="Plangranskning",
    schema=_FE_PLAN_ID_SCHEMA,
    outcome=status_outcome("validering", default_status="validerad"),
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="fe",
    deep_phase=True,
    capability_group="fe_plan",
)
def handle_validate_plan(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    return tools._file_result(tools.service.validate_file_plan(site_id, str(arguments["plan_id"]), actor=ctx.actor), site_id)


@register_tool(
    name="wp_apply_staging",
    group="file",
    label="Förhandsgranska staging-applicering",
    short_label="Stagingförslag",
    schema={
        "type": "object",
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("staging-förhandsvisning {pid}", "staging-förhandsvisning"),
    in_file_schemas=True,
    in_core_file_tools=True,
    requires_site=False,
    plan_kind="fe",
    deep_phase=True,
    capability_group="fe_plan",
)
def handle_apply_staging(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    preview = tools.service.preview_file_stage(str(arguments["plan_id"]), actor=ctx.actor)
    if not preview.ok:
        return ToolOutput(safe_failure(preview))
    data = dict(preview.data or {})
    data.pop("diff", None)
    data["executed"] = False
    return ToolOutput(
        data,
        site_id=data.get("site_id") if isinstance(data.get("site_id"), str) else None,
    )


@register_tool(
    name="wp_request_production",
    group="file",
    label="Begär produktionsgranskning",
    short_label="Produktionsbegäran",
    schema=_FE_PLAN_ID_SCHEMA,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="fe",
    capability_group="fe_plan",
)
def handle_request_production(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    return tools._file_result(
        tools.service.request_file_production(site_id, str(arguments["plan_id"]), actor=ctx.actor), site_id
    )


@register_tool(
    name="wp_deploy_production",
    group="file",
    label="Produktionsdeploy (spärrad)",
    short_label="Produktionsstopp",
    schema=_FE_PLAN_ID_SCHEMA,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="fe",
    capability_group="fe_plan",
)
def handle_deploy_production(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    return tools._file_result(
        tools.service.deploy_file_production(site_id, str(arguments["plan_id"]), actor=ctx.actor), site_id
    )


@register_tool(
    name="wp_verify_change",
    group="file",
    label="Verifiera filändring",
    short_label="Hashkontroll",
    schema=_FE_PLAN_ID_SCHEMA,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="fe",
    capability_group="fe_plan",
)
def handle_verify_change(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    return tools._file_result(tools.service.verify_file_plan(site_id, str(arguments["plan_id"]), actor=ctx.actor), site_id)


@register_tool(
    name="wp_rollback_file",
    group="file",
    label="Återställ fil (spärrad)",
    short_label="Återställning",
    schema=_FE_PLAN_ID_SCHEMA,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="fe",
    capability_group="fe_plan",
)
def handle_rollback_file(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    return tools._file_result(
        tools.service.rollback_file_plan(site_id, str(arguments["plan_id"]), actor=ctx.actor), site_id
    )


# --- Workspace tools (17 tools in this module; 2 sandbox_edits tools live in sandbox_edits.py) ---

def _fork_workspace_impl(
    tools: Any,
    site_id: str,
    plan_id: str,
    arguments: dict[str, Any],
    *,
    actor: str,
    ops: Any,
) -> ToolOutput:
    summary = arguments.get("summary", "")
    variant_group = arguments.get("variant_group", "")
    if not isinstance(summary, str) or len(summary) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if not isinstance(variant_group, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    clean_vg = variant_group.strip().lower()
    if clean_vg and VARIANT_GROUP_RE.fullmatch(clean_vg) is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    forked = ops.fork(site_id, plan_id, actor=actor, summary=summary, variant_group=clean_vg)
    data = dict(forked.data or {})
    data["executed"] = False
    data["live_calls"] = False
    data["ok"] = forked.ok
    if forked.ok:
        new_pid = data.get("plan_id")
        if isinstance(new_pid, str) and REVIEW_PLAN_RE.fullmatch(new_pid):
            data["workspace_files"] = workspace_file_list(tools.service.data_dir, site_id, new_pid)
    return tools._file_result(WpOpsResult(forked.ok, forked.message, data), site_id)


@register_tool(
    name="wp_open_review_workspace",
    group="file",
    label="Öppna granskningsyta",
    short_label="Granskningsyta",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "kind": {"type": "string"},
            "slug": {"type": "string"},
            "summary": {"type": "string"},
            "variant_group": {"type": "string"},
            "base_plan_id": {"type": "string"},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("granskningsyta {pid}", "granskningsyta öppnad"),
    in_file_schemas=True,
    in_core_file_tools=True,
    requires_site=False,
    deep_phase=True,
    capability_group="workspace",
)
def handle_open_review_workspace(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = arguments.get("site_id", ctx.sticky_site_id)
    kind = arguments.get("kind")
    slug = arguments.get("slug", "")
    summary = arguments.get("summary", "")
    variant_group = arguments.get("variant_group", "")
    base_plan_id = arguments.get("base_plan_id", "")
    if not isinstance(site_id, str) or not SITE_ID_RE.fullmatch(site_id):
        return tools._site_required_error(ctx.actor)
    if kind not in {"theme", "plugin"} or not isinstance(slug, str) or not isinstance(summary, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if not isinstance(variant_group, str) or not isinstance(base_plan_id, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    clean_vg = variant_group.strip().lower()
    clean_base = base_plan_id.strip().strip("`")
    if clean_vg and VARIANT_GROUP_RE.fullmatch(clean_vg) is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if clean_base and REVIEW_PLAN_RE.fullmatch(clean_base) is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if slug and SLUG_RE.fullmatch(slug) is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if len(summary) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    ops = tools._review_plans()
    if clean_base:
        return _fork_workspace_impl(
            tools, site_id, clean_base, {"summary": summary, "variant_group": clean_vg}, actor=ctx.actor, ops=ops
        )
    v_theme = verified_theme_pair(tools, site_id, ctx.actor, slug) if kind == "theme" else None
    plugin_target = slug
    if kind == "plugin" and not plugin_target:
        try:
            plugin_target = site_plugin_slug(site_id)
        except ReviewError:
            plugin_target = ""
    v_plugin = verified_plugin(tools, site_id, ctx.actor, plugin_target) if kind == "plugin" and plugin_target else None
    opened = ops.open(
        site_id,
        str(kind),
        slug,
        actor=ctx.actor,
        summary=summary,
        verified_theme=v_theme,
        verified_plugin=v_plugin,
        variant_group=clean_vg,
    )
    data = dict(opened.data or {})
    data["executed"] = False
    data["live_calls"] = False
    data["ok"] = opened.ok
    if opened.ok:
        data["status"] = data.get("status") or "draft"
        opened_pid = data.get("plan_id")
        if isinstance(opened_pid, str) and REVIEW_PLAN_RE.fullmatch(opened_pid):
            ws_files = workspace_file_list(tools.service.data_dir, site_id, opened_pid)
            data["workspace_files"] = ws_files
            if kind == "theme":
                parent_slug: str | None = None
                if v_theme is not None and v_theme.template != v_theme.stylesheet:
                    parent_slug = v_theme.template
                else:
                    parent_slug = workspace_parent_slug(tools, site_id, opened_pid, actor=ctx.actor)
                if parent_slug:
                    data["parent_theme_slug"] = parent_slug
                    is_scaffold_only = not ws_files or set(ws_files).issubset(SCAFFOLD_ONLY_FILES)
                    if is_scaffold_only:
                        data["hint"] = (
                            f"Barntemats arbetsyta innehåller bara {', '.join(ws_files) or 'grundfiler'}. "
                            f"Mallfiler ligger i föräldratemat '{parent_slug}'. Sök där med "
                            f"wp_search_component(site_id='{site_id}', kind='theme', slug='{parent_slug}', query=...) "
                            f"utan plan_id och kopiera in filen med wp_workspace_copy_from_parent(plan_id='{opened_pid}', path=...) före patch."
                        )
    return tools._file_result(WpOpsResult(opened.ok, opened.message, data), site_id)


@register_tool(
    name="wp_list_workspaces",
    group="file",
    label="Lista arbetsytor",
    short_label="Arbetsytelista",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string"}},
        "additionalProperties": False,
    },
    outcome=count_outcome("plans", "arbetsytor", "arbetsytor listade"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    capability_group="workspace",
)
def handle_list_workspaces(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    target_site: str | None
    if "site_id" in arguments:
        raw_site = arguments.get("site_id")
        if raw_site == "":
            target_site = None
        elif isinstance(raw_site, str) and SITE_ID_RE.fullmatch(raw_site):
            target_site = raw_site
        else:
            return ToolOutput(error_payload("InvalidToolArguments"))
    elif isinstance(ctx.sticky_site_id, str) and SITE_ID_RE.fullmatch(ctx.sticky_site_id):
        target_site = ctx.sticky_site_id
    else:
        target_site = None
    listed = tools.service.list_workspaces(target_site, actor=ctx.actor)
    if listed.ok and isinstance(listed.data, dict):
        payload = dict(listed.data)
        payload.setdefault("ok", True)
        payload.setdefault("status", "listed")
        listed = WpOpsResult(True, listed.message, payload)
    return tools._file_result(listed, target_site)


@register_tool(
    name="wp_workspace_fork",
    group="workspace",
    label="Klona/förgrena granskningsyta",
    short_label="Klona arbetsyta",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "summary": {"type": "string"},
            "variant_group": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("granskningsyta {pid} klonad", "granskningsyta klonad"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_fork(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    return _fork_workspace_impl(tools, str(ctx.site_id), str(arguments["plan_id"]), arguments, actor=ctx.actor, ops=ops)


@register_tool(
    name="wp_workspace_list_files",
    group="workspace",
    label="Lista filer i arbetsyta",
    short_label="Arbetsytans filer",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=count_outcome("files", "filer i arbetsyta", "arbetsytans filer listade"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    capability_group="workspace",
)
def handle_workspace_list_files(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_dir = arguments.get("path", "")
    if not isinstance(rel_dir, str) or (rel_dir and FILE_PATH_RE.fullmatch(rel_dir) is None):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    res = ops.workspace_list_files(site_id, plan_id, rel_dir, actor=ctx.actor)
    out = tools._file_result(res, site_id)
    if res.ok and isinstance(res.data, dict) and isinstance(res.data.get("files"), list):
        data = dict(out.data)
        data["status"] = "listed"
        data["files"] = res.data["files"]
        return ToolOutput(data, site_id=out.site_id)
    return out


@register_tool(
    name="wp_workspace_read_file",
    group="workspace",
    label="Läs fil i arbetsyta",
    short_label="Arbetsytefil",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1, "maximum": 50000},
            "end_line": {"type": "integer", "minimum": 1, "maximum": 50000},
        },
        "required": ["plan_id", "path"],
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("läsning", fallback="fil läst", with_truncated=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    read_budget=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="workspace",
)
def handle_workspace_read_file(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_path = arguments.get("path")
    if not isinstance(rel_path, str) or not rel_path or FILE_PATH_RE.fullmatch(rel_path) is None:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    start_line, end_line, range_ok = parse_line_range(arguments)
    if not range_ok:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if "start_line" not in arguments:
        start_line = None
    if "end_line" not in arguments:
        end_line = None

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

    res = ops.workspace_read_file(
        site_id,
        plan_id,
        rel_path,
        actor=ctx.actor,
        start_line=start_line,
        end_line=end_line,
    )
    if not res.ok:
        return enrich_workspace_missing(
            tools, tools._file_result(res, site_id), site_id, plan_id, rel_path, actor=ctx.actor, ops=ops
        )
    data = dict(res.data or {})
    body = data.get("content")
    if isinstance(body, str):
        scrubbed = editor._scrub(site_obj, body)
        scrubbed = redact_secret_text(scrubbed, redact_whole_lines=True)
        data["content"] = scrubbed
    data.setdefault("status", "read")
    return tools._file_result(WpOpsResult(True, res.message, data), site_id)


@register_tool(
    name="wp_workspace_diff",
    group="workspace",
    label="Visa diff i arbetsyta",
    short_label="Arbetsytediff",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("diff", fallback="diff hämtad", parenthesized=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    read_budget=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="workspace",
)
def handle_workspace_diff(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_path: str | None = None
    if "path" in arguments:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            return ToolOutput(error_payload("InvalidFileTarget"), site_id)
        if raw_path != "":
            if FILE_PATH_RE.fullmatch(raw_path) is None:
                return ToolOutput(error_payload("InvalidFileTarget"), site_id)
            rel_path = raw_path

    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    for perm in (Permission.WP_OPS_ADMIN, Permission.CODE_EDIT):
        perm_denied = editor.need(ctx.actor, perm, site=site_obj, action="wp_ops_file_read")
        if perm_denied is not None:
            return tools._file_result(perm_denied, site_id)

    res = ops.workspace_diff(site_id, plan_id, rel_path, actor=ctx.actor)
    if not res.ok:
        return tools._file_result(res, site_id)
    data = dict(res.data or {})
    raw_diff = data.pop("diff", "")
    if isinstance(raw_diff, str):
        scrubbed = editor._scrub(site_obj, raw_diff)
        scrubbed = redact_secret_text(scrubbed, redact_whole_lines=True)
    else:
        scrubbed = ""
    data["unified_diff"] = scrubbed
    data.setdefault("status", "diff")
    return tools._file_result(WpOpsResult(True, res.message, data), site_id)


@register_tool(
    name="wp_workspace_patch_file",
    group="workspace",
    label="Patcha fil i arbetsyta",
    short_label="Arbetsytepatch",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "base_sha256": {"type": "string"},
        },
        "required": ["plan_id", "path", "old_text", "new_text"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_patch_file(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_path = arguments.get("path")
    if not isinstance(rel_path, str) or not rel_path or FILE_PATH_RE.fullmatch(rel_path) is None:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")
    base_sha256 = arguments.get("base_sha256")
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if "base_sha256" in arguments and (
        not isinstance(base_sha256, str) or not FILE_SHA_RE.fullmatch(base_sha256)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    patched_out = tools._file_result(
        ops.patch_file(site_id, plan_id, rel_path, old_text, new_text, actor=ctx.actor, base_sha256=base_sha256),
        site_id,
    )
    return enrich_workspace_missing(tools, patched_out, site_id, plan_id, rel_path, actor=ctx.actor, ops=ops)


@register_tool(
    name="wp_workspace_create_file",
    group="workspace",
    label="Skapa fil i arbetsyta",
    short_label="Ny fil i arbetsyta",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["plan_id", "path", "content"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_create_file(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_path = arguments.get("path")
    if not isinstance(rel_path, str) or not rel_path or FILE_PATH_RE.fullmatch(rel_path) is None:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    content = arguments.get("content")
    if not isinstance(content, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    return tools._file_result(ops.create_file(site_id, plan_id, rel_path, content, actor=ctx.actor), site_id)


@register_tool(
    name="wp_workspace_apply_changes",
    group="workspace",
    label="Applicera ändringar i arbetsyta",
    short_label="Flera arbetsyteändringar",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "op": {"type": "string", "enum": ["edit", "create", "delete", "revert"]},
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                },
                                "required": ["old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        },
                        "content": {"type": "string"},
                        "base_sha256": {"type": "string"},
                    },
                    "required": ["path", "op"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan_id", "changes"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_apply_changes(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    changes = arguments.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 50:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    allowed_item_keys = {"path", "op", "edits", "content", "base_sha256"}
    for item in changes:
        if not isinstance(item, dict) or (set(item) - allowed_item_keys):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        op = item.get("op")
        rel = item.get("path")
        if op not in {"edit", "create", "delete", "revert"}:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if not isinstance(rel, str) or not rel or FILE_PATH_RE.fullmatch(rel) is None:
            return ToolOutput(error_payload("InvalidFileTarget"), site_id)
        if "base_sha256" in item and item["base_sha256"] is not None:
            if not isinstance(item["base_sha256"], str) or not FILE_SHA_RE.fullmatch(item["base_sha256"]):
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if op == "edit":
            edits = item.get("edits")
            if not isinstance(edits, list) or not edits:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
            for patch in edits:
                if (
                    not isinstance(patch, dict)
                    or set(patch) != {"old_text", "new_text"}
                    or not isinstance(patch.get("old_text"), str)
                    or not isinstance(patch.get("new_text"), str)
                ):
                    return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        elif op == "create":
            if not isinstance(item.get("content"), str):
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    res = ops.workspace_apply_changes(site_id, plan_id, changes, actor=ctx.actor)
    out = tools._file_result(res, site_id)
    if out.data.get("error_class") == "FileMissing":
        first_path = str(changes[0].get("path") or "") if changes else ""
        return enrich_workspace_missing(tools, out, site_id, plan_id, first_path, actor=ctx.actor, ops=ops)
    return out


@register_tool(
    name="wp_workspace_bulk_replace",
    group="workspace",
    label="Massersätt i arbetsyta",
    short_label="Massersättning i arbetsyta",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "find": {"type": "string"},
            "replace": {"type": "string"},
            "mode": {"type": "string", "enum": ["literal", "regex"]},
            "extensions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "path_prefix": {"type": "string"},
            "dry_run": {"type": "boolean"},
        },
        "required": ["plan_id", "find", "replace"],
        "additionalProperties": False,
    },
    outcome=_bulk_replace_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_bulk_replace(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    find = arguments.get("find")
    replace = arguments.get("replace")
    if not isinstance(find, str) or not isinstance(replace, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    mode = arguments.get("mode", "literal")
    if mode not in {"literal", "regex"}:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    dry_run = arguments.get("dry_run", False)
    if type(dry_run) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    extensions = arguments.get("extensions")
    if "extensions" in arguments and (
        not isinstance(extensions, list)
        or not extensions
        or len(extensions) > 10
        or any(not isinstance(item, str) for item in extensions)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    path_prefix = arguments.get("path_prefix")
    if "path_prefix" in arguments:
        if not isinstance(path_prefix, str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if path_prefix and FILE_PATH_RE.fullmatch(path_prefix) is None:
            return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    res = ops.workspace_bulk_replace(
        site_id,
        plan_id,
        find,
        replace,
        actor=ctx.actor,
        mode=mode,
        extensions=extensions,
        path_prefix=path_prefix,
        dry_run=dry_run,
    )
    out = tools._file_result(res, site_id)
    if res.ok and isinstance(res.data, dict) and isinstance(res.data.get("files"), list):
        data = dict(out.data)
        data["files"] = res.data["files"]
        return ToolOutput(data, site_id=out.site_id)
    return out


@register_tool(
    name="wp_workspace_scaffold",
    group="workspace",
    label="Skapa grundfiler i arbetsyta",
    short_label="Sajtplugin-/barntemamall",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "template": {
                "type": "string",
                "enum": ["child_theme_basics", "site_plugin_basics", "woo_compat_declaration"],
            },
        },
        "required": ["plan_id", "template"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_scaffold(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    template = arguments.get("template")
    if template not in {"child_theme_basics", "site_plugin_basics", "woo_compat_declaration"}:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    return tools._file_result(ops.workspace_scaffold(site_id, plan_id, str(template), actor=ctx.actor), site_id)


@register_tool(
    name="wp_workspace_theme_json",
    group="workspace",
    label="Uppdatera theme.json i arbetsyta",
    short_label="theme.json-uppdatering",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["set", "remove"]},
                        "pointer": {"type": "string"},
                        "value": {},
                    },
                    "required": ["op", "pointer"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan_id", "operations"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_theme_json(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not operations or len(operations) > 128:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    for item in operations:
        if not isinstance(item, dict) or (set(item) - {"op", "pointer", "value"}):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        op = item.get("op")
        pointer = item.get("pointer")
        if op not in {"set", "remove"} or not isinstance(pointer, str) or not pointer:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if op == "set" and "value" not in item:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    return tools._file_result(ops.workspace_theme_json(site_id, plan_id, operations, actor=ctx.actor), site_id)


@register_tool(
    name="wp_workspace_copy_from_parent",
    group="workspace",
    label="Kopiera från föräldratema",
    short_label="Kopiera mall från föräldratema",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["plan_id", "path"],
        "additionalProperties": False,
    },
    outcome=status_outcome("arbetsyta"),
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_copy_from_parent(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any
) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    rel_path = arguments.get("path")
    if not isinstance(rel_path, str) or not rel_path or FILE_PATH_RE.fullmatch(rel_path) is None:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    parent = workspace_parent_slug(tools, site_id, plan_id, actor=ctx.actor)
    res = ops.copy_from_parent(site_id, plan_id, rel_path, actor=ctx.actor, parent_slug=parent)
    out = tools._file_result(res, site_id)
    if res.ok:
        data = dict(out.data)
        data["workspace_files"] = workspace_file_list(tools.service.data_dir, site_id, plan_id)
        data["hint"] = (
            f"Filen '{rel_path}' finns nu i arbetsytans work/ "
            "och är redo att patchas direkt med wp_workspace_patch_file (du behöver inte läsa om den om du redan har utdraget)."
        )
        return ToolOutput(data, site_id=out.site_id)
    if out.data.get("error_class") == "FileMissing":
        data = dict(out.data)
        if parent:
            data["parent_theme_slug"] = parent
        data["recovery_tool"] = "wp_search_component"
        data["hint"] = (
            f"Filen '{rel_path}' hittades inte i föräldratemat"
            + (f" '{parent}'" if parent else "")
            + ". Använd wp_search_component eller wp_list_component_tree på föräldratemat för att hitta rätt sökväg."
        )
        return ToolOutput(data, site_id=out.site_id)
    return out


@register_tool(
    name="wp_workspace_copy_woo_template",
    group="workspace",
    label="Kopiera WooCommerce-mall",
    short_label="Kopiera WooCommerce-mall",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "template": {"type": "string"},
        },
        "required": ["plan_id", "template"],
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("mall kopierad", fallback="mall kopierad", parenthesized=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    read_budget=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_copy_woo_template(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any
) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    raw_tpl = arguments.get("template")
    if not isinstance(raw_tpl, str) or not raw_tpl or FILE_PATH_RE.fullmatch(raw_tpl) is None:
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)
    clean_tpl = raw_tpl[len("woocommerce/") :] if raw_tpl.startswith("woocommerce/") else raw_tpl
    if not clean_tpl or FILE_PATH_RE.fullmatch(clean_tpl) is None or sensitive_relative(clean_tpl):
        return ToolOutput(error_payload("InvalidFileTarget"), site_id)

    editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    for perm in (Permission.WP_OPS_OPERATE, Permission.WP_OPS_ADMIN):
        perm_denied = editor.need(ctx.actor, perm, site=site_obj, action="wp_review_copy_woo_template")
        if perm_denied is not None:
            return tools._file_result(perm_denied, site_id)

    plan_row = ops.get(plan_id, site_id=site_id)
    if plan_row is None:
        return ToolOutput(error_payload("UnknownPlan"), site_id)
    status_res = ops.workspace_status(site_id, plan_id, actor=ctx.actor)
    if not status_res.ok:
        return tools._file_result(status_res, site_id)
    if plan_row.get("kind") != "theme":
        return ToolOutput(error_payload("InvalidTarget"), site_id)
    plan_slug = str(plan_row.get("slug") or "")
    if plan_slug and is_parent_theme_on_site(tools, site_obj, plan_slug, site_id=site_id, actor=ctx.actor):
        return ToolOutput(error_payload("ParentThemeBlocked"), site_id)

    try:
        source_abs = resolve_file(site_obj.files_root, "plugin", "woocommerce", f"templates/{clean_tpl}")
        source_text = editor._read_text(site_obj, source_abs)
    except PathRejected:
        return ToolOutput(error_payload("PathRejected"), site_id)
    except TransportError as exc:
        return tools._file_result(
            editor._fail(exc.error_class, "WooCommerce template read failed.", ssh=editor._ssh(site_obj)),
            site_id,
        )

    dest_rel = f"woocommerce/{clean_tpl}"
    created = ops.create_file(site_id, plan_id, dest_rel, source_text, actor=ctx.actor)
    if not created.ok:
        return tools._file_result(created, site_id)
    data = dict(created.data or {})
    data["source_template"] = f"woocommerce/templates/{clean_tpl}"
    return tools._file_result(WpOpsResult(True, created.message, data), site_id)


@register_tool(
    name="wp_workspace_status",
    group="workspace",
    label="Kontrollera arbetsytans status",
    short_label="Arbetsytestatus",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=_workspace_status_outcome,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    capability_group="workspace",
)
def handle_workspace_status(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    return tools._file_result(ops.workspace_status(site_id, plan_id, actor=ctx.actor), site_id)


@register_tool(
    name="wp_workspace_static_checks",
    group="workspace",
    label="Kör statisk kodkontroll",
    short_label="Statiska kodkontroller",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=count_outcome("findings", "statiska fynd", "statisk kontroll klar"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    capability_group="workspace",
)
def handle_workspace_static_checks(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    _editor, site_obj, denied = authorize_editor_site(tools.service, site_id, ctx.actor, write=False)
    if denied is not None:
        return tools._file_result(denied, site_id)
    assert site_obj is not None
    status_res = ops.workspace_status(site_id, plan_id, actor=ctx.actor)
    if not status_res.ok:
        return tools._file_result(status_res, site_id)
    try:
        has_admin = resolve_actor(ctx.actor).has(Permission.WP_OPS_ADMIN)
    except Exception:
        has_admin = False

    files_dict, _truncated_read = read_workspace_text_files(
        tools,
        site_id,
        plan_id,
        allowed_exts=CODE_INDEX_EXTENSIONS,
        site_obj=site_obj,
    )
    report = run_static_checks(files_dict)
    findings: list[dict[str, Any]] = []
    for item in report.get("findings") or []:
        if not isinstance(item, dict):
            continue
        if has_admin:
            entry = dict(item)
            entry["excerpt"] = tools._scrub_bounded(site_obj, item.get("excerpt") or "", max_len=120)
        else:
            entry = {k: v for k, v in item.items() if k != "excerpt"}
        findings.append(entry)

    checks_ok = bool(report.get("ok"))
    return ToolOutput(
        {
            "ok": True,
            "status": "passed" if checks_ok else "findings",
            "checks_passed": checks_ok,
            "plan_id": plan_id,
            "site_id": site_id,
            "summary": tools._scrub_bounded(site_obj, report.get("summary") or "", max_len=200),
            "counts": report.get("counts") or {},
            "findings": findings,
            "truncated": bool(report.get("truncated")),
            "review_path": f"/wp/{site_id}/reviews/{plan_id}",
            "login_required": True,
            "executed": True,
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_workspace_run_checks",
    group="workspace",
    label="Kör arbetsytans kontroller",
    short_label="Granskningskontroller",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    outcome=_run_checks_outcome,
    in_file_schemas=True,
    in_core_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_run_checks(tools: Any, ctx: ToolContext, arguments: dict[str, Any], *, ops: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    res = ops.run_checks(site_id, plan_id, actor=ctx.actor)
    if not res.ok:
        return tools._file_result(res, site_id)
    cdata = dict(res.data or {})
    site_obj = tools._site_model(site_id)
    plan_row = ops.get(plan_id, site_id=site_id)
    raw_paths = tuple(str(p) for p in cdata.get("screenshot_paths") or [] if isinstance(p, str))[:4]
    compact_checks: list[dict[str, Any]] = []
    top_findings: list[str] = []
    for row in cdata.get("checks") or []:
        if not isinstance(row, dict):
            continue
        row_summary = tools._scrub_bounded(site_obj, row.get("summary") or "", max_len=200)
        raw_findings = row.get("findings")
        clean_findings = (
            [tools._scrub_bounded(site_obj, str(item), max_len=120) for item in raw_findings[:10]]
            if isinstance(raw_findings, list)
            else []
        )
        compact_checks.append(
            {
                "job": row.get("job"),
                "status": row.get("status"),
                "reason": str(row.get("reason") or "")[:80],
                "summary": row_summary,
                "findings": clean_findings,
                "executed": row.get("executed") is True,
                "simulated": row.get("simulated") is True,
            }
        )
        for item in clean_findings:
            if len(top_findings) < 10 and item:
                top_findings.append(item)
        if not clean_findings and row.get("status") == "failed" and row_summary:
            if len(top_findings) < 10:
                top_findings.append(row_summary[:120])

    resolved_plan_id = str(cdata.get("plan_id") or plan_id)
    check_run = cdata.get("check_run") if isinstance(cdata.get("check_run"), dict) else {}
    env = (
        site_obj.environment
        if site_obj is not None
        else (plan_row.get("environment") if isinstance(plan_row, dict) else None)
    )
    risk = plan_row.get("risk") if isinstance(plan_row, dict) else cdata.get("risk")
    status_val = cdata.get("status")
    publishable = (
        status_val == "tests_passed"
        and check_run.get("published") is True
        and env == "staging"
        and risk != "high"
        and REVIEW_PLAN_RE.fullmatch(resolved_plan_id) is not None
        and (plan_row is None or plan_row.get("status") == "tests_passed")
    )
    review_stage_id = resolved_plan_id if publishable else None
    summary_bits = ", ".join(f"{c['job']}={c['status']}" for c in compact_checks)
    overall_summary = tools._scrub_bounded(
        site_obj,
        f"{status_val or 'unknown'}: {summary_bits}" if summary_bits else (res.message or ""),
        max_len=200,
    )
    preview_obj = cdata.get("preview") if isinstance(cdata.get("preview"), dict) else {}
    preview_ok = bool(preview_obj)
    preview_path_val, preview_url_val, preview_html_ready = resolve_sandbox_preview_meta(
        tools.service.data_dir, site_id, resolved_plan_id, preview_obj
    )
    preview_status = cdata.get("preview_status") or (
        "ok" if preview_ok else ("failed" if cdata.get("preview_error") else "skipped")
    )
    preview_note = cdata.get("preview_note") or (
        f"Live-förhandsvisning lyckades ({len(cdata.get('screenshots') or [])} skärmdumpar); "
        "syntetisk browser_smoke=unavailable påverkar inte live-förhandsvisningen."
        if preview_ok
        else ""
    )
    summary_data = {
        "ok": True,
        "plan_id": resolved_plan_id,
        "site_id": cdata.get("site_id") or site_id,
        "status": status_val,
        "summary": overall_summary,
        "findings": top_findings[:10],
        "checks": compact_checks,
        "check_run": check_run,
        "screenshots": list(cdata.get("screenshots") or []),
        "preview_status": preview_status,
        "preview_note": tools._scrub_bounded(site_obj, preview_note, max_len=240) if preview_note else "",
        "preview_path": preview_path_val,
        "preview_url": preview_url_val,
        "preview_html_ready": preview_html_ready,
        "staging_ready": review_stage_id is not None,
        "review_path": f"/wp/{site_id}/reviews/{plan_id}",
        "login_required": True,
        "executed": False,
        "live_calls": preview_ok,
    }
    out = tools._file_result(WpOpsResult(True, res.message, summary_data), site_id)
    return ToolOutput(
        out.data,
        site_id=out.site_id,
        operation_ids=out.operation_ids,
        attachments=raw_paths,
        review_stage_plan_id=review_stage_id,
    )


__all__ = [
    "enrich_workspace_missing",
    "handle_apply_staging",
    "handle_deploy_production",
    "handle_list_workspaces",
    "handle_open_review_workspace",
    "handle_propose_file_diff",
    "handle_propose_new_file",
    "handle_request_production",
    "handle_rollback_file",
    "handle_validate_plan",
    "handle_verify_change",
    "handle_workspace_apply_changes",
    "handle_workspace_bulk_replace",
    "handle_workspace_copy_from_parent",
    "handle_workspace_copy_woo_template",
    "handle_workspace_create_file",
    "handle_workspace_diff",
    "handle_workspace_fork",
    "handle_workspace_list_files",
    "handle_workspace_patch_file",
    "handle_workspace_read_file",
    "handle_workspace_run_checks",
    "handle_workspace_scaffold",
    "handle_workspace_static_checks",
    "handle_workspace_status",
    "handle_workspace_theme_json",
    "read_workspace_text_files",
    "verified_plugin",
    "verified_theme_pair",
    "workspace_file_list",
    "workspace_parent_slug",
]
