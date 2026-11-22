"""Typed SSH reads and writes for one existing theme or plugin file.

The remote command is one of four fixed strings. Callers cannot pass a
command. File bytes travel on stdin for ``tee`` only. stderr is discarded.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ecom_ops.wp_ops.files import LocalTree, PathRejected, TransportError
from ecom_ops.wp_ops.models import Site

_HOST = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_REMOTE = re.compile(r"^/[A-Za-z0-9_./-]{1,220}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
_LIST = re.compile(r"^ls -1 -- '(/[A-Za-z0-9_./-]{1,220})'$")
_READ = re.compile(r"^cat -- '(/[A-Za-z0-9_./-]{1,220})'$")
_COPY = re.compile(
    r"^cp -p -- '(/[A-Za-z0-9_./-]{1,220})' '(/[A-Za-z0-9_./-]{1,220})'$"
)
_WRITE = re.compile(r"^tee -- '(/[A-Za-z0-9_./-]{1,220})'$")
_MAX_BYTES = 65536

Execute = Callable[[list[str], bytes | None, int], tuple[int, bytes]]


class FileSshRunner(Protocol):
    def run(
        self,
        host: str,
        command: str,
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]: ...


def host_allowed(host: str) -> bool:
    text = str(host or "").strip().lower().rstrip(".")
    if not text or text.endswith(".example.test"):
        return False
    return _HOST.fullmatch(text) is not None


def checked_path(path: str) -> str:
    if not isinstance(path, str) or _REMOTE.fullmatch(path) is None:
        raise PathRejected("remote")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise PathRejected("remote")
    return path


def list_command(directory: str) -> str:
    return f"ls -1 -- '{checked_path(directory)}'"


def read_command(path: str) -> str:
    return f"cat -- '{checked_path(path)}'"


def backup_command(path: str) -> str:
    source = checked_path(path)
    return f"cp -p -- '{source}' '{checked_path(source + '.azom-bak')}'"


def write_command(path: str) -> str:
    return f"tee -- '{checked_path(path)}'"


def command_allowed(command: str, stdin: bytes | None) -> bool:
    if not isinstance(command, str) or ".." in command.split("/"):
        return False
    if _LIST.fullmatch(command) or _READ.fullmatch(command):
        return stdin is None
    if _WRITE.fullmatch(command):
        return stdin is not None and len(stdin) <= _MAX_BYTES and b"\x00" not in stdin
    copied = _COPY.fullmatch(command)
    if copied is None or stdin is not None:
        return False
    return copied.group(2) == copied.group(1) + ".azom-bak"


def _subprocess_execute(argv: list[str], stdin: bytes | None, timeout: int) -> tuple[int, bytes]:
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, b""
    except OSError:
        return 1, b""
    out = proc.stdout or b""
    if len(out) > _MAX_BYTES:
        return 1, b""
    return proc.returncode, out


class OpenSshRunner:
    """Run one already-checked remote command. This does not open a shell locally."""

    def __init__(self, execute: Execute | None = None) -> None:
        self._execute = execute or _subprocess_execute

    def run(
        self,
        host: str,
        command: str,
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]:
        argv = self.argv(host, command, stdin=stdin)
        if argv is None:
            return 126, b""
        return self._execute(argv, stdin, timeout)

    def argv(self, host: str, command: str, *, stdin: bytes | None) -> list[str] | None:
        if not host_allowed(host) or not command_allowed(command, stdin):
            return None
        user = os.environ.get("SSH_USER", "root")
        port = os.environ.get("SSH_PORT", "22")
        key = os.environ.get("SSH_IDENTITY_FILE") or ""
        if _USER.fullmatch(user) is None or not port.isdigit() or not 1 <= int(port) <= 65535:
            return None
        if key and ("\x00" in key or "\n" in key):
            return None
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            port,
        ]
        if key:
            argv.extend(["-i", key])
        argv.extend([f"{user}@{host}", command])
        return argv


class SshFileTransport:
    def __init__(self, site: Site, runner: FileSshRunner) -> None:
        self.site = site
        self.runner = runner

    def list_dir(self, absolute_dir: str) -> list[str]:
        out = self._exec(list_command(absolute_dir), stdin=None)
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("SshFailed") from exc
        names = []
        for line in text.splitlines():
            name = line.strip()
            if name and name not in {".", ".."} and "/" not in name:
                names.append(name)
        return names

    def read_bytes(self, absolute_file: str) -> bytes:
        return self._exec(read_command(absolute_file), stdin=None)

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        if not isinstance(data, bytes) or b"\x00" in data:
            raise TransportError("NotText")
        if len(data) > _MAX_BYTES:
            raise TransportError("FileTooLarge")
        self._exec(backup_command(absolute_file), stdin=None)
        try:
            self._exec(write_command(absolute_file), stdin=data)
        except TransportError:
            self._restore_backup(absolute_file)
            raise

    def _restore_backup(self, absolute_file: str) -> None:
        try:
            previous = self._exec(read_command(absolute_file + ".azom-bak"), stdin=None)
            self._exec(write_command(absolute_file), stdin=previous)
        except (PathRejected, TransportError):
            return

    def _exec(self, command: str, *, stdin: bytes | None) -> bytes:
        if not host_allowed(self.site.ssh_host):
            raise TransportError("SshHostRefused")
        if not command_allowed(command, stdin):
            raise TransportError("SshFailed")
        code, out = self.runner.run(self.site.ssh_host, command, stdin=stdin, timeout=30)
        if code != 0:
            raise TransportError("SshFailed")
        return out


def build_file_transport(
    site: Site,
    data_dir: Path,
    runner: FileSshRunner | None = None,
) -> LocalTree | SshFileTransport:
    from ecom_ops.runtime_env import env_flag

    if env_flag("AZOM_WP_FILES_SSH") and not env_flag("AZOM_USE_MOCK"):
        return SshFileTransport(site, runner or OpenSshRunner())
    root = site.files_root or "/var/www/unconfigured"
    return LocalTree(root, Path(data_dir) / "wp_files_mock" / site.id)
