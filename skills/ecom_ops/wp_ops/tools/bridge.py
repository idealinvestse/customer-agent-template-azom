"""WordPress Companion Bridge & Change Ledger read-only agent tools."""

from __future__ import annotations

from typing import Any

from ecom_ops.wp_ops.tools.registry import count_outcome, register_tool, status_outcome
from ecom_ops.wp_ops.tools.types import SITE_ID_RE, ToolContext, ToolOutput
from ecom_ops.wp_ops.tools.validation import error_payload, safe_failure, short_text


def _bridge_translations_outcome(data: dict[str, Any], _reads: int | None) -> str:
    tr = data.get("translations")
    if isinstance(tr, dict):
        langs = tr.get("languages")
        provider = short_text(tr.get("provider"), 24) or "none"
        if isinstance(langs, list):
            return f"översättningar ({provider}): {len(langs)} språk"
    return "översättningar lästa"


@register_tool(
    name="wp_bridge_health",
    group="file",
    label="Läs bridge-hälsa och miljö",
    short_label="Bridge-hälsa",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
        },
        "additionalProperties": False,
    },
    outcome=status_outcome("bridge-hälsa", default_status="ok"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    capability_group="drift_analysis",
    untrusted_output=True,
)
def handle_bridge_health(
    tools: Any, ctx: ToolContext, _arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    res = tools.service.bridge_health(site_id, actor=ctx.actor)
    if not res.ok:
        return ToolOutput(safe_failure(res), site_id=site_id)
    raw = dict((res.data or {}).get("health") or {})
    site_obj = tools._site_model(site_id)
    theme_obj = raw.get("theme") if isinstance(raw.get("theme"), dict) else {}
    active_theme = (
        theme_obj.get("name")
        or theme_obj.get("stylesheet")
        or raw.get("active_theme")
        or ""
    )
    wp_version = raw.get("wordpress_version") or raw.get("wp_version") or ""
    woo_version = raw.get("woocommerce_version") or raw.get("woo_version") or ""
    health_view = {
        "wp_version": short_text(wp_version, 32),
        "woo_version": short_text(woo_version, 32),
        "wordpress_version": short_text(wp_version, 32),
        "woocommerce_version": short_text(woo_version, 32),
        "php_version": short_text(raw.get("php_version"), 32),
        "active_theme": short_text(active_theme, 64),
        "active_plugins": int(raw.get("active_plugins") or 0)
        if isinstance(raw.get("active_plugins"), int)
        else len(raw.get("active_plugins") or [])
        if isinstance(raw.get("active_plugins"), list)
        else 0,
        "multilingual": short_text(raw.get("multilingual"), 32) or "none",
        "maintenance_active": bool(raw.get("maintenance_active")),
        "opcache_enabled": bool(raw.get("opcache_enabled")),
        "memory_limit": short_text(raw.get("memory_limit"), 24),
        "site_url": tools._scrub_bounded(site_obj, raw.get("site_url") or "", max_len=160),
        "home_url": tools._scrub_bounded(site_obj, raw.get("home_url") or "", max_len=160),
    }
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "health": health_view,
            "live_calls": bool((res.data or {}).get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_list_translations",
    group="file",
    label="Läs språk och översättningar (WPML/Polylang)",
    short_label="Översättningar",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "post_id": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    outcome=_bridge_translations_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    capability_group="drift_analysis",
    untrusted_output=True,
)
def handle_list_translations(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    post_id_raw = arguments.get("post_id")
    post_id: int | None = None
    if post_id_raw is not None:
        if isinstance(post_id_raw, bool) or not isinstance(post_id_raw, int) or post_id_raw < 1:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        post_id = post_id_raw
    res = tools.service.bridge_translations(site_id, post_id=post_id, actor=ctx.actor)
    if not res.ok:
        return ToolOutput(safe_failure(res), site_id=site_id)
    raw = dict((res.data or {}).get("translations") or {})
    site_obj = tools._site_model(site_id)
    langs: list[dict[str, Any]] = []
    for lang in (raw.get("languages") or [])[:20]:
        if not isinstance(lang, dict):
            continue
        langs.append(
            {
                "code": short_text(lang.get("code"), 16),
                "name": short_text(lang.get("name"), 48),
                "locale": short_text(lang.get("locale"), 24),
                "active": bool(lang.get("active")),
            }
        )
    tr_map: dict[str, dict[str, Any]] = {}
    raw_map = raw.get("translations")
    if isinstance(raw_map, dict):
        for code, entry in list(raw_map.items())[:20]:
            if not isinstance(entry, dict):
                continue
            tr_map[str(code)[:16]] = {
                "id": entry.get("id"),
                "status": short_text(entry.get("status"), 24),
                "title": tools._scrub_bounded(site_obj, entry.get("title") or "", max_len=120),
            }
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "translations": {
                "provider": short_text(raw.get("provider"), 32) or "none",
                "default_lang": short_text(raw.get("default_lang"), 16),
                "languages": langs,
                "post_id": raw.get("post_id"),
                "translations": tr_map,
            },
            "live_calls": bool((res.data or {}).get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_change_history",
    group="file",
    label="Visa ändringslogg och snapshots",
    short_label="Ändringslogg",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "category": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=count_outcome("changes", "ändringar", "ändringslogg läst"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    capability_group="drift_analysis",
    untrusted_output=True,
)
def handle_change_history(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    shown = tools.service.show_site(site_id, actor=ctx.actor)
    if not shown.ok:
        return ToolOutput(safe_failure(shown), site_id=site_id)
    limit = arguments.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    raw_cat = arguments.get("category")
    category: str | None = None
    if raw_cat is not None:
        if not isinstance(raw_cat, str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        category = raw_cat.strip()[:48] or None
    res = tools.service.list_changes(site_id, actor=ctx.actor, category=category, limit=limit)
    if not res.ok:
        return ToolOutput(safe_failure(res), site_id=site_id)
    site_obj = tools._site_model(site_id)
    rows: list[dict[str, Any]] = []
    for row in (res.data or {}).get("changes") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("change_id") or "")
        rows.append(
            {
                "change_id": cid,
                "category": short_text(row.get("category"), 32),
                "transport": short_text(row.get("transport"), 24),
                "actor": short_text(row.get("actor"), 32),
                "target_ref": tools._scrub_bounded(site_obj, row.get("target_ref") or "", max_len=96),
                "summary": tools._scrub_bounded(site_obj, row.get("summary") or "", max_len=160),
                "status": short_text(row.get("status"), 24),
                "snapshot_verified": bool(row.get("snapshot_verified")),
                "created_at": short_text(row.get("created_at"), 40),
                "dashboard_path": f"/wp/{site_id}/changes/{cid}" if cid else f"/wp/{site_id}/changes",
            }
        )
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "changes": rows,
            "dashboard_path": f"/wp/{site_id}/changes",
            "live_calls": False,
        },
        site_id=site_id,
    )


__all__ = [
    "handle_bridge_health",
    "handle_change_history",
    "handle_list_translations",
]
