"""Per-plan WordPress review workspace paths, hashes, and manifest I/O.

Nothing here writes to a live site, starts a shell, or creates a container.
``base/`` is sealed through this API. Windows does not rely on chmod.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.file_paths import sensitive_relative

PLAN_ID = re.compile(r"^wr-[a-f0-9]{12}$")
SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")

_MAX_FILE = 65536
_MAX_FILES = 200
_MAX_TOTAL = 8 * 1024 * 1024
_MAX_ENTRIES = 256
_MAX_CHANGES = 50
_TEXT_EXTENSIONS = frozenset(
    {".php", ".css", ".scss", ".js", ".json", ".txt", ".md", ".html", ".pot", ".po"}
)
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
    def __init__(self, error_class: str, message: str | None = None) -> None:
        super().__init__(message or error_class)
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


def iter_files(folder: Path) -> list[tuple[str, bytes]]:
    if not folder.is_dir():
        return []
    found: list[tuple[str, bytes]] = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found.append((path.relative_to(folder).as_posix(), path.read_bytes()))
    return found


def write_tree(folder: Path, files: list[tuple[str, bytes]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    for relative, data in files:
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise ReviewError("PathRejected")
        candidate = folder.joinpath(*parts)
        if candidate.is_symlink():
            raise ReviewError("PathRejected")
        dest = candidate.resolve()
        if dest.is_symlink() or (dest != root and root not in dest.parents):
            raise ReviewError("PathRejected")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _work_dir(ws_root: Path) -> Path:
    root = Path(ws_root)
    raw = root / "work"
    if raw.is_symlink():
        raise ReviewError("PathRejected")
    work = raw.resolve()
    if not work.is_dir():
        raise ReviewError("WorkspaceMissing")
    return work


def _base_dir(ws_root: Path) -> Path:
    root = Path(ws_root)
    raw = root / "base"
    if raw.is_symlink():
        raise ReviewError("PathRejected")
    base = raw.resolve()
    if not base.is_dir():
        raise ReviewError("WorkspaceMissing")
    return base


def _check_no_symlink(folder: Path, parts: list[str]) -> Path:
    cursor = folder
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ReviewError("PathRejected")
    resolved = cursor.resolve()
    root_resolved = folder.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ReviewError("BaseSealed")
    return resolved


def _validate_work_relative(relative: str) -> list[str]:
    if not isinstance(relative, str):
        raise ReviewError("PathRejected")
    raw = relative.strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ReviewError("PathRejected")
    parts = raw.split("/")
    if any(part == "" or part in {".", ".."} or _ENTRY.fullmatch(part) is None for part in parts):
        raise ReviewError("PathRejected")
    clean_rel = "/".join(parts)
    if sensitive_relative(clean_rel):
        raise ReviewError("PathRejected")
    leaf = parts[-1]
    if leaf.endswith(".azom-bak") or Path(leaf).suffix.lower() not in _TEXT_EXTENSIONS:
        raise ReviewError("PathRejected")
    return parts


def _resolve_work_target(ws_root: Path, rel_path: str) -> tuple[Path, Path, str]:
    parts = _validate_work_relative(rel_path)
    work = _work_dir(ws_root)
    dest = _check_no_symlink(work, parts)
    if dest == work or work not in dest.parents:
        raise ReviewError("BaseSealed")
    return work, dest, "/".join(parts)


def _validate_rel_dir(rel_dir: str) -> list[str]:
    if not isinstance(rel_dir, str):
        raise ReviewError("PathRejected")
    raw = rel_dir.strip().replace("\\", "/").strip("/")
    if not raw:
        return []
    if rel_dir.strip().startswith("/"):
        raise ReviewError("PathRejected")
    parts = raw.split("/")
    if any(part == "" or part in {".", ".."} or _ENTRY.fullmatch(part) is None for part in parts):
        raise ReviewError("PathRejected")
    if sensitive_relative("/".join(parts)):
        raise ReviewError("PathRejected")
    return parts


def _decode_utf8_bytes(data: bytes) -> str:
    if len(data) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    if b"\x00" in data:
        raise ReviewError("NotText")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("NotText") from exc


def _bounded_diff_text(chunks: list[str], *, max_bytes: int = 12288) -> tuple[str, bool]:
    cap = max(1024, min(65536, int(max_bytes)))
    raw = "".join(chunks)
    encoded = raw.encode("utf-8")
    if len(encoded) <= cap:
        return raw, False
    end = cap
    while end > 0:
        try:
            return encoded[:end].decode("utf-8"), True
        except UnicodeDecodeError:
            end -= 1
    return "", True


def _prune_empty_dirs(work: Path, parts: list[str]) -> None:
    for depth in range(len(parts) - 1, 0, -1):
        candidate = work.joinpath(*parts[:depth])
        if candidate == work or work not in candidate.parents:
            break
        if candidate.is_dir() and not candidate.is_symlink():
            try:
                candidate.rmdir()
            except OSError:
                break


def place_work_file(root: Path, relative: str, data: bytes) -> None:
    """Write one file under ``work/``. A path that reaches ``base/`` or fails validation is refused."""
    work = _work_dir(root)
    if len(data) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    parts = _validate_work_relative(relative)
    dest = _check_no_symlink(work, parts)
    if dest != work and work not in dest.parents:
        raise ReviewError("BaseSealed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def write_manifest(root: Path, public: dict) -> None:
    body = {key: public.get(key) for key in _MANIFEST_KEYS}
    path = root / "manifest.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def read_manifest(ws_root: Path) -> dict[str, Any]:
    path = Path(ws_root) / "manifest.json"
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


_read_manifest = read_manifest


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


def unified_diff_text(
    base_files: dict[str, bytes],
    work_files: dict[str, bytes],
    *,
    max_bytes: int = _MAX_FILE,
) -> str:
    """Compute a secret-filtered unified diff between ``base_files`` and ``work_files``."""
    chunks: list[str] = []
    for rel in sorted(set(base_files) | set(work_files)):
        if sensitive_relative(rel):
            continue
        old_b = base_files.get(rel)
        new_b = work_files.get(rel)
        if old_b == new_b:
            continue
        before_text = old_b.decode("utf-8", errors="replace") if old_b is not None else ""
        after_text = new_b.decode("utf-8", errors="replace") if new_b is not None else ""
        chunks.extend(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=2,
            )
        )
    diff_text, _truncated = _bounded_diff_text(chunks, max_bytes=max_bytes)
    return diff_text


__all__ = [
    "PLAN_ID",
    "ReviewError",
    "SITE_ID",
    "SLUG",
    "VerifiedPlugin",
    "VerifiedTheme",
    "_ENTRY",
    "_MANIFEST_KEYS",
    "_MAX_CHANGES",
    "_MAX_ENTRIES",
    "_MAX_FILE",
    "_MAX_FILES",
    "_MAX_TOTAL",
    "_TEXT_EXTENSIONS",
    "_base_dir",
    "_bounded_diff_text",
    "_check_no_symlink",
    "_decode_utf8_bytes",
    "_prune_empty_dirs",
    "_read_manifest",
    "_resolve_work_target",
    "_validate_rel_dir",
    "_validate_work_relative",
    "_work_dir",
    "delete_work",
    "iter_files",
    "new_plan_id",
    "place_work_file",
    "read_diff",
    "read_manifest",
    "restore_work_from_base",
    "site_plugin_slug",
    "tree_hash",
    "unified_diff_text",
    "workspace_root",
    "write_diff",
    "write_manifest",
    "write_tree",
]
