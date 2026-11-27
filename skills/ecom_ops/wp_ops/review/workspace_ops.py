"""Workspace file read, list, diff, patch, create, multi-change, bulk replace, and theme.json ops."""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.file_paths import sensitive_relative
from ecom_ops.wp_ops.review.workspace_io import (
    _MAX_CHANGES,
    _MAX_FILE,
    _MAX_FILES,
    _MAX_TOTAL,
    _TEXT_EXTENSIONS,
    ReviewError,
    _base_dir,
    _bounded_diff_text,
    _check_no_symlink,
    _decode_utf8_bytes,
    _prune_empty_dirs,
    _resolve_work_target,
    _validate_rel_dir,
    _work_dir,
    iter_files,
)
from ecom_ops.wp_ops.search import _normalize_extensions, validate_safe_regex
from ecom_ops.wp_ops.theme_json import apply_theme_json_operations


def _encode_text_payload(text: Any) -> bytes:
    if not isinstance(text, str) or "\x00" in text:
        raise ReviewError("NotText")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_FILE:
        raise ReviewError("FileTooLarge")
    return encoded


def _apply_edit_patches(current_text: str, edits: list[dict[str, Any]]) -> bytes:
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
    return _encode_text_payload(updated)


def patch_work_file(
    root: Path,
    relative: str,
    old_text: str,
    new_text: str,
    *,
    base_sha256: str | None = None,
) -> dict[str, Any]:
    """Patch one unique occurrence of ``old_text`` inside ``work/``."""
    _work, dest, clean_rel = _resolve_work_target(root, relative)
    if not dest.is_file():
        raise ReviewError("FileMissing")
    raw_bytes = dest.read_bytes()
    before_sha = hashlib.sha256(raw_bytes).hexdigest()
    if base_sha256 is not None and base_sha256 != "" and before_sha != base_sha256:
        raise ReviewError("DigestMismatch")
    current = _decode_utf8_bytes(raw_bytes)
    encoded = _apply_edit_patches(current, [{"old_text": old_text, "new_text": new_text}])
    dest.write_bytes(encoded)
    return {
        "path": clean_rel,
        "before_sha256": before_sha,
        "after_sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
    }


def create_work_file(root: Path, relative: str, content: str) -> dict[str, Any]:
    """Create one new text file inside ``work/`` without overwriting existing files."""
    _work, dest, clean_rel = _resolve_work_target(root, relative)
    if dest.exists() or dest.is_symlink():
        raise ReviewError("AlreadyExists")
    encoded = _encode_text_payload(content)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(encoded)
    return {
        "path": clean_rel,
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
    max_page_bytes = 20480
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
        rows.append({"path": rel, "size": size, "status": status, "sha256": sha})
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
        change = "created" if old_b is None else "deleted" if new_b is None else "modified"
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


def _apply_one_virtual_change(
    *,
    item: dict[str, Any],
    clean_rel: str,
    current_bytes: bytes | None,
    before_sha: str | None,
    base_dest: Path,
    allow_create: bool,
    allow_delete: bool,
) -> tuple[bytes | None, dict[str, Any]]:
    op = str(item.get("op") or "")
    if op == "edit":
        if current_bytes is None:
            raise ReviewError("FileMissing")
        current_text = _decode_utf8_bytes(current_bytes)
        edits = item.get("edits")
        if edits is None and "old_text" in item and "new_text" in item:
            edits = [{"old_text": item["old_text"], "new_text": item["new_text"]}]
        if not isinstance(edits, list) or not edits:
            raise ReviewError("InvalidToolArguments")
        encoded = _apply_edit_patches(current_text, edits)
        return encoded, {
            "path": clean_rel,
            "op": op,
            "before_sha256": before_sha,
            "after_sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_length": len(encoded),
        }
    if op == "create":
        if not allow_create:
            raise ReviewError("CreateNotAllowed")
        if current_bytes is not None:
            raise ReviewError("AlreadyExists")
        encoded = _encode_text_payload(item.get("content"))
        return encoded, {
            "path": clean_rel,
            "op": op,
            "before_sha256": None,
            "after_sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_length": len(encoded),
        }
    if op == "delete":
        if not allow_delete:
            raise ReviewError("DeleteNotAllowed")
        if current_bytes is None:
            raise ReviewError("FileMissing")
        return None, {
            "path": clean_rel,
            "op": op,
            "before_sha256": before_sha,
            "after_sha256": None,
            "byte_length": 0,
        }
    if op == "revert":
        if base_dest.is_file():
            base_bytes = base_dest.read_bytes()
            if len(base_bytes) > _MAX_FILE:
                raise ReviewError("FileTooLarge")
            return base_bytes, {
                "path": clean_rel,
                "op": op,
                "before_sha256": before_sha,
                "after_sha256": hashlib.sha256(base_bytes).hexdigest(),
                "byte_length": len(base_bytes),
            }
        if current_bytes is None:
            raise ReviewError("FileMissing")
        return None, {
            "path": clean_rel,
            "op": op,
            "before_sha256": before_sha,
            "after_sha256": None,
            "byte_length": 0,
        }
    raise ReviewError("InvalidToolArguments")


def _commit_virtual_writes(
    work: Path,
    virtual: dict[str, bytes | None],
    targets: dict[str, Path],
) -> None:
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
    if not isinstance(changes, list) or not changes or len(changes) > _MAX_CHANGES:
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

        new_payload, row = _apply_one_virtual_change(
            item=item,
            clean_rel=clean_rel,
            current_bytes=current_bytes,
            before_sha=before_sha,
            base_dest=base_dest,
            allow_create=allow_create,
            allow_delete=allow_delete,
        )
        virtual[clean_rel] = new_payload
        results.append(row)

    final_state = dict(existing_work)
    for clean_rel, payload in virtual.items():
        if payload is None:
            final_state.pop(clean_rel, None)
        else:
            final_state[clean_rel] = payload
    if len(final_state) > _MAX_FILES or sum(len(b) for b in final_state.values()) > _MAX_TOTAL:
        raise ReviewError("CopyLimit")

    _commit_virtual_writes(work, virtual, targets)
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
            validate_safe_regex(find)
            compiled = re.compile(find)
        except (ValueError, re.error) as exc:
            raise ReviewError("InvalidSearch", str(exc)) from exc

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
        encoded = _encode_text_payload(updated)
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

    if len(matched_changes) > _MAX_CHANGES:
        raise ReviewError("InvalidToolArguments")
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


def theme_json_work(
    ws_root: Path,
    operations: list[dict[str, Any]],
    *,
    seed_json: str | None = None,
) -> dict[str, Any]:
    """Apply structured JSON pointer operations to ``work/theme.json``."""
    work, dest, clean_rel = _resolve_work_target(ws_root, "theme.json")
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

    existing_work = dict(iter_files(work))
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


__all__ = [
    "apply_work_changes",
    "bulk_replace_work",
    "create_work_file",
    "list_work_changes",
    "list_work_files",
    "patch_work_file",
    "read_work_file",
    "theme_json_work",
    "work_diff",
]
