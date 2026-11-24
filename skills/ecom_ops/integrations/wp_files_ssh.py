"""Read-only, site-scoped SFTP for existing WordPress theme/plugin files.

The live transport never requests an SSH shell or sends a remote command. It
authenticates with one per-site private key, checks the pinned host key before
authentication, refuses symlinks, and permits only bounded file reads.
"""

from __future__ import annotations

import os
import posixpath
import re
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import paramiko
from paramiko.hostkeys import HostKeyEntry
from paramiko.sftp_client import SFTPClient
from paramiko.transport import Transport

from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.files import LocalTree, PathRejected, TransportError
from ecom_ops.wp_ops.models import Site, files_root_narrow
from ecom_ops.wp_ops.ssh_settings import host_key_ok, stored_key_path, user_ok

_HOST = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(^\.env(?:$|\.)|^wp-config[^/]*\.php$|"
    r"(?:^|[-_.])(?:secrets?|credentials?|tokens?|passwords?|auth|private[-_.]?key|api[-_.]?key)(?:[-_.]|$)|"
    r"\.(?:bak|backup|sql|sqlite3?|dump|swp|orig)$)"
)
_EXTENSIONS = frozenset({".php", ".css", ".js", ".json", ".txt", ".html"})
_MAX_BYTES = 65536
_MAX_DIRECTORY_ENTRIES = 256
_CONNECT_TIMEOUT = 10
_CHANNEL_TIMEOUT = 10


def host_allowed(host: str) -> bool:
    text = str(host or "").strip().lower().rstrip(".")
    if not text or text.endswith(".example.test"):
        return False
    return _HOST.fullmatch(text) is not None


class SshFileTransport:
    """Use a dedicated per-site key for read-only, typed SFTP operations."""

    def __init__(self, site: Site, data_dir: Path) -> None:
        self.site = site
        self.data_dir = Path(data_dir)

    def list_dir(self, absolute_dir: str) -> list[str]:
        path = self._safe_path(absolute_dir, listing=True)
        with self._session() as sftp:
            self._check_remote_path(sftp, path, directory=True)
            # internal-sftp does not finish listdir_iter, so the channel times out.
            entries = list(sftp.listdir_attr(path))
            if len(entries) > _MAX_DIRECTORY_ENTRIES:
                raise TransportError("SshDirectoryTooLarge")
            names: list[str] = []
            for entry in entries:
                name = str(getattr(entry, "filename", "") or "")
                if (
                    not _ENTRY.fullmatch(name)
                    or name in {".", ".."}
                    or _SENSITIVE_NAME.search(name)
                ):
                    continue
                mode = getattr(entry, "st_mode", None)
                if mode is None:
                    try:
                        mode = sftp.lstat(posixpath.join(path, name)).st_mode
                    except OSError:
                        continue
                if mode is None or stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                    names.append(name)
            return sorted(names)

    def read_bytes(self, absolute_file: str) -> bytes:
        path = self._safe_path(absolute_file, listing=False)
        with self._session() as sftp:
            attrs = self._check_remote_path(sftp, path, directory=False)
            size = getattr(attrs, "st_size", None)
            if size is None or size > _MAX_BYTES:
                raise TransportError("FileTooLarge")
            try:
                remote = sftp.open(path, "rb")
                remote.settimeout(_CHANNEL_TIMEOUT)
                try:
                    opened = remote.stat()
                    opened_mode = getattr(opened, "st_mode", None)
                    if opened_mode is None or not stat.S_ISREG(opened_mode):
                        raise PathRejected("remote file type")
                    if getattr(opened, "st_size", _MAX_BYTES + 1) > _MAX_BYTES:
                        raise TransportError("FileTooLarge")
                    current = sftp.lstat(path)
                    current_mode = getattr(current, "st_mode", None)
                    if current_mode is None or stat.S_ISLNK(current_mode):
                        raise PathRejected("remote symlink")
                    opened_ino = getattr(opened, "st_ino", None)
                    current_ino = getattr(current, "st_ino", None)
                    if opened_ino and current_ino and opened_ino != current_ino:
                        raise PathRejected("remote file changed")
                    data = remote.read(_MAX_BYTES + 1)
                finally:
                    remote.close()
            except (OSError, EOFError, TimeoutError, paramiko.SSHException) as exc:
                raise TransportError("SshFailed") from exc
            if len(data) > _MAX_BYTES:
                raise TransportError("FileTooLarge")
            return data

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        del absolute_file, data
        raise TransportError("SshWriteDisabled")

    @contextmanager
    def _session(self) -> Iterator[SFTPClient]:
        host = str(self.site.ssh_host or "").strip().lower().rstrip(".")
        user = str(self.site.ssh_user or "").strip()
        port = int(self.site.ssh_port or 22)
        if not host_allowed(host):
            raise TransportError("SshHostRefused")
        if not host_key_ok(self.site.host_public_key):
            raise TransportError("SshHostKeyMissing")
        if not user_ok(user) or user.casefold() == "root":
            raise TransportError("SshUserRefused")
        if not user.casefold().endswith("-wp-reader"):
            raise TransportError("SshDedicatedUserRequired")
        if not 1 <= port <= 65535:
            raise TransportError("SshFailed")

        key_path = stored_key_path(self.data_dir, self.site.id)
        try:
            key_stat = key_path.lstat()
        except OSError as exc:
            raise TransportError("SshIdentityMissing") from exc
        try:
            key_dir_stat = key_path.parent.lstat()
        except OSError as exc:
            raise TransportError("SshIdentityMissing") from exc
        if not stat.S_ISDIR(key_dir_stat.st_mode) or stat.S_ISLNK(key_dir_stat.st_mode):
            raise TransportError("SshIdentityRejected")
        if os.name != "nt" and stat.S_IMODE(key_dir_stat.st_mode) & 0o077:
            raise TransportError("SshIdentityPermissions")
        if not stat.S_ISREG(key_stat.st_mode) or key_path.is_symlink():
            raise TransportError("SshIdentityRejected")
        if os.name != "nt" and stat.S_IMODE(key_stat.st_mode) & 0o077:
            raise TransportError("SshIdentityPermissions")
        try:
            private_key = paramiko.PKey.from_path(key_path)
        except (OSError, paramiko.SSHException, ValueError) as exc:
            raise TransportError("SshIdentityRejected") from exc

        try:
            entry = HostKeyEntry.from_line(f"{host} {self.site.host_public_key.strip()}", None)
        except (TypeError, ValueError, paramiko.SSHException) as exc:
            raise TransportError("SshHostKeyMissing") from exc
        expected = entry.key if entry else None
        if expected is None:
            raise TransportError("SshHostKeyMissing")

        sock: socket.socket | None = None
        transport: Transport | None = None
        sftp: SFTPClient | None = None
        try:
            sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
            transport = paramiko.Transport(sock)
            transport.set_log_channel("ecom_ops.wp_sftp")
            transport.banner_timeout = _CONNECT_TIMEOUT
            transport.auth_timeout = _CONNECT_TIMEOUT
            transport.start_client(timeout=_CONNECT_TIMEOUT)
            actual = transport.get_remote_server_key()
            if actual.get_name() != expected.get_name() or actual.asbytes() != expected.asbytes():
                raise TransportError("SshHostKeyMismatch")
            transport.auth_publickey(user, private_key)
            if not transport.is_authenticated():
                raise TransportError("SshAuthenticationFailed")
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                raise TransportError("SftpUnavailable")
            sftp.get_channel().settimeout(_CHANNEL_TIMEOUT)
            yield sftp
        except TransportError:
            raise
        except (OSError, EOFError, TimeoutError, paramiko.SSHException) as exc:
            raise TransportError("SshFailed") from exc
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            elif sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _safe_path(self, path: str, *, listing: bool) -> str:
        if not isinstance(path, str) or not files_root_narrow(self.site.files_root):
            raise PathRejected("root")
        root = self.site.files_root.rstrip("/")
        if not path.startswith(root + "/") or "\\" in path:
            raise PathRejected("scope")
        parts = path[len(root) + 1 :].split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise PathRejected("segments")
        if len(parts) < 2 or parts[0] != "wp-content" or parts[1] not in {"themes", "plugins"}:
            raise PathRejected("scope")
        if listing:
            if len(parts) >= 3 and not _SLUG.fullmatch(parts[2]):
                raise PathRejected("slug")
            if any(
                _SEGMENT.fullmatch(part) is None or _SENSITIVE_NAME.search(part)
                for part in parts[3:]
            ):
                raise PathRejected("segments")
        else:
            if len(parts) < 4 or not _SLUG.fullmatch(parts[2]):
                raise PathRejected("file")
            if any(
                _SEGMENT.fullmatch(part) is None or _SENSITIVE_NAME.search(part)
                for part in parts[3:]
            ):
                raise PathRejected("segments")
            leaf = parts[-1]
            if (
                leaf.endswith(".azom-bak")
                or _SENSITIVE_NAME.search(leaf)
                or Path(leaf).suffix.lower() not in _EXTENSIONS
            ):
                raise PathRejected("extension")
        return path

    @staticmethod
    def _check_remote_path(sftp: SFTPClient, path: str, *, directory: bool) -> Any:
        """lstat every path segment and refuse symlinks and special files."""
        current = "/"
        attrs: Any = None
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            raise PathRejected("root")
        for index, segment in enumerate(segments):
            current = posixpath.join(current, segment)
            try:
                attrs = sftp.lstat(current)
            except OSError as exc:
                raise TransportError("SshFailed") from exc
            mode = getattr(attrs, "st_mode", None)
            if mode is None or stat.S_ISLNK(mode):
                raise PathRejected("remote symlink")
            is_last = index == len(segments) - 1
            if not is_last and not stat.S_ISDIR(mode):
                raise PathRejected("remote parent")
            if is_last:
                if directory and not stat.S_ISDIR(mode):
                    raise PathRejected("remote directory")
                if not directory and not stat.S_ISREG(mode):
                    raise PathRejected("remote file")
        assert attrs is not None
        return attrs


def build_file_transport(
    site: Site,
    data_dir: Path,
) -> LocalTree | SshFileTransport:
    """Mock stays local; live mode uses read-only SFTP and never a shell."""
    if env_flag("AZOM_WP_FILES_SSH") and not env_flag("AZOM_USE_MOCK"):
        return SshFileTransport(site, data_dir)
    root = site.files_root or "/var/www/unconfigured"
    return LocalTree(root, Path(data_dir) / "wp_files_mock" / site.id)
