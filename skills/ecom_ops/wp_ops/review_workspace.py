"""Per-plan theme and plugin workspace under AZOM_DATA_DIR.

The site transport is read for an explicit, already verified component.
Nothing here writes the site, starts a shell, or creates a container.
``base/`` is sealed through this API. Windows does not rely on chmod.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.file_changes import (
    _normalize_extensions,
    sensitive_relative,
)
from ecom_ops.wp_ops.files import (
    LocalTree,
    PathRejected,
    TransportError,
    component_dir,
    resolve_file,
)
from ecom_ops.wp_ops.theme_json import apply_theme_json_operations

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
_SCAFFOLD_TEMPLATES = frozenset(
    {"child_theme_basics", "site_plugin_basics", "woo_compat_declaration"}
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
    batch_ctx = (
        transport.open_batch()
        if hasattr(transport, "open_batch") and callable(transport.open_batch)
        else contextlib.nullcontext()
    )
    has_entries = hasattr(transport, "list_entries") and callable(transport.list_entries)
    with batch_ctx:
        while pending:
            relative_dir, absolute_dir = pending.pop()
            try:
                if has_entries:
                    raw_entries = transport.list_entries(absolute_dir)
                else:
                    names = transport.list_dir(absolute_dir)
                    if not isinstance(names, list):
                        raise ReviewError("CopyReadFailed")
                    raw_entries = [
                        (name, Path(str(name or "")).suffix.lower() not in _TEXT_EXTENSIONS)
                        for name in names
                    ]
            except ReviewError:
                raise
            except Exception as exc:
                raise ReviewError("CopyReadFailed") from exc
            if not isinstance(raw_entries, list):
                raise ReviewError("CopyReadFailed")
            for raw_item in raw_entries:
                raw_name, is_dir = raw_item
                name = str(raw_name or "")
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
                    child = absolute_dir.rstrip("/") + "/" + name
                    pending.append((relative, child))
                    continue
                suffix = Path(name).suffix.lower()
                if suffix not in _TEXT_EXTENSIONS:
                    continue
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


def patch_work_file(
    root: Path,
    relative: str,
    old_text: str,
    new_text: str,
    *,
    base_sha256: str | None = None,
) -> dict[str, Any]:
    """Patch one unique occurrence of ``old_text`` inside ``work/``."""
    parts = _validate_work_relative(relative)
    work = (root / "work").resolve()
    if not work.is_dir():
        raise ReviewError("WorkspaceMissing")
    raw_dest = work.joinpath(*parts)
    dest = raw_dest.resolve()
    if dest == work or work not in dest.parents:
        raise ReviewError("BaseSealed")
    if raw_dest.is_symlink() or dest.is_symlink():
        raise ReviewError("PathRejected")
    if not dest.is_file():
        raise ReviewError("FileMissing")
    raw_bytes = dest.read_bytes()
    before_sha = hashlib.sha256(raw_bytes).hexdigest()
    if base_sha256 is not None and base_sha256 != "" and before_sha != base_sha256:
        raise ReviewError("DigestMismatch")
    try:
        current = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("NotText") from exc
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise ReviewError("NotText")
    if old_text == "":
        raise ReviewError("PatchTargetNotFound")
    occurrences = current.count(old_text)
    if occurrences == 0:
        raise ReviewError("PatchTargetNotFound")
    if occurrences > 1:
        raise ReviewError("PatchAmbiguous")
    updated = current.replace(old_text, new_text, 1)
    if updated == current:
        raise ReviewError("NoChange")
    if "\x00" in updated:
        raise ReviewError("NotText")
    encoded = updated.encode("utf-8")
    if len(encoded) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    dest.write_bytes(encoded)
    after_sha = hashlib.sha256(encoded).hexdigest()
    return {
        "path": "/".join(parts),
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "byte_length": len(encoded),
    }


def create_work_file(root: Path, relative: str, content: str) -> dict[str, Any]:
    """Create one new text file inside ``work/`` without overwriting existing files."""
    parts = _validate_work_relative(relative)
    work = (root / "work").resolve()
    if not work.is_dir():
        raise ReviewError("WorkspaceMissing")
    raw_dest = work.joinpath(*parts)
    dest = raw_dest.resolve()
    if dest == work or work not in dest.parents:
        raise ReviewError("BaseSealed")
    if raw_dest.is_symlink() or dest.exists() or dest.is_symlink():
        raise ReviewError("AlreadyExists")
    if not isinstance(content, str) or "\x00" in content:
        raise ReviewError("NotText")
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(encoded)
    return {
        "path": "/".join(parts),
        "after_sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
    }


def list_work_changes(root: Path) -> list[dict[str, str]]:
    """Return sorted relative path change statuses comparing ``base/`` and ``work/``."""
    base_files = dict(iter_files(root / "base"))
    work_files = dict(iter_files(root / "work"))
    changes: list[dict[str, str]] = []
    for rel in sorted(set(base_files) | set(work_files)):
        in_base = rel in base_files
        in_work = rel in work_files
        if not in_base and in_work:
            changes.append({"path": rel, "change": "created"})
        elif in_base and not in_work:
            changes.append({"path": rel, "change": "deleted"})
        elif base_files[rel] != work_files[rel]:
            changes.append({"path": rel, "change": "modified"})
    return changes


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


def _read_manifest(ws_root: Path) -> dict[str, Any]:
    path = Path(ws_root) / "manifest.json"
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


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


def read_work_file(
    ws_root: Path,
    rel_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Read a text file from ``work/`` with line numbers and base/work hashes."""
    if start_line is not None and (type(start_line) is not int or start_line < 1 or start_line > 50000):
        raise ReviewError("InvalidToolArguments")
    if end_line is not None and (type(end_line) is not int or end_line < 1 or end_line > 50000):
        raise ReviewError("InvalidToolArguments")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise ReviewError("InvalidToolArguments")

    _work, dest, clean_rel = _resolve_work_target(ws_root, rel_path)
    base = _base_dir(ws_root)
    if not dest.is_file():
        raise ReviewError("FileMissing")
    raw_bytes = dest.read_bytes()
    text = _decode_utf8_bytes(raw_bytes)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    base_sha256: str | None = None
    base_target = _check_no_symlink(base, clean_rel.split("/"))
    if base_target.is_file():
        base_sha256 = hashlib.sha256(base_target.read_bytes()).hexdigest()

    lines = text.splitlines()
    total_lines = len(lines)
    s_line = start_line if start_line is not None else 1
    e_line = end_line if end_line is not None else min(total_lines, s_line + 199)
    out_lines: list[str] = []
    used_bytes = 0
    max_page_bytes = 12288
    actual_end = total_lines if s_line > total_lines else s_line - 1
    for idx in range(s_line, min(e_line, total_lines) + 1):
        formatted = f"{idx}: {lines[idx - 1]}"
        chunk = formatted.encode("utf-8")
        sep_bytes = 1 if out_lines else 0
        if out_lines and used_bytes + sep_bytes + len(chunk) > max_page_bytes:
            break
        if not out_lines and len(chunk) > max_page_bytes:
            end_cut = max_page_bytes
            while end_cut > 0:
                try:
                    out_lines.append(chunk[:end_cut].decode("utf-8"))
                    break
                except UnicodeDecodeError:
                    end_cut -= 1
            actual_end = idx
            break
        out_lines.append(formatted)
        used_bytes += sep_bytes + len(chunk)
        actual_end = idx
    next_line = actual_end + 1 if actual_end < total_lines else None
    return {
        "path": clean_rel,
        "content": "\n".join(out_lines),
        "start_line": s_line,
        "end_line": actual_end,
        "total_lines": total_lines,
        "next_line": next_line,
        "truncated": next_line is not None,
        "sha256": sha256,
        "base_sha256": base_sha256,
        "byte_length": len(raw_bytes),
    }


def list_work_files(ws_root: Path, rel_dir: str = "") -> dict[str, Any]:
    """List workspace files with size and status relative to ``base/``."""
    work = _work_dir(ws_root)
    base = _base_dir(ws_root)
    dir_parts = _validate_rel_dir(rel_dir)
    clean_dir = "/".join(dir_parts)
    if dir_parts:
        _check_no_symlink(work, dir_parts)
        _check_no_symlink(base, dir_parts)

    base_files = dict(iter_files(base))
    work_files = dict(iter_files(work))
    prefix = clean_dir + "/" if clean_dir else ""

    rows: list[dict[str, Any]] = []
    truncated = False
    for rel in sorted(set(base_files) | set(work_files)):
        if sensitive_relative(rel):
            continue
        if clean_dir and rel != clean_dir and not rel.startswith(prefix):
            continue
        if len(rows) >= _MAX_FILES:
            truncated = True
            break
        in_base = rel in base_files
        in_work = rel in work_files
        if not in_base and in_work:
            status = "created"
            size = len(work_files[rel])
            sha: str | None = hashlib.sha256(work_files[rel]).hexdigest()
        elif in_base and not in_work:
            status = "deleted"
            size = 0
            sha = None
        elif base_files[rel] != work_files[rel]:
            status = "modified"
            size = len(work_files[rel])
            sha = hashlib.sha256(work_files[rel]).hexdigest()
        else:
            status = "unchanged"
            size = len(work_files[rel])
            sha = hashlib.sha256(work_files[rel]).hexdigest()
        rows.append(
            {
                "path": rel,
                "size": size,
                "status": status,
                "sha256": sha,
            }
        )
    return {
        "directory": clean_dir,
        "files": rows,
        "count": len(rows),
        "truncated": truncated,
    }


def work_diff(
    ws_root: Path,
    rel_path: str | None = None,
    *,
    max_bytes: int = 12288,
) -> dict[str, Any]:
    """Compute a bounded unified diff between ``base/`` and ``work/``."""
    work = _work_dir(ws_root)
    base = _base_dir(ws_root)
    target_rel: str | None = None
    if rel_path is not None and rel_path != "":
        _work, work_dest, target_rel = _resolve_work_target(ws_root, rel_path)
        base_dest = _check_no_symlink(base, target_rel.split("/"))
        if not work_dest.is_file() and not base_dest.is_file():
            raise ReviewError("FileMissing")
        old_bytes = base_dest.read_bytes() if base_dest.is_file() else None
        new_bytes = work_dest.read_bytes() if work_dest.is_file() else None
        pairs: list[tuple[str, bytes | None, bytes | None]] = [(target_rel, old_bytes, new_bytes)]
    else:
        base_files = dict(iter_files(base))
        work_files = dict(iter_files(work))
        pairs = []
        for rel in sorted(set(base_files) | set(work_files)):
            if sensitive_relative(rel):
                continue
            old_b = base_files.get(rel)
            new_b = work_files.get(rel)
            if old_b != new_b:
                pairs.append((rel, old_b, new_b))

    chunks: list[str] = []
    changed_files: list[dict[str, str]] = []
    for rel, old_b, new_b in pairs:
        if old_b == new_b:
            continue
        if old_b is None:
            change = "created"
        elif new_b is None:
            change = "deleted"
        else:
            change = "modified"
        changed_files.append({"path": rel, "change": change})
        before_text = _decode_utf8_bytes(old_b) if old_b is not None else ""
        after_text = _decode_utf8_bytes(new_b) if new_b is not None else ""
        chunks.extend(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=2,
            )
        )
    diff_text, truncated = _bounded_diff_text(chunks, max_bytes=max_bytes)
    return {
        "path": target_rel,
        "diff": diff_text,
        "changed_files": changed_files,
        "truncated": truncated,
    }


def apply_work_changes(
    ws_root: Path,
    changes: list[dict[str, Any]],
    *,
    allow_create: bool = True,
    allow_delete: bool = True,
) -> dict[str, Any]:
    """Validate and apply multi-file workspace changes atomically."""
    work = _work_dir(ws_root)
    base = _base_dir(ws_root)
    if not isinstance(changes, list) or not changes:
        raise ReviewError("InvalidToolArguments")
    if len(changes) > _MAX_CHANGES:
        raise ReviewError("InvalidToolArguments")

    existing_work = dict(iter_files(work))
    virtual: dict[str, bytes | None] = {}
    targets: dict[str, Path] = {}
    results: list[dict[str, Any]] = []

    for item in changes:
        if not isinstance(item, dict):
            raise ReviewError("InvalidToolArguments")
        op = item.get("op")
        if op not in {"edit", "create", "delete", "revert"}:
            raise ReviewError("InvalidToolArguments")
        rel_path = item.get("path")
        if not isinstance(rel_path, str):
            raise ReviewError("PathRejected")
        _work, dest, clean_rel = _resolve_work_target(ws_root, rel_path)
        base_dest = _check_no_symlink(base, clean_rel.split("/"))
        targets[clean_rel] = dest

        if clean_rel not in virtual:
            if dest.is_file():
                virtual[clean_rel] = dest.read_bytes()
            elif dest.exists():
                raise ReviewError("PathRejected")
            else:
                virtual[clean_rel] = None

        current_bytes = virtual[clean_rel]
        before_sha = hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None

        expected_sha = item.get("base_sha256")
        if expected_sha is not None and expected_sha != "":
            if not isinstance(expected_sha, str):
                raise ReviewError("InvalidToolArguments")
            if current_bytes is None and op != "create":
                raise ReviewError("FileMissing")
            actual_sha = before_sha if before_sha is not None else hashlib.sha256(b"").hexdigest()
            if actual_sha != expected_sha:
                raise ReviewError("DigestMismatch")

        if op == "edit":
            if current_bytes is None:
                raise ReviewError("FileMissing")
            current_text = _decode_utf8_bytes(current_bytes)
            edits = item.get("edits")
            if edits is None and "old_text" in item and "new_text" in item:
                edits = [{"old_text": item["old_text"], "new_text": item["new_text"]}]
            if not isinstance(edits, list) or not edits:
                raise ReviewError("InvalidToolArguments")
            updated = current_text
            for patch in edits:
                if not isinstance(patch, dict):
                    raise ReviewError("InvalidToolArguments")
                old_text = patch.get("old_text")
                new_text = patch.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str):
                    raise ReviewError("NotText")
                if old_text == "":
                    raise ReviewError("PatchTargetNotFound")
                occurrences = updated.count(old_text)
                if occurrences == 0:
                    raise ReviewError("PatchTargetNotFound")
                if occurrences > 1:
                    raise ReviewError("PatchAmbiguous")
                updated = updated.replace(old_text, new_text, 1)
            if updated == current_text:
                raise ReviewError("NoChange")
            if "\x00" in updated:
                raise ReviewError("NotText")
            encoded = updated.encode("utf-8")
            if len(encoded) > _MAX_FILE:
                raise ReviewError("FileTooLarge")
            virtual[clean_rel] = encoded
            results.append(
                {
                    "path": clean_rel,
                    "op": op,
                    "before_sha256": before_sha,
                    "after_sha256": hashlib.sha256(encoded).hexdigest(),
                    "byte_length": len(encoded),
                }
            )
        elif op == "create":
            if not allow_create:
                raise ReviewError("CreateNotAllowed")
            if current_bytes is not None:
                raise ReviewError("AlreadyExists")
            content = item.get("content")
            if not isinstance(content, str) or "\x00" in content:
                raise ReviewError("NotText")
            encoded = content.encode("utf-8")
            if len(encoded) > _MAX_FILE:
                raise ReviewError("FileTooLarge")
            virtual[clean_rel] = encoded
            results.append(
                {
                    "path": clean_rel,
                    "op": op,
                    "before_sha256": None,
                    "after_sha256": hashlib.sha256(encoded).hexdigest(),
                    "byte_length": len(encoded),
                }
            )
        elif op == "delete":
            if not allow_delete:
                raise ReviewError("DeleteNotAllowed")
            if current_bytes is None:
                raise ReviewError("FileMissing")
            virtual[clean_rel] = None
            results.append(
                {
                    "path": clean_rel,
                    "op": op,
                    "before_sha256": before_sha,
                    "after_sha256": None,
                    "byte_length": 0,
                }
            )
        elif op == "revert":
            if base_dest.is_file():
                base_bytes = base_dest.read_bytes()
                if len(base_bytes) > _MAX_FILE:
                    raise ReviewError("FileTooLarge")
                virtual[clean_rel] = base_bytes
                results.append(
                    {
                        "path": clean_rel,
                        "op": op,
                        "before_sha256": before_sha,
                        "after_sha256": hashlib.sha256(base_bytes).hexdigest(),
                        "byte_length": len(base_bytes),
                    }
                )
            else:
                if current_bytes is None:
                    raise ReviewError("FileMissing")
                virtual[clean_rel] = None
                results.append(
                    {
                        "path": clean_rel,
                        "op": op,
                        "before_sha256": before_sha,
                        "after_sha256": None,
                        "byte_length": 0,
                    }
                )

    final_state = dict(existing_work)
    for clean_rel, payload in virtual.items():
        if payload is None:
            final_state.pop(clean_rel, None)
        else:
            final_state[clean_rel] = payload
    if len(final_state) > _MAX_FILES or sum(len(b) for b in final_state.values()) > _MAX_TOTAL:
        raise ReviewError("CopyLimit")

    snapshots: dict[str, bytes | None] = {
        clean_rel: (targets[clean_rel].read_bytes() if targets[clean_rel].is_file() else None)
        for clean_rel in virtual
    }
    try:
        for clean_rel, payload in virtual.items():
            dest = targets[clean_rel]
            if payload is None:
                if dest.exists():
                    dest.unlink()
                _prune_empty_dirs(work, clean_rel.split("/"))
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
    except OSError as exc:
        for clean_rel, original in snapshots.items():
            dest = targets[clean_rel]
            try:
                if original is None:
                    if dest.exists():
                        dest.unlink()
                    _prune_empty_dirs(work, clean_rel.split("/"))
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(original)
            except OSError:
                pass
        raise ReviewError("WriteFailed", str(exc)) from exc

    return {"applied": results, "count": len(results)}


def bulk_replace_work(
    ws_root: Path,
    find: str,
    replace: str,
    *,
    mode: str = "literal",
    extensions: list[str] | None = None,
    path_prefix: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Literal or regex find-and-replace across ``work/`` files with atomic apply."""
    work = _work_dir(ws_root)
    _base_dir(ws_root)
    if mode not in {"literal", "regex"}:
        raise ReviewError("InvalidToolArguments")
    if not isinstance(find, str) or not find or len(find) > 200:
        raise ReviewError("InvalidSearch")
    if not isinstance(replace, str) or "\x00" in replace:
        raise ReviewError("NotText")
    if type(dry_run) is not bool:
        raise ReviewError("InvalidToolArguments")

    compiled: re.Pattern[str] | None = None
    if mode == "regex":
        try:
            compiled = re.compile(find)
        except re.error as exc:
            raise ReviewError("InvalidSearch", exc.msg) from exc

    try:
        allowed_exts = _normalize_extensions(extensions, allowed=_TEXT_EXTENSIONS)
    except ValueError as exc:
        raise ReviewError("InvalidToolArguments", str(exc)) from exc

    clean_prefix = ""
    if path_prefix is not None and path_prefix != "":
        clean_prefix = "/".join(_validate_rel_dir(path_prefix))

    work_files = dict(iter_files(work))
    matched_changes: list[dict[str, Any]] = []
    file_stats: list[dict[str, Any]] = []
    diff_chunks: list[str] = []
    total_matches = 0

    for rel in sorted(work_files):
        if sensitive_relative(rel):
            continue
        if clean_prefix and rel != clean_prefix and not rel.startswith(clean_prefix + "/"):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix not in _TEXT_EXTENSIONS:
            continue
        if allowed_exts is not None and suffix not in allowed_exts:
            continue
        raw_bytes = work_files[rel]
        if len(raw_bytes) > _MAX_FILE or b"\x00" in raw_bytes:
            continue
        try:
            current_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue

        if compiled is not None:
            try:
                updated, count = compiled.subn(replace, current_text)
            except re.error as exc:
                raise ReviewError("InvalidSearch", exc.msg) from exc
        else:
            count = current_text.count(find)
            updated = current_text.replace(find, replace) if count > 0 else current_text

        if count <= 0 or updated == current_text:
            continue
        if "\x00" in updated:
            raise ReviewError("NotText")
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_FILE:
            raise ReviewError("FileTooLarge")

        before_sha = hashlib.sha256(raw_bytes).hexdigest()
        after_sha = hashlib.sha256(encoded).hexdigest()
        total_matches += count
        file_stats.append(
            {
                "path": rel,
                "matches": count,
                "before_sha256": before_sha,
                "after_sha256": after_sha,
            }
        )
        matched_changes.append(
            {
                "op": "edit",
                "path": rel,
                "base_sha256": before_sha,
                "edits": [{"old_text": current_text, "new_text": updated}],
            }
        )
        diff_chunks.extend(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=2,
            )
        )

    diff_preview, truncated = _bounded_diff_text(diff_chunks)
    if not dry_run and matched_changes:
        apply_work_changes(ws_root, matched_changes, allow_create=False, allow_delete=False)

    return {
        "dry_run": dry_run,
        "mode": mode,
        "total_matches": total_matches,
        "files_changed": len(file_stats),
        "files": file_stats,
        "diff": diff_preview if dry_run else "",
        "truncated": truncated if dry_run else False,
    }


def scaffold_work(
    ws_root: Path,
    template: str,
    component_slug: str | None = None,
) -> dict[str, Any]:
    """Create starter theme/plugin/WooCommerce HPOS files safely in ``work/``."""
    work = _work_dir(ws_root)
    _base_dir(ws_root)
    if template not in _SCAFFOLD_TEMPLATES:
        raise ReviewError("InvalidToolArguments")

    manifest = _read_manifest(ws_root)
    slug = component_slug if component_slug is not None else str(manifest.get("work_slug") or manifest.get("slug") or "")
    if component_slug is not None and (not isinstance(component_slug, str) or not SLUG.fullmatch(component_slug)):
        raise ReviewError("SlugRejected")
    if slug and not SLUG.fullmatch(slug):
        raise ReviewError("SlugRejected")

    changes: list[dict[str, Any]] = []
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    if template == "child_theme_basics":
        parent = slug[:-6] if slug.endswith("-child") and len(slug) > 6 else "twentytwentyfive"
        if not SLUG.fullmatch(parent):
            parent = "twentytwentyfive"
        style_target = _check_no_symlink(work, ["style.css"])
        if style_target.exists():
            skipped.append("style.css")
        else:
            style_content = (
                "/*\n"
                "Theme Name: Azom Child\n"
                f"Template: {parent}\n"
                "Version: 0.1.0\n"
                "*/\n"
            )
            changes.append({"op": "create", "path": "style.css", "content": style_content})
            created.append("style.css")

        func_target = _check_no_symlink(work, ["functions.php"])
        if func_target.exists():
            skipped.append("functions.php")
        else:
            func_content = (
                "<?php\n"
                "/**\n"
                " * Child theme functions and asset enqueues.\n"
                " */\n\n"
                "defined('ABSPATH') || exit;\n\n"
                "add_action('wp_enqueue_scripts', static function (): void {\n"
                "    wp_enqueue_style(\n"
                "        'azom-child-style',\n"
                "        get_stylesheet_uri(),\n"
                "        [],\n"
                "        wp_get_theme()->get('Version')\n"
                "    );\n"
                "});\n"
            )
            changes.append({"op": "create", "path": "functions.php", "content": func_content})
            created.append("functions.php")

    elif template == "site_plugin_basics":
        if not slug:
            raise ReviewError("SlugRejected")
        main_rel = f"{slug}.php"
        main_target = _check_no_symlink(work, [main_rel])
        if main_target.exists():
            skipped.append(main_rel)
        else:
            main_content = (
                "<?php\n"
                "/**\n"
                f" * Plugin Name: Azom Site ({slug})\n"
                " * Description: Site-specific customisations.\n"
                " * Version: 0.1.0\n"
                " */\n\n"
                "defined('ABSPATH') || exit;\n"
            )
            changes.append({"op": "create", "path": main_rel, "content": main_content})
            created.append(main_rel)

    elif template == "woo_compat_declaration":
        compat_rel = "includes/woo-hpos-compat.php"
        compat_target = _check_no_symlink(work, compat_rel.split("/"))
        if compat_target.exists():
            skipped.append(compat_rel)
        else:
            compat_content = (
                "<?php\n"
                "/**\n"
                " * WooCommerce High-Performance Order Storage (HPOS) compatibility.\n"
                " *\n"
                " * WC tested up to: 9.6\n"
                " */\n\n"
                "defined('ABSPATH') || exit;\n\n"
                "if (!function_exists('azom_declare_woo_hpos_compat')) {\n"
                "    function azom_declare_woo_hpos_compat(string $plugin_file = __FILE__): void {\n"
                "        add_action('before_woocommerce_init', static function () use ($plugin_file): void {\n"
                "            if (class_exists(\\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::class)) {\n"
                "                \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility(\n"
                "                    'custom_order_tables',\n"
                "                    $plugin_file,\n"
                "                    true\n"
                "                );\n"
                "            }\n"
                "        });\n"
                "    }\n"
                "}\n"
            )
            changes.append({"op": "create", "path": compat_rel, "content": compat_content})
            created.append(compat_rel)

        kind = str(manifest.get("kind") or "")
        wire_snippet = (
            "\nrequire_once __DIR__ . '/includes/woo-hpos-compat.php';\n"
            "add_action('before_woocommerce_init', static function (): void {\n"
            "    if (class_exists(\\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::class)) {\n"
            "        \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);\n"
            "    }\n"
            "});\n"
        )
        if slug and kind != "theme":
            main_rel = f"{slug}.php"
            main_target = _check_no_symlink(work, [main_rel])
            if main_target.is_file():
                existing_bytes = main_target.read_bytes()
                existing_text = _decode_utf8_bytes(existing_bytes)
                if "custom_order_tables" in existing_text or "woo-hpos-compat.php" in existing_text:
                    skipped.append(main_rel)
                else:
                    if not existing_text.strip():
                        raise ReviewError("NotText")
                    new_text = existing_text.rstrip() + "\n" + wire_snippet
                    changes.append(
                        {
                            "op": "edit",
                            "path": main_rel,
                            "base_sha256": hashlib.sha256(existing_bytes).hexdigest(),
                            "edits": [{"old_text": existing_text, "new_text": new_text}],
                        }
                    )
                    updated.append(main_rel)
            else:
                main_content = (
                    "<?php\n"
                    "/**\n"
                    f" * Plugin Name: Azom Site ({slug})\n"
                    " * Description: Site-specific customisations.\n"
                    " * Version: 0.1.0\n"
                    " * WC tested up to: 9.6\n"
                    " */\n\n"
                    "defined('ABSPATH') || exit;\n"
                    + wire_snippet
                )
                changes.append({"op": "create", "path": main_rel, "content": main_content})
                created.append(main_rel)

    if changes:
        apply_work_changes(ws_root, changes, allow_create=True, allow_delete=False)

    return {
        "template": template,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


def theme_json_work(
    ws_root: Path,
    operations: list[dict[str, Any]],
    *,
    seed_json: str | None = None,
) -> dict[str, Any]:
    """Apply structured JSON pointer operations to ``work/theme.json``."""
    _work, dest, clean_rel = _resolve_work_target(ws_root, "theme.json")
    base = _base_dir(ws_root)
    base_dest = _check_no_symlink(base, ["theme.json"])

    existed_in_work = dest.is_file()
    if dest.exists() and not existed_in_work:
        raise ReviewError("PathRejected")
    before_sha: str | None = None
    raw_json: str | None = None
    if existed_in_work:
        raw_bytes = dest.read_bytes()
        before_sha = hashlib.sha256(raw_bytes).hexdigest()
        raw_json = _decode_utf8_bytes(raw_bytes)
    elif base_dest.is_file():
        raw_json = _decode_utf8_bytes(base_dest.read_bytes())
    elif seed_json is not None:
        raw_json = seed_json

    try:
        result = apply_theme_json_operations(raw_json, operations)
    except ValueError as exc:
        raise ReviewError("InvalidThemeJson", str(exc)) from exc

    encoded = result["updated_json"].encode("utf-8")
    if len(encoded) > _MAX_FILE:
        raise ReviewError("FileTooLarge")

    existing_work = dict(iter_files(_work))
    existing_work[clean_rel] = encoded
    if len(existing_work) > _MAX_FILES or sum(len(b) for b in existing_work.values()) > _MAX_TOTAL:
        raise ReviewError("CopyLimit")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(encoded)
    return {
        "path": clean_rel,
        "before_sha256": before_sha,
        "after_sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
        "changed_paths": result["changed_paths"],
        "version": result["version"],
        "created": not existed_in_work,
    }
