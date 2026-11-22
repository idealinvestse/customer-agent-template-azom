"""Bounded, typed WordPress operations tools for the chat agent.

Every tool resolves a registered site through ``WpOpsService`` and runs under
its server-side RBAC checks. This module exposes reads and non-executable plan
previews only; it has no shell, PHP, update, or restore execution path.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import resolve_actor
from ecom_ops.wp_ops.service import WpOpsResult, WpOpsService

WP_TOOL_MAX_CALLS = 3
WP_TOOL_MAX_SECONDS = 30.0
WP_TOOL_MAX_RESULT_BYTES = 4096
WP_TOOL_MAX_TOTAL_BYTES = 10_240
WP_TOOL_MAX_AGE_SECONDS = 12 * 60 * 60

_SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPERATION_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|"
    r"wp-plan-[0-9a-f]{16})$"
)
_PLAN_LOCK = threading.RLock()
_COMPONENTS = frozenset({"core", "plugins", "themes"})

WP_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "wp_list_sites": {"type": "object", "properties": {}, "additionalProperties": False},
    "wp_site_status": {
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": _SITE_ID.pattern}},
        "additionalProperties": False,
    },
    "wp_backup_status": {
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": _SITE_ID.pattern}},
        "additionalProperties": False,
    },
    "wp_security_findings": {
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": _SITE_ID.pattern}},
        "additionalProperties": False,
    },
    "wp_prepare_update_plan": {
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": _SITE_ID.pattern},
            "component_filter": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_COMPONENTS)},
                "maxItems": 3,
                "uniqueItems": True,
            },
        },
        "additionalProperties": False,
    },
    "wp_prepare_restore_plan": {
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": _SITE_ID.pattern},
            "backup_id": {"type": "string", "pattern": r"^bk-[a-f0-9]{12}$"},
        },
        "required": ["backup_id"],
        "additionalProperties": False,
    },
    "wp_list_open_operations": {
        "type": "object",
        "properties": {"site_id": {"type": "string", "pattern": _SITE_ID.pattern}},
        "additionalProperties": False,
    },
}

WP_TOOL_GUIDE = (
    "WordPress-driftfrågor ska besvaras med namngivna verktyg här nedan; "
    "skapa exakt ett <tool_call>{\"name\":\"...\",\"arguments\":{...}}</tool_call> "
    "per steg och invänta resultatet. Verktygen är wp_list_sites(), "
    "wp_site_status(site_id?), wp_backup_status(site_id?), "
    "wp_security_findings(site_id?), wp_prepare_update_plan(site_id?, "
    "component_filter?), wp_prepare_restore_plan(site_id?, backup_id), och "
    "wp_list_open_operations(site_id?). Utelämnat site_id betyder senast "
    "auktoriserad sajt i denna konversation. Site-id måste komma från "
    "wp_list_sites eller kontexten. Förklara alltid unknown/unsupported "
    "ärligt. Planverktyg skapar endast låsta förhandsgranskningar: de kan inte "
    "godkännas eller köras. När en plan returneras ska du visa plan-id, sajt, "
    "miljö, operation, komponenter, kända och okända versioner, backupstatus, "
    "förvillkor, stagingbevis, påverkan, återställningsnotering, utgångstid och "
    "digest. Säg tydligt att planen inte kan godkännas eller köras. Föreslå "
    "aldrig shell, PHP, WP-CLI eller egna verktyg."
) + (
    f" Verktygsgräns: högst {WP_TOOL_MAX_CALLS} anrop och {int(WP_TOOL_MAX_SECONDS)} sekunder; "
    f"max {WP_TOOL_MAX_RESULT_BYTES} byte per resultat och {WP_TOOL_MAX_TOTAL_BYTES} byte totalt. "
    "Servern stoppar fler anrop. Status, backup och säkerhetsresultat kan vara mock eller unknown: "
    "detta gör inga livekontroller, skannar inga filer och skapar inga riktiga/off-site-backuper. "
    "Säkerhetsskanning, versionsupptäckt och update-/restore-exekvering saknar implementation. "
    "Svara att informationen saknas i stället för att dra slutsatser. JSON Schemas: "
    + json.dumps(WP_TOOL_SCHEMAS, separators=(",", ":"))
)


@dataclass(frozen=True)
class ToolOutput:
    data: dict[str, Any]
    site_id: str | None = None
    operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolLoopResult:
    handled: bool
    content: str
    results: tuple[dict[str, Any], ...] = ()
    site_id: str | None = None
    operation_ids: tuple[str, ...] = ()
    calls: int = 0
    tool_seconds: float = 0.0
    followup_cost_usd: float = 0.0
    model_error: bool = False


class WpAgentTools:
    """Server-side WordPress tool dispatcher with strict argument checks."""

    def __init__(self, service: WpOpsService | None = None) -> None:
        self.service = service or WpOpsService()

    def execute(
        self,
        name: str,
        arguments: Any,
        *,
        actor: str,
        sticky_site_id: str | None = None,
        channel: str = "",
        conversation_id: str = "",
    ) -> ToolOutput:
        output = self._execute(
            name,
            arguments,
            actor=actor,
            sticky_site_id=sticky_site_id,
            channel=channel,
            conversation_id=conversation_id,
        )
        args = arguments if isinstance(arguments, dict) else {}
        site_id = output.site_id or args.get("site_id") or sticky_site_id
        target_id = site_id if isinstance(site_id, str) and _SITE_ID.fullmatch(site_id) else None
        plan = output.data.get("plan") if isinstance(output.data, dict) else None
        try:
            log_action(
                actor=actor,
                action=f"wp_agent_tool_{name}" if name in WP_TOOL_SCHEMAS else "wp_agent_tool_rejected",
                target="wp_sites",
                target_id=target_id,
                success=output.data.get("ok") is True,
                details={
                    "tool": name if name in WP_TOOL_SCHEMAS else "unknown",
                    "result_class": output.data.get("error_class") or output.data.get("status"),
                    "plan_id": plan.get("plan_id") if isinstance(plan, dict) else None,
                    "live_calls": False,
                },
            )
        except Exception:
            return ToolOutput(_error("AuditUnavailable"), output.site_id)
        return output

    def _execute(
        self,
        name: str,
        arguments: Any,
        *,
        actor: str,
        sticky_site_id: str | None = None,
        channel: str = "",
        conversation_id: str = "",
    ) -> ToolOutput:
        if name not in WP_TOOL_SCHEMAS or not isinstance(arguments, dict):
            return ToolOutput(_error("InvalidToolRequest"))
        allowed = set(WP_TOOL_SCHEMAS[name].get("properties", {}))
        if set(arguments) - allowed:
            return ToolOutput(_error("InvalidToolArguments"))

        if name == "wp_list_sites":
            return self._list_sites(actor)

        site_id = arguments.get("site_id", sticky_site_id)
        if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
            return ToolOutput(_error("SiteRequired"))

        # Every non-list tool rechecks RBAC and exact registry membership.
        site_result = self.service.show_site(site_id, actor=actor)
        if not site_result.ok:
            return ToolOutput(_safe_failure(site_result))
        site_record = (site_result.data or {}).get("site")
        if not isinstance(site_record, dict):
            return ToolOutput(_error("SiteUnavailable"))

        if name == "wp_site_status":
            return self._status(site_result, site_id)
        if name == "wp_backup_status":
            return self._backups(site_id, actor)
        if name == "wp_security_findings":
            return self._security(site_record, site_result, site_id)
        if name == "wp_prepare_update_plan":
            components = arguments.get("component_filter", ["core", "plugins", "themes"])
            if (
                not isinstance(components, list)
                or len(components) > 3
                or any(not isinstance(value, str) or value not in _COMPONENTS for value in components)
                or len(set(components)) != len(components)
            ):
                return ToolOutput(_error("InvalidComponentFilter"), site_id)
            return self._plan(
                site_id,
                actor,
                site_record,
                tuple(sorted(components)),
                "update",
                None,
                channel=channel,
                conversation_id=conversation_id,
            )
        if name == "wp_prepare_restore_plan":
            backup_id = arguments.get("backup_id")
            if not isinstance(backup_id, str) or not re.fullmatch(r"bk-[a-f0-9]{12}", backup_id):
                return ToolOutput(_error("InvalidBackupId"), site_id)
            backup_result = self.service.list_backups(site_id, actor=actor)
            if not backup_result.ok:
                return ToolOutput(_safe_failure(backup_result), site_id)
            record = next(
                (row for row in (backup_result.data or {}).get("backups", []) if row.get("id") == backup_id),
                None,
            )
            if not record:
                return ToolOutput(_error("UnknownBackupOrDenied"), site_id)
            return self._plan(
                site_id,
                actor,
                site_record,
                (),
                "restore",
                record,
                channel=channel,
                conversation_id=conversation_id,
            )
        if name == "wp_list_open_operations":
            return self._open_operations(
                site_result,
                site_id,
                actor,
                channel=channel,
                conversation_id=conversation_id,
            )
        return ToolOutput(_error("InvalidToolRequest"), site_id)

    def _list_sites(self, actor: str) -> ToolOutput:
        result = self.service.list_sites(actor=actor)
        if not result.ok:
            return ToolOutput(_safe_failure(result))
        sites = []
        for row in (result.data or {}).get("sites", []):
            if not isinstance(row, dict):
                continue
            sites.append(
                {
                    "site_id": row.get("id"),
                    "name": row.get("display_name"),
                    "environment": row.get("environment"),
                    "status": _site_state(row),
                }
            )
        return ToolOutput({"ok": True, "status": "ok", "sites": sites, "live_calls": False})

    def _status(self, result: WpOpsResult, site_id: str) -> ToolOutput:
        data = result.data or {}
        site = data.get("site") or {}
        checks = []
        for row in data.get("checks", []):
            if isinstance(row, dict):
                checks.append(
                    {
                        "check": _short(row.get("check"), 32),
                        "result": _result(row.get("result")),
                        "checked_at": _short(row.get("checked_at"), 32),
                        "error_class": _error_class(row.get("error_class")),
                    }
                )
        operation_ids = tuple(
            str(row.get("id"))
            for row in data.get("incidents", [])
            if (
                isinstance(row, dict)
                and row.get("state") == "open"
                and isinstance(row.get("id"), str)
                and _OPERATION_ID.fullmatch(row["id"])
            )
        )[:20]
        return ToolOutput(
            {
                "ok": True,
                "status": _site_state(site),
                "site_id": site_id,
                "environment": _short(site.get("environment"), 16),
                "wordpress_version": _short(site.get("wordpress_version"), 32),
                "version_source": "registry",
                "php_version": _short(site.get("php_version"), 32),
                "open_incidents": int(site.get("open_incidents") or 0),
                "checks": checks[:16],
                "provenance": "registered-site mock probes and stored state; no live calls",
                "live_calls": False,
            },
            site_id=site_id,
            operation_ids=operation_ids,
        )

    def _backups(self, site_id: str, actor: str) -> ToolOutput:
        result = self.service.list_backups(site_id, actor=actor)
        if not result.ok:
            return ToolOutput(_safe_failure(result), site_id)
        rows = []
        for row in (result.data or {}).get("backups", []):
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "backup_id": _short(row.get("id"), 32),
                    "status": _short(row.get("completion"), 20) or "unknown",
                    "integrity": _short(row.get("integrity"), 20) or "unknown",
                    "created_at": _short(row.get("created_at"), 32),
                    "verified_at": _short(row.get("verified_at"), 32),
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

    def _security(self, site: dict[str, Any], result: WpOpsResult, site_id: str) -> ToolOutput:
        capabilities = site.get("capabilities") if isinstance(site.get("capabilities"), dict) else {}
        capability = capabilities.get("security_scanner", "unknown")
        checks = [
            row
            for row in (result.data or {}).get("checks", [])
            if isinstance(row, dict) and row.get("check") == "checksum"
        ]
        return ToolOutput(
            {
                "ok": True,
                "status": "unknown",
                "site_id": site_id,
                "coverage": "not_configured",
                "scanner_capability": capability if capability in {"available", "unavailable", "unknown"} else "unknown",
                "checksum_state": _result(checks[-1].get("result")) if checks else "unknown",
                "findings": None,
                "message": "Ingen säkerhetsskanner eller checksumma-adapter är implementerad; resultat betyder inte rent.",
                "live_calls": False,
            },
            site_id=site_id,
        )

    def _plan(
        self,
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
        backups = self.service.list_backups(site_id, actor=actor)
        if not backups.ok:
            return ToolOutput(_safe_failure(backups), site_id)
        latest = backup or next(iter((backups.data or {}).get("backups", [])), None)
        plan_id = "wp-plan-" + uuid.uuid4().hex[:16]
        unsupported_reason = "Update discovery adapter is not configured." if operation == "update" else "Restore adapter is not implemented; mock manifests contain no restorable bytes."
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "site_id": site_id,
            "environment": _short(site.get("environment"), 16),
            "operation": operation,
            "target_components": list(components),
            "current_versions": {
                "wordpress": _short(site.get("wordpress_version"), 32),
                "php": _short(site.get("php_version"), 32),
            },
            "current_versions_source": "registry",
            "target_versions": {},
            "backup_ref": _short((latest or {}).get("id"), 32),
            "backup_status": _short((latest or {}).get("completion"), 20) or "unknown",
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
        digest = _digest(payload)
        record = {
            **payload,
            "digest": digest,
            "_scope": {"channel": channel[:32], "conversation_id": conversation_id[:120]},
        }
        record["_scope_digest"] = _digest(record["_scope"])
        self._store_plan(record)
        return ToolOutput(
            {"ok": True, "status": "unsupported", "plan": {**payload, "digest": digest}, "live_calls": False},
            site_id=site_id,
        )

    def _open_operations(
        self,
        result: WpOpsResult,
        site_id: str,
        actor: str,
        *,
        channel: str,
        conversation_id: str,
    ) -> ToolOutput:
        data = result.data or {}
        incidents = []
        for row in data.get("incidents", []):
            if (
                not isinstance(row, dict)
                or row.get("state") != "open"
                or not _safe_operation_id(row.get("id"))
            ):
                continue
            incidents.append(
                {
                    "operation_id": row["id"],
                    "type": "incident",
                    "state": "open",
                    "check": _short(row.get("check"), 32),
                    "opened_at": _short(row.get("opened_at"), 32),
                }
            )
        plans = [
            {"operation_id": row["plan_id"], "type": row["operation"], "state": row["status"]}
            for row in self._load_plans()
            if row.get("site_id") == site_id
            and row.get("created_by") == actor
            and (row.get("_scope") or {}).get("channel") == channel
            and (row.get("_scope") or {}).get("conversation_id") == conversation_id[:120]
            and _safe_operation_id(row.get("plan_id"))
            and row.get("status") in {"pending_approval", "executing"}
            and not _expired(row.get("expires_at"))
        ]
        operations = (incidents + plans)[:20]
        return ToolOutput(
            {"ok": True, "status": "ok", "site_id": site_id, "operations": operations, "live_calls": False},
            site_id=site_id,
            operation_ids=tuple(str(row["operation_id"]) for row in operations),
        )

    def _store_plan(self, record: dict[str, Any]) -> None:
        path = self.service.data_dir / "wp_ops_plans.json"
        with _PLAN_LOCK:
            rows = self._read_plan_rows(path)
            rows = [row for row in rows if not _expired(row.get("expires_at"))]
            rows.append(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps({"plans": rows[-200:]}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def _load_plans(self) -> list[dict[str, Any]]:
        return self._read_plan_rows(self.service.data_dir / "wp_ops_plans.json")

    @staticmethod
    def _read_plan_rows(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("plans", []) if isinstance(raw, dict) else []
        return [row for row in rows if isinstance(row, dict) and _valid_plan(row)]


def run_bounded_wp_tool_loop(
    initial_content: str,
    messages: list[dict[str, str]],
    *,
    complete: Callable[[list[dict[str, str]]], tuple[str, float]],
    actor: str,
    channel: str,
    conversation_id: str,
    context: dict[str, Any] | None = None,
    tools: WpAgentTools | None = None,
    wp_intent: bool,
    on_tool_status: Callable[[str, str], None] | None = None,
    before_tool: Callable[[], list[str]] | None = None,
) -> ToolLoopResult:
    """Execute at most three exact named tool calls, with bounded results."""
    content = initial_content or ""
    if "<tool_call>" not in content.lower():
        return ToolLoopResult(False, content)
    loop_tools = tools or WpAgentTools()
    memory = _valid_context(context, actor=actor, channel=channel, conversation_id=conversation_id)
    site_id = memory.get("site_id")
    operation_ids = list(memory.get("operation_ids", []))
    results: list[dict[str, Any]] = []
    total_bytes = 0
    tool_seconds = 0.0
    calls = 0
    followup_cost = 0.0
    next_content = content
    steer_count = 0

    def report_tool_status(name: str, status: str) -> None:
        if on_tool_status:
            try:
                on_tool_status(name, status)
            except Exception:
                pass

    while "<tool_call>" in next_content.lower():
        if before_tool and steer_count < 3:
            try:
                steers = before_tool() or []
            except Exception:
                raise
            if steers:
                steer_count += len(steers)
                messages.append({"role": "assistant", "content": next_content[:4000]})
                messages.extend(
                    {"role": "user", "content": str(text)[:8000]}
                    for text in steers[:3]
                )
                try:
                    next_content, cost = complete(messages)
                    followup_cost += float(cost or 0.0)
                except Exception:
                    return ToolLoopResult(True, _format_results_sv(results, "Modellen kunde inte väga in följdfrågan."), tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost, True)
                continue
        parsed = _parse_tool_call(next_content)
        if parsed is None:
            return ToolLoopResult(True, "Det verktygsanropet kunde inte valideras. Inget WordPress-verktyg kördes.", tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)
        name, arguments = parsed
        if name not in WP_TOOL_SCHEMAS:
            return ToolLoopResult(False, next_content)
        if not wp_intent:
            return ToolLoopResult(True, "Jag kör bara WordPress-verktyg när du frågar om en registrerad sajts drift.", tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)
        if calls >= WP_TOOL_MAX_CALLS:
            messages.append({"role": "system", "content": "Verktygsgränsen är nådd. Svara med resultaten hittills och ange tydligt vad som återstår okänt. Inga fler verktygsanrop."})
            try:
                final, cost = complete(messages)
                followup_cost += cost
                if "<tool_call>" in (final or "").lower():
                    return ToolLoopResult(True, _format_results_sv(results, "Tool call limit reached; no further tool ran."), tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)
                return ToolLoopResult(True, final, tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)
            except Exception:
                return ToolLoopResult(True, _format_results_sv(results, "Verktygsgränsen är nådd."), tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost, True)

        report_tool_status(name, "started")
        try:
            resolve_actor(actor)
        except Exception:
            safe = _error("AccessDenied")
            output = ToolOutput(safe)
        else:
            started = time.monotonic()
            try:
                output = loop_tools.execute(
                    name,
                    arguments,
                    actor=actor,
                    sticky_site_id=site_id,
                    channel=channel,
                    conversation_id=conversation_id,
                )
            except Exception:
                output = ToolOutput(_error("ToolFailed"))
            elapsed = time.monotonic() - started
            tool_seconds += elapsed
            calls += 1
            if tool_seconds > WP_TOOL_MAX_SECONDS:
                output = ToolOutput(_error("ToolTimeBudgetExceeded"), site_id=site_id)
                results.append(output.data)
                report_tool_status(name, "failed")
                return ToolLoopResult(True, _format_results_sv(results, "Tidsgränsen för WordPress-verktyg nåddes."), tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)

        encoded = json.dumps(output.data, ensure_ascii=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size > WP_TOOL_MAX_RESULT_BYTES:
            output = ToolOutput(_error("ToolResultTooLarge"), site_id=output.site_id)
            encoded = json.dumps(output.data, separators=(",", ":"))
            size = len(encoded.encode("utf-8"))
        if total_bytes + size > WP_TOOL_MAX_TOTAL_BYTES:
            output = ToolOutput(_error("ToolResultBudgetExceeded"), site_id=site_id)
            encoded = json.dumps(output.data, separators=(",", ":"))
            size = len(encoded.encode("utf-8"))
        total_bytes += size
        results.append(output.data)
        if output.site_id:
            site_id = output.site_id
        if output.operation_ids:
            operation_ids = list(output.operation_ids[:20])
        report_tool_status(name, "result" if output.data.get("ok") is True else "failed")

        messages.append({"role": "assistant", "content": next_content[:4000]})
        messages.append({"role": "system", "content": f"Server tool result for {name}: {encoded}"})
        try:
            next_content, cost = complete(messages)
            followup_cost += float(cost or 0.0)
        except Exception:
            return ToolLoopResult(True, _format_results_sv(results, "Modellen kunde inte sammanfatta resultatet."), tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost, True)

    return ToolLoopResult(True, next_content, tuple(results), site_id, tuple(operation_ids), calls, tool_seconds, followup_cost)


def context_for_storage(
    *, actor: str, channel: str, conversation_id: str, site_id: str | None, operation_ids: tuple[str, ...]
) -> dict[str, Any] | None:
    if not site_id or not _SITE_ID.fullmatch(site_id):
        return None
    return {
        "actor_id": actor,
        "channel": channel,
        "conversation_id": conversation_id[:120],
        "site_id": site_id,
        "operation_ids": [value for value in operation_ids[:20] if _safe_operation_id(value)],
        "updated_at": datetime.now(UTC).isoformat(),
    }


def context_matches(
    context: Any, *, actor: str, channel: str, conversation_id: str
) -> bool:
    """Check ownership and TTL before a persisted site context is reused."""
    if not isinstance(context, dict):
        return False
    return bool(
        context.get("actor_id") == actor
        and context.get("channel") == channel
        and context.get("conversation_id") == conversation_id[:120]
        and not _expired(context.get("updated_at"), age_seconds=WP_TOOL_MAX_AGE_SECONDS)
        and isinstance(context.get("site_id"), str)
        and _SITE_ID.fullmatch(context["site_id"])
    )


def _valid_context(
    context: dict[str, Any] | None, *, actor: str, channel: str, conversation_id: str
) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    if (
        context.get("actor_id") != actor
        or context.get("channel") != channel
        or context.get("conversation_id") != conversation_id[:120]
        or _expired(context.get("updated_at"), age_seconds=WP_TOOL_MAX_AGE_SECONDS)
    ):
        return {}
    site_id = context.get("site_id")
    if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
        return {}
    raw_ids = context.get("operation_ids")
    ids = [value for value in raw_ids[:20] if _safe_operation_id(value)] if isinstance(raw_ids, list) else []
    return {"site_id": site_id, "operation_ids": ids}


def _parse_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    match = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", content or "", re.I | re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        return None
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        return None
    return payload["name"], arguments


def _error(error_class: str) -> dict[str, Any]:
    return {"ok": False, "status": "unknown", "error_class": error_class, "live_calls": False}


def _safe_failure(result: WpOpsResult) -> dict[str, Any]:
    data = result.data or {}
    error = _error(_error_class(data.get("error_class")) or "OperationUnavailable")
    if error["error_class"] == "UnknownSite":
        error["error_class"] = "UnknownSiteOrDenied"
    if error["error_class"] == "AccessDenied":
        error["status"] = "denied"
    return error


def _site_state(site: dict[str, Any]) -> str:
    if site.get("down") is True:
        return "attention"
    if site.get("healthy") is True:
        return "ok"
    return "unknown"


def _result(value: Any) -> str:
    return value if value in {"ok", "fail", "unknown", "verified"} else "unknown"


def _short(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:max_length]


def _error_class(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", value):
        return None
    return value


def _safe_operation_id(value: Any) -> bool:
    return isinstance(value, str) and _OPERATION_ID.fullmatch(value) is not None


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_plan(row: dict[str, Any]) -> bool:
    digest = row.get("digest")
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"digest", "_scope", "_scope_digest"}
    }
    scope = row.get("_scope")
    scope_digest = row.get("_scope_digest")
    scope_ok = (
        isinstance(scope, dict)
        and isinstance(scope.get("channel"), str)
        and len(scope.get("channel")) <= 32
        and isinstance(scope.get("conversation_id"), str)
        and len(scope.get("conversation_id")) <= 120
    )
    return (
        scope_ok
        and scope_digest == _digest(scope)
        and isinstance(digest, str)
        and digest == _digest(payload)
    )


def _expired(value: Any, *, age_seconds: int | None = None) -> bool:
    if not isinstance(value, str):
        return True
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if age_seconds is not None:
        return (now - expires).total_seconds() > age_seconds
    return expires <= now


def _format_results_sv(results: list[dict[str, Any]], reason: str) -> str:
    lines = [reason]
    for result in results[:WP_TOOL_MAX_CALLS]:
        site_id = result.get("site_id")
        status = result.get("status", "unknown")
        name = result.get("plan", {}).get("operation") if isinstance(result.get("plan"), dict) else None
        if name:
            lines.append(f"Förhandsgranskning för {site_id or 'sajten'} ({name}): {status}; ingen ändring utfördes.")
        else:
            lines.append(f"WordPress-verktygsresultat för {site_id or 'sajten'}: {status}.")
    return "\n".join(lines)
