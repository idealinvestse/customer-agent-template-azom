"""SSH/VPS maintenance actions with Oscar escalation for unsafe ops."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ecom_ops.escalation import EscalationReason, EscalationService, Severity, default_escalation
from ecom_ops.integrations.ssh import SSHClient, SSHResult
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import (
    SecurityError,
    is_critical_ssh_command,
    is_ssh_allowlisted,
    validate_site,
)
from ecom_ops.telemetry import Telemetry, default_telemetry


@dataclass(frozen=True)
class SSHOpsResult:
    ok: bool
    result: SSHResult | None
    message: str
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
            "result": None
            if self.result is None
            else {
                "command": self.result.command,
                "exit_code": self.result.exit_code,
                "stdout": self.result.stdout,
                "stderr": self.result.stderr,
                "host": self.result.host,
            },
        }


class SSHOpsService:
    def __init__(
        self,
        client: SSHClient | None = None,
        *,
        host: str | None = None,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.host = host or os.environ.get("SSH_HOST", "azom-vps")
        self.client = client or SSHClient(host=self.host)
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def run(
        self,
        command: str,
        *,
        site: str = "azom",
        actor: Actor | str | None = None,
    ) -> SSHOpsResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        cmd = " ".join((command or "").strip().split())

        try:
            if not cmd:
                raise SecurityError("Empty SSH command")

            # Code-edit / destructive always escalate to Oscar
            if is_critical_ssh_command(cmd) and not is_ssh_allowlisted(cmd):
                require_permission(actor_obj, Permission.SSH_READ)  # even viewers can request
                reason = (
                    EscalationReason.CODE_EDIT
                    if any(x in cmd for x in ("sed -i", "vim", "nano", "tee", "git "))
                    else EscalationReason.SSH_UNSAFE
                )
                ticket = self.escalation.escalate(
                    reason=reason,
                    summary=f"SSH command requires Oscar approval: {cmd[:120]}",
                    details={
                        "command": cmd,
                        "host": self.host,
                        "site": site,
                        "actor": actor_obj.name,
                    },
                    severity=Severity.CRITICAL,
                )
                self.telemetry.record(
                    action="ssh_escalated",
                    site=site,
                    unit_type="ssh_cmds",
                    meta={"command": cmd, "ticket_id": ticket.id},
                )
                return SSHOpsResult(
                    ok=False,
                    result=SSHResult(
                        command=cmd,
                        exit_code=126,
                        stdout="",
                        stderr="escalated to Oscar",
                        host=self.host,
                        escalated=True,
                        ticket_id=ticket.id,
                    ),
                    message=f"Escalated to {ticket.assignee}",
                    escalated=True,
                    ticket_id=ticket.id,
                )

            require_permission(actor_obj, Permission.SSH_READ)
            result = self.client.run_safe(cmd)
            if result.escalated:
                ticket = self.escalation.escalate(
                    reason=EscalationReason.SSH_UNSAFE,
                    summary=f"SSH blocked/escalated: {cmd[:120]}",
                    details={"command": cmd, "host": self.host, "site": site},
                )
                return SSHOpsResult(
                    ok=False,
                    result=result,
                    message="Command blocked; escalated to Oscar",
                    escalated=True,
                    ticket_id=ticket.id,
                )

            self.telemetry.record(
                action="ssh_run",
                site=site,
                unit_type="ssh_cmds",
                meta={
                    "command": cmd,
                    "exit_code": result.exit_code,
                    "actor": actor_obj.name,
                },
            )
            return SSHOpsResult(
                ok=result.ok,
                result=result,
                message="SSH command executed" if result.ok else "SSH command failed",
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"SSH denied for {actor_obj.name}",
                details={"error": str(exc), "command": cmd, "site": site},
            )
            return SSHOpsResult(
                ok=False,
                result=None,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )
        except SecurityError as exc:
            return SSHOpsResult(ok=False, result=None, message=str(exc))

    def health(self, *, site: str = "azom", actor: str | None = None) -> list[SSHOpsResult]:
        return [
            self.run(cmd, site=site, actor=actor)
            for cmd in ("uptime", "df -h", "free -m", "hostname")
        ]


def run_ssh_command(
    command: str,
    *,
    site: str = "azom",
    actor: str | None = None,
    host: str | None = None,
) -> SSHOpsResult:
    return SSHOpsService(host=host).run(command, site=site, actor=actor)
