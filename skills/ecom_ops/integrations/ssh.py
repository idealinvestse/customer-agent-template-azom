"""SSH / VPS operations with allowlist and critical escalation hooks."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ecom_ops.security import (
    SSH_ALLOWLIST,
    SecurityError,
    is_critical_ssh_command,
    is_ssh_allowlisted,
)


@dataclass(frozen=True)
class SSHResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    host: str
    escalated: bool = False
    ticket_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.escalated


class SSHRunner(Protocol):
    def run(self, host: str, command: str, *, timeout: int = 30) -> SSHResult: ...


class LocalMockSSHRunner:
    """Deterministic runner for tests / dry-run pilot."""

    def __init__(self) -> None:
        self.history: list[tuple[str, str]] = []

    def run(self, host: str, command: str, *, timeout: int = 30) -> SSHResult:
        self.history.append((host, command))
        if not is_ssh_allowlisted(command):
            return SSHResult(
                command=command,
                exit_code=126,
                stdout="",
                stderr="blocked: not allowlisted",
                host=host,
            )
        # Fake healthy outputs
        outputs = {
            "uptime": " 12:00:01 up 10 days,  3:21,  1 user,  load average: 0.10, 0.05, 0.01",
            "df -h": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        40G   12G   26G  32% /",
            "free -m": "Mem:  7942  2100  4200",
            "whoami": "azom-agent",
            "hostname": host,
            "uname -a": "Linux azom 6.1.0 x86_64",
            "docker ps": "CONTAINER ID   IMAGE     STATUS",
        }
        stdout = outputs.get(command, f"ok: {command}")
        return SSHResult(
            command=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            host=host,
        )


class SubprocessSSHRunner:
    """Real SSH via system ssh client (keys from agent / env)."""

    def run(self, host: str, command: str, *, timeout: int = 30) -> SSHResult:
        user = os.environ.get("SSH_USER", "root")
        port = os.environ.get("SSH_PORT", "22")
        key = os.environ.get("SSH_IDENTITY_FILE")
        target = f"{user}@{host}"
        ssh_cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            port,
        ]
        if key:
            ssh_cmd.extend(["-i", key])
        ssh_cmd.extend([target, command])
        try:
            proc = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return SSHResult(
                command=command,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                host=host,
            )
        except subprocess.TimeoutExpired:
            return SSHResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr="ssh timeout",
                host=host,
            )


class SSHClient:
    def __init__(
        self,
        *,
        host: str,
        runner: SSHRunner | None = None,
        allow_non_allowlisted: bool = False,
    ) -> None:
        self.host = host
        if runner is not None:
            self.runner = runner
        elif os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}:
            self.runner = LocalMockSSHRunner()
        else:
            self.runner = SubprocessSSHRunner()
        self.allow_non_allowlisted = allow_non_allowlisted

    def run_safe(self, command: str, *, timeout: int = 30) -> SSHResult:
        cmd = " ".join(command.strip().split())
        if not cmd:
            raise SecurityError("SSH command must not be empty")
        # Reject shell metacharacters that enable chaining
        if any(ch in cmd for ch in [";", "&&", "||", "|", "`", "$(", "\n"]):
            raise SecurityError("SSH command chaining/metacharacters are not allowed")

        if is_critical_ssh_command(cmd) or not is_ssh_allowlisted(cmd):
            if not self.allow_non_allowlisted:
                return SSHResult(
                    command=cmd,
                    exit_code=126,
                    stdout="",
                    stderr="command requires escalation to Oscar",
                    host=self.host,
                    escalated=True,
                )
        return self.runner.run(self.host, cmd, timeout=timeout)

    def health_checks(self) -> list[SSHResult]:
        results = []
        for cmd in sorted(SSH_ALLOWLIST):
            # Keep pilot checks tight
            if cmd in {"uptime", "df -h", "free -m", "hostname"}:
                results.append(self.run_safe(cmd))
        return results


def quote_arg(value: str) -> str:
    return shlex.quote(value)
