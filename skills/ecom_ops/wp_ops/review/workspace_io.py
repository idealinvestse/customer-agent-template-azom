"""Per-plan WordPress review workspace paths, hashes, and manifest I/O.

Nothing here writes to a live site, starts a shell, or creates a container.
``base/`` is sealed through this API. Windows does not rely on chmod.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.file_paths import sensitive_relative
from ecom_ops.wp_ops.store import atomic_write_text

PLAN_ID = re.compile(r"^wr-[a-f0-9]{12}$")
SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
RESEARCH_ID = re.compile(r"^res-[0-9a-f]{12}$")

_MAX_FILE = 65536
_MAX_FILES = 200
_MAX_TOTAL = 8 * 1024 * 1024
_MAX_ENTRIES = 256
_MAX_CHANGES = 50
_MAX_RESEARCH_SNAPSHOT_BYTES = 32 * 1024
_TEXT_EXTENSIONS = frozenset(
    {".php", ".css", ".scss", ".js", ".json", ".txt", ".md", ".html", ".pot", ".po"}
)
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_CONTROL_TAG_RE = re.compile(r"<\s*/?\s*(?:tool_call|clarify)\b[^>]*>", re.IGNORECASE)
_DELIMITER_SPOOF_RE = re.compile(r"<<<\s*(?:END_)?UNTRUSTED_[A-Z0-9_]+[^>]*>>>", re.IGNORECASE)
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
_BIDI_OVERRIDE_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_RESEARCH_SECRET_ENV_KEYS = (
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "SEARXNG_API_KEY",
    "PARALLEL_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
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
    "research_ids",
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
    if not isinstance(body.get("research_ids"), list):
        body["research_ids"] = []
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


def _active_research_secrets() -> list[str]:
    secrets: list[str] = []
    for key in _RESEARCH_SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            secrets.append(val)
    for key, val in os.environ.items():
        if key.startswith("WP_SECRET_") and isinstance(val, str) and len(val.strip()) >= 8:
            secrets.append(val.strip())
    return secrets


def _scrub_research_text(text: str, *, max_chars: int = 8000, extra_secrets: list[str] | None = None) -> str:
    cleaned = str(text or "").replace("\x00", "")
    cleaned = _ZERO_WIDTH_RE.sub("", cleaned)
    cleaned = _BIDI_OVERRIDE_RE.sub("", cleaned)
    cleaned = _CONTROL_TAG_RE.sub("", cleaned)
    cleaned = _DELIMITER_SPOOF_RE.sub("", cleaned)
    cleaned = redact_secret_text(cleaned, extra_secrets=extra_secrets)
    cleaned = cleaned.strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        if max_chars <= 3:
            return cleaned[:max_chars]
        return cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned


def _scrub_snapshot_value(
    value: Any,
    *,
    max_chars: int,
    max_items: int,
    extra_secrets: list[str],
    depth: int = 0,
) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _scrub_research_text(value, max_chars=max_chars, extra_secrets=extra_secrets)
    if isinstance(value, (list, tuple)):
        return [
            _scrub_snapshot_value(
                item,
                max_chars=max_chars,
                max_items=max_items,
                extra_secrets=extra_secrets,
                depth=depth + 1,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in list(value.items())[:100]:
            key = _scrub_research_text(str(raw_key), max_chars=120, extra_secrets=extra_secrets)
            if not key:
                continue
            if key == "target_ref" and str(raw_val or "").strip().startswith("case:"):
                continue
            out[key] = _scrub_snapshot_value(
                raw_val,
                max_chars=max_chars,
                max_items=max_items,
                extra_secrets=extra_secrets,
                depth=depth + 1,
            )
        return out
    return _scrub_research_text(str(value), max_chars=max_chars, extra_secrets=extra_secrets)


def scrub_research_snapshot(
    snapshot: dict[str, Any],
    *,
    max_bytes: int = _MAX_RESEARCH_SNAPSHOT_BYTES,
) -> dict[str, Any]:
    """Scrub secrets/control tags from a research snapshot and bound it to ``<= max_bytes``."""
    if not isinstance(snapshot, dict):
        raise ReviewError("InvalidToolArguments")
    cap = max(1024, min(_MAX_RESEARCH_SNAPSHOT_BYTES, int(max_bytes)))
    extra_secrets = _active_research_secrets()
    scrubbed = _scrub_snapshot_value(
        snapshot,
        max_chars=8000,
        max_items=50,
        extra_secrets=extra_secrets,
    )
    assert isinstance(scrubbed, dict)

    def _encoded_len(obj: dict[str, Any]) -> int:
        return len(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    if _encoded_len(scrubbed) <= cap:
        return scrubbed

    trimmed = dict(scrubbed)
    trimmed["truncated"] = True
    for drop_or_trim in (
        ("markdown", 3000),
        ("markdown", 1000),
        ("markdown", 0),
        ("findings", 10),
        ("findings", 5),
        ("findings", 0),
        ("citations", 10),
        ("citations", 5),
        ("summary", 2000),
        ("summary", 800),
        ("recommendations", 10),
        ("recommendations", 5),
        ("gaps", 5),
        ("gaps", 0),
    ):
        key, limit = drop_or_trim
        if key not in trimmed:
            continue
        if limit == 0:
            trimmed.pop(key, None)
        elif isinstance(trimmed[key], str):
            trimmed[key] = _scrub_research_text(trimmed[key], max_chars=limit, extra_secrets=extra_secrets)
        elif isinstance(trimmed[key], list):
            trimmed[key] = trimmed[key][:limit]
        if _encoded_len(trimmed) <= cap:
            return trimmed

    for str_cap, item_cap in ((512, 10), (160, 5), (80, 3)):
        compacted = _scrub_snapshot_value(
            trimmed,
            max_chars=str_cap,
            max_items=item_cap,
            extra_secrets=extra_secrets,
        )
        assert isinstance(compacted, dict)
        compacted["truncated"] = True
        if _encoded_len(compacted) <= cap:
            return compacted

    raise ReviewError("FileTooLarge")


def _research_dir(ws_root: Path, *, create: bool = False) -> Path | None:
    root = Path(ws_root)
    if root.is_symlink():
        raise ReviewError("PathRejected")
    resolved_root = root.resolve()
    raw = root / "research"
    if raw.is_symlink():
        raise ReviewError("PathRejected")
    if not raw.exists():
        if not create:
            return None
        raw.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(raw, 0o700)
        except OSError:
            pass
    if not raw.is_dir():
        raise ReviewError("PathRejected")
    resolved_dir = raw.resolve()
    if resolved_dir.parent != resolved_root or resolved_dir.is_symlink():
        raise ReviewError("PathRejected")
    return resolved_dir


def _resolve_research_file(ws_root: Path, research_id: str, *, create_dir: bool = False) -> Path | None:
    if not isinstance(research_id, str):
        raise ReviewError("PathRejected")
    clean_id = research_id.strip()
    if RESEARCH_ID.fullmatch(clean_id) is None:
        raise ReviewError("PathRejected")
    res_dir = _research_dir(ws_root, create=create_dir)
    if res_dir is None:
        return None
    raw_file = res_dir / f"{clean_id}.json"
    if raw_file.is_symlink():
        raise ReviewError("PathRejected")
    resolved = raw_file.resolve()
    if resolved.parent != res_dir or resolved.is_symlink():
        raise ReviewError("PathRejected")
    return resolved


def write_research_snapshot(
    ws_root: Path,
    research_id: str,
    summary_snapshot: dict[str, Any],
) -> Path:
    """Safely write a scrubbed ``<= 32 KiB`` snapshot to sibling ``research/<res_id>.json``."""
    dest = _resolve_research_file(ws_root, research_id, create_dir=True)
    assert dest is not None
    scrubbed = scrub_research_snapshot(summary_snapshot)
    payload = json.dumps(scrubbed, ensure_ascii=False, indent=2)
    if len(payload.encode("utf-8")) > _MAX_RESEARCH_SNAPSHOT_BYTES:
        raise ReviewError("FileTooLarge")
    atomic_write_text(dest, payload, mode=0o600)
    return dest


def read_research_snapshot(ws_root: Path, research_id: str) -> dict[str, Any] | None:
    """Safely read one scrubbed snapshot from sibling ``research/<res_id>.json``."""
    target = _resolve_research_file(ws_root, research_id, create_dir=False)
    if target is None or not target.is_file():
        return None
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_RESEARCH_SNAPSHOT_BYTES or b"\x00" in raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def copy_research_dir(src_root: Path, dst_root: Path) -> list[str]:
    """Safely copy sibling ``research/res-*.json`` snapshots into a forked workspace."""
    if Path(src_root).is_symlink() or Path(dst_root).is_symlink():
        raise ReviewError("PathRejected")
    src_dir = _research_dir(src_root, create=False)
    if src_dir is None:
        return []
    copied: list[str] = []
    for entry in sorted(src_dir.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            raise ReviewError("PathRejected")
        resolved = entry.resolve()
        if resolved.parent != src_dir or resolved.is_symlink():
            raise ReviewError("PathRejected")
        if not resolved.is_file() or resolved.suffix != ".json" or RESEARCH_ID.fullmatch(resolved.stem) is None:
            continue
        snapshot = read_research_snapshot(src_root, resolved.stem)
        if snapshot is None:
            continue
        write_research_snapshot(dst_root, resolved.stem, snapshot)
        copied.append(resolved.stem)
    return copied


__all__ = [
    "PLAN_ID",
    "RESEARCH_ID",
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
    "_MAX_RESEARCH_SNAPSHOT_BYTES",
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
    "copy_research_dir",
    "delete_work",
    "iter_files",
    "new_plan_id",
    "place_work_file",
    "read_diff",
    "read_manifest",
    "read_research_snapshot",
    "restore_work_from_base",
    "scrub_research_snapshot",
    "site_plugin_slug",
    "tree_hash",
    "unified_diff_text",
    "workspace_root",
    "write_diff",
    "write_manifest",
    "write_research_snapshot",
    "write_tree",
]
