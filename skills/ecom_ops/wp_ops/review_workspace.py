"""Per-plan theme and plugin workspace under AZOM_DATA_DIR.

The site transport is read for an explicit, already verified component.
Nothing here writes the site, starts a shell, or creates a container.
``base/`` is sealed through this API. Windows does not rely on chmod.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ecom_ops.wp_ops.file_changes import sensitive_relative
from ecom_ops.wp_ops.files import (
    LocalTree,
    PathRejected,
    TransportError,
    component_dir,
    resolve_file,
)

PLAN_ID = re.compile(r"^wr-[a-f0-9]{12}$")
SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_MAX_FILE = 65536
_MAX_FILES = 200
_MAX_TOTAL = 8 * 1024 * 1024
_MAX_ENTRIES = 256
_TEXT_EXTENSIONS = frozenset({".php", ".css", ".js", ".json", ".txt", ".html"})
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_MANIFEST_KEYS = (
    "plan_id",
    "site_id",
    "environment",
    "kind",
    "slug",
    "base_sha256",
    "work_sha256",
    "risk",
    "status",
    "expires_at",
    "theme_relationship",
)


class ReviewError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class VerifiedTheme:
    """Server-owned theme relationship. The model cannot set these fields."""

    stylesheet: str
    template: str


@dataclass(frozen=True)
class VerifiedPlugin:
    """Server-confirmed active plugin slug; the model cannot set this value."""

    slug: str


def new_plan_id() -> str:
    return "wr-" + uuid.uuid4().hex[:12]


def site_plugin_slug(site_id: str) -> str:
    slug = f"azom-site-{site_id}"
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    return slug


def workspace_root(data_dir: Path, site_id: str, plan_id: str) -> Path:
    if not SITE_ID.fullmatch(site_id) or not PLAN_ID.fullmatch(plan_id):
        raise ReviewError("UnknownPlan")
    root = (Path(data_dir) / "wp_workspaces" / site_id).resolve()
    path = (root / plan_id).resolve()
    if path.parent != root:
        raise ReviewError("UnknownPlan")
    return path


def tree_hash(folder: Path) -> str:
    rows: list[str] = []
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{relative} {digest}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def child_skeleton(stylesheet: str) -> list[tuple[str, bytes]]:
    if not SLUG.fullmatch(stylesheet):
        raise ReviewError("SlugRejected")
    css = (
        "/*\n"
        "Theme Name: Azom Child\n"
        f"Template: {stylesheet}\n"
        "*/\n"
    ).encode()
    return [("style.css", css), ("functions.php", b"<?php\n")]


def plugin_skeleton(slug: str) -> list[tuple[str, bytes]]:
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    php = (
        "<?php\n"
        "/**\n"
        f" * Plugin Name: Azom Site {slug}\n"
        " */\n"
    ).encode()
    return [(f"{slug}.php", php)]


def read_local_component(transport: object, files_root: str, kind: str, slug: str) -> list[tuple[str, bytes]]:
    """Read one component from a LocalTree. Other transports copy nothing."""
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


def read_site_component(transport: object, files_root: str, kind: str, slug: str) -> list[tuple[str, bytes]]:
    """Copy one allowlisted text component from a LocalTree or read-only SFTP."""
    if isinstance(transport, LocalTree):
        return read_local_component(transport, files_root, kind, slug)
    if not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")
    component = component_dir(files_root, kind, slug)
    pending = [("", component)]
    files: list[tuple[str, bytes]] = []
    total = 0
    entries = 0
    while pending:
        relative_dir, absolute_dir = pending.pop()
        try:
            names = transport.list_dir(absolute_dir)
        except Exception as exc:
            raise ReviewError("CopyReadFailed") from exc
        if not isinstance(names, list):
            raise ReviewError("CopyReadFailed")
        for raw_name in names:
            name = str(raw_name or "")
            entries += 1
            if entries > _MAX_ENTRIES:
                raise ReviewError("CopyLimit")
            if not _ENTRY.fullmatch(name):
                raise ReviewError("PathRejected")
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if sensitive_relative(relative):
                continue
            suffix = Path(name).suffix.lower()
            if suffix in _TEXT_EXTENSIONS:
                try:
                    absolute_file = resolve_file(files_root, kind, slug, relative)
                    data = transport.read_bytes(absolute_file)
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
                continue
            if name.startswith("."):
                continue
            child = absolute_dir.rstrip("/") + "/" + name
            pending.append((relative, child))
    return sorted(files)


def write_tree(folder: Path, files: list[tuple[str, bytes]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    for relative, data in files:
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise ReviewError("PathRejected")
        dest = (folder.joinpath(*parts)).resolve()
        if dest != root and root not in dest.parents:
            raise ReviewError("PathRejected")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def place_work_file(root: Path, relative: str, data: bytes) -> None:
    """Write one file under ``work/``. A path that reaches ``base/`` is refused."""
    work = (root / "work").resolve()
    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ReviewError("PathRejected")
    if len(data) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    dest = work.joinpath(*parts).resolve()
    if dest != work and work not in dest.parents:
        raise ReviewError("BaseSealed")
    if not work.is_dir():
        raise ReviewError("WorkspaceMissing")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def write_manifest(root: Path, public: dict) -> None:
    body = {key: public.get(key) for key in _MANIFEST_KEYS}
    path = root / "manifest.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def write_diff(root: Path, diff: str) -> None:
    (root / "diff.txt").write_text(diff[:_MAX_FILE], encoding="utf-8")


def read_diff(root: Path) -> str:
    path = root / "diff.txt"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def delete_work(root: Path) -> None:
    work = root / "work"
    if work.exists():
        shutil.rmtree(work)


def restore_work_from_base(root: Path) -> None:
    base = root / "base"
    work = root / "work"
    if not base.is_dir():
        raise ReviewError("WorkspaceMissing")
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(base, work)


def iter_files(folder: Path) -> list[tuple[str, bytes]]:
    if not folder.is_dir():
        return []
    found: list[tuple[str, bytes]] = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found.append((path.relative_to(folder).as_posix(), path.read_bytes()))
    return found
