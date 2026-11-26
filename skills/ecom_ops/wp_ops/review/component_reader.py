"""Unified read-only WordPress component readers over ``FileTransport``.

Copies verified theme/plugin files from ``LocalTree`` or read-only SFTP transports
using ``transport_open_batch`` and ``transport_list_entries`` without writing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.file_paths import (
    PathRejected,
    TransportError,
    component_dir,
    resolve_file,
    sensitive_relative,
)
from ecom_ops.wp_ops.file_transport import (
    LocalTree,
    transport_list_entries,
    transport_open_batch,
)
from ecom_ops.wp_ops.review.workspace_io import (
    _ENTRY,
    _MAX_ENTRIES,
    _MAX_FILE,
    _MAX_FILES,
    _MAX_TOTAL,
    _TEXT_EXTENSIONS,
    SLUG,
    ReviewError,
)


def _list_transport_entries(transport: Any, absolute_dir: str) -> list[tuple[str, bool]]:
    """List directory entries from a ``FileTransport``, normalizing malformed outputs."""
    list_entries = getattr(transport, "list_entries", None)
    if callable(list_entries):
        raw = list_entries(absolute_dir)
        if not isinstance(raw, list):
            raise ReviewError("CopyReadFailed")
        return [(str(name or ""), bool(is_dir)) for name, is_dir in raw]
    names = transport.list_dir(absolute_dir)
    if not isinstance(names, list):
        raise ReviewError("CopyReadFailed")
    return [
        (str(name or ""), Path(str(name or "")).suffix.lower() not in _TEXT_EXTENSIONS)
        for name in names
    ]


def read_local_component(
    transport: object,
    files_root: str,
    kind: str,
    slug: str,
) -> list[tuple[str, bytes]]:
    """Read one component from a ``LocalTree``. Other transports copy nothing."""
    if not isinstance(transport, LocalTree):
        return []
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    absolute = component_dir(files_root, kind, slug)
    base = transport._local(absolute)
    if not base.exists():
        return []
    if base.is_symlink() or not base.is_dir():
        raise ReviewError("CopyRefused")
    files: list[tuple[str, bytes]] = []
    total = 0
    entries = 0
    for path in sorted(base.rglob("*")):
        entries += 1
        if entries > _MAX_ENTRIES:
            raise ReviewError("CopyLimit")
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if sensitive_relative(relative):
            continue
        data = path.read_bytes()
        if len(data) > _MAX_FILE:
            raise ReviewError("FileTooLarge")
        total += len(data)
        if total > _MAX_TOTAL or len(files) >= _MAX_FILES:
            raise ReviewError("CopyLimit")
        files.append((relative, data))
    return files


def read_site_component(
    transport: object,
    files_root: str,
    kind: str,
    slug: str,
) -> list[tuple[str, bytes]]:
    """Copy one allowlisted text component from a ``LocalTree`` or read-only SFTP."""
    if isinstance(transport, LocalTree):
        return read_local_component(transport, files_root, kind, slug)
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    component = component_dir(files_root, kind, slug)
    pending = [("", component)]
    files: list[tuple[str, bytes]] = []
    total = 0
    entries = 0

    with transport_open_batch(transport):
        while pending:
            relative_dir, absolute_dir = pending.pop()
            try:
                raw_entries = _list_transport_entries(transport, absolute_dir)
            except ReviewError:
                raise
            except Exception as exc:
                raise ReviewError("CopyReadFailed") from exc
            for name, is_dir in raw_entries:
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise ReviewError("CopyLimit")
                if not _ENTRY.fullmatch(name):
                    raise ReviewError("PathRejected")
                relative = f"{relative_dir}/{name}" if relative_dir else name
                if sensitive_relative(relative):
                    continue
                if is_dir:
                    if name.startswith("."):
                        continue
                    pending.append((relative, absolute_dir.rstrip("/") + "/" + name))
                    continue
                if Path(name).suffix.lower() not in _TEXT_EXTENSIONS:
                    continue
                try:
                    absolute_file = resolve_file(files_root, kind, slug, relative)
                    data = transport.read_bytes(absolute_file)  # type: ignore[attr-defined]
                except (PathRejected, TransportError) as exc:
                    raise ReviewError("CopyReadFailed") from exc
                except Exception as exc:
                    raise ReviewError("CopyReadFailed") from exc
                if len(data) > _MAX_FILE:
                    raise ReviewError("FileTooLarge")
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ReviewError("BinaryFileRefused") from exc
                total += len(data)
                if total > _MAX_TOTAL or len(files) >= _MAX_FILES:
                    raise ReviewError("CopyLimit")
                files.append((relative, bytes(data)))
    return sorted(files)


def read_theme_woo_files(transport: object, files_root: str, slug: str) -> dict[str, Any]:
    """Safely inspect only WooCommerce override files (``woocommerce/``) + root ``wc-*.php`` templates.

    Uses the unified ``FileTransport`` batch/list/read interface for both ``LocalTree``
    and read-only SFTP transports. Never fails when a theme lacks ``woocommerce/`` or
    contains large non-WooCommerce asset files (>64KB).
    """
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    component = component_dir(files_root, "theme", slug)
    root_wc_templates: list[str] = []
    woo_files: list[tuple[str, str]] = []
    skipped_files = 0
    has_woocommerce_dir = False
    is_local = isinstance(transport, LocalTree)

    with transport_open_batch(transport):
        try:
            root_entries = transport_list_entries(transport, component)
        except PathRejected:
            if is_local:
                return {
                    "has_woocommerce_dir": False,
                    "root_wc_templates": [],
                    "woo_files": [],
                    "skipped_files": 0,
                }
            raise ReviewError("CopyReadFailed") from None
        except Exception as exc:
            if is_local:
                return {
                    "has_woocommerce_dir": False,
                    "root_wc_templates": [],
                    "woo_files": [],
                    "skipped_files": 0,
                }
            raise ReviewError("CopyReadFailed") from exc

        for raw_name, is_dir in root_entries:
            name = str(raw_name or "")
            if not _ENTRY.fullmatch(name):
                continue
            if is_dir and name == "woocommerce":
                has_woocommerce_dir = True
            elif not is_dir and name.lower().startswith("wc-") and name.lower().endswith(".php"):
                root_wc_templates.append(name)

        if has_woocommerce_dir:
            pending = [("woocommerce", component.rstrip("/") + "/woocommerce")]
            seen = 0
            while pending and seen < _MAX_ENTRIES:
                rel_dir, abs_dir = pending.pop()
                try:
                    sub_entries = transport_list_entries(transport, abs_dir)
                except Exception:
                    skipped_files += 1
                    continue
                for raw_name, is_dir in sub_entries:
                    name = str(raw_name or "")
                    seen += 1
                    if seen > _MAX_ENTRIES:
                        break
                    if not _ENTRY.fullmatch(name):
                        continue
                    rel = f"{rel_dir}/{name}"
                    if sensitive_relative(rel):
                        continue
                    if is_dir:
                        if not name.startswith("."):
                            pending.append((rel, abs_dir.rstrip("/") + "/" + name))
                        continue
                    if Path(name).suffix.lower() not in _TEXT_EXTENSIONS:
                        continue
                    try:
                        abs_file = resolve_file(files_root, "theme", slug, rel)
                        raw_bytes = transport.read_bytes(abs_file)  # type: ignore[attr-defined]
                    except Exception:
                        skipped_files += 1
                        continue
                    if len(raw_bytes) > _MAX_FILE or b"\x00" in raw_bytes:
                        skipped_files += 1
                        continue
                    woo_files.append((rel, raw_bytes.decode("utf-8", errors="replace")))

    return {
        "has_woocommerce_dir": has_woocommerce_dir,
        "root_wc_templates": sorted(root_wc_templates),
        "woo_files": sorted(woo_files),
        "skipped_files": skipped_files,
    }


__all__ = [
    "read_local_component",
    "read_site_component",
    "read_theme_woo_files",
]
