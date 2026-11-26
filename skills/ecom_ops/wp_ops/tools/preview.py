"""Live HTML fetch, DOM/CSS element inspection, and screenshot preview WordPress tools."""

from __future__ import annotations

import inspect
import json
import re
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.file_changes import _MAX_BYTES
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError, workspace_root
from ecom_ops.wp_ops.tools.html_parse import parse_html_meta
from ecom_ops.wp_ops.tools.registry import register_tool
from ecom_ops.wp_ops.tools.scrub import scrub_nested
from ecom_ops.wp_ops.tools.types import (
    DASHBOARD_REVIEW_PATH_RE,
    PREVIEW_FOCUS_SECTIONS,
    PRODUCT_EDITS_FILE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import error_payload, safe_failure
from ecom_ops.wp_ops.tools.workspace_io import (
    resolve_sandbox_preview_meta,
    safe_sandbox_preview_url,
)


def supported_kwargs(fn: Any, extra: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return extra
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return extra
    return {k: v for k, v in extra.items() if k in sig.parameters}


def site_for_preview(tools: Any, site_id: str, actor: str) -> tuple[Site | None, ToolOutput | None]:
    shown = tools.service.show_site(site_id, actor=actor)
    if not shown.ok:
        return None, ToolOutput(safe_failure(shown), site_id)
    site_obj = tools._site_model(site_id)
    if site_obj is None:
        return None, ToolOutput(error_payload("SiteUnavailable"), site_id)
    return site_obj, None


def workspace_product_path(data_dir: Path, site_id: str, plan_id: str) -> str | None:
    from ecom_ops.integrations.wp_live_preview import validate_preview_path

    try:
        ws_root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return None
    edits_file = ws_root / "work" / PRODUCT_EDITS_FILE
    if edits_file.is_symlink() or not edits_file.is_file():
        return None
    try:
        raw = edits_file.read_bytes()
        if len(raw) > _MAX_BYTES or b"\x00" in raw:
            return None
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw_path = parsed.get("product_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        clean = validate_preview_path(raw_path)
    except ReviewError:
        return None
    return clean if clean != "/" else None


def _fetch_html_outcome(data: dict[str, Any], _reads: int | None) -> str:
    size = data.get("bytes")
    return f"HTML hämtad ({size} B)" if isinstance(size, int) else "HTML hämtad"


def _inspect_element_outcome(data: dict[str, Any], _reads: int | None) -> str:
    vps = data.get("viewports")
    if isinstance(vps, dict):
        desktop = vps.get("desktop") if isinstance(vps.get("desktop"), dict) else {}
        if desktop.get("found") is True:
            return "element & CSS inspekterat"
        return "element saknades på sidan"
    return "element inspekterat"


def _preview_page_outcome(data: dict[str, Any], _reads: int | None) -> str:
    shots = data.get("screenshots")
    n = len(shots) if isinstance(shots, list) else 0
    return f"{n} skärmdumpar" if n else "förhandsvisning klar"


@register_tool(
    name="wp_fetch_page_html",
    group="file",
    label="Hämta renderad HTML",
    short_label="Live-HTML",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "path": {"type": "string"},
            "selector": {"type": "string"},
            "include_styles": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=_fetch_html_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_fetch_page_html(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    from ecom_ops.integrations.wp_live_preview import (
        fetch_live_html,
        live_preview_enabled,
        validate_focus_options,
        validate_preview_path,
    )

    site_id = str(ctx.site_id)
    site_obj, denied_out = site_for_preview(tools, site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None
    if not live_preview_enabled():
        return ToolOutput(error_payload("LivePreviewDisabled"), site_id)
    raw_path = arguments.get("path", "/")
    if raw_path is not None and not isinstance(raw_path, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    raw_selector = arguments.get("selector")
    if raw_selector is not None and not isinstance(raw_selector, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    include_styles = arguments.get("include_styles", False)
    if type(include_styles) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    raw_path_str = str(raw_path or "/").strip()
    dash_match = DASHBOARD_REVIEW_PATH_RE.fullmatch(raw_path_str)
    if dash_match is not None or raw_path_str.lower().startswith(("http://127.0.0.1", "http://localhost")):
        matched_pid = dash_match.group(2) if dash_match is not None else ""
        err = error_payload("InvalidPreviewPath")
        err["recovery_tool"] = "wp_inspect_element"
        err["hint"] = (
            "wp_fetch_page_html hämtar bara publika sidor från livesajten (t.ex. '/produkt/...'), "
            "inte lokala dashboard-länkar (/wp/.../reviews/wr-.../preview). "
            + (
                f"Använd wp_inspect_element(site_id='{site_id}', plan_id='{matched_pid}', selector=...) "
                f"för att inspektera renderad HTML/CSS i sandlådan eller wp_workspace_read_file(plan_id='{matched_pid}', path='style.css')."
                if matched_pid
                else f"Använd wp_inspect_element(site_id='{site_id}', plan_id='wr-...', selector=...) för att inspektera sandlådeförhandsvisningen."
            )
        )
        return ToolOutput(err, site_id)

    try:
        clean_path = validate_preview_path(raw_path)
        _sec, clean_selector, _txt, _full = validate_focus_options(focus_selector=raw_selector)
        fetched = fetch_live_html(site_obj, clean_path)
    except ReviewError as exc:
        return ToolOutput(error_payload(exc.error_class), site_id)

    raw_html = str(fetched.get("html") or "")
    site_host = (urllib.parse.urlparse(str(site_obj.canonical_url or "")).hostname or "").lower()
    parser = parse_html_meta(raw_html, site_host=site_host)

    selector_matched = False
    selector_matches = 0
    extracted_styles = ""
    if include_styles:
        style_blocks = re.findall(r"(?is)<style\b[^>]*>.*?</style>", raw_html)
        if style_blocks:
            extracted_styles, _ = tools._scrub_keep_newlines(
                site_obj, "\n".join(style_blocks), max_bytes=4096
            )
    if clean_selector:
        extracted, selector_matches = tools._extract_html_by_selector(raw_html, clean_selector)
        if extracted:
            selector_matched = True
            source_for_excerpt = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "<script>…</script>", extracted)
        else:
            source_for_excerpt = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "<script>…</script>", raw_html)
            if not include_styles:
                source_for_excerpt = re.sub(r"(?is)<style\b[^>]*>.*?</style>", "<style>…</style>", source_for_excerpt)
    else:
        source_for_excerpt = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "<script>…</script>", raw_html)
        if not include_styles:
            source_for_excerpt = re.sub(r"(?is)<style\b[^>]*>.*?</style>", "<style>…</style>", source_for_excerpt)
    excerpt, excerpt_clipped = tools._scrub_keep_newlines(site_obj, source_for_excerpt, max_bytes=9000)

    payload: dict[str, Any] = {
        "ok": True,
        "status": "read",
        "site_id": site_id,
        "path": clean_path,
        "final_url": str(fetched.get("final_url") or ""),
        "http_status": int(fetched.get("status") or 200),
        "bytes": int(fetched.get("bytes") or len(raw_html.encode("utf-8"))),
        "title": tools._scrub_bounded(site_obj, "".join(parser.title_parts), max_len=200),
        "lang": tools._scrub_bounded(site_obj, parser.lang, max_len=24),
        "meta_description": tools._scrub_bounded(site_obj, parser.meta_desc, max_len=300),
        "canonical": tools._scrub_bounded(site_obj, parser.canonical, max_len=240),
        "h1": [tools._scrub_bounded(site_obj, h, max_len=160) for h in parser.h1_list],
        "counts": {
            "links": parser.links,
            "images": parser.images,
            "images_missing_alt": parser.images_missing_alt,
            "scripts": parser.scripts,
        },
        "external_hosts": parser.external_hosts[:10],
        "html_excerpt": excerpt,
        "truncated": excerpt_clipped,
        "live_calls": True,
    }
    if clean_selector:
        payload["selector"] = clean_selector
        payload["selector_matched"] = selector_matched
        payload["selector_matches"] = selector_matches
    if include_styles:
        payload["include_styles"] = True
        payload["styles"] = extracted_styles
    return ToolOutput(payload, site_id=site_id)


@register_tool(
    name="wp_inspect_element",
    group="file",
    label="Inspektera element & CSS",
    short_label="Elementinspektion",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "path": {"type": "string"},
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "plan_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=_inspect_element_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_inspect_element(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    from ecom_ops.integrations.wp_live_preview import (
        inspect_element,
        live_preview_enabled,
        validate_focus_options,
        validate_preview_path,
    )

    site_id = str(ctx.site_id)
    site_obj, denied_out = site_for_preview(tools, site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None
    if not live_preview_enabled():
        return ToolOutput(error_payload("LivePreviewDisabled"), site_id)

    raw_path = arguments.get("path")
    raw_selector = arguments.get("selector")
    raw_text = arguments.get("text")
    plan_id = arguments.get("plan_id")
    if raw_path is not None and not isinstance(raw_path, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if raw_selector is not None and not isinstance(raw_selector, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if raw_text is not None and not isinstance(raw_text, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    if isinstance(raw_path, str) and raw_path.strip():
        dm = DASHBOARD_REVIEW_PATH_RE.fullmatch(raw_path.strip())
        if dm is not None:
            if plan_id is None:
                plan_id = dm.group(2)
            raw_path = None

    try:
        _sec, clean_selector, clean_text, _full = validate_focus_options(
            focus_selector=raw_selector,
            focus_text=raw_text,
        )
    except ReviewError as exc:
        return ToolOutput(error_payload(exc.error_class), site_id)

    html_override: str | None = None
    target_url: str | None = None
    effective_path = str(raw_path or "/").strip() or "/"
    overlay_meta: dict[str, Any] | None = None

    if isinstance(plan_id, str) and plan_id:
        ops = tools._review_plans()
        built, err = ops.build_overlay_html(
            site_id,
            plan_id,
            actor=ctx.actor,
            path=raw_path if (isinstance(raw_path, str) and raw_path.strip()) else "/",
            use_cache=True,
        )
        if err is not None or built is None:
            return tools._file_result(err, site_id) if err is not None else ToolOutput(error_payload("LiveFetchFailed"), site_id)
        effective_path = str(built["clean_path"])
        html_override = str(built["overlay"].html)
        target_url = str((built.get("fetched") or {}).get("final_url") or "") or None
        plan_row = built.get("plan") if isinstance(built.get("plan"), dict) else {}
        if not clean_selector and not clean_text:
            stored_sel = str(plan_row.get("focus_selector") or "").strip()
            if not stored_sel and isinstance(plan_row.get("page_draft"), dict):
                stored_sel = str(plan_row["page_draft"].get("focus_selector") or "").strip()
            if not stored_sel and isinstance(plan_row.get("page_draft"), dict):
                stored_sel = ".azom-page-draft-root"
            if stored_sel:
                clean_selector = stored_sel
        overlay_meta = {
            "injected_css_bytes": built["overlay"].injected_css_bytes,
            "hunks": len(built["overlay"].hunks),
        }
    else:
        try:
            effective_path = validate_preview_path(effective_path)
        except ReviewError as exc:
            return ToolOutput(error_payload(exc.error_class), site_id)

    if not clean_selector and not clean_text:
        err = error_payload("InvalidToolArguments")
        err["hint"] = "Ange minst en av selector (CSS-selektor, t.ex. '.single_add_to_cart_button') eller text (synlig knapptext) för wp_inspect_element."
        return ToolOutput(err, site_id)

    try:
        inspected = inspect_element(
            site_obj,
            path=effective_path,
            html_override=html_override,
            selector=clean_selector,
            text=clean_text,
            target_url=target_url,
        )
    except ReviewError as exc:
        return ToolOutput(error_payload(exc.error_class), site_id)

    safe_inspected = scrub_nested(inspected) if isinstance(inspected, dict) else {}
    payload: dict[str, Any] = {
        "ok": True,
        "status": "inspected" if safe_inspected.get("found") else "not_found",
        "site_id": site_id,
        "plan_id": plan_id,
        "source_scope": "workspace_overlay" if plan_id else "live",
        "path": effective_path,
        **safe_inspected,
        "live_calls": True,
    }
    if overlay_meta is not None:
        payload["overlay"] = overlay_meta
    return ToolOutput(payload, site_id=site_id)


@register_tool(
    name="wp_preview_page",
    group="file",
    label="Skärmdump/förhandsvisning",
    short_label="Förhandsvisning",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "path": {"type": "string"},
            "plan_id": {"type": "string"},
            "focus_section": {
                "type": "string",
                "enum": list(PREVIEW_FOCUS_SECTIONS),
            },
            "focus_selector": {"type": "string"},
            "focus_text": {"type": "string"},
            "full_page": {"type": "boolean"},
            "reuse_live_baseline": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=_preview_page_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_preview_page(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    from ecom_ops.bot.reply import safe_workspace_attachments
    from ecom_ops.integrations.wp_live_preview import (
        capture_preview,
        fetch_live_html,
        live_preview_enabled,
        validate_focus_options,
        validate_preview_path,
    )

    site_id = str(ctx.site_id)
    raw_path = arguments.get("path", "/")
    if raw_path is not None and not isinstance(raw_path, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    path_provided = isinstance(arguments.get("path"), str) and arguments["path"].strip() != ""
    raw_section = arguments.get("focus_section")
    raw_selector = arguments.get("focus_selector")
    raw_text = arguments.get("focus_text")
    raw_full = arguments.get("full_page", False)
    raw_reuse = arguments.get("reuse_live_baseline", True)
    if raw_section is not None and not isinstance(raw_section, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if raw_selector is not None and not isinstance(raw_selector, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if raw_text is not None and not isinstance(raw_text, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if type(raw_full) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if type(raw_reuse) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    plan_id = arguments.get("plan_id")
    try:
        clean_path = validate_preview_path(raw_path)
        focus_section, focus_selector, focus_text, full_page = validate_focus_options(
            focus_section=raw_section,
            focus_selector=raw_selector,
            focus_text=raw_text,
            full_page=raw_full,
        )
    except ReviewError as exc:
        err_payload = error_payload(exc.error_class)
        if exc.error_class == "PreviewPathRefused" and isinstance(plan_id, str) and plan_id.strip():
            try:
                ops_hint = tools._review_plans()
                plan_hint = ops_hint.get(str(plan_id), site_id=site_id)
            except Exception:
                plan_hint = None
            if isinstance(plan_hint, dict) and isinstance(plan_hint.get("page_draft"), dict):
                shell_p = str(plan_hint["page_draft"].get("shell_path") or "/")
                err_payload["hint"] = (
                    "Kassasökvägar (/checkout, /cart, /varukorg m.fl.) är blockerade för live-hämtning. "
                    f"Utelämna 'path' (eller ange publik shell_path '{shell_p}') så renderas "
                    "work/page_draft.json som fristående förhandsvisning i sajtens skal."
                )
        return ToolOutput(err_payload, site_id)

    focus_kwargs = {
        "focus_section": focus_section,
        "focus_selector": focus_selector,
        "focus_text": focus_text,
        "full_page": full_page,
    }

    if plan_id is not None:
        ops = tools._review_plans()
        plan_row = ops.get(str(plan_id), site_id=site_id)
        if not path_provided:
            inferred_path = workspace_product_path(tools.service.data_dir, site_id, str(plan_id))
            if not inferred_path and isinstance(plan_row, dict) and isinstance(plan_row.get("preview_path"), str):
                stored_p = plan_row["preview_path"].strip()
                if stored_p and stored_p != "/" and not isinstance(plan_row.get("page_draft"), dict):
                    inferred_path = stored_p
            if inferred_path:
                clean_path = inferred_path
        live_kwargs = {**focus_kwargs, "reuse_live_baseline": raw_reuse}
        res = ops.live_preview(
            site_id,
            str(plan_id),
            actor=ctx.actor,
            path=clean_path,
            **supported_kwargs(ops.live_preview, live_kwargs),
        )
        if not res.ok:
            return tools._file_result(res, site_id)
        pdata = dict(res.data or {})
        effective_path = str(pdata.get("path") or clean_path)
        preview_path_val, preview_url_val, preview_html_ready = resolve_sandbox_preview_meta(
            tools.service.data_dir, site_id, str(plan_id), pdata
        )
        validated_paths = safe_workspace_attachments(
            [str(p) for p in pdata.get("screenshot_paths") or [] if isinstance(p, str)],
            data_dir=tools.service.data_dir,
        )
        note = str(pdata.get("note") or "")
        if preview_url_val:
            note = (f"{note} " if note else "") + f"Interaktiv sandbox-URL: {preview_url_val}"
        resolved_risk = (
            str(pdata.get("risk") or "")
            or (str(plan_row.get("risk") or "") if isinstance(plan_row, dict) else "")
            or "okänd"
        )
        payload = {
            "ok": True,
            "status": "preview",
            "plan_id": str(plan_id),
            "site_id": site_id,
            "risk": resolved_risk,
            "path": effective_path,
            "preview_kind": pdata.get("preview_kind") or "live_overlay",
            "reused_live_baseline": bool(pdata.get("reused_live_baseline")),
            "synthetic_data": bool(pdata.get("synthetic_data")),
            "page_draft": pdata.get("page_draft"),
            "screenshots": list(pdata.get("screenshots") or []),
            "attachments_count": len(validated_paths),
            "focus_section": focus_section,
            "focus_selector": focus_selector,
            "focus_text": focus_text,
            "full_page": full_page,
            "focus_matched": bool(pdata.get("focus_matched")),
            "focus_target": pdata.get("focus_target"),
            "visual_health": pdata.get("visual_health"),
            "overlay": pdata.get("overlay") or {},
            "blocked_counts": pdata.get("blocked_counts") or {},
            "preview_path": preview_path_val,
            "preview_url": preview_url_val,
            "preview_html_ready": preview_html_ready,
            "review_path": f"/wp/{site_id}/reviews/{plan_id}",
            "login_required": True,
            "note": note,
            "live_calls": True,
        }
        return ToolOutput(payload, site_id=site_id, attachments=validated_paths)

    site_obj, denied_out = site_for_preview(tools, site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None
    if not live_preview_enabled():
        return ToolOutput(error_payload("LivePreviewDisabled"), site_id)
    artifact_dir = Path(tools.service.data_dir) / "wp_workspaces" / site_id / f"pv-{secrets.token_hex(6)}" / "artifacts"
    try:
        fetched = fetch_live_html(site_obj, clean_path)
        rendered = capture_preview(
            site_obj,
            path=clean_path,
            artifact_dir=artifact_dir,
            revisions=[("live", str(fetched.get("html") or ""))],
            target_url=str(fetched.get("final_url") or "") or None,
            **supported_kwargs(capture_preview, focus_kwargs),
        )
    except ReviewError as exc:
        return ToolOutput(error_payload(exc.error_class), site_id)

    shot_names = [s for s in (rendered.get("screenshots") or []) if isinstance(s, str)]
    validated_paths = safe_workspace_attachments(
        [str(artifact_dir / name) for name in shot_names],
        data_dir=tools.service.data_dir,
    )
    live_revs = (rendered.get("revisions") or {}).get("live") or {}
    console_errors: list[str] = []
    network_errors: list[dict[str, Any]] = []
    for vp_name in ("desktop", "mobile"):
        vp = live_revs.get(vp_name) or {}
        for err in vp.get("console_errors") or []:
            if err not in console_errors:
                console_errors.append(str(err))
        for nerr in vp.get("network_errors") or []:
            if nerr not in network_errors:
                network_errors.append(nerr)
    return ToolOutput(
        {
            "ok": True,
            "status": "preview",
            "site_id": site_id,
            "path": clean_path,
            "preview_kind": "live_read_only",
            "screenshots": shot_names,
            "attachments_count": len(validated_paths),
            "focus_section": focus_section,
            "focus_selector": focus_selector,
            "focus_text": focus_text,
            "full_page": full_page,
            "focus_matched": bool(rendered.get("focus_matched")),
            "focus_target": rendered.get("focus_target"),
            "blocked_counts": rendered.get("blocked_counts") or {},
            "console_errors": console_errors[:10],
            "network_errors": network_errors[:10],
            "note": (
                f"{len(validated_paths)} PNG-skärmdumpar bifogas automatiskt i svaret. "
                "Endast skrivskyddad live-GET utan cookies. Inga ändringar har gjorts."
            ),
            "live_calls": True,
        },
        site_id=site_id,
        attachments=validated_paths,
    )


__all__ = [
    "handle_fetch_page_html",
    "handle_inspect_element",
    "handle_preview_page",
    "resolve_sandbox_preview_meta",
    "safe_sandbox_preview_url",
    "site_for_preview",
    "supported_kwargs",
    "workspace_product_path",
]
