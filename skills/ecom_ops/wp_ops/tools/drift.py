"""WordPress drift / operations tool handlers (status, backups, security, plans, open ops)."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.tools.registry import count_outcome, plan_id_outcome, register_tool
from ecom_ops.wp_ops.tools.types import (
    COMPONENTS,
    OPERATION_ID_RE,
    SITE_ID_RE,
    WP_TOOL_MAX_AGE_SECONDS,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import (
    error_class_token,
    error_payload,
    is_expired,
    payload_digest,
    result_token,
    safe_failure,
    safe_operation_id,
    short_text,
    site_state,
    valid_plan,
)

PLAN_LOCK = threading.RLock()


def read_plan_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("plans", []) if isinstance(raw, dict) else []
    return [row for row in rows if isinstance(row, dict) and valid_plan(row)]


def store_plan(data_dir: Path, record: dict[str, Any]) -> None:
    path = data_dir / "wp_ops_plans.json"
    with PLAN_LOCK:
        rows = read_plan_rows(path)
        rows = [row for row in rows if not is_expired(row.get("expires_at"))]
        rows.append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"plans": rows[-200:]}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def load_plans(data_dir: Path) -> list[dict[str, Any]]:
    return read_plan_rows(data_dir / "wp_ops_plans.json")


def _status_outcome(data: dict[str, Any], _reads: int | None) -> str:
    st = short_text(data.get("status"), 24) or "ok"
    incidents = data.get("open_incidents")
    if isinstance(incidents, int):
        return f"status: {st}, {incidents} öppna driftärenden"
    return f"status: {st}"


def _security_outcome(data: dict[str, Any], _reads: int | None) -> str:
    if data.get("coverage") == "not_configured":
        return "ingen skanner konfigurerad"
    return f"status: {short_text(data.get('status'), 24) or 'unknown'}"


def _build_preview_plan(
    tools: Any,
    site_id: str,
    actor: str,
    site: dict[str, Any],
    components: tuple[str, ...],
    operation: str,
    backup: dict[str, Any] | None,
    *,
    channel: str,
    conversation_id: str,
) -> ToolOutput:
    now = datetime.now(UTC)
    backups = tools.service.list_backups(site_id, actor=actor)
    if not backups.ok:
        return ToolOutput(safe_failure(backups), site_id)
    latest = backup or next(iter((backups.data or {}).get("backups", [])), None)
    plan_id = "wp-plan-" + uuid.uuid4().hex[:16]
    unsupported_reason = (
        "Update discovery adapter is not configured."
        if operation == "update"
        else "Restore adapter is not implemented; mock manifests contain no restorable bytes."
    )
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "site_id": site_id,
        "environment": short_text(site.get("environment"), 16),
        "operation": operation,
        "target_components": list(components),
        "current_versions": {
            "wordpress": short_text(site.get("wordpress_version"), 32),
            "php": short_text(site.get("php_version"), 32),
        },
        "current_versions_source": "registry",
        "target_versions": {},
        "backup_ref": short_text((latest or {}).get("id"), 32),
        "backup_status": short_text((latest or {}).get("completion"), 20) or "unknown",
        "preconditions": [
            "Registered site and actor permission are rechecked on each read.",
            "Verified update or restore adapter is unavailable.",
            "Production changes and restores are not executable.",
        ],
        "staging_evidence": "not_available",
        "expected_impact": "No change will be made; verified target versions and provider impact are unavailable.",
        "recovery_note": "No restore path is available; do not treat this preview as a rollback plan.",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=WP_TOOL_MAX_AGE_SECONDS)).isoformat(),
        "created_by": actor,
        "status": "unsupported",
        "unsupported_reason": unsupported_reason,
        "executable": False,
        "approval_available": False,
    }
    digest = payload_digest(payload)
    record = {
        **payload,
        "digest": digest,
        "_scope": {"channel": channel[:32], "conversation_id": conversation_id[:120]},
    }
    record["_scope_digest"] = payload_digest(record["_scope"])
    tools._store_plan(record)
    return ToolOutput(
        {"ok": True, "status": "unsupported", "plan": {**payload, "digest": digest}, "live_calls": False},
        site_id=site_id,
    )


@register_tool(
    name="wp_list_sites",
    group="drift",
    label="Lista WordPress-sajter",
    short_label="WordPress-sajter",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    outcome=count_outcome("sites", "sajter", "sajter listade"),
    in_wp_schemas=True,
    requires_site=False,
    capability_group="drift_analysis",
)
def handle_list_sites(tools: Any, ctx: ToolContext, _arguments: dict[str, Any], **_kw: Any) -> ToolOutput:
    result = tools.service.list_sites(actor=ctx.actor)
    if not result.ok:
        return ToolOutput(safe_failure(result))
    sites = []
    for row in (result.data or {}).get("sites", []):
        if not isinstance(row, dict):
            continue
        sites.append(
            {
                "site_id": row.get("id"),
                "name": row.get("display_name"),
                "environment": row.get("environment"),
                "status": site_state(row),
            }
        )
    return ToolOutput({"ok": True, "status": "ok", "sites": sites, "live_calls": False})


@register_tool(
    name="wp_site_status",
    group="drift",
    label="Kontrollera sajtstatus",
    short_label="WordPress-status",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": SITE_ID_RE.pattern}},
        "additionalProperties": False,
    },
    outcome=_status_outcome,
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_site_status(
    _tools: Any,
    ctx: ToolContext,
    _arguments: dict[str, Any],
    *,
    site_result: WpOpsResult,
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    data = site_result.data or {}
    site = data.get("site") or {}
    checks = []
    for row in data.get("checks", []):
        if isinstance(row, dict):
            checks.append(
                {
                    "check": short_text(row.get("check"), 32),
                    "result": result_token(row.get("result")),
                    "checked_at": short_text(row.get("checked_at"), 32),
                    "error_class": error_class_token(row.get("error_class")),
                }
            )
    operation_ids = tuple(
        str(row.get("id"))
        for row in data.get("incidents", [])
        if (
            isinstance(row, dict)
            and row.get("state") == "open"
            and isinstance(row.get("id"), str)
            and OPERATION_ID_RE.fullmatch(row["id"])
        )
    )[:20]
    return ToolOutput(
        {
            "ok": True,
            "status": site_state(site),
            "site_id": site_id,
            "environment": short_text(site.get("environment"), 16),
            "wordpress_version": short_text(site.get("wordpress_version"), 32),
            "version_source": "registry",
            "php_version": short_text(site.get("php_version"), 32),
            "open_incidents": int(site.get("open_incidents") or 0),
            "checks": checks[:16],
            "provenance": "registered-site mock probes and stored state; no live calls",
            "live_calls": False,
        },
        site_id=site_id,
        operation_ids=operation_ids,
    )


@register_tool(
    name="wp_backup_status",
    group="drift",
    label="Kontrollera backupstatus",
    short_label="Backupstatus",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": SITE_ID_RE.pattern}},
        "additionalProperties": False,
    },
    outcome=count_outcome("backups", "backups", "backupstatus läst"),
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_backup_status(tools: Any, ctx: ToolContext, _arguments: dict[str, Any], **_kw: Any) -> ToolOutput:
    site_id = str(ctx.site_id)
    result = tools.service.list_backups(site_id, actor=ctx.actor)
    if not result.ok:
        return ToolOutput(safe_failure(result), site_id)
    rows = []
    for row in (result.data or {}).get("backups", []):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "backup_id": short_text(row.get("id"), 32),
                "status": short_text(row.get("completion"), 20) or "unknown",
                "integrity": short_text(row.get("integrity"), 20) or "unknown",
                "created_at": short_text(row.get("created_at"), 32),
                "verified_at": short_text(row.get("verified_at"), 32),
                "provider": "mock",
                "offsite": False,
                "encryption": "unconfigured",
                "bytes_copied": False,
                "payload": "metadata only",
                "live_calls": False,
            }
        )
    return ToolOutput(
        {
            "ok": True,
            "status": (
                "verified_mock_metadata"
                if rows and rows[0]["status"] == "verified" and rows[0]["integrity"] == "passed"
                else "unknown"
            ),
            "site_id": site_id,
            "backups": rows[:10],
            "provenance": "local mock manifest metadata; no site files or database bytes copied",
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_security_findings",
    group="drift",
    label="Säkerhetsöversikt",
    short_label="Säkerhetsfynd",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": SITE_ID_RE.pattern}},
        "additionalProperties": False,
    },
    outcome=_security_outcome,
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_security_findings(
    _tools: Any,
    ctx: ToolContext,
    _arguments: dict[str, Any],
    *,
    site_result: WpOpsResult,
    site_record: dict[str, Any],
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    capabilities = site_record.get("capabilities") if isinstance(site_record.get("capabilities"), dict) else {}
    capability = capabilities.get("security_scanner", "unknown")
    checks = [
        row
        for row in (site_result.data or {}).get("checks", [])
        if isinstance(row, dict) and row.get("check") == "checksum"
    ]
    return ToolOutput(
        {
            "ok": True,
            "status": "unknown",
            "site_id": site_id,
            "coverage": "not_configured",
            "scanner_capability": capability if capability in {"available", "unavailable", "unknown"} else "unknown",
            "checksum_state": result_token(checks[-1].get("result")) if checks else "unknown",
            "findings": None,
            "message": "Ingen säkerhetsskanner eller checksumma-adapter är implementerad; resultat betyder inte rent.",
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_prepare_update_plan",
    group="drift",
    label="Förbered uppdateringsplan",
    short_label="Uppdateringsplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "component_filter": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(COMPONENTS)},
                "maxItems": 3,
                "uniqueItems": True,
            },
        },
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("förhandsgranskning {pid} (ej körbar)", "låst förhandsgranskning", nested_plan=True),
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_prepare_update_plan(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    *,
    site_record: dict[str, Any],
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    components = arguments.get("component_filter", ["core", "plugins", "themes"])
    if (
        not isinstance(components, list)
        or len(components) > 3
        or any(not isinstance(value, str) or value not in COMPONENTS for value in components)
        or len(set(components)) != len(components)
    ):
        return ToolOutput(error_payload("InvalidComponentFilter"), site_id)
    return _build_preview_plan(
        tools,
        site_id,
        ctx.actor,
        site_record,
        tuple(sorted(components)),
        "update",
        None,
        channel=ctx.channel,
        conversation_id=ctx.conversation_id,
    )


@register_tool(
    name="wp_prepare_restore_plan",
    group="drift",
    label="Förbered återställningsplan",
    short_label="Återställningsplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "backup_id": {"type": "string", "pattern": r"^bk-[a-f0-9]{12}$"},
        },
        "required": ["backup_id"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("förhandsgranskning {pid} (ej körbar)", "låst förhandsgranskning", nested_plan=True),
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_prepare_restore_plan(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    *,
    site_record: dict[str, Any],
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    backup_id = arguments.get("backup_id")
    if not isinstance(backup_id, str) or not re.fullmatch(r"bk-[a-f0-9]{12}", backup_id):
        return ToolOutput(error_payload("InvalidBackupId"), site_id)
    backup_result = tools.service.list_backups(site_id, actor=ctx.actor)
    if not backup_result.ok:
        return ToolOutput(safe_failure(backup_result), site_id)
    record = next(
        (row for row in (backup_result.data or {}).get("backups", []) if row.get("id") == backup_id),
        None,
    )
    if not record:
        return ToolOutput(error_payload("UnknownBackupOrDenied"), site_id)
    return _build_preview_plan(
        tools,
        site_id,
        ctx.actor,
        site_record,
        (),
        "restore",
        record,
        channel=ctx.channel,
        conversation_id=ctx.conversation_id,
    )


@register_tool(
    name="wp_list_open_operations",
    group="drift",
    label="Lista öppna driftärenden",
    short_label="Öppna driftärenden",
    schema={
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": SITE_ID_RE.pattern}},
        "additionalProperties": False,
    },
    outcome=count_outcome("operations", "öppna driftärenden", "driftärenden listade"),
    in_wp_schemas=True,
    capability_group="drift_analysis",
)
def handle_list_open_operations(
    tools: Any,
    ctx: ToolContext,
    _arguments: dict[str, Any],
    *,
    site_result: WpOpsResult,
    **_kw: Any,
) -> ToolOutput:
    site_id = str(ctx.site_id)
    data = site_result.data or {}
    incidents = []
    for row in data.get("incidents", []):
        if (
            not isinstance(row, dict)
            or row.get("state") != "open"
            or not safe_operation_id(row.get("id"))
        ):
            continue
        incidents.append(
            {
                "operation_id": row["id"],
                "type": "incident",
                "state": "open",
                "check": short_text(row.get("check"), 32),
                "opened_at": short_text(row.get("opened_at"), 32),
            }
        )
    plans = [
        {"operation_id": row["plan_id"], "type": row["operation"], "state": row["status"]}
        for row in tools._load_plans()
        if row.get("site_id") == site_id
        and row.get("created_by") == ctx.actor
        and (row.get("_scope") or {}).get("channel") == ctx.channel
        and (row.get("_scope") or {}).get("conversation_id") == ctx.conversation_id[:120]
        and safe_operation_id(row.get("plan_id"))
        and row.get("status") in {"pending_approval", "executing"}
        and not is_expired(row.get("expires_at"))
    ]
    operations = (incidents + plans)[:20]
    return ToolOutput(
        {"ok": True, "status": "ok", "site_id": site_id, "operations": operations, "live_calls": False},
        site_id=site_id,
        operation_ids=tuple(str(row["operation_id"]) for row in operations),
    )


__all__ = [
    "PLAN_LOCK",
    "handle_backup_status",
    "handle_list_open_operations",
    "handle_list_sites",
    "handle_prepare_restore_plan",
    "handle_prepare_update_plan",
    "handle_security_findings",
    "handle_site_status",
    "load_plans",
    "read_plan_rows",
    "store_plan",
]
