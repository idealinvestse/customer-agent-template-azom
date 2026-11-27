"""Per-change backup snapshot vault and SQLite WAL change ledger for WordPress & WooCommerce.

Every mutating action (Woo product/order, WP post/page/draft/settings/plugin,
and theme/plugin file deploy) records an immutable before/after snapshot under
``AZOM_DATA_DIR/wp_snapshots/<site_id>/<change_id>/`` with SHA-256 verification
and an indexed row in ``AZOM_DATA_DIR/wp_change_ledger.sqlite3``.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ecom_ops.security import redact_secrets
from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode
from ecom_ops.wp_ops.credentials import redact_secret_text

logger = logging.getLogger(__name__)

CHANGE_ID_RE = re.compile(r"^chg-[a-f0-9]{12}$")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SAFE_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,62}$")
_DB_NAME = "wp_change_ledger.sqlite3"
_SNAPSHOTS_DIR = "wp_snapshots"
_MAX_DIFF_BYTES = 131_072
_PROCESS_LOCK = threading.Lock()


def valid_change_id(value: str) -> bool:
    return bool(CHANGE_ID_RE.fullmatch(str(value or "").strip()))


def valid_batch_id(value: str) -> bool:
    return bool(BATCH_ID_RE.fullmatch(str(value or "").strip()))


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_bundle_digest(entries: list[dict[str, Any]], files: dict[str, bytes]) -> str:
    manifest: list[dict[str, Any]] = []
    for item in sorted(entries, key=lambda r: str(r.get("path") or "")):
        rel = str(item.get("path") or "")
        blob = files.get(rel)
        manifest.append(
            {
                "path": rel,
                "present": blob is not None,
                "sha256": _sha256_bytes(blob) if blob is not None else None,
                "op": str(item.get("op") or ""),
            }
        )
    return _sha256_bytes(_canonical_json_bytes(manifest))


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(tmp, flags, mode)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        for attempt in range(5):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _safe_rel(rel_path: str) -> str:
    clean = str(rel_path or "").replace("\\", "/").strip("/")
    parts = [p for p in clean.split("/") if p]
    if not parts or any(p in {".", ".."} for p in parts):
        raise ValueError(f"Unsafe relative snapshot path: {rel_path!r}")
    return "/".join(parts)


def resolve_ledger_site_id(
    *,
    site_id: str | None = None,
    domain: str | None = None,
) -> str:
    """Resolve a canonical site_id for ledger recording without failing on missing registry."""
    if site_id and _SAFE_SITE_RE.fullmatch(site_id.strip().lower()):
        return site_id.strip().lower()
    dom = str(domain or "").strip().lower().lstrip(".")
    try:
        from ecom_ops.wp_ops.registry import load_sites

        sites = load_sites()
        if dom:
            matches = [
                s
                for s in sites
                if (urlparse(str(s.canonical_url or "")).hostname or "").lower().endswith(f".{dom}")
                or s.id == f"azom-{dom}"
            ]
            for s in matches:
                if s.environment == "production":
                    return s.id
            if matches:
                return matches[0].id
            return f"azom-{dom}"
        if sites:
            for s in sites:
                if s.environment == "production":
                    return s.id
            return sites[0].id
    except Exception:
        pass
    if dom:
        return f"azom-{dom}"
    return "mock-shop" if is_mock_mode() else "azom-no"


class SnapshotStore:
    """Content-addressed pre/post snapshot store under ``AZOM_DATA_DIR/wp_snapshots``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.root = self.data_dir / _SNAPSHOTS_DIR

    def change_dir(self, site_id: str, change_id: str) -> Path:
        sid = str(site_id or "").strip().lower()
        cid = str(change_id or "").strip().lower()
        if not _SAFE_SITE_RE.fullmatch(sid) or not CHANGE_ID_RE.fullmatch(cid):
            raise ValueError("Invalid site_id or change_id")
        base = self.root.resolve()
        target = (base / sid / cid).resolve()
        if base not in target.parents:
            raise ValueError("Snapshot path escaped root")
        return target

    def write_entity_snapshot(
        self,
        site_id: str,
        change_id: str,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> tuple[str, str]:
        cdir = self.change_dir(site_id, change_id)
        clean_before = redact_secrets(before or {})
        clean_after = redact_secrets(after or {})
        before_bytes = _canonical_json_bytes(clean_before)
        after_bytes = _canonical_json_bytes(clean_after)
        _atomic_write_bytes(cdir / "before.json", before_bytes)
        _atomic_write_bytes(cdir / "after.json", after_bytes)
        meta = {
            "kind": "entity",
            "before_sha256": _sha256_bytes(before_bytes),
            "after_sha256": _sha256_bytes(after_bytes),
        }
        _atomic_write_bytes(cdir / "snapshot_meta.json", _canonical_json_bytes(meta))
        return meta["before_sha256"], meta["after_sha256"]

    def write_file_snapshot(
        self,
        site_id: str,
        change_id: str,
        *,
        entries: list[dict[str, Any]],
        before_files: dict[str, bytes],
        after_files: dict[str, bytes],
    ) -> tuple[str, str]:
        cdir = self.change_dir(site_id, change_id)
        norm_in_before = {_safe_rel(k): v for k, v in before_files.items()}
        norm_in_after = {_safe_rel(k): v for k, v in after_files.items()}
        norm_entries: list[dict[str, Any]] = []
        norm_before: dict[str, bytes] = {}
        norm_after: dict[str, bytes] = {}
        for item in entries:
            rel = _safe_rel(str(item.get("path") or ""))
            op = str(item.get("op") or "updated")
            b_bytes = norm_in_before.get(rel)
            a_bytes = norm_in_after.get(rel)
            if b_bytes is not None:
                _atomic_write_bytes(cdir / "before_files" / rel, b_bytes)
                norm_before[rel] = b_bytes
            if a_bytes is not None:
                _atomic_write_bytes(cdir / "after_files" / rel, a_bytes)
                norm_after[rel] = a_bytes
            norm_entries.append(
                {
                    "path": rel,
                    "op": op,
                    "before_present": b_bytes is not None,
                    "before_sha256": _sha256_bytes(b_bytes) if b_bytes is not None else None,
                    "after_present": a_bytes is not None,
                    "after_sha256": _sha256_bytes(a_bytes) if a_bytes is not None else None,
                }
            )
        before_sha = _file_bundle_digest(norm_entries, norm_before)
        after_sha = _file_bundle_digest(norm_entries, norm_after)
        manifest = {
            "kind": "files",
            "entries": norm_entries,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
        }
        _atomic_write_bytes(cdir / "file_manifest.json", _canonical_json_bytes(manifest))
        _atomic_write_bytes(
            cdir / "snapshot_meta.json",
            _canonical_json_bytes(
                {"kind": "files", "before_sha256": before_sha, "after_sha256": after_sha}
            ),
        )
        return before_sha, after_sha

    def read_snapshot(self, site_id: str, change_id: str) -> dict[str, Any]:
        cdir = self.change_dir(site_id, change_id)
        meta_path = cdir / "snapshot_meta.json"
        if not meta_path.is_file():
            raise FileNotFoundError("snapshot_meta.json missing")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        kind = str(meta.get("kind") or "")
        if kind == "entity":
            before_bytes = (cdir / "before.json").read_bytes()
            after_bytes = (cdir / "after.json").read_bytes()
            return {
                "kind": "entity",
                "before": json.loads(before_bytes.decode("utf-8")),
                "after": json.loads(after_bytes.decode("utf-8")),
                "before_sha256": _sha256_bytes(before_bytes),
                "after_sha256": _sha256_bytes(after_bytes),
                "meta_before_sha256": meta.get("before_sha256"),
                "meta_after_sha256": meta.get("after_sha256"),
            }
        if kind == "files":
            manifest = json.loads((cdir / "file_manifest.json").read_text(encoding="utf-8"))
            entries = list(manifest.get("entries") or [])
            before_files: dict[str, bytes] = {}
            after_files: dict[str, bytes] = {}
            for item in entries:
                rel = _safe_rel(str(item.get("path") or ""))
                bp = cdir / "before_files" / rel
                ap = cdir / "after_files" / rel
                if item.get("before_present"):
                    if not bp.is_file() or bp.is_symlink():
                        raise FileNotFoundError(f"Missing before file {rel}")
                    before_files[rel] = bp.read_bytes()
                if item.get("after_present"):
                    if not ap.is_file() or ap.is_symlink():
                        raise FileNotFoundError(f"Missing after file {rel}")
                    after_files[rel] = ap.read_bytes()
            entries_digests_ok = True
            for item in entries:
                rel = _safe_rel(str(item.get("path") or ""))
                exp_b = _sha256_bytes(before_files[rel]) if rel in before_files else None
                exp_a = _sha256_bytes(after_files[rel]) if rel in after_files else None
                if "before_sha256" in item and item.get("before_sha256") != exp_b:
                    entries_digests_ok = False
                if "after_sha256" in item and item.get("after_sha256") != exp_a:
                    entries_digests_ok = False
            return {
                "kind": "files",
                "entries": entries,
                "before_files": before_files,
                "after_files": after_files,
                "before_sha256": _file_bundle_digest(entries, before_files),
                "after_sha256": _file_bundle_digest(entries, after_files),
                "meta_before_sha256": meta.get("before_sha256"),
                "meta_after_sha256": meta.get("after_sha256"),
                "entries_digests_ok": entries_digests_ok,
            }
        raise ValueError(f"Unsupported snapshot kind: {kind}")


class ChangeLedger:
    """SQLite WAL change ledger paired with content-addressed ``SnapshotStore``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.db_path = self.data_dir / _DB_NAME
        self.snapshots = SnapshotStore(self.data_dir)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with _PROCESS_LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wp_changes (
                        change_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        environment TEXT NOT NULL,
                        category TEXT NOT NULL,
                        transport TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        target_ref TEXT NOT NULL,
                        before_sha256 TEXT NOT NULL,
                        after_sha256 TEXT NOT NULL,
                        snapshot_verified INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'applied',
                        parent_change_id TEXT,
                        rolled_back_by TEXT,
                        created_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        batch_id TEXT DEFAULT ''
                    )
                    """
                )
                cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(wp_changes)").fetchall()
                }
                if "batch_id" not in cols:
                    conn.execute("ALTER TABLE wp_changes ADD COLUMN batch_id TEXT DEFAULT ''")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_wp_changes_site_created "
                    "ON wp_changes (site_id, created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_changes_batch_id "
                    "ON wp_changes (batch_id, created_at DESC)"
                )
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    def record_change(
        self,
        *,
        site_id: str,
        environment: str,
        category: str,
        transport: str,
        actor: str,
        summary: str,
        target_ref: str,
        before_data: dict[str, Any] | None = None,
        after_data: dict[str, Any] | None = None,
        file_entries: list[dict[str, Any]] | None = None,
        before_files: dict[str, bytes] | None = None,
        after_files: dict[str, bytes] | None = None,
        status: str = "applied",
        parent_change_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        change_id = "chg-" + uuid.uuid4().hex[:12]
        sid = str(site_id or "").strip().lower()
        if not _SAFE_SITE_RE.fullmatch(sid):
            sid = resolve_ledger_site_id(site_id=None)
        clean_batch_id = str(batch_id or "").strip()
        if clean_batch_id and not valid_batch_id(clean_batch_id):
            raise ValueError(f"Invalid batch_id: {batch_id!r}")
        if file_entries is not None:
            before_sha, after_sha = self.snapshots.write_file_snapshot(
                sid,
                change_id,
                entries=file_entries,
                before_files=before_files or {},
                after_files=after_files or {},
            )
        else:
            before_sha, after_sha = self.snapshots.write_entity_snapshot(
                sid,
                change_id,
                before=before_data or {},
                after=after_data or {},
            )
        created_at = datetime.now(UTC).isoformat()
        clean_summary = redact_secret_text(" ".join(str(summary or "").split())[:240])
        clean_meta = redact_secrets(metadata or {})
        meta_json = json.dumps(clean_meta, ensure_ascii=False, sort_keys=True)
        with _PROCESS_LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO wp_changes (
                        change_id, site_id, environment, category, transport, actor,
                        summary, target_ref, before_sha256, after_sha256,
                        snapshot_verified, status, parent_change_id, rolled_back_by,
                        created_at, metadata_json, batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        change_id,
                        sid,
                        str(environment or "unknown"),
                        str(category or "other"),
                        str(transport or "unknown"),
                        str(actor or "agent"),
                        clean_summary,
                        str(target_ref or ""),
                        before_sha,
                        after_sha,
                        str(status or "applied"),
                        parent_change_id,
                        created_at,
                        meta_json,
                        clean_batch_id,
                    ),
                )
        row = self.get_change(sid, change_id)
        assert row is not None
        return row

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        meta_raw = row["metadata_json"] or "{}"
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        keys = row.keys()
        batch_val = str(row["batch_id"] or "") if "batch_id" in keys else ""
        return {
            "change_id": row["change_id"],
            "site_id": row["site_id"],
            "environment": row["environment"],
            "category": row["category"],
            "transport": row["transport"],
            "actor": row["actor"],
            "summary": row["summary"],
            "target_ref": row["target_ref"],
            "before_sha256": row["before_sha256"],
            "after_sha256": row["after_sha256"],
            "snapshot_verified": bool(row["snapshot_verified"]),
            "status": row["status"],
            "parent_change_id": row["parent_change_id"],
            "rolled_back_by": row["rolled_back_by"],
            "created_at": row["created_at"],
            "metadata": meta if isinstance(meta, dict) else {},
            "batch_id": batch_val,
        }

    def list_changes(
        self,
        site_id: str | None = None,
        *,
        category: str | None = None,
        status: str | None = None,
        batch_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if site_id:
            clauses.append("site_id = ?")
            args.append(str(site_id).strip().lower())
        if category:
            clauses.append("category = ?")
            args.append(str(category).strip())
        if status:
            clauses.append("status = ?")
            args.append(str(status).strip())
        if batch_id:
            clauses.append("batch_id = ?")
            args.append(str(batch_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        lim = max(1, min(int(limit), 200))
        args.append(lim)
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT * FROM wp_changes {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                args,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_batch_changes(
        self,
        batch_id: str,
        *,
        site_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all changes belonging to ``batch_id`` in reverse chronological order."""
        clean_bid = str(batch_id or "").strip()
        if not clean_bid:
            return []
        clauses = ["batch_id = ?"]
        args: list[Any] = [clean_bid]
        if site_id:
            clauses.append("site_id = ?")
            args.append(str(site_id).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}"
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT * FROM wp_changes {where} ORDER BY created_at DESC, rowid DESC",
                args,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_change(self, site_id: str | None, change_id: str) -> dict[str, Any] | None:
        if not valid_change_id(change_id):
            return None
        with contextlib.closing(self._connect()) as conn, conn:
            if site_id:
                row = conn.execute(
                    "SELECT * FROM wp_changes WHERE site_id = ? AND change_id = ?",
                    (str(site_id).strip().lower(), change_id.strip().lower()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM wp_changes WHERE change_id = ?",
                    (change_id.strip().lower(),),
                ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def verify_snapshot(self, site_id: str, change_id: str) -> dict[str, Any]:
        row = self.get_change(site_id, change_id)
        if row is None:
            return {"ok": False, "error_class": "UnknownChange", "verified": False}
        try:
            snap = self.snapshots.read_snapshot(row["site_id"], row["change_id"])
        except Exception as exc:
            return {
                "ok": False,
                "error_class": "SnapshotMissing",
                "verified": False,
                "detail": str(exc)[:120],
            }
        before_ok = (
            snap.get("before_sha256") == row["before_sha256"]
            and snap.get("meta_before_sha256") == row["before_sha256"]
        )
        after_ok = (
            snap.get("after_sha256") == row["after_sha256"]
            and snap.get("meta_after_sha256") == row["after_sha256"]
        )
        entries_ok = bool(snap.get("entries_digests_ok", True))
        verified = bool(before_ok and after_ok and entries_ok)
        with _PROCESS_LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE wp_changes SET snapshot_verified = ? WHERE change_id = ?",
                    (1 if verified else 0, row["change_id"]),
                )
        return {
            "ok": verified,
            "verified": verified,
            "error_class": None if verified else "SnapshotCorrupted",
            "kind": snap.get("kind"),
            "before_sha256": snap.get("before_sha256"),
            "after_sha256": snap.get("after_sha256"),
        }

    def read_snapshot_diff(self, site_id: str, change_id: str) -> dict[str, Any]:
        row = self.get_change(site_id, change_id)
        if row is None:
            return {"ok": False, "error_class": "UnknownChange", "diff": "", "fields": []}
        try:
            snap = self.snapshots.read_snapshot(row["site_id"], row["change_id"])
        except Exception:
            return {"ok": False, "error_class": "SnapshotMissing", "diff": "", "fields": []}
        if snap["kind"] == "entity":
            before = snap["before"] if isinstance(snap["before"], dict) else {}
            after = snap["after"] if isinstance(snap["after"], dict) else {}
            keys = sorted(set(before) | set(after))
            fields = [
                {
                    "field": k,
                    "before": redact_secret_text(str(before.get(k, ""))),
                    "after": redact_secret_text(str(after.get(k, ""))),
                    "changed": before.get(k) != after.get(k),
                }
                for k in keys
            ]
            before_txt = _canonical_json_bytes(before).decode("utf-8", errors="replace")
            after_txt = _canonical_json_bytes(after).decode("utf-8", errors="replace")
            diff = "".join(
                difflib.unified_diff(
                    before_txt.splitlines(keepends=True),
                    after_txt.splitlines(keepends=True),
                    fromfile="before.json",
                    tofile="after.json",
                    n=3,
                )
            )
            return {
                "ok": True,
                "kind": "entity",
                "before": redact_secrets(before),
                "after": redact_secrets(after),
                "fields": fields,
                "diff": redact_secret_text(diff[:_MAX_DIFF_BYTES]),
            }

        chunks: list[str] = []
        file_rows: list[dict[str, Any]] = []
        before_files = snap["before_files"]
        after_files = snap["after_files"]
        for entry in snap["entries"]:
            rel = str(entry.get("path") or "")
            b_txt = (
                before_files[rel].decode("utf-8", errors="replace")
                if rel in before_files
                else ""
            )
            a_txt = (
                after_files[rel].decode("utf-8", errors="replace")
                if rel in after_files
                else ""
            )
            udiff = "".join(
                difflib.unified_diff(
                    b_txt.splitlines(keepends=True),
                    a_txt.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                    n=2,
                )
            )
            if udiff:
                chunks.append(udiff)
            file_rows.append(
                {
                    "path": rel,
                    "op": entry.get("op"),
                    "before_sha256": entry.get("before_sha256"),
                    "after_sha256": entry.get("after_sha256"),
                }
            )
        joined = "".join(chunks)
        return {
            "ok": True,
            "kind": "files",
            "files": file_rows,
            "diff": redact_secret_text(joined[:_MAX_DIFF_BYTES]),
        }

    def mark_status(self, site_id: str, change_id: str, status: str) -> None:
        with _PROCESS_LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE wp_changes SET status = ? WHERE site_id = ? AND change_id = ?",
                    (str(status), str(site_id).strip().lower(), change_id),
                )

    def mark_rolled_back(
        self, site_id: str, change_id: str, *, rollback_change_id: str
    ) -> None:
        with _PROCESS_LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE wp_changes SET status = 'rolled_back', rolled_back_by = ? "
                    "WHERE site_id = ? AND change_id = ?",
                    (rollback_change_id, str(site_id).strip().lower(), change_id),
                )


def safe_record_change(data_dir: Path | None = None, **kwargs: Any) -> dict[str, Any] | None:
    """Best-effort snapshot capture so mutation callers never crash if ledger dir is readonly."""
    try:
        return ChangeLedger(data_dir=data_dir).record_change(**kwargs)
    except Exception as exc:
        logger.warning("safe_record_change failed (%s): %s", type(exc).__name__, str(exc)[:160])
        return None


__all__ = [
    "BATCH_ID_RE",
    "CHANGE_ID_RE",
    "ChangeLedger",
    "SnapshotStore",
    "resolve_ledger_site_id",
    "safe_record_change",
    "valid_batch_id",
    "valid_change_id",
]
