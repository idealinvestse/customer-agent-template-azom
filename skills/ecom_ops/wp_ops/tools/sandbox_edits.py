"""Sandbox-only workspace product edit and page draft handlers."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.file_policy import is_safe_pure_css_file
from ecom_ops.wp_ops.html_sanitize import UNSAFE_PRODUCT_HTML_RE, sanitize_page_draft_html
from ecom_ops.wp_ops.review_workspace import ReviewError
from ecom_ops.wp_ops.tools.registry import register_tool
from ecom_ops.wp_ops.tools.types import (
    FILE_PATH_RE,
    PAGE_DRAFT_FILE,
    PAGE_DRAFT_MAX_CSS_BYTES,
    PAGE_DRAFT_MAX_HTML_BYTES,
    PAGE_DRAFT_TYPES,
    PAGE_SLUG_RE,
    PRODUCT_EDIT_LIMITS,
    PRODUCT_EDITS_FILE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import error_payload, short_text
from ecom_ops.wp_ops.tools.workspace_io import read_workspace_json, upsert_workspace_json


def _product_edit_outcome(data: dict[str, Any], _reads: int | None) -> str:
    fields = data.get("updated_fields")
    if isinstance(fields, list):
        return f"produktändring sparad ({len(fields)} fält)"
    return "produktändring sparad"


def _page_draft_outcome(data: dict[str, Any], _reads: int | None) -> str:
    draft = data.get("page_draft")
    slug = short_text(draft.get("slug"), 40) if isinstance(draft, dict) else ""
    return f"sidutkast /{slug}/ sparat" if slug else "sidutkast sparat"


@register_tool(
    name="wp_workspace_product_edit",
    group="workspace",
    label="Redigera produkt i sandlåda",
    short_label="Produktändring (sandbox)",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "product_path": {"type": "string"},
            "product_id": {"type": ["integer", "string"]},
            "title": {"type": "string"},
            "short_description": {"type": "string"},
            "description": {"type": "string"},
            "price": {"type": "string"},
            "regular_price": {"type": "string"},
            "sale_price": {"type": "string"},
            "button_text": {"type": "string"},
            "badge_text": {"type": "string"},
            "replacements": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
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
        },
        "required": ["plan_id", "product_path"],
        "additionalProperties": False,
    },
    outcome=_product_edit_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_product_edit(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    *,
    ops: Any,
) -> ToolOutput:
    from ecom_ops.integrations.wp_live_preview import validate_preview_path

    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WP_OPS_OPERATE)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id)

    raw_path = arguments.get("product_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        err = error_payload("InvalidToolArguments")
        err["hint"] = "Ange en giltig product_path (t.ex. '/produkt/slug/')."
        return ToolOutput(err, site_id)
    try:
        clean_product_path = validate_preview_path(raw_path)
    except ReviewError as exc:
        return ToolOutput(error_payload(exc.error_class), site_id)

    product_id: int | None = None
    if "product_id" in arguments:
        product_id = tools._parse_product_id(arguments.get("product_id"))
        if product_id is None:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    updates: dict[str, Any] = {}
    for field_name, max_len in PRODUCT_EDIT_LIMITS.items():
        if field_name not in arguments:
            continue
        val = arguments[field_name]
        if not isinstance(val, str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if "\x00" in val or len(val) > max_len:
            err = error_payload("InvalidToolArguments")
            err["hint"] = f"Fältet '{field_name}' överskrider maxlängden ({max_len} tecken) eller innehåller ogiltiga byte."
            return ToolOutput(err, site_id)
        try:
            val.encode("utf-8")
        except UnicodeEncodeError:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if UNSAFE_PRODUCT_HTML_RE.search(val):
            err = error_payload("InvalidToolArguments")
            err["hint"] = f"Fältet '{field_name}' får inte innehålla <script>, <iframe> eller inline event-handlers."
            return ToolOutput(err, site_id)
        updates[field_name] = val

    if "replacements" in arguments:
        raw_reps = arguments["replacements"]
        if not isinstance(raw_reps, list) or not raw_reps or len(raw_reps) > 20:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        clean_replacements: list[dict[str, str]] = []
        for item in raw_reps:
            if (
                not isinstance(item, dict)
                or set(item) != {"old_text", "new_text"}
                or not isinstance(item.get("old_text"), str)
                or not isinstance(item.get("new_text"), str)
            ):
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
            old_t = item["old_text"]
            new_t = item["new_text"]
            if not old_t or len(old_t) > 500 or len(new_t) > 500 or "\x00" in old_t or "\x00" in new_t:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
            try:
                old_t.encode("utf-8")
                new_t.encode("utf-8")
            except UnicodeEncodeError:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
            if UNSAFE_PRODUCT_HTML_RE.search(new_t):
                err = error_payload("InvalidToolArguments")
                err["hint"] = "replacements.new_text får inte innehålla <script>, <iframe> eller inline event-handlers."
                return ToolOutput(err, site_id)
            clean_replacements.append({"old_text": old_t, "new_text": new_t})
        updates["replacements"] = clean_replacements

    if not updates:
        err = error_payload("InvalidToolArguments")
        err["hint"] = (
            "Ange minst ett ändringsfält (title, short_description, description, price, "
            "regular_price, sale_price, button_text, badge_text eller replacements)."
        )
        return ToolOutput(err, site_id)

    file_exists, existing_raw, existing_data, read_err = read_workspace_json(
        ops, tools.service.data_dir, site_id, plan_id, PRODUCT_EDITS_FILE, actor=ctx.actor
    )
    if read_err is not None:
        return tools._file_result(read_err, site_id)

    if file_exists and existing_data.get("product_path") == clean_product_path:
        merged: dict[str, Any] = dict(existing_data)
        merged["schema_version"] = 1
        merged["product_path"] = clean_product_path
    else:
        merged = {
            "schema_version": 1,
            "product_path": clean_product_path,
        }

    if product_id is not None:
        merged["product_id"] = product_id

    for field_name, val in updates.items():
        if field_name == "replacements":
            prev_reps = merged.get("replacements") if isinstance(merged.get("replacements"), list) else []
            by_old: dict[str, str] = {}
            order: list[str] = []
            for entry in prev_reps:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("old_text"), str)
                    and isinstance(entry.get("new_text"), str)
                ):
                    if entry["old_text"] not in by_old:
                        order.append(entry["old_text"])
                    by_old[entry["old_text"]] = entry["new_text"]
            for entry in val:
                if entry["old_text"] not in by_old:
                    order.append(entry["old_text"])
                by_old[entry["old_text"]] = entry["new_text"]
            merged["replacements"] = [{"old_text": old_k, "new_text": by_old[old_k]} for old_k in order[:20]]
        else:
            merged[field_name] = val

    new_json = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    write_res = upsert_workspace_json(
        ops,
        tools.service.data_dir,
        site_id,
        plan_id,
        PRODUCT_EDITS_FILE,
        new_json,
        actor=ctx.actor,
        existing_state=(file_exists, existing_raw),
    )

    if not write_res.ok:
        return tools._file_result(write_res, site_id)

    wdata = dict(write_res.data or {})
    updated_fields = sorted(updates.keys())
    return ToolOutput(
        {
            "ok": True,
            "status": wdata.get("status") or "validated",
            "plan_id": plan_id,
            "site_id": site_id,
            "path": PRODUCT_EDITS_FILE,
            "product_path": clean_product_path,
            "product_id": merged.get("product_id"),
            "updated_fields": updated_fields,
            "work_sha256": wdata.get("work_sha256"),
            "base_sha256": wdata.get("base_sha256"),
            "risk": wdata.get("risk"),
            "review_path": f"/wp/{site_id}/reviews/{plan_id}",
            "login_required": True,
            "executed": False,
            "live_calls": False,
            "hint": (
                f"Produktändringen är sparad i sandlådan ({plan_id}) utan skrivning till live WooCommerce. "
                f"Kör wp_preview_page(site_id='{site_id}', plan_id='{plan_id}', path='{clean_product_path}', focus_text=...) "
                "för att ta skärmdumpar och skapa klickbar sandbox-URL (preview_url)."
            ),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_workspace_page_draft",
    group="workspace",
    label="Skapa sidutkast i sandlåda",
    short_label="Sidutkast (sandbox)",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "page_type": {
                "type": "string",
                "enum": ["thankyou", "landing", "campaign", "comparison", "upsell", "faq", "custom"],
            },
            "body_html": {"type": "string"},
            "page_css": {"type": "string"},
            "shell_path": {"type": "string"},
            "template_file": {"type": "string"},
            "focus_selector": {"type": "string"},
            "synthetic_context": {"type": "object"},
        },
        "required": ["plan_id", "slug", "title", "body_html"],
        "additionalProperties": False,
    },
    outcome=_page_draft_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=True,
    deep_phase=True,
    capability_group="workspace",
)
def handle_workspace_page_draft(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    *,
    ops: Any,
) -> ToolOutput:
    from ecom_ops.integrations.wp_live_preview import (
        _STATIC_ASSET_HOSTS,
        _allowed_site_hosts,
        validate_focus_options,
        validate_preview_path,
    )

    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WP_OPS_OPERATE)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id)

    raw_slug = arguments.get("slug")
    raw_title = arguments.get("title")
    raw_body = arguments.get("body_html")
    raw_type = arguments.get("page_type", "custom")
    raw_css = arguments.get("page_css", "")
    raw_shell = arguments.get("shell_path", "/")
    raw_template = arguments.get("template_file", "")
    raw_focus = arguments.get("focus_selector")
    raw_ctx = arguments.get("synthetic_context")

    if not isinstance(raw_slug, str) or PAGE_SLUG_RE.fullmatch(raw_slug.strip().strip("/").lower()) is None:
        err = error_payload("InvalidToolArguments")
        err["hint"] = "Ange ett giltigt slug (t.ex. 'tack', 'takk-for-bestillingen', 'sommarkampanj')."
        return ToolOutput(err, site_id)
    slug = raw_slug.strip().strip("/").lower()

    if not isinstance(raw_title, str) or not raw_title.strip() or len(raw_title.strip()) > 200 or "\x00" in raw_title:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    title = " ".join(raw_title.split()).strip()

    if not isinstance(raw_type, str) or raw_type not in PAGE_DRAFT_TYPES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    if not isinstance(raw_body, str) or not raw_body.strip() or "\x00" in raw_body:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    if len(raw_body.encode("utf-8")) > PAGE_DRAFT_MAX_HTML_BYTES:
        err = error_payload("FileTooLarge")
        err["hint"] = "body_html får vara högst 48 KiB."
        return ToolOutput(err, site_id)
    if UNSAFE_PRODUCT_HTML_RE.search(raw_body):
        err = error_payload("InvalidToolArguments")
        err["hint"] = "body_html får inte innehålla <script>, <iframe>, <object>, <embed>, javascript: eller inline on*-attribut."
        return ToolOutput(err, site_id)

    page_css = ""
    if raw_css is not None:
        if not isinstance(raw_css, str) or "\x00" in raw_css:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if len(raw_css.encode("utf-8")) > PAGE_DRAFT_MAX_CSS_BYTES:
            err = error_payload("FileTooLarge")
            err["hint"] = "page_css får vara högst 24 KiB."
            return ToolOutput(err, site_id)
        if raw_css.strip() and not is_safe_pure_css_file("page-draft.css", raw_css):
            err = error_payload("InvalidToolArguments")
            err["hint"] = "page_css får endast innehålla säker CSS (ingen PHP, <script>, javascript: eller @import)."
            return ToolOutput(err, site_id)
        page_css = raw_css

    try:
        shell_path = validate_preview_path(raw_shell if isinstance(raw_shell, str) else "/")
    except ReviewError as exc:
        err = error_payload(exc.error_class)
        err["hint"] = "shell_path måste vara en publik sida på sajten (t.ex. '/' eller '/butikk/'), aldrig /checkout eller /cart."
        return ToolOutput(err, site_id)

    template_file = ""
    if raw_template is not None and raw_template != "":
        if not isinstance(raw_template, str) or FILE_PATH_RE.fullmatch(raw_template.strip()) is None:
            return ToolOutput(error_payload("InvalidFileTarget"), site_id)
        template_file = raw_template.strip()

    clean_focus: str | None = None
    if raw_focus is not None:
        if not isinstance(raw_focus, str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if raw_focus.strip():
            try:
                _, clean_focus, _, _ = validate_focus_options(focus_selector=raw_focus)
            except ReviewError as exc:
                return ToolOutput(error_payload(exc.error_class), site_id)

    synthetic_ctx: dict[str, Any] = {}
    if raw_ctx is not None:
        if not isinstance(raw_ctx, dict):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        if tools._contains_real_pii(raw_ctx):
            err = error_payload("RealPiiRefused")
            err["hint"] = (
                "synthetic_context får aldrig innehålla riktiga kundmejl, telefonnummer eller personnummer. "
                "Använd syntetiska adresser som 'test@example.invalid' (eller hämta via wp_build_sample_data)."
            )
            return ToolOutput(err, site_id)
        synthetic_ctx = dict(raw_ctx)

    site_model = tools._site_model(site_id)
    allowed_img_hosts: frozenset[str] = _STATIC_ASSET_HOSTS
    if site_model is not None and site_model.canonical_url:
        parsed_site = urllib.parse.urlparse(str(site_model.canonical_url))
        site_host = (parsed_site.hostname or "").strip().lower().rstrip(".")
        if site_host:
            allowed_img_hosts = _allowed_site_hosts(site_host) | _STATIC_ASSET_HOSTS

    sanitized_html = sanitize_page_draft_html(raw_body, allowed_hosts=allowed_img_hosts)
    if any(
        tools._contains_real_pii(field)
        for field in (title, raw_body, sanitized_html, page_css, synthetic_ctx)
    ):
        err = error_payload("RealPiiRefused")
        err["hint"] = (
            "Sidutkast får inte innehålla riktiga e-postadresser, telefonnummer eller personnummer/PII. "
            "Använd syntetisk exempeldata (t.ex. 'test@example.invalid')."
        )
        return ToolOutput(err, site_id)
    synthetic_ctx["contains_real_pii"] = False
    synthetic_ctx["synthetic_data"] = True
    draft_obj: dict[str, Any] = {
        "schema_version": 1,
        "slug": slug,
        "title": title,
        "page_type": raw_type,
        "shell_path": shell_path,
        "template_file": template_file,
        "focus_selector": clean_focus or "",
        "body_html": sanitized_html,
        "page_css": page_css,
        "synthetic_context": synthetic_ctx,
        "synthetic_data": True,
        "contains_real_pii": False,
    }
    new_json = json.dumps(draft_obj, ensure_ascii=False, indent=2) + "\n"
    if len(new_json.encode("utf-8")) > 65536:
        err = error_payload("FileTooLarge")
        err["hint"] = "Sammanlagd page_draft.json överskrider 64 KiB; korta ned body_html eller page_css."
        return ToolOutput(err, site_id)

    write_res = upsert_workspace_json(
        ops,
        tools.service.data_dir,
        site_id,
        plan_id,
        PAGE_DRAFT_FILE,
        new_json,
        actor=ctx.actor,
    )
    if not write_res.ok:
        return tools._file_result(write_res, site_id)

    wdata = dict(write_res.data or {})
    draft_summary = {
        "slug": slug,
        "title": title,
        "page_type": raw_type,
        "shell_path": shell_path,
        "template_file": template_file,
        "synthetic_data": True,
    }
    return ToolOutput(
        {
            "ok": True,
            "status": wdata.get("status") or "validated",
            "plan_id": plan_id,
            "site_id": site_id,
            "path": PAGE_DRAFT_FILE,
            "page_draft": draft_summary,
            "focus_selector": clean_focus,
            "work_sha256": wdata.get("work_sha256"),
            "base_sha256": wdata.get("base_sha256"),
            "risk": wdata.get("risk"),
            "review_path": f"/wp/{site_id}/reviews/{plan_id}",
            "login_required": True,
            "synthetic_data": True,
            "contains_real_pii": False,
            "executed": False,
            "live_calls": False,
            "note": "Sidutkast sparat i arbetsytans work/page_draft.json. Kör wp_preview_page(plan_id=...) för att rendera sidan i sajtens riktiga skal (desktop + mobil).",
        },
        site_id=site_id,
    )


__all__ = [
    "handle_workspace_page_draft",
    "handle_workspace_product_edit",
]
