"""Unified runtime housekeeping and disk garbage collection (Feature C3).

Safely cleans expired WordPress review workspace artifacts (past their grace
window), untracked ephemeral ``pv-*`` preview directories, old inbound media
files, expired SQLite proposals/review pins, and rotates oversized JSONL logs
with gzip compression.

Security & safety invariants:
- Never deletes active (non-expired) ``wr-*`` review workspaces or expired
  workspaces still within ``artifact_grace_hours``.
- Refuses all symlinks and strictly enforces containment inside ``data_dir``
  before any unlink or directory removal.
- ``dry_run=True`` is strictly read-only and never modifies files or SQLite DBs.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROTATE_LOCK = threading.Lock()
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WR_CLEAN_SUBDIRS = ("artifacts", "base", "origin", "work")
_MAX_GZ_ARCHIVES = 3
_WIN_RETRY_DELAYS = (0.02, 0.04, 0.06, 0.08)


def _resolve_data_dir(data_dir: Path | None = None) -> Path:
    base = (
        Path(data_dir)
        if data_dir is not None
        else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    )
    return base.resolve()


def _parse_iso_utc(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_safe_contained(path: Path, ancestor: Path) -> bool:
    """Return True iff ``path`` and every segment under ``ancestor`` are non-symlink and contained."""
    try:
        ancestor_res = ancestor.resolve()
        if path.is_symlink():
            return False
        # Walk up from path to ancestor checking every segment for symlinks.
        cur = path
        seen = 0
        while seen < 64:
            if cur.is_symlink():
                return False
            try:
                if cur.resolve() == ancestor_res:
                    break
            except OSError:
                return False
            parent = cur.parent
            if parent == cur:
                return False
            cur = parent
            seen += 1
        else:
            return False
        resolved = path.resolve()
        return resolved == ancestor_res or ancestor_res in resolved.parents
    except OSError:
        return False


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 5) -> None:
    total = max(1, int(attempts))
    for attempt in range(total):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= total - 1:
                raise
            time.sleep(_WIN_RETRY_DELAYS[min(attempt, len(_WIN_RETRY_DELAYS) - 1)])


def _tree_stats_non_symlink(root_dir: Path, ancestor: Path) -> tuple[int, float]:
    """Return ``(total_bytes, newest_mtime)`` for non-symlink entries contained in ``ancestor``."""
    if not _is_safe_contained(root_dir, ancestor) or not root_dir.is_dir():
        return 0, 0.0
    total = 0
    try:
        newest = root_dir.lstat().st_mtime
    except OSError:
        newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=True, followlinks=False):
        dp = Path(dirpath)
        if not _is_safe_contained(dp, ancestor):
            dirnames[:] = []
            continue
        safe_dirs: list[str] = []
        for dname in dirnames:
            dpath = dp / dname
            if dpath.is_symlink() or not _is_safe_contained(dpath, ancestor):
                continue
            try:
                newest = max(newest, dpath.lstat().st_mtime)
            except OSError:
                pass
            safe_dirs.append(dname)
        dirnames[:] = safe_dirs
        for fname in filenames:
            fpath = dp / fname
            if fpath.is_symlink() or not _is_safe_contained(fpath, ancestor):
                continue
            try:
                st = fpath.lstat()
                total += st.st_size
                newest = max(newest, st.st_mtime)
            except OSError:
                continue
    return total, newest


def _safe_remove_tree(target_dir: Path, ancestor: Path) -> bool:
    """Safely remove ``target_dir`` without following symlinks or escaping ``ancestor``.

    Returns True if all contained non-symlink entries (and ``target_dir`` if no
    symlinks blocked it) were removed cleanly. Any symlink encountered is skipped
    so external targets are never touched.
    """
    if not _is_safe_contained(target_dir, ancestor) or not target_dir.is_dir():
        return False
    skipped_symlink = False
    for dirpath, dirnames, filenames in os.walk(target_dir, topdown=False, followlinks=False):
        dp = Path(dirpath)
        if not _is_safe_contained(dp, ancestor):
            skipped_symlink = True
            continue
        for fname in filenames:
            fpath = dp / fname
            if fpath.is_symlink() or not _is_safe_contained(fpath, ancestor):
                skipped_symlink = True
                continue
            try:
                fpath.unlink()
            except OSError:
                skipped_symlink = True
        for dname in dirnames:
            dpath = dp / dname
            if dpath.is_symlink() or not _is_safe_contained(dpath, ancestor):
                skipped_symlink = True
                continue
            try:
                dpath.rmdir()
            except OSError:
                skipped_symlink = True
    if not skipped_symlink:
        try:
            target_dir.rmdir()
        except OSError:
            return False
    return not skipped_symlink


def _load_plans_list(root: Path) -> list[dict[str, Any]]:
    plans_path = root / "wp_review_plans.json"
    if not plans_path.is_file() or plans_path.is_symlink() or not _is_safe_contained(plans_path, root):
        return []
    try:
        raw = json.loads(plans_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        items = raw.get("plans")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _expire_due_plans(
    root: Path,
    now_dt: datetime,
    *,
    dry_run: bool,
    errors: list[str],
) -> int:
    plans = _load_plans_list(root)
    if not plans:
        return 0
    due_count = 0
    for plan in plans:
        if plan.get("status") == "expired":
            continue
        exp = _parse_iso_utc(plan.get("expires_at"))
        if exp is not None and exp <= now_dt:
            due_count += 1
    if dry_run or due_count == 0:
        return due_count
    try:
        from ecom_ops.wp_ops.review_plan import ReviewPlans

        return int(ReviewPlans(data_dir=root, clock=lambda: now_dt).expire_due(actor="agent"))
    except Exception as exc:
        errors.append(f"expire_plans:{type(exc).__name__}")
        return 0


def _clean_expired_wr_workspaces(
    root: Path,
    now_dt: datetime,
    *,
    artifact_grace_hours: int,
    dry_run: bool,
    errors: list[str],
) -> tuple[int, int]:
    ws_root = root / "wp_workspaces"
    if not ws_root.is_dir() or ws_root.is_symlink() or not _is_safe_contained(ws_root, root):
        return 0, 0
    plans = _load_plans_list(root)
    if not plans:
        return 0, 0
    grace_cutoff = now_dt - timedelta(hours=max(0, int(artifact_grace_hours)))
    cleaned_count = 0
    reclaimed = 0
    seen_targets: set[tuple[str, str]] = set()
    for plan in plans:
        site_id = str(plan.get("site_id") or "").strip()
        plan_id = str(plan.get("plan_id") or "").strip()
        if (
            not site_id
            or not plan_id
            or not _SAFE_ID_RE.fullmatch(site_id)
            or not _SAFE_ID_RE.fullmatch(plan_id)
            or ".." in site_id
            or ".." in plan_id
        ):
            continue
        key = (site_id, plan_id)
        if key in seen_targets:
            continue
        seen_targets.add(key)

        exp = _parse_iso_utc(plan.get("expires_at"))
        is_expired = plan.get("status") == "expired" or (exp is not None and exp <= now_dt)
        # Never touch active plans, plans without an expiry timestamp, or plans inside grace window.
        if not is_expired or exp is None or exp > grace_cutoff:
            continue

        plan_dir = ws_root / site_id / plan_id
        if not plan_dir.is_dir() or not _is_safe_contained(plan_dir, ws_root):
            continue

        subdirs_found: list[tuple[Path, int]] = []
        for sub_name in _WR_CLEAN_SUBDIRS:
            sub = plan_dir / sub_name
            if not sub.exists() or sub.is_symlink() or not sub.is_dir():
                continue
            if not _is_safe_contained(sub, ws_root):
                continue
            sub_bytes, _ = _tree_stats_non_symlink(sub, ws_root)
            subdirs_found.append((sub, sub_bytes))

        if not subdirs_found:
            continue

        cleaned_any = False
        for sub, sub_bytes in subdirs_found:
            if dry_run:
                reclaimed += sub_bytes
                cleaned_any = True
            else:
                try:
                    _safe_remove_tree(sub, ws_root)
                    reclaimed += sub_bytes
                    cleaned_any = True
                except Exception as exc:
                    errors.append(f"wr_cleanup:{plan_id}:{sub.name}:{type(exc).__name__}")
        if cleaned_any:
            cleaned_count += 1
    return cleaned_count, reclaimed


def _clean_untracked_pv_dirs(
    root: Path,
    now_ts: float,
    *,
    pv_max_age_hours: int,
    dry_run: bool,
    errors: list[str],
) -> tuple[int, int]:
    ws_root = root / "wp_workspaces"
    if not ws_root.is_dir() or ws_root.is_symlink() or not _is_safe_contained(ws_root, root):
        return 0, 0
    cutoff = now_ts - max(0, int(pv_max_age_hours)) * 3600.0
    removed = 0
    reclaimed = 0
    try:
        site_dirs = list(ws_root.iterdir())
    except OSError as exc:
        errors.append(f"pv_scan:{type(exc).__name__}")
        return 0, 0

    for site_dir in site_dirs:
        if site_dir.is_symlink() or not site_dir.is_dir() or not _is_safe_contained(site_dir, ws_root):
            continue
        try:
            entries = list(site_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith("pv-"):
                continue
            if entry.is_symlink() or not entry.is_dir() or not _is_safe_contained(entry, ws_root):
                continue
            size_bytes, newest_mtime = _tree_stats_non_symlink(entry, ws_root)
            if newest_mtime >= cutoff:
                continue
            if dry_run:
                removed += 1
                reclaimed += size_bytes
            else:
                try:
                    _safe_remove_tree(entry, ws_root)
                    removed += 1
                    reclaimed += size_bytes
                except Exception as exc:
                    errors.append(f"pv_cleanup:{entry.name}:{type(exc).__name__}")
    return removed, reclaimed


def _clean_old_inbox_media(
    root: Path,
    now_ts: float,
    *,
    inbox_max_age_hours: int,
    dry_run: bool,
    errors: list[str],
) -> tuple[int, int]:
    cutoff = now_ts - max(0, int(inbox_max_age_hours)) * 3600.0
    removed = 0
    reclaimed = 0
    for folder_name in ("telegram_inbox_media", "messenger_inbox_media"):
        media_root = root / folder_name
        if not media_root.is_dir() or media_root.is_symlink() or not _is_safe_contained(media_root, root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(
                media_root, topdown=False, followlinks=False
            ):
                dp = Path(dirpath)
                if not _is_safe_contained(dp, media_root):
                    continue
                for fname in filenames:
                    fpath = dp / fname
                    if fpath.is_symlink() or not _is_safe_contained(fpath, media_root):
                        continue
                    try:
                        st = fpath.lstat()
                    except OSError:
                        continue
                    if st.st_mtime >= cutoff:
                        continue
                    if dry_run:
                        removed += 1
                        reclaimed += st.st_size
                    else:
                        try:
                            fpath.unlink()
                            removed += 1
                            reclaimed += st.st_size
                        except OSError as exc:
                            errors.append(f"media_cleanup:{folder_name}:{type(exc).__name__}")
                if not dry_run and dp != media_root:
                    try:
                        if not any(dp.iterdir()):
                            dp.rmdir()
                    except OSError:
                        pass
        except OSError as exc:
            errors.append(f"media_scan:{folder_name}:{type(exc).__name__}")
    return removed, reclaimed


def _purge_sqlite_proposals(
    root: Path,
    now_ts: float,
    *,
    proposal_max_age_hours: int,
    dry_run: bool,
    errors: list[str],
) -> int:
    max_age_s = max(0, int(proposal_max_age_hours)) * 3600.0
    fallback_cutoff = now_ts - max_age_s
    purged_total = 0

    for db_name in ("telegram_jobs.sqlite3", "messenger_proposals.sqlite3"):
        db_path = root / db_name
        if not db_path.is_file() or db_path.is_symlink() or not _is_safe_contained(db_path, root):
            continue
        conn: sqlite3.Connection | None = None
        try:
            if dry_run:
                try:
                    conn = sqlite3.connect(
                        f"{db_path.resolve().as_uri()}?mode=ro",
                        uri=True,
                        timeout=5.0,
                    )
                except sqlite3.OperationalError:
                    conn = sqlite3.connect(str(db_path), timeout=5.0)
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("PRAGMA busy_timeout=5000")
            else:
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                conn.execute("PRAGMA busy_timeout=5000")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            has_proposals = "bot_proposals" in tables
            has_pins = "wp_review_pins" in tables
            if not has_proposals and not has_pins:
                continue

            has_expires_col = False
            if has_proposals:
                cols = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(bot_proposals)").fetchall()
                }
                has_expires_col = "expires_at" in cols

            if dry_run:
                if has_proposals:
                    if has_expires_col:
                        row = conn.execute(
                            """
                            SELECT COUNT(*) FROM bot_proposals
                            WHERE state = 'open'
                              AND (
                                (expires_at IS NOT NULL AND expires_at <= ?)
                                OR (expires_at IS NULL AND created_at <= ?)
                              )
                            """,
                            (now_ts, fallback_cutoff),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM bot_proposals WHERE state = 'open' AND created_at <= ?",
                            (fallback_cutoff,),
                        ).fetchone()
                    purged_total += int(row[0] or 0) if row else 0
                if has_pins:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM wp_review_pins WHERE expires_at <= ?",
                        (now_ts,),
                    ).fetchone()
                    purged_total += int(row[0] or 0) if row else 0
            else:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if has_proposals:
                        if has_expires_col:
                            cur = conn.execute(
                                """
                                DELETE FROM bot_proposals
                                WHERE state = 'open'
                                  AND (
                                    (expires_at IS NOT NULL AND expires_at <= ?)
                                    OR (expires_at IS NULL AND created_at <= ?)
                                  )
                                """,
                                (now_ts, fallback_cutoff),
                            )
                        else:
                            cur = conn.execute(
                                "DELETE FROM bot_proposals WHERE state = 'open' AND created_at <= ?",
                                (fallback_cutoff,),
                            )
                        purged_total += max(0, int(cur.rowcount or 0))
                    if has_pins:
                        cur = conn.execute(
                            "DELETE FROM wp_review_pins WHERE expires_at <= ?",
                            (now_ts,),
                        )
                        purged_total += max(0, int(cur.rowcount or 0))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as exc:
            errors.append(f"sqlite_purge:{db_name}:{type(exc).__name__}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return purged_total


def _module_lock_for_log(filename: str) -> threading.Lock | None:
    try:
        if filename == "telemetry.jsonl":
            from ecom_ops import telemetry as _telemetry_mod

            return getattr(_telemetry_mod, "_lock", None)
        if filename == "audit.jsonl":
            from ecom_ops import audit as _audit_mod

            return getattr(_audit_mod, "_lock", None)
    except Exception:
        return None
    return None


def _rotate_jsonl_logs(
    root: Path,
    *,
    rotate_max_mb: float,
    dry_run: bool,
    errors: list[str],
) -> tuple[list[str], int]:
    threshold_bytes = max(1, int(float(rotate_max_mb) * 1024 * 1024))
    rotated: list[str] = []
    reclaimed = 0

    for filename in ("telemetry.jsonl", "audit.jsonl"):
        log_path = root / filename
        if not log_path.is_file() or log_path.is_symlink() or not _is_safe_contained(log_path, root):
            continue
        try:
            orig_size = log_path.stat().st_size
        except OSError as exc:
            errors.append(f"log_stat:{filename}:{type(exc).__name__}")
            continue
        if orig_size <= threshold_bytes:
            continue
        if dry_run:
            rotated.append(filename)
            reclaimed += orig_size
            continue

        mod_lock = _module_lock_for_log(filename)
        try:
            with _ROTATE_LOCK:
                if mod_lock is not None:
                    mod_lock.acquire()
                try:
                    if not log_path.is_file() or log_path.is_symlink():
                        continue
                    cur_size = log_path.stat().st_size
                    if cur_size <= threshold_bytes:
                        continue

                    # Shift archives: drop oldest beyond _MAX_GZ_ARCHIVES.
                    oldest = root / f"{filename}.{_MAX_GZ_ARCHIVES}.gz"
                    if oldest.exists() and not oldest.is_symlink() and _is_safe_contained(oldest, root):
                        try:
                            oldest.unlink()
                        except OSError:
                            pass
                    for idx in range(_MAX_GZ_ARCHIVES - 1, 0, -1):
                        src_arc = root / f"{filename}.{idx}.gz"
                        dst_arc = root / f"{filename}.{idx + 1}.gz"
                        if src_arc.is_file() and not src_arc.is_symlink() and _is_safe_contained(src_arc, root):
                            _replace_with_retry(src_arc, dst_arc)

                    tmp_gz = root / (
                        f".{filename}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.gz.tmp"
                    )
                    dst_1 = root / f"{filename}.1.gz"
                    try:
                        with log_path.open("rb") as src_fh, gzip.open(
                            tmp_gz, "wb", compresslevel=6
                        ) as gz_fh:
                            shutil.copyfileobj(src_fh, gz_fh, length=65536)
                        try:
                            os.chmod(tmp_gz, 0o600)
                        except OSError:
                            pass
                        _replace_with_retry(tmp_gz, dst_1)
                        try:
                            os.chmod(dst_1, 0o600)
                        except OSError:
                            pass
                    finally:
                        if tmp_gz.exists():
                            try:
                                tmp_gz.unlink()
                            except OSError:
                                pass

                    # Truncate live log file in-place so append writers keep working seamlessly.
                    with log_path.open("r+b") as live_fh:
                        live_fh.truncate(0)
                        live_fh.flush()

                    gz_size = dst_1.stat().st_size if dst_1.is_file() else 0
                    reclaimed += max(0, cur_size - gz_size)
                    rotated.append(filename)
                finally:
                    if mod_lock is not None:
                        mod_lock.release()
        except Exception as exc:
            errors.append(f"log_rotate:{filename}:{type(exc).__name__}")
    return rotated, reclaimed


def _open_sqlite_conn(db_path: Path, *, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        try:
            return sqlite3.connect(
                f"{db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=5000")
            except Exception:
                conn.close()
                raise
            return conn
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        conn.close()
        raise
    return conn


def _purge_expired_search_cache(
    data_dir: Path,
    *,
    dry_run: bool,
    now_ts: float,
    errors: list[str] | None = None,
) -> tuple[int, int]:
    """Purge expired rows and LRU excess from ``web_search_cache.sqlite3``."""
    root = _resolve_data_dir(data_dir)
    db_path = root / "web_search_cache.sqlite3"
    if not db_path.is_file() or db_path.is_symlink() or not _is_safe_contained(db_path, root):
        return 0, 0

    try:
        from ecom_ops.research.config import load_research_config

        max_entries = max(1, int(load_research_config().cache.max_entries))
    except Exception:
        max_entries = 1000

    conn: sqlite3.Connection | None = None
    purged_rows = 0
    reclaimed_bytes = 0
    try:
        conn = _open_sqlite_conn(db_path, dry_run=bool(dry_run))
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "search_cache" not in tables:
            return 0, 0

        if dry_run:
            exp_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(
                    SUM(CASE WHEN size_bytes > 0 THEN size_bytes ELSE LENGTH(CAST(payload_json AS BLOB)) END),
                    0
                )
                FROM search_cache
                WHERE expires_at <= ?
                """,
                (float(now_ts),),
            ).fetchone()
            expired_count = int(exp_row[0] or 0) if exp_row else 0
            expired_bytes = int(exp_row[1] or 0) if exp_row else 0

            rem_row = conn.execute(
                "SELECT COUNT(*) FROM search_cache WHERE expires_at > ?",
                (float(now_ts),),
            ).fetchone()
            remaining = int(rem_row[0] or 0) if rem_row else 0
            excess = max(0, remaining - max_entries)
            excess_bytes = 0
            if excess > 0:
                ex_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(sz), 0) FROM (
                        SELECT CASE WHEN size_bytes > 0 THEN size_bytes ELSE LENGTH(CAST(payload_json AS BLOB)) END AS sz
                        FROM search_cache
                        WHERE expires_at > ?
                        ORDER BY last_accessed_at ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (float(now_ts), excess),
                ).fetchone()
                excess_bytes = int(ex_row[0] or 0) if ex_row else 0
            return expired_count + excess, expired_bytes + excess_bytes

        conn.execute("BEGIN IMMEDIATE")
        try:
            exp_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(
                    SUM(CASE WHEN size_bytes > 0 THEN size_bytes ELSE LENGTH(CAST(payload_json AS BLOB)) END),
                    0
                )
                FROM search_cache
                WHERE expires_at <= ?
                """,
                (float(now_ts),),
            ).fetchone()
            expired_bytes = int(exp_row[1] or 0) if exp_row else 0
            cur_exp = conn.execute("DELETE FROM search_cache WHERE expires_at <= ?", (float(now_ts),))
            deleted_exp = max(0, int(cur_exp.rowcount or 0))
            purged_rows += deleted_exp
            if deleted_exp > 0:
                reclaimed_bytes += expired_bytes

            rem_row = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()
            remaining = int(rem_row[0] or 0) if rem_row else 0
            excess = max(0, remaining - max_entries)
            if excess > 0:
                ex_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(sz), 0) FROM (
                        SELECT CASE WHEN size_bytes > 0 THEN size_bytes ELSE LENGTH(CAST(payload_json AS BLOB)) END AS sz
                        FROM search_cache
                        ORDER BY last_accessed_at ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                ).fetchone()
                excess_bytes = int(ex_row[0] or 0) if ex_row else 0
                cur_ex = conn.execute(
                    """
                    DELETE FROM search_cache WHERE cache_key IN (
                        SELECT cache_key FROM search_cache
                        ORDER BY last_accessed_at ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                )
                deleted_ex = max(0, int(cur_ex.rowcount or 0))
                purged_rows += deleted_ex
                if deleted_ex > 0:
                    reclaimed_bytes += excess_bytes
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except Exception as exc:
        if errors is not None:
            errors.append(f"search_cache_purge:{type(exc).__name__}")
        return 0, 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return purged_rows, reclaimed_bytes


def _clean_expired_research_artifacts(
    data_dir: Path,
    *,
    dry_run: bool,
    grace_hours: int,
    now_ts: float,
    errors: list[str] | None = None,
) -> tuple[int, int]:
    """Remove expired non-pinned terminal research artifacts and prune DB rows safely."""
    root = _resolve_data_dir(data_dir)
    try:
        from ecom_ops.research.config import load_research_config

        ttl_days = max(1, int(load_research_config().retention.artifact_ttl_days))
    except Exception:
        ttl_days = 14

    cutoff_ts = float(now_ts) - (ttl_days * 86400.0 + max(0, int(grace_hours)) * 3600.0)
    artifacts_root = root / "research_artifacts"
    artifacts_root_ok = (
        artifacts_root.is_dir()
        and not artifacts_root.is_symlink()
        and _is_safe_contained(artifacts_root, root)
    )
    db_path = root / "research_jobs.sqlite3"
    db_ok = db_path.is_file() and not db_path.is_symlink() and _is_safe_contained(db_path, root)

    all_db_ids: set[str] = set()
    eligible_ids: list[str] = []
    conn: sqlite3.Connection | None = None
    removed_artifacts = 0
    reclaimed = 0

    try:
        if db_ok:
            conn = _open_sqlite_conn(db_path, dry_run=bool(dry_run))
            if not dry_run:
                conn.execute("PRAGMA foreign_keys=ON")
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "research_jobs" in tables:
                all_db_ids = {
                    str(row[0])
                    for row in conn.execute("SELECT id FROM research_jobs").fetchall()
                    if row[0]
                }
                eligible_rows = conn.execute(
                    """
                    SELECT id FROM research_jobs
                    WHERE status IN ('completed', 'partial', 'cancelled', 'failed')
                      AND COALESCE(pinned, 0) = 0
                      AND COALESCE(completed_at, updated_at, created_at) <= ?
                    """,
                    (cutoff_ts,),
                ).fetchall()
                eligible_ids = [str(row[0]) for row in eligible_rows if row[0]]

        prunable_ids: list[str] = []
        for job_id in eligible_ids:
            if not _SAFE_ID_RE.fullmatch(job_id) or ".." in job_id:
                continue
            dir_blocked = False
            if artifacts_root_ok:
                job_dir = artifacts_root / job_id
                if job_dir.exists() or job_dir.is_symlink():
                    if (
                        job_dir.is_symlink()
                        or not _is_safe_contained(job_dir, artifacts_root)
                        or not job_dir.is_dir()
                    ):
                        dir_blocked = True
                    else:
                        size_bytes, _ = _tree_stats_non_symlink(job_dir, artifacts_root)
                        if dry_run:
                            removed_artifacts += 1
                            reclaimed += size_bytes
                        else:
                            if _safe_remove_tree(job_dir, artifacts_root):
                                removed_artifacts += 1
                                reclaimed += size_bytes
                            else:
                                dir_blocked = True
            if not dir_blocked:
                prunable_ids.append(job_id)

        if not dry_run and conn is not None and prunable_ids:
            conn.execute("BEGIN IMMEDIATE")
            try:
                chunk_size = 500
                for start in range(0, len(prunable_ids), chunk_size):
                    chunk = prunable_ids[start : start + chunk_size]
                    marks = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"""
                        DELETE FROM research_jobs
                        WHERE id IN ({marks})
                          AND COALESCE(pinned, 0) = 0
                          AND status IN ('completed', 'partial', 'cancelled', 'failed')
                        """,
                        chunk,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if artifacts_root_ok:
            try:
                entries = list(artifacts_root.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if not entry.name.startswith("res-"):
                    continue
                if entry.name in all_db_ids:
                    continue
                if entry.is_symlink() or not entry.is_dir() or not _is_safe_contained(entry, artifacts_root):
                    continue
                meta_path = entry / "meta.json"
                if (
                    meta_path.is_file()
                    and not meta_path.is_symlink()
                    and _is_safe_contained(meta_path, artifacts_root)
                ):
                    try:
                        meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
                        if isinstance(meta_obj, dict) and meta_obj.get("pinned"):
                            continue
                    except (OSError, json.JSONDecodeError):
                        pass
                size_bytes, newest_mtime = _tree_stats_non_symlink(entry, artifacts_root)
                if newest_mtime > cutoff_ts:
                    continue
                if dry_run:
                    removed_artifacts += 1
                    reclaimed += size_bytes
                else:
                    if _safe_remove_tree(entry, artifacts_root):
                        removed_artifacts += 1
                        reclaimed += size_bytes
    except Exception as exc:
        if errors is not None:
            errors.append(f"research_artifacts_cleanup:{type(exc).__name__}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return removed_artifacts, reclaimed


def run_ops_gc(
    *,
    data_dir: Path | None = None,
    dry_run: bool = False,
    artifact_grace_hours: int = 24,
    pv_max_age_hours: int = 24,
    inbox_max_age_hours: int = 24,
    proposal_max_age_hours: int = 24,
    rotate_max_mb: float = 10.0,
) -> dict[str, Any]:
    """Run unified housekeeping across ``data_dir`` and return a JSON-safe summary."""
    root = _resolve_data_dir(data_dir)
    now_dt = datetime.now(UTC)
    now_ts = now_dt.timestamp()
    errors: list[str] = []
    bytes_reclaimed = 0

    expired_plans = _expire_due_plans(root, now_dt, dry_run=bool(dry_run), errors=errors)
    removed_ws, ws_bytes = _clean_expired_wr_workspaces(
        root,
        now_dt,
        artifact_grace_hours=artifact_grace_hours,
        dry_run=bool(dry_run),
        errors=errors,
    )
    bytes_reclaimed += ws_bytes

    removed_pv, pv_bytes = _clean_untracked_pv_dirs(
        root,
        now_ts,
        pv_max_age_hours=pv_max_age_hours,
        dry_run=bool(dry_run),
        errors=errors,
    )
    bytes_reclaimed += pv_bytes

    removed_media, media_bytes = _clean_old_inbox_media(
        root,
        now_ts,
        inbox_max_age_hours=inbox_max_age_hours,
        dry_run=bool(dry_run),
        errors=errors,
    )
    bytes_reclaimed += media_bytes

    purged_proposals = _purge_sqlite_proposals(
        root,
        now_ts,
        proposal_max_age_hours=proposal_max_age_hours,
        dry_run=bool(dry_run),
        errors=errors,
    )

    purged_search_cache_rows, search_cache_bytes = _purge_expired_search_cache(
        root,
        dry_run=bool(dry_run),
        now_ts=now_ts,
        errors=errors,
    )
    bytes_reclaimed += search_cache_bytes

    removed_research_artifacts, research_bytes = _clean_expired_research_artifacts(
        root,
        dry_run=bool(dry_run),
        grace_hours=artifact_grace_hours,
        now_ts=now_ts,
        errors=errors,
    )
    bytes_reclaimed += research_bytes

    rotated_logs, log_bytes = _rotate_jsonl_logs(
        root,
        rotate_max_mb=rotate_max_mb,
        dry_run=bool(dry_run),
        errors=errors,
    )
    bytes_reclaimed += log_bytes

    return {
        "ok": len(errors) == 0,
        "dry_run": bool(dry_run),
        "ran_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expired_plans": expired_plans,
        "removed_workspace_dirs": removed_ws,
        "removed_pv_dirs": removed_pv,
        "removed_media_files": removed_media,
        "purged_proposals": purged_proposals,
        "purged_search_cache_rows": purged_search_cache_rows,
        "removed_research_artifacts": removed_research_artifacts,
        "rotated_logs": rotated_logs,
        "bytes_reclaimed": bytes_reclaimed,
        "mb_reclaimed": round(bytes_reclaimed / (1024 * 1024), 2),
        "errors": errors,
    }
