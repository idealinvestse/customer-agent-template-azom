"""Read-only, site-scoped SFTP transport for existing WordPress theme/plugin files.

The live transport never requests an SSH shell or sends a remote command. It
authenticates with one per-site private key, checks the pinned host key before
authentication, refuses symlinks, and permits only bounded file reads.
"""

from __future__ import annotations

import errno
import os
import posixpath
import re
import socket
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import paramiko
from paramiko.hostkeys import HostKeyEntry
from paramiko.sftp_client import SFTPClient
from paramiko.transport import Transport

from ecom_ops.wp_ops.config import ssh_files_enabled
from ecom_ops.wp_ops.files import LocalTree, PathRejected, TransportError
from ecom_ops.wp_ops.models import Site, files_root_narrow
from ecom_ops.wp_ops.ssh_settings import host_key_ok, stored_key_path, user_ok

_FACADE_MODULE = "ecom_ops.integrations.wp_files_ssh"

_HOST = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(^\.env(?:$|\.)|^wp-config[^/]*\.php$|"
    r"(?:^|[-_.])(?:secrets?|credentials?|tokens?|passwords?|auth|private[-_.]?key|api[-_.]?key)(?:[-_.]|$)|"
    r"\.(?:bak|backup|sql|sqlite3?|dump|swp|orig)$)"
)
_EXTENSIONS = frozenset(
    {".php", ".css", ".scss", ".js", ".json", ".txt", ".md", ".html", ".pot", ".po"}
)
_MAX_BYTES = 65536
_MAX_DIRECTORY_ENTRIES = 256
_CONNECT_TIMEOUT = 10
_CHANNEL_TIMEOUT = 10


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)
    return default


def host_allowed(host: str) -> bool:
    text = str(host or "").strip().lower().rstrip(".")
    if not text or text.endswith(".example.test"):
        return False
    return _HOST.fullmatch(text) is not None


def _pinned_host_key_types(expected_key: paramiko.PKey) -> list[str]:
    """Pin KEX host-key algorithms matching ``expected_key``, keeping RSA-SHA2 for OpenSSH 8.8+."""
    name = expected_key.get_name()
    if name == "ssh-rsa":
        return ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"]
    return [name]


def _is_missing_error(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return getattr(exc, "errno", None) == errno.ENOENT


class SshFileTransport:
    """Use a dedicated per-site key for read-only, typed SFTP operations."""

    is_ssh: bool = True

    def __init__(self, site: Site, data_dir: Path) -> None:
        self.site = site
        self.data_dir = Path(data_dir)
        self._batch_state = threading.local()

    @staticmethod
    @contextmanager
    def _sftp_errors() -> Iterator[None]:
        try:
            yield
        except (OSError, EOFError, TimeoutError, paramiko.SSHException) as exc:
            if _is_missing_error(exc):
                raise TransportError("FileMissing") from exc
            raise TransportError("SshFailed") from exc

    @contextmanager
    def open_batch(self) -> Iterator[SshFileTransport]:
        """Hold one verified SFTP session open across nested read/list calls."""
        state = self._batch_state
        depth = getattr(state, "depth", 0)
        if depth > 0:
            state.depth = depth + 1
            try:
                yield self
            finally:
                state.depth -= 1
            return

        cm = self._session()
        sftp = cm.__enter__()
        state.cm = cm
        state.sftp = sftp
        state.depth = 1
        try:
            yield self
        finally:
            state.depth = 0
            state.sftp = None
            state.cm = None
            cm.__exit__(None, None, None)

    @contextmanager
    def _active_session(self) -> Iterator[SFTPClient]:
        state = self._batch_state
        sftp = getattr(state, "sftp", None)
        if getattr(state, "depth", 0) > 0 and sftp is not None:
            yield sftp
            return
        with self._session() as fresh:
            yield fresh

    def list_dir(self, absolute_dir: str) -> list[str]:
        return [name for name, _is_dir in self._list_entries_abs(absolute_dir)]

    def list_entries(self, rel_dir: str = "") -> list[tuple[str, bool]]:
        """Return sorted ``(name, is_dir)`` pairs inside ``rel_dir`` using SFTP attrs."""
        if not isinstance(rel_dir, str):
            raise PathRejected("scope")
        raw = rel_dir.strip()
        if not raw or "\\" in raw:
            raise PathRejected("scope")
        if raw.startswith("/"):
            candidate = raw
        else:
            root = str(self.site.files_root or "").rstrip("/")
            if not root:
                raise PathRejected("root")
            candidate = f"{root}/{raw.lstrip('/')}"
        return self._list_entries_abs(candidate)

    def _list_entries_abs(self, absolute_dir: str) -> list[tuple[str, bool]]:
        path = self._safe_path(absolute_dir, listing=True)
        with self._active_session() as sftp:
            self._check_remote_path(sftp, path, directory=True)
            # internal-sftp does not finish listdir_iter, so the channel times out.
            entries: list[Any] | None = None
            if hasattr(sftp, "listdir_attr") and callable(sftp.listdir_attr):
                try:
                    with self._sftp_errors():
                        entries = list(sftp.listdir_attr(path))
                except (AttributeError, NotImplementedError):
                    entries = None
            if entries is None:
                with self._sftp_errors():
                    raw_names = list(sftp.listdir(path))
                entries = [type("_Entry", (), {"filename": str(n), "st_mode": None})() for n in raw_names]
            if len(entries) > _MAX_DIRECTORY_ENTRIES:
                raise TransportError("SshDirectoryTooLarge")
            out: list[tuple[str, bool]] = []
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
                    except (OSError, EOFError, TimeoutError, paramiko.SSHException):
                        continue
                if mode is None or stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    out.append((name, True))
                elif stat.S_ISREG(mode):
                    out.append((name, False))
            return sorted(out, key=lambda item: item[0])

    def _read_bounded(
        self,
        absolute_file: str,
        *,
        cap: int,
        require_css: bool = False,
        require_text: bool = False,
    ) -> bytes:
        path = self._safe_path(absolute_file, listing=False)
        if require_css and Path(path).suffix.lower() != ".css":
            raise PathRejected("extension")
        with self._active_session() as sftp:
            attrs = self._check_remote_path(sftp, path, directory=False)
            size = getattr(attrs, "st_size", None)
            if size is None or size > cap:
                raise TransportError("FileTooLarge")
            with self._sftp_errors():
                remote = sftp.open(path, "rb")
                remote.settimeout(_CHANNEL_TIMEOUT)
                try:
                    opened = remote.stat()
                    opened_mode = getattr(opened, "st_mode", None)
                    if opened_mode is None or not stat.S_ISREG(opened_mode):
                        raise PathRejected("remote file type")
                    if getattr(opened, "st_size", cap + 1) > cap:
                        raise TransportError("FileTooLarge")
                    current = sftp.lstat(path)
                    current_mode = getattr(current, "st_mode", None)
                    if current_mode is None or stat.S_ISLNK(current_mode):
                        raise PathRejected("remote symlink")
                    opened_ino = getattr(opened, "st_ino", None)
                    current_ino = getattr(current, "st_ino", None)
                    if opened_ino and current_ino and opened_ino != current_ino:
                        raise PathRejected("remote file changed")
                    data = remote.read(cap + 1)
                finally:
                    remote.close()
            if len(data) > cap:
                raise TransportError("FileTooLarge")
            if require_text and b"\x00" in data:
                raise TransportError("NotText")
            return data

    def read_bytes(self, absolute_file: str) -> bytes:
        return self._read_bounded(absolute_file, cap=_MAX_BYTES)

    def read_css_search_bytes(self, absolute_file: str, *, max_bytes: int = 512 * 1024) -> bytes:
        """Read-only bounded SFTP read for larger parent ``.css`` files during selector search."""
        cap = min(max(int(max_bytes), _MAX_BYTES), 512 * 1024)
        return self._read_bounded(absolute_file, cap=cap, require_css=True, require_text=True)

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        del absolute_file, data
        raise TransportError("SshWriteDisabled")

    def create_bytes(self, absolute_file: str, data: bytes, *, overwrite: bool = False) -> None:
        del absolute_file, data, overwrite
        raise TransportError("SshWriteDisabled")

    def _validate_ssh_config(self) -> tuple[str, str, int]:
        host = str(self.site.ssh_host or "").strip().lower().rstrip(".")
        user = str(self.site.ssh_user or "").strip()
        port = int(self.site.ssh_port or 22)
        host_allowed_fn = _facade_attr("host_allowed", host_allowed)
        if not host_allowed_fn(host):
            raise TransportError("SshHostRefused")
        if not host_key_ok(self.site.host_public_key):
            raise TransportError("SshHostKeyMissing")
        if not user_ok(user) or user.casefold() == "root":
            raise TransportError("SshUserRefused")
        if not user.casefold().endswith("-wp-reader"):
            raise TransportError("SshDedicatedUserRequired")
        if not 1 <= port <= 65535:
            raise TransportError("SshFailed")
        return host, user, port

    def _load_private_key(self) -> paramiko.PKey:
        paramiko_mod = _facade_attr("paramiko", paramiko)
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
            return paramiko_mod.PKey.from_path(key_path)
        except (OSError, paramiko.SSHException, ValueError) as exc:
            raise TransportError("SshIdentityRejected") from exc

    def _parse_host_key(self, host: str) -> paramiko.PKey:
        try:
            entry = HostKeyEntry.from_line(f"{host} {self.site.host_public_key.strip()}", None)
        except (TypeError, ValueError, paramiko.SSHException) as exc:
            raise TransportError("SshHostKeyMissing") from exc
        expected = entry.key if entry else None
        if expected is None:
            raise TransportError("SshHostKeyMissing")
        return expected

    @contextmanager
    def _open_sftp_connection(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key: paramiko.PKey,
        expected_key: paramiko.PKey,
    ) -> Iterator[SFTPClient]:
        socket_mod = _facade_attr("socket", socket)
        paramiko_mod = _facade_attr("paramiko", paramiko)
        sock: socket.socket | None = None
        transport: Transport | None = None
        sftp: SFTPClient | None = None
        try:
            sock = socket_mod.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
            transport = paramiko_mod.Transport(sock)
            sec_opts = getattr(transport, "get_security_options", None)
            if callable(sec_opts):
                sec_opts().key_types = _pinned_host_key_types(expected_key)
            transport.set_log_channel("ecom_ops.wp_sftp")
            transport.banner_timeout = _CONNECT_TIMEOUT
            transport.auth_timeout = _CONNECT_TIMEOUT
            transport.start_client(timeout=_CONNECT_TIMEOUT)
            actual = transport.get_remote_server_key()
            if actual.get_name() != expected_key.get_name() or actual.asbytes() != expected_key.asbytes():
                raise TransportError("SshHostKeyMismatch")
            transport.auth_publickey(user, private_key)
            if not transport.is_authenticated():
                raise TransportError("SshAuthenticationFailed")
            sftp = paramiko_mod.SFTPClient.from_transport(transport)
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

    @contextmanager
    def _session(self) -> Iterator[SFTPClient]:
        host, user, port = self._validate_ssh_config()
        private_key = self._load_private_key()
        expected = self._parse_host_key(host)
        with self._open_sftp_connection(
            host=host,
            port=port,
            user=user,
            private_key=private_key,
            expected_key=expected,
        ) as sftp:
            yield sftp

    def _safe_path(self, path: str, *, listing: bool) -> str:
        if not isinstance(path, str) or not files_root_narrow(self.site.files_root):
            raise PathRejected("root")
        root = self.site.files_root.rstrip("/")
        if not path.startswith(root + "/") or "\\" in path:
            raise PathRejected("scope")
        parts = path[len(root) + 1 :].split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise PathRejected("segments")
        if len(parts) < 2 or parts[0] != "wp-content" or parts[1] not in {"themes", "plugins", "mu-plugins"}:
            raise PathRejected("scope")
        is_mu = parts[1] == "mu-plugins"
        if listing:
            if len(parts) >= 3 and not _SLUG.fullmatch(parts[2]):
                raise PathRejected("slug")
            if any(
                _SEGMENT.fullmatch(part) is None or _SENSITIVE_NAME.search(part)
                for part in parts[3:]
            ):
                raise PathRejected("segments")
        else:
            if is_mu and len(parts) == 3:
                leaf = parts[2]
                if (
                    _SEGMENT.fullmatch(leaf) is None
                    or leaf.endswith(".azom-bak")
                    or _SENSITIVE_NAME.search(leaf)
                    or Path(leaf).suffix.lower() not in _EXTENSIONS
                ):
                    raise PathRejected("extension")
                return path
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
            with SshFileTransport._sftp_errors():
                attrs = sftp.lstat(current)
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
    ssh_enabled_fn = _facade_attr("ssh_files_enabled", ssh_files_enabled)
    ssh_cls = _facade_attr("SshFileTransport", SshFileTransport)
    if ssh_enabled_fn():
        return ssh_cls(site, data_dir)
    root = site.files_root or "/var/www/unconfigured"
    return LocalTree(root, Path(data_dir) / "wp_files_mock" / site.id)


__all__ = [
    "SshFileTransport",
    "_CHANNEL_TIMEOUT",
    "_CONNECT_TIMEOUT",
    "_ENTRY",
    "_EXTENSIONS",
    "_HOST",
    "_MAX_BYTES",
    "_MAX_DIRECTORY_ENTRIES",
    "_SEGMENT",
    "_SENSITIVE_NAME",
    "_SLUG",
    "_is_missing_error",
    "build_file_transport",
    "host_allowed",
]
