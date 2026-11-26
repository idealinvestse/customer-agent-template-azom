"""File transport protocol and local mock filesystem adapter for WordPress files."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

from ecom_ops.wp_ops.file_paths import (
    ALLOWED_EXTENSIONS,
    PathRejected,
    TransportError,
    sha256_digest,
)


class FileTransport(Protocol):
    is_ssh: bool

    def list_dir(self, absolute_dir: str) -> list[str]: ...

    def list_entries(self, rel_dir: str = "") -> list[tuple[str, bool]]: ...

    def open_batch(self) -> AbstractContextManager[Any]: ...

    def read_bytes(self, absolute_file: str) -> bytes: ...

    def read_css_search_bytes(self, absolute_file: str, *, max_bytes: int = 512 * 1024) -> bytes: ...

    def write_bytes(self, absolute_file: str, data: bytes) -> None: ...

    def create_bytes(self, rel_path: str, data: bytes, *, overwrite: bool = False) -> Any: ...


class LocalTree:
    """Map the site's remote root onto a directory in AZOM_DATA_DIR."""

    is_ssh: bool = False

    def __init__(self, remote_root: str, local_root: Path) -> None:
        self.remote_root = remote_root.rstrip("/")
        self.local_root = local_root

    @contextmanager
    def open_batch(self) -> Iterator[LocalTree]:
        yield self

    def ensure_demo(self) -> None:
        theme = self._local(self.remote_root + "/wp-content/themes/twentytwentyfive")
        plugin = self._local(self.remote_root + "/wp-content/plugins/hello")
        theme.mkdir(parents=True, exist_ok=True)
        plugin.mkdir(parents=True, exist_ok=True)
        style = theme / "style.css"
        hello = plugin / "hello.php"
        if not style.exists():
            style.write_text("/* mock theme */\n", encoding="utf-8")
        if not hello.exists():
            hello.write_text("<?php\n// mock plugin\n", encoding="utf-8")

    def parent_dir_exists(self, absolute_file: str) -> bool:
        return self._local(absolute_file).parent.is_dir()

    def list_dir(self, absolute_dir: str) -> list[str]:
        path = self._local(absolute_dir)
        if not path.is_dir():
            return []
        return sorted(item.name for item in path.iterdir() if item.name not in {".", ".."})

    def list_entries(self, rel_dir: str = "") -> list[tuple[str, bool]]:
        """Return sorted ``(name, is_dir)`` pairs inside ``rel_dir`` without following symlinks."""
        remote, _clean_rel = self._resolve_remote(rel_dir)
        path = self._local_checked(remote)
        if not path.is_dir():
            return []
        root = self.local_root.resolve()
        entries: list[tuple[str, bool]] = []
        for item in sorted(path.iterdir(), key=lambda entry: entry.name):
            if item.name in {".", ".."} or item.is_symlink():
                continue
            resolved = item.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            if item.is_dir():
                entries.append((item.name, True))
            elif item.is_file():
                entries.append((item.name, False))
        return entries

    def read_bytes(self, absolute_file: str) -> bytes:
        path = self._local_checked(absolute_file)
        if not path.is_file():
            raise TransportError("FileMissing")
        return path.read_bytes()

    def read_css_search_bytes(self, absolute_file: str, *, max_bytes: int = 512 * 1024) -> bytes:
        """Read-only bounded read for larger parent ``.css`` files during selector search."""
        path = self._local_checked(absolute_file)
        if path.suffix.lower() != ".css":
            raise PathRejected("extension")
        if not path.is_file():
            raise TransportError("FileMissing")
        try:
            if path.stat().st_size > max_bytes:
                raise TransportError("FileTooLarge")
            with path.open("rb") as fh:
                data = fh.read(max_bytes + 1)
        except OSError as exc:
            raise TransportError("FileMissing") from exc
        if len(data) > max_bytes:
            raise TransportError("FileTooLarge")
        if b"\x00" in data:
            raise TransportError("NotText")
        return data

    def create_bytes(self, rel_path: str, data: bytes, *, overwrite: bool = False) -> dict[str, Any]:
        """Create a file inside ``local_root`` with parent dirs, refusing symlinks and overwrites by default."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        payload = bytes(data)
        remote, clean_rel = self._resolve_remote(rel_path)
        if not clean_rel:
            raise PathRejected("root")
        if Path(clean_rel).name.endswith(".azom-bak"):
            raise PathRejected("backup")
        path = self._local_checked(remote)
        existed = path.exists()
        if existed:
            if path.is_dir():
                raise PathRejected("directory")
            if not overwrite:
                raise TransportError("AlreadyExists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {
            "path": clean_rel,
            "bytes": len(payload),
            "sha256": sha256_digest(payload),
            "created": not existed,
            "overwritten": existed,
        }

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        path = self._local_checked(absolute_file)
        if not path.is_file():
            raise TransportError("FileMissing")
        backup = path.with_name(path.name + ".azom-bak")
        backup.write_bytes(path.read_bytes())
        path.write_bytes(data)

    def _resolve_remote(self, path_or_rel: str) -> tuple[str, str]:
        if not isinstance(path_or_rel, str):
            raise PathRejected("root")
        raw = path_or_rel.strip()
        if "\\" in raw:
            raise PathRejected("root")
        if not raw:
            return self.remote_root, ""
        prefix = self.remote_root + "/"
        if raw.startswith("/"):
            if raw != self.remote_root and not raw.startswith(prefix):
                raise PathRejected("root")
            relative = "" if raw == self.remote_root else raw[len(prefix) :]
        else:
            relative = raw.rstrip("/")
        if relative == "":
            return self.remote_root, ""
        raw_parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise PathRejected("root")
        clean_rel = "/".join(raw_parts)
        return f"{self.remote_root}/{clean_rel}", clean_rel

    def _local_checked(self, remote: str) -> Path:
        local = self._local(remote)
        if self.local_root.is_symlink():
            raise PathRejected("symlink")
        prefix = self.remote_root + "/"
        relative = "" if remote == self.remote_root else remote[len(prefix) :]
        if relative:
            cursor = self.local_root
            for part in relative.split("/"):
                cursor = cursor / part
                if cursor.is_symlink():
                    raise PathRejected("symlink")
        return local

    def _local(self, remote: str) -> Path:
        prefix = self.remote_root + "/"
        if remote != self.remote_root and not remote.startswith(prefix):
            raise PathRejected("root")
        relative = "" if remote == self.remote_root else remote[len(prefix) :]
        if ".." in relative.split("/"):
            raise PathRejected("root")
        local = (self.local_root / relative).resolve()
        root = self.local_root.resolve()
        if local != root and root not in local.parents:
            raise PathRejected("root")
        return local


def transport_is_remote(transport: Any) -> bool:
    """Fail-closed check: only transports explicitly marked ``is_ssh=False`` are local."""
    flag = getattr(transport, "is_ssh", None)
    if isinstance(flag, bool):
        return flag
    return not isinstance(transport, LocalTree)


def transport_list_entries(transport: Any, directory: str) -> list[tuple[str, bool]]:
    """List directory entries via ``list_entries`` with fallback for minimal test doubles."""
    list_entries = getattr(transport, "list_entries", None)
    if callable(list_entries):
        return list(list_entries(directory))
    return [
        (name, Path(name).suffix.lower() not in ALLOWED_EXTENSIONS)
        for name in transport.list_dir(directory)
    ]


def transport_open_batch(transport: Any) -> AbstractContextManager[Any]:
    """Enter ``transport.open_batch()`` when available, otherwise a no-op context."""
    open_batch = getattr(transport, "open_batch", None)
    if callable(open_batch):
        return open_batch()
    return contextlib.nullcontext()


__all__ = [
    "FileTransport",
    "LocalTree",
    "transport_is_remote",
    "transport_list_entries",
    "transport_open_batch",
]
