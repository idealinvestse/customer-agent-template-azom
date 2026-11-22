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
from ecom_ops.wp_ops.ssh_settings import (
    effective_identity,
    host_key_ok,
    identity_ok,
    known_hosts_path,
    user_ok,
)

_HOST = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_REMOTE = re.compile(r"^/[A-Za-z0-9_./-]{1,220}$")
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

    def __init__(
        self,
        execute: Execute | None = None,
        *,
        user: str | None = None,
        port: str | None = None,
        identity: str | None = None,
        host_public_key: str | None = None,
        known_hosts: Path | None = None,
    ) -> None:
        self._execute = execute or _subprocess_execute
        self._user = user or None
        self._port = port or None
        self._identity = identity or None
        self._host_public_key = host_public_key or None
        self._known_hosts = known_hosts

    def run(
        self,
        host: str,
        command: str,
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
    ) -> tuple[int, bytes]:
        key = self._identity or os.environ.get("SSH_IDENTITY_FILE") or ""
        if key and not identity_ok(key, limit=1024):
            raise TransportError("SshIdentityRejected")
        argv = self.argv(host, command, stdin=stdin)
        if argv is None:
            return 126, b""
        if not host_key_ok(self._host_public_key or ""):
            raise TransportError("SshHostKeyMissing")
        self._write_known_hosts(host)
        return self._execute(argv, stdin, timeout)

    def argv(self, host: str, command: str, *, stdin: bytes | None) -> list[str] | None:
        if not host_allowed(host) or not command_allowed(command, stdin):
            return None
        user = self._user or os.environ.get("SSH_USER") or ""
        port = self._port or os.environ.get("SSH_PORT") or "22"
        key = self._identity or os.environ.get("SSH_IDENTITY_FILE") or ""
        if (
            not user_ok(user)
            or user.casefold() == "root"
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
        ):
            return None
        if key and not identity_ok(key, limit=1024):
            return None
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-p",
            port,
        ]
        if self._known_hosts is not None:
            argv.extend(["-o", f"UserKnownHostsFile={self._known_hosts}"])
        if key:
            argv.extend(["-i", key])
        argv.extend([f"{user}@{host}", command])
        return argv

    def _write_known_hosts(self, host: str) -> None:
        path = self._known_hosts
        pinned = (self._host_public_key or "").strip()
        if path is None or not host_key_ok(pinned) or not host_allowed(host):
            raise TransportError("SshHostKeyMissing")
        data = f"{host} {pinned}\n".encode()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            finally:
                os.close(fd)
            tmp.replace(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise TransportError("SshFailed") from exc


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
        if not host_key_ok(self.site.host_public_key):
            raise TransportError("SshHostKeyMissing")
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
        chosen = runner or OpenSshRunner(
            user=site.ssh_user or None,
            port=str(site.ssh_port) if site.ssh_port else None,
            identity=effective_identity(site, data_dir),
            host_public_key=site.host_public_key,
            known_hosts=known_hosts_path(data_dir, site.id),
        )
        return SshFileTransport(site, chosen)
    root = site.files_root or "/var/www/unconfigured"
    return LocalTree(root, Path(data_dir) / "wp_files_mock" / site.id)
