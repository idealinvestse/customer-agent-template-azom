"""WordPress plugin lifecycle, auto-update policy, MU-plugins/drop-ins, and recovery tools."""

from __future__ import annotations

import re
from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.tools.registry import (
    plan_id_outcome,
    read_budget_outcome,
    register_tool,
)
from ecom_ops.wp_ops.tools.types import (
    INTEL_SLUG_RE,
    SITE_ID_RE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import error_payload, short_text

_CHANGE_ID_RE = re.compile(r"^chg-[a-f0-9]{12}$")
_HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,100}$")
_PLUGIN_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,140}$")
_UPDATE_MODES = frozenset({"single", "selected", "all", "policy"})
_UPDATE_POLICIES = frozenset({"security_only", "patch_only", "minor_safe", "non_critical_only"})
_AUTOUPDATE_PRESETS = frozenset({"disable_all", "non_critical_only", "security_only", "custom"})
_RECOVERY_ACTIONS = frozenset(
    {"rollback_snapshot", "deactivate", "quarantine_sftp", "restore_from_quarantine"}
)


def _require_perm(actor: str, perm: Permission, site_id: str) -> ToolOutput | None:
    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, perm)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id=site_id)
    return None


def _plugin_inventory_outcome(data: dict[str, Any], _reads: int | None) -> str:
    plugins = data.get("plugins")
    if isinstance(plugins, list):
        updates = sum(
            1 for row in plugins if isinstance(row, dict) and row.get("update_available") is True
        )
        if updates:
            return f"{len(plugins)} plugins ({updates} uppdateringar)"
        return f"{len(plugins)} plugins"
    return "plugininventering hämtad"


def _mu_dropins_outcome(data: dict[str, Any], _reads: int | None) -> str:
    mu = data.get("mu_plugins")
    dropins = data.get("dropins")
    if isinstance(mu, list) and isinstance(dropins, list):
        return f"{len(mu)} MU-plugins, {len(dropins)} drop-ins"
    if isinstance(mu, list):
        return f"{len(mu)} MU-plugins"
    return "MU-plugins och drop-ins hämtade"


def _scrub_str(tools: Any, site_obj: Any, value: Any, *, max_len: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = redact_secret_text(value, redact_whole_lines=True)
    return tools._scrub_bounded(site_obj, cleaned, max_len=max_len)


def _scrub_plugin_row(tools: Any, site_obj: Any, row: dict[str, Any]) -> dict[str, Any]:
    req_plugins = row.get("requires_plugins")
    clean_req = (
        [short_text(item, 64) for item in req_plugins[:16] if isinstance(item, str) and item.strip()]
        if isinstance(req_plugins, list)
        else []
    )
    return {
        "slug": short_text(row.get("slug"), 80),
        "plugin_file": short_text(row.get("plugin_file"), 140),
        "name": _scrub_str(tools, site_obj, row.get("name") or "", max_len=100),
        "version": short_text(row.get("version"), 32),
        "active": bool(row.get("active")),
        "network_active": bool(row.get("network_active")),
        "update_available": bool(row.get("update_available")),
        "new_version": short_text(row.get("new_version"), 32) or None,
        "auto_update": bool(row.get("auto_update")),
        "requires_wp": short_text(row.get("requires_wp"), 24),
        "requires_php": short_text(row.get("requires_php"), 24),
        "tested_wp": short_text(row.get("tested_wp"), 24),
        "wc_tested_up_to": short_text(row.get("wc_tested_up_to"), 24),
        "hpos_compatible": (
            row.get("hpos_compatible") if isinstance(row.get("hpos_compatible"), bool) else None
        ),
        "is_paused": bool(row.get("is_paused")),
        "is_critical": bool(row.get("is_critical")),
        "is_bridge": bool(row.get("is_bridge")),
        "requires_plugins": clean_req,
    }


def _scrub_mu_row(tools: Any, site_obj: Any, row: dict[str, Any]) -> dict[str, Any]:
    load_order = row.get("load_order")
    return {
        "slug": short_text(row.get("slug"), 80),
        "file": short_text(row.get("file"), 140),
        "name": _scrub_str(tools, site_obj, row.get("name") or "", max_len=100),
        "version": short_text(row.get("version"), 32),
        "is_loader": bool(row.get("is_loader")),
        "sha256": short_text(row.get("sha256"), 64),
        "load_order": load_order if isinstance(load_order, int) else 0,
    }


def _scrub_dropin_row(tools: Any, site_obj: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": short_text(row.get("file"), 120),
        "name": _scrub_str(tools, site_obj, row.get("name") or "", max_len=100),
        "description": _scrub_str(tools, site_obj, row.get("description") or "", max_len=200),
        "read_only": True,
    }


def _scrub_nested(tools: Any, site_obj: Any, obj: Any, *, depth: int = 0) -> Any:
    """Recursively redact secrets and cap depth/size across nested plan & health values."""
    if isinstance(obj, str):
        return _scrub_str(tools, site_obj, obj, max_len=120 if depth >= 4 else 240)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if depth >= 6:
        return "[truncated]"
    if isinstance(obj, list):
        return [_scrub_nested(tools, site_obj, item, depth=depth + 1) for item in obj[:40]]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(obj.items()):
            if idx >= 40:
                break
            if k in {"path", "diff", "new_text", "secret"}:
                continue
            clean_k = _scrub_str(tools, site_obj, str(k), max_len=48)
            out[clean_k] = _scrub_nested(tools, site_obj, v, depth=depth + 1)
        return out
    return _scrub_str(tools, site_obj, str(obj), max_len=120)


def _sanitize_plan_payload(
    tools: Any,
    site_obj: Any,
    site_id: str,
    raw_data: dict[str, Any],
) -> dict[str, Any]:
    """Return a clean, secret-scrubbed, strictly non-executable plan preview payload."""
    plan = raw_data.get("plan") if isinstance(raw_data.get("plan"), dict) else {}
    plan_id = short_text(raw_data.get("plan_id") or plan.get("plan_id"), 32)
    plan_sha256 = short_text(raw_data.get("plan_sha256") or plan.get("plan_sha256"), 64)
    warnings_raw = raw_data.get("warnings")
    if not isinstance(warnings_raw, list):
        warnings_raw = plan.get("warnings") if isinstance(plan.get("warnings"), list) else []
    warnings = [_scrub_str(tools, site_obj, str(w), max_len=200) for w in warnings_raw[:25]]

    payload: dict[str, Any] = {
        "ok": True,
        "status": short_text(raw_data.get("status") or plan.get("status"), 32) or "planned",
        "site_id": site_id,
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "warnings": warnings,
        "executed": False,
        "approval_required": True,
        "dashboard_path": f"/wp/{site_id}/plugins",
        "login_required": True,
        "live_calls": bool(raw_data.get("live_calls")),
    }
    if plan:
        safe_plan = _scrub_nested(tools, site_obj, plan)
        if isinstance(safe_plan, dict):
            safe_plan["executed"] = False
            safe_plan["approval_required"] = True
            payload["plan"] = safe_plan
        return payload

    for key in (
        "environment",
        "selection_mode",
        "policy",
        "preset",
        "action",
        "slug",
        "change_id",
        "canary",
        "include_critical",
        "allow_critical",
        "expires_at",
        "created_at",
        "risk",
        "preconditions",
        "batches",
        "skipped",
        "changes",
        "summary",
        "cli_command",
        "snapshot_verified",
        "sftp_rescue_ready",
    ):
        val = raw_data.get(key)
        if val is None:
            continue
        payload[key] = _scrub_nested(tools, site_obj, val)
    return payload


@register_tool(
    name="wp_plugin_inventory",
    group="file",
    label="Läs plugininventering och uppdateringar",
    short_label="Plugininventering",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "include_updates": {"type": "boolean"},
            "include_wporg": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=_plugin_inventory_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_plugin_inventory(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    include_updates = arguments.get("include_updates", True)
    include_wporg = arguments.get("include_wporg", False)
    if type(include_updates) is not bool or type(include_wporg) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    res = tools.service.plugin_inventory(
        site_id,
        actor=ctx.actor,
        include_updates=include_updates,
        include_wporg=include_wporg,
    )
    if not res.ok:
        return tools._file_result(res, site_id)
    raw = dict(res.data or {})
    site_obj = tools._site_model(site_id)
    plugins = [
        _scrub_plugin_row(tools, site_obj, row)
        for row in (raw.get("plugins") or [])[:120]
        if isinstance(row, dict)
    ]
    mu_plugins = [
        _scrub_mu_row(tools, site_obj, row)
        for row in (raw.get("mu_plugins") or [])[:50]
        if isinstance(row, dict)
    ]
    dropins = [
        _scrub_dropin_row(tools, site_obj, row)
        for row in (raw.get("dropins") or [])[:30]
        if isinstance(row, dict)
    ]
    constants_raw = raw.get("constants") if isinstance(raw.get("constants"), dict) else {}
    constants = {
        "disallow_file_mods": bool(constants_raw.get("disallow_file_mods")),
        "automatic_updater_disabled": bool(constants_raw.get("automatic_updater_disabled")),
        "wp_auto_update_core": constants_raw.get("wp_auto_update_core"),
    }
    filters_raw = raw.get("autoupdate_filters")
    autoupdate_filters = (
        [_scrub_str(tools, site_obj, str(f), max_len=120) for f in filters_raw[:20]]
        if isinstance(filters_raw, list)
        else []
    )
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "wp_version": short_text(raw.get("wp_version"), 32),
            "php_version": short_text(raw.get("php_version"), 32),
            "plugins": plugins,
            "mu_plugins": mu_plugins,
            "dropins": dropins,
            "constants": constants,
            "autoupdate_filters": autoupdate_filters,
            "updates_available": sum(1 for row in plugins if row["update_available"]),
            "dashboard_path": f"/wp/{site_id}/plugins",
            "live_calls": bool(raw.get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_mu_plugins_and_dropins",
    group="file",
    label="Läs MU-plugins och drop-ins",
    short_label="MU-plugins & drop-ins",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
        },
        "additionalProperties": False,
    },
    outcome=_mu_dropins_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_mu_plugins_and_dropins(
    tools: Any, ctx: ToolContext, _arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied

    res = tools.service.mu_plugins_and_dropins(site_id, actor=ctx.actor)
    if not res.ok:
        return tools._file_result(res, site_id)
    raw = dict(res.data or {})
    site_obj = tools._site_model(site_id)
    mu_plugins = [
        _scrub_mu_row(tools, site_obj, row)
        for row in (raw.get("mu_plugins") or [])[:50]
        if isinstance(row, dict)
    ]
    dropins = [
        _scrub_dropin_row(tools, site_obj, row)
        for row in (raw.get("dropins") or [])[:30]
        if isinstance(row, dict)
    ]
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "mu_plugins": mu_plugins,
            "dropins": dropins,
            "dropins_read_only": True,
            "load_order": "mu-plugins -> plugins -> theme",
            "dashboard_path": f"/wp/{site_id}/plugins",
            "live_calls": bool(raw.get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_plugin_diagnostics",
    group="file",
    label="Kör plugindiagnostik och felloggstriage",
    short_label="Plugindiagnostik",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "hooks": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "log_lines": {"type": "integer", "minimum": 1, "maximum": 150},
        },
        "additionalProperties": False,
    },
    outcome=read_budget_outcome("plugindiagnostik", fallback="plugindiagnostik klar", parenthesized=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    read_budget=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_plugin_diagnostics(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
    if denied is not None:
        return denied

    hooks_raw = arguments.get("hooks")
    hooks: list[str] | None = None
    if "hooks" in arguments:
        if (
            not isinstance(hooks_raw, list)
            or len(hooks_raw) > 12
            or any(not isinstance(h, str) or not _HOOK_NAME_RE.fullmatch(h) for h in hooks_raw)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        hooks = list(hooks_raw)

    log_lines = arguments.get("log_lines", 80)
    if type(log_lines) is not int or not (1 <= log_lines <= 150):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    res = tools.service.plugin_diagnostics(
        site_id, actor=ctx.actor, hooks=hooks, log_lines=log_lines
    )
    if not res.ok:
        return tools._file_result(res, site_id)
    raw = dict(res.data or {})
    site_obj = tools._site_model(site_id)

    paused_clean: list[dict[str, Any]] = []
    for item in (raw.get("paused_plugins") or [])[:30]:
        if isinstance(item, dict):
            paused_clean.append(
                {
                    "slug": short_text(item.get("slug") or item.get("plugin"), 80),
                    "error": _scrub_str(tools, site_obj, str(item.get("error") or item.get("message") or ""), max_len=200),
                }
            )
    log_tail = [
        _scrub_str(tools, site_obj, str(line), max_len=240)
        for line in (raw.get("debug_log_tail") or [])[:150]
    ]
    hook_map_clean: dict[str, list[dict[str, Any]]] = {}
    raw_hmap = raw.get("hook_map")
    if isinstance(raw_hmap, dict):
        for hook_name, entries in list(raw_hmap.items())[:12]:
            if not isinstance(entries, list):
                continue
            hook_map_clean[short_text(hook_name, 80)] = [
                {
                    "priority": e.get("priority") if isinstance(e.get("priority"), int) else 10,
                    "callback": _scrub_str(tools, site_obj, str(e.get("callback") or ""), max_len=120),
                }
                for e in entries[:25]
                if isinstance(e, dict)
            ]
    backups_clean: list[dict[str, Any]] = []
    for bk in (raw.get("available_backups") or [])[:30]:
        if isinstance(bk, dict):
            backups_clean.append(
                {
                    "backup_id": short_text(bk.get("backup_id") or bk.get("file"), 96),
                    "slug": short_text(bk.get("slug"), 80),
                    "created_at": short_text(bk.get("created_at"), 40),
                    "size_bytes": bk.get("size_bytes") if isinstance(bk.get("size_bytes"), int) else 0,
                }
            )
    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "paused_plugins": paused_clean,
            "debug_log_tail": log_tail,
            "hook_map": hook_map_clean,
            "available_backups": backups_clean,
            "bridge_health": (
                _scrub_nested(tools, site_obj, raw.get("bridge_health"))
                if isinstance(raw.get("bridge_health"), dict)
                else {}
            ),
            "sftp_rescue_available": bool(raw.get("sftp_rescue_available")),
            "dashboard_path": f"/wp/{site_id}/plugins",
            "live_calls": bool(raw.get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_plan_plugin_updates",
    group="file",
    label="Skapa pluginuppdateringsplan (förhandsvisning)",
    short_label="Pluginuppdateringsplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "selection_mode": {
                "type": "string",
                "enum": ["single", "selected", "all", "policy"],
            },
            "slugs": {"type": "array", "items": {"type": "string"}, "maxItems": 40},
            "policy": {
                "type": "string",
                "enum": ["security_only", "patch_only", "minor_safe", "non_critical_only"],
            },
            "exclude_slugs": {"type": "array", "items": {"type": "string"}, "maxItems": 40},
            "include_critical": {"type": "boolean"},
            "canary": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("uppdateringsplan {pid} skapad", "uppdateringsplan skapad", nested_plan=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_plan_plugin_updates(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    selection_mode = arguments.get("selection_mode", "policy")
    policy = arguments.get("policy", "non_critical_only")
    include_critical = arguments.get("include_critical", False)
    canary = arguments.get("canary", True)
    slugs = arguments.get("slugs")
    exclude_slugs = arguments.get("exclude_slugs")

    if not isinstance(selection_mode, str) or selection_mode not in _UPDATE_MODES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if not isinstance(policy, str) or policy not in _UPDATE_POLICIES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if type(include_critical) is not bool or type(canary) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if slugs is not None and (
        not isinstance(slugs, list)
        or len(slugs) > 40
        or any(not isinstance(s, str) or not INTEL_SLUG_RE.fullmatch(s) for s in slugs)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if exclude_slugs is not None and (
        not isinstance(exclude_slugs, list)
        or len(exclude_slugs) > 40
        or any(not isinstance(s, str) or not INTEL_SLUG_RE.fullmatch(s) for s in exclude_slugs)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    site_obj = tools._site_model(site_id)
    needed_perm = (
        Permission.WP_OPS_ADMIN
        if (include_critical or (site_obj is not None and site_obj.environment == "production"))
        else Permission.WP_OPS_OPERATE
    )
    denied = _require_perm(ctx.actor, needed_perm, site_id)
    if denied is not None:
        return denied

    res = tools.service.plan_plugin_updates(
        site_id,
        actor=ctx.actor,
        selection_mode=selection_mode,
        slugs=slugs,
        policy=policy,
        exclude_slugs=exclude_slugs,
        include_critical=include_critical,
        canary=canary,
    )
    if not res.ok:
        return tools._file_result(res, site_id)
    return ToolOutput(
        _sanitize_plan_payload(tools, site_obj, site_id, dict(res.data or {})),
        site_id=site_id,
    )


@register_tool(
    name="wp_plan_plugin_autoupdate",
    group="file",
    label="Skapa autouppdateringsplan (förhandsvisning)",
    short_label="Autouppdateringsplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "preset": {
                "type": "string",
                "enum": ["disable_all", "non_critical_only", "security_only", "custom"],
            },
            "changes": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "plugin": {"type": "string"},
                        "plugin_file": {"type": "string"},
                        "slug": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            },
            "allow_critical": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("autouppdateringsplan {pid} skapad", "autouppdateringsplan skapad", nested_plan=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_plan_plugin_autoupdate(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
    if denied is not None:
        return denied

    preset = arguments.get("preset", "non_critical_only")
    allow_critical = arguments.get("allow_critical", False)
    changes_raw = arguments.get("changes")
    if not isinstance(preset, str) or preset not in _AUTOUPDATE_PRESETS:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if type(allow_critical) is not bool:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    changes: list[dict[str, Any]] | None = None
    if changes_raw is not None:
        if not isinstance(changes_raw, list) or len(changes_raw) > 50:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        changes = []
        for item in changes_raw:
            if not isinstance(item, dict) or type(item.get("enabled")) is not bool:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
            target = item.get("plugin_file") or item.get("plugin") or item.get("slug")
            if not isinstance(target, str) or not _PLUGIN_FILE_RE.fullmatch(target):
                return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
            normalized: dict[str, Any] = {"enabled": item["enabled"]}
            for k in ("plugin", "plugin_file", "slug"):
                if isinstance(item.get(k), str) and item[k]:
                    normalized[k] = item[k]
            changes.append(normalized)

    res = tools.service.plan_plugin_autoupdate(
        site_id,
        actor=ctx.actor,
        preset=preset,
        changes=changes,
        allow_critical=allow_critical,
    )
    if not res.ok:
        return tools._file_result(res, site_id)
    site_obj = tools._site_model(site_id)
    return ToolOutput(
        _sanitize_plan_payload(tools, site_obj, site_id, dict(res.data or {})),
        site_id=site_id,
    )


@register_tool(
    name="wp_plan_plugin_recovery",
    group="file",
    label="Skapa återställnings-/karantänplan (förhandsvisning)",
    short_label="Pluginåterställningsplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "slug": {"type": "string"},
            "action": {
                "type": "string",
                "enum": [
                    "rollback_snapshot",
                    "deactivate",
                    "quarantine_sftp",
                    "restore_from_quarantine",
                ],
            },
            "change_id": {"type": "string"},
        },
        "required": ["slug", "action"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("återställningsplan {pid} skapad", "återställningsplan skapad", nested_plan=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="drift_analysis",
)
def handle_plan_plugin_recovery(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
    if denied is not None:
        return denied

    slug = arguments.get("slug")
    action = arguments.get("action")
    change_id = arguments.get("change_id")
    if not isinstance(slug, str) or not INTEL_SLUG_RE.fullmatch(slug):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if not isinstance(action, str) or action not in _RECOVERY_ACTIONS:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if change_id is not None and (
        not isinstance(change_id, str) or not _CHANGE_ID_RE.fullmatch(change_id.strip())
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    res = tools.service.plan_plugin_recovery(
        site_id,
        actor=ctx.actor,
        slug=slug,
        action=action,
        change_id=change_id.strip() if isinstance(change_id, str) else None,
    )
    if not res.ok:
        return tools._file_result(res, site_id)
    site_obj = tools._site_model(site_id)
    return ToolOutput(
        _sanitize_plan_payload(tools, site_obj, site_id, dict(res.data or {})),
        site_id=site_id,
    )


__all__ = [
    "handle_mu_plugins_and_dropins",
    "handle_plan_plugin_autoupdate",
    "handle_plan_plugin_recovery",
    "handle_plan_plugin_updates",
    "handle_plugin_diagnostics",
    "handle_plugin_inventory",
]
