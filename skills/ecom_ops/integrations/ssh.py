"""SSH / VPS operations with allowlist and critical escalation hooks."""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ecom_ops.runtime_env import env_flag
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
    """Fail-closed SSH for exact allowlisted health commands only."""

    def run(self, host: str, command: str, *, timeout: int = 30) -> SSHResult:
        cmd = " ".join(str(command or "").strip().split())
        if not is_ssh_allowlisted(cmd):
            return _ssh_refused(host, cmd, "SSH command is not allowlisted.")
        if not env_flag("AZOM_SSH_READ_ENABLED"):
            return _ssh_refused(host, cmd, "Live SSH reads are disabled.")

        user = os.environ.get("SSH_USER", "").strip()
        port = os.environ.get("SSH_PORT", "22")
        port_number = int(port) if re.fullmatch(r"[0-9]{1,5}", port) else 0
        key = _safe_ssh_file(os.environ.get("SSH_IDENTITY_FILE", ""), private=True)
        known_hosts = _safe_ssh_file(os.environ.get("SSH_KNOWN_HOSTS", ""), private=False)
        if (
            not _safe_ssh_host(host)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", user)
            or user.casefold() == "root"
            or not user.casefold().endswith("-ssh-reader")
            or not 1 <= port_number <= 65535
            or key is None
            or known_hosts is None
            or not _has_exact_host_pin(known_hosts, host, port_number)
        ):
            return _ssh_refused(host, cmd, "SSH requires a pinned host, dedicated non-root user, key, and known_hosts file.")
        if timeout < 1 or timeout > 30:
            return _ssh_refused(host, cmd, "SSH timeout is outside the allowed range.")

        target = f"{user}@{host}"
        ssh_cmd = [
            "ssh",
            "-F",
            os.devnull,
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"GlobalKnownHostsFile={os.devnull}",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "RequestTTY=no",
            "-o",
            "ConnectTimeout=10",
            "-p",
            port,
            "-i",
            str(key),
            target,
            cmd,
        ]
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


def _safe_ssh_host(value: str) -> bool:
    host = str(value or "").strip().rstrip(".")
    if not host or len(host) > 253 or host.startswith("-") or any(ch.isspace() for ch in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    return re.fullmatch(rf"{label}(?:\.{label})*", host) is not None


def _safe_ssh_file(value: str, *, private: bool) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        return None
    try:
        attrs = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(attrs.st_mode) or stat.S_ISLNK(attrs.st_mode):
        return None
    if os.name != "nt":
        mode = stat.S_IMODE(attrs.st_mode)
        if private and mode & 0o077:
            return None
        if not private and mode & 0o022:
            return None
    return path


def _has_exact_host_pin(path: Path, host: str, port: int) -> bool:
    expected = host if port == 22 else f"[{host}]:{port}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    pins = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        fields = raw.split()
        if len(fields) < 3 or fields[0] != expected:
            return False
        pins.append(fields[1:3])
    return bool(pins)


def _ssh_refused(host: str, command: str, reason: str) -> SSHResult:
    return SSHResult(
        command=command,
        exit_code=126,
        stdout="",
        stderr=reason,
        host=str(host or "")[:253],
    )


class SSHClient:
    def __init__(
        self,
        *,
        host: str,
        runner: SSHRunner | None = None,
    ) -> None:
        self.host = host
        if runner is not None:
            self.runner = runner
        elif os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}:
            self.runner = LocalMockSSHRunner()
        else:
            self.runner = SubprocessSSHRunner()

    def run_safe(self, command: str, *, timeout: int = 30) -> SSHResult:
        cmd = " ".join(command.strip().split())
        if not cmd:
            raise SecurityError("SSH command must not be empty")
        if timeout < 1 or timeout > 30:
            raise SecurityError("SSH timeout is outside the allowed range")
        # Reject shell metacharacters that enable chaining
        if any(ch in cmd for ch in [";", "&&", "||", "|", "`", "$(", "\n"]):
            raise SecurityError("SSH command chaining/metacharacters are not allowed")

        if is_critical_ssh_command(cmd) or not is_ssh_allowlisted(cmd):
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
