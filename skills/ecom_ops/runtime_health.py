"""Runtime health snapshot, bounded error ring log, and Telegram summary (Feature C4).

Provides:
- ``record_runtime_error(...)``: appends sanitized runtime errors to
  ``AZOM_DATA_DIR/runtime_errors.jsonl`` capped to the latest 200 entries.
- ``collect_runtime_snapshot(...)``: read-only snapshot of Telegram poller lease,
  Telegram & Messenger queues, model-router circuit breakers, ChatGPT OAuth TTL
  (strictly whitelisted metadata — NEVER reads/returns tokens), disk usage,
  recent errors, and Waitress thread count.
- ``format_ops_telegram_summary(snapshot)``: concise Swedish ops summary for
  Oscar's ``/ops`` command and CLI ``ops status``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.security import redact_secrets

_RUNTIME_ERRORS_LOCK = threading.Lock()
_MAX_RUNTIME_ERRORS = 200
_WIN_RETRY_DELAYS = (0.02, 0.04, 0.06, 0.08)

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\bbot\d+:[A-Za-z0-9_-]{8,}\b", re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_API_KEY_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|xai-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,}|"
    r"EAA[A-Za-z0-9]{12,}|AIza[A-Za-z0-9_-]{12,}|rk-[A-Za-z0-9_-]{8,}|"
    r"BSA[A-Za-z0-9_-]{12,}|tvly-[A-Za-z0-9_-]{8,}|pplx-[A-Za-z0-9_-]{8,})\b"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|key|secret|code|access_token|refresh_token|authorization|sig)=)[^&\s]+"
)
_CTRL_WS_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")


def _resolve_data_dir(data_dir: Path | None = None) -> Path:
    base = (
        Path(data_dir)
        if data_dir is not None
        else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    )
    return base.resolve()


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


def _clean_label(raw: Any, *, max_len: int) -> str:
    text = _CTRL_WS_RE.sub(" ", str(raw or "")).strip()
    return text[:max_len]


def _sanitize_error_detail(raw: Any, *, max_len: int = 180) -> str:
    text = str(raw or "")
    text = _URL_RE.sub("[URL_REDACTED]", text)
    text = _TELEGRAM_BOT_TOKEN_RE.sub("[BOT_TOKEN_REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _API_KEY_RE.sub("[REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    redacted = redact_secrets(text)
    if not isinstance(redacted, str):
        redacted = str(redacted)
    cleaned = _CTRL_WS_RE.sub(" ", redacted).strip()
    return cleaned[:max_len]


def _format_utc_iso(ts: float | None) -> str:
    if ts is None or ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return ""


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


def record_runtime_error(
    source: str,
    error_class: str,
    *,
    detail: str = "",
    status_code: int | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Append a sanitized runtime error entry to ``runtime_errors.jsonl`` (ring cap 200)."""
    root = _resolve_data_dir(data_dir)
    path = root / "runtime_errors.jsonl"
    code_val = (
        int(status_code)
        if isinstance(status_code, int) and not isinstance(status_code, bool)
        else None
    )
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": _clean_label(source, max_len=60) or "unknown",
        "error_class": _clean_label(error_class, max_len=80) or "Error",
        "status_code": code_val,
        "detail": _sanitize_error_detail(detail, max_len=180),
    }
    encoded = json.dumps(entry, ensure_ascii=False)
    with _RUNTIME_ERRORS_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if path.is_file() and not path.is_symlink():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if s:
                        existing.append(s)
            except OSError:
                existing = []
        existing.append(encoded)
        kept = existing[-_MAX_RUNTIME_ERRORS:]
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            _replace_with_retry(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return entry


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file() or db_path.is_symlink():
        return None
    try:
        return sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
    except sqlite3.OperationalError:
        try:
            conn = sqlite3.connect(str(db_path), timeout=2.0)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            return conn
        except sqlite3.Error:
            return None
    except sqlite3.Error:
        return None


def _collect_telegram_db(root: Path, now_ts: float) -> tuple[dict[str, Any], dict[str, Any]]:
    poller: dict[str, Any] = {
        "present": False,
        "healthy": False,
        "status": "missing",
        "owner": "",
        "expires_at": None,
        "expires_at_iso": "",
        "ttl_seconds": 0,
    }
    queue: dict[str, Any] = {
        "db_present": False,
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "needs_reconciliation": 0,
        "reconciled": 0,
        "open_proposals": 0,
    }
    db_path = root / "telegram_jobs.sqlite3"
    conn = _open_sqlite_ro(db_path)
    if conn is None:
        return poller, queue
    queue["db_present"] = True
    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "telegram_poller_lease" in tables:
            row = conn.execute(
                "SELECT owner, expires_at FROM telegram_poller_lease WHERE lease_name = 'poller'"
            ).fetchone()
            if row is not None:
                owner = str(row[0] or "")
                try:
                    exp = float(row[1]) if row[1] is not None else 0.0
                except (TypeError, ValueError):
                    exp = 0.0
                ttl = max(0, int(round(exp - now_ts))) if exp > 0 else 0
                healthy = bool(owner and exp > now_ts)
                poller = {
                    "present": True,
                    "healthy": healthy,
                    "status": "healthy" if healthy else "expired",
                    "owner": owner[:80],
                    "expires_at": exp if exp > 0 else None,
                    "expires_at_iso": _format_utc_iso(exp),
                    "ttl_seconds": ttl,
                }
        if "bot_jobs" in tables:
            for state, count in conn.execute(
                "SELECT state, COUNT(*) FROM bot_jobs GROUP BY state"
            ).fetchall():
                key = str(state or "")
                if key in queue:
                    queue[key] = int(count or 0)
        if "bot_proposals" in tables:
            row = conn.execute(
                "SELECT COUNT(*) FROM bot_proposals WHERE state = 'open'"
            ).fetchone()
            if row:
                queue["open_proposals"] = int(row[0] or 0)
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return poller, queue


def _collect_messenger_dbs(root: Path) -> dict[str, Any]:
    queue: dict[str, Any] = {
        "db_present": False,
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "open_proposals": 0,
    }
    conn = _open_sqlite_ro(root / "messenger_jobs.sqlite3")
    if conn is not None:
        queue["db_present"] = True
        try:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "messenger_jobs" in tables:
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM messenger_jobs GROUP BY status"
                ).fetchall():
                    key = str(status or "")
                    if key in queue:
                        queue[key] = int(count or 0)
        except sqlite3.Error:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    pconn = _open_sqlite_ro(root / "messenger_proposals.sqlite3")
    if pconn is not None:
        try:
            tables = {
                str(r[0])
                for r in pconn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "bot_proposals" in tables:
                row = pconn.execute(
                    "SELECT COUNT(*) FROM bot_proposals WHERE state = 'open'"
                ).fetchone()
                if row:
                    queue["open_proposals"] = int(row[0] or 0)
        except sqlite3.Error:
            pass
        finally:
            try:
                pconn.close()
            except Exception:
                pass
    return queue


def _collect_breakers_file(path: Path, now_ts: float) -> dict[str, dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, ent in sorted(entries.items()):
        if not isinstance(ent, dict):
            continue
        state = str(ent.get("state") or "closed")
        try:
            cd = float(ent.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            cd = 0.0
        rem = max(0, int(round(cd - now_ts))) if state != "closed" else 0
        out[str(key)] = {
            "state": state,
            "remaining_s": rem,
            "failure_count": int(ent.get("failure_count") or 0),
            "last_error_class": str(ent.get("last_error_class") or ""),
            "last_error_code": str(ent.get("last_error_code") or ""),
        }
    return out


def _collect_breakers(root: Path, now_ts: float) -> dict[str, dict[str, Any]]:
    return _collect_breakers_file(root / "model_router_health.json", now_ts)


def _collect_research_db(root: Path) -> dict[str, Any]:
    queue: dict[str, Any] = {
        "db_present": False,
        "queued": 0,
        "running": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "cancelled": 0,
    }
    conn = _open_sqlite_ro(root / "research_jobs.sqlite3")
    if conn is None:
        return queue
    queue["db_present"] = True
    running_states = {"planning", "searching", "fetching", "evaluating", "synthesizing", "running"}
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "research_jobs" in tables:
            for status, count in conn.execute(
                "SELECT status, COUNT(*) FROM research_jobs GROUP BY status"
            ).fetchall():
                key = str(status or "").strip()
                n = int(count or 0)
                if key in running_states:
                    queue["running"] += n
                elif key in queue:
                    queue[key] += n
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return queue


def _collect_search_router(root: Path, now_ts: float) -> dict[str, Any]:
    from ecom_ops.research.budget import research_budget_status
    from ecom_ops.research.config import is_mock_mode, kill_switch_active, web_search_enabled
    from ecom_ops.telemetry import Telemetry

    breakers = _collect_breakers_file(root / "search_router_health.json", now_ts)
    open_breakers = sum(
        1 for item in breakers.values() if str(item.get("state") or "closed") != "closed"
    )
    budget = research_budget_status(telemetry=Telemetry(path=root / "telemetry.jsonl"))
    return {
        "enabled": web_search_enabled(),
        "mock_mode": is_mock_mode(),
        "kill_switch": kill_switch_active("search"),
        "breakers": breakers,
        "open_breakers_count": open_breakers,
        "daily_queries_used": int(budget["queries_today"]),
        "daily_queries_cap": int(budget["max_queries_per_day"]),
        "monthly_external_usd": float(budget["monthly_external_usd"]),
        "monthly_external_usd_cap": float(budget["monthly_external_usd_cap"]),
    }


def _collect_chatgpt_oauth_meta(root: Path, now_ts: float) -> dict[str, Any]:
    """Read strictly whitelisted non-secret ChatGPT OAuth metadata (no tokens ever returned)."""
    default: dict[str, Any] = {
        "connected": False,
        "status": "disconnected",
        "needs_reauth": False,
        "generation": 0,
        "expires_at": 0.0,
        "expires_at_iso": "",
        "ttl_seconds": 0,
    }
    session_path = root / "model_oauth" / "openai_default.json"
    if not session_path.is_file() or session_path.is_symlink():
        return default
    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(raw, dict):
        return default

    # Whitelist metadata fields only — token strings are checked only for truthiness, never stored.
    needs_reauth = bool(raw.get("needs_reauth") or False)
    raw_status = str(raw.get("status") or "connected").strip().lower()
    has_access = bool(raw.get("access_token"))
    try:
        generation = int(raw.get("generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    try:
        expires_at = float(raw.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0

    ttl_seconds = max(0, int(round(expires_at - now_ts))) if expires_at > 0 else 0
    if needs_reauth:
        connected = False
        status = "needs_reauth"
    elif not has_access or raw_status != "connected":
        connected = False
        status = "disconnected"
    elif expires_at > 0 and expires_at <= now_ts:
        connected = False
        status = "expired"
    else:
        connected = True
        status = "connected"

    return {
        "connected": connected,
        "status": status,
        "needs_reauth": needs_reauth,
        "generation": generation,
        "expires_at": expires_at,
        "expires_at_iso": _format_utc_iso(expires_at),
        "ttl_seconds": ttl_seconds,
    }


def _safe_dir_bytes(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (dp / d).is_symlink()]
        for fname in filenames:
            fp = dp / fname
            if fp.is_symlink():
                continue
            try:
                total += fp.lstat().st_size
            except OSError:
                continue
    return total


def _collect_disk_usage(root: Path, now_dt: datetime) -> dict[str, Any]:
    total_bytes = _safe_dir_bytes(root) if root.is_dir() else 0

    def _entry(num_bytes: int) -> dict[str, Any]:
        return {"bytes": num_bytes, "mb": round(num_bytes / (1024 * 1024), 2)}

    def _glob_bytes(pattern: str) -> int:
        if not root.is_dir():
            return 0
        acc = 0
        try:
            for p in root.glob(pattern):
                if p.is_file() and not p.is_symlink():
                    try:
                        acc += p.lstat().st_size
                    except OSError:
                        continue
        except OSError:
            pass
        return acc

    wp_bytes = _safe_dir_bytes(root / "wp_workspaces")
    tg_media_bytes = _safe_dir_bytes(root / "telegram_inbox_media")
    ms_media_bytes = _safe_dir_bytes(root / "messenger_inbox_media")
    research_artifacts_bytes = _safe_dir_bytes(root / "research_artifacts")
    web_search_cache_bytes = _glob_bytes("web_search_cache.sqlite3*")
    telemetry_bytes = _glob_bytes("telemetry.jsonl*")
    audit_bytes = _glob_bytes("audit.jsonl*")
    sqlite_bytes = _glob_bytes("*.sqlite3*")

    # Expired WP review plans count
    expired_plans_count = 0
    plans_path = root / "wp_review_plans.json"
    if plans_path.is_file() and not plans_path.is_symlink():
        try:
            raw = json.loads(plans_path.read_text(encoding="utf-8"))
            items = raw.get("plans") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                for plan in items:
                    if not isinstance(plan, dict):
                        continue
                    exp = _parse_iso_utc(plan.get("expires_at"))
                    if plan.get("status") == "expired" or (exp is not None and exp <= now_dt):
                        expired_plans_count += 1
        except (OSError, json.JSONDecodeError):
            pass

    # Untracked pv-* dirs count
    untracked_pv_count = 0
    ws_root = root / "wp_workspaces"
    if ws_root.is_dir() and not ws_root.is_symlink():
        try:
            for site_dir in ws_root.iterdir():
                if site_dir.is_symlink() or not site_dir.is_dir():
                    continue
                try:
                    for item in site_dir.iterdir():
                        if item.name.startswith("pv-") and not item.is_symlink() and item.is_dir():
                            untracked_pv_count += 1
                except OSError:
                    continue
        except OSError:
            pass

    # Expired non-pinned terminal research artifacts count
    expired_research_artifacts_count = 0
    try:
        from ecom_ops.research.config import load_research_config

        ret_cfg = load_research_config().retention
        ttl_s = max(1, int(ret_cfg.artifact_ttl_days)) * 86400.0
        grace_s = max(0, int(ret_cfg.artifact_grace_hours)) * 3600.0
    except Exception:
        ttl_s = 14 * 86400.0
        grace_s = 24 * 3600.0
    res_cutoff_ts = now_dt.timestamp() - (ttl_s + grace_s)
    res_expired_ids: set[str] = set()
    res_db_ids: set[str] = set()
    rconn = _open_sqlite_ro(root / "research_jobs.sqlite3")
    if rconn is not None:
        try:
            rtables = {
                str(r[0])
                for r in rconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "research_jobs" in rtables:
                for row in rconn.execute("SELECT id FROM research_jobs").fetchall():
                    if row[0]:
                        res_db_ids.add(str(row[0]))
                for row in rconn.execute(
                    """
                    SELECT id FROM research_jobs
                    WHERE status IN ('completed', 'partial', 'cancelled', 'failed')
                      AND COALESCE(pinned, 0) = 0
                      AND COALESCE(completed_at, updated_at, created_at) <= ?
                    """,
                    (res_cutoff_ts,),
                ).fetchall():
                    if row[0]:
                        res_expired_ids.add(str(row[0]))
        except sqlite3.Error:
            pass
        finally:
            try:
                rconn.close()
            except Exception:
                pass
    res_art_root = root / "research_artifacts"
    if res_art_root.is_dir() and not res_art_root.is_symlink():
        try:
            for item in res_art_root.iterdir():
                if not item.name.startswith("res-") or item.is_symlink() or not item.is_dir():
                    continue
                if item.name in res_db_ids:
                    continue
                try:
                    newest_mt = item.lstat().st_mtime
                    for dp, _dns, fns in os.walk(item, topdown=True, followlinks=False):
                        for fn in fns:
                            fp = Path(dp) / fn
                            if not fp.is_symlink():
                                newest_mt = max(newest_mt, fp.lstat().st_mtime)
                    if newest_mt <= res_cutoff_ts:
                        res_expired_ids.add(item.name)
                except OSError:
                    continue
        except OSError:
            pass
    expired_research_artifacts_count = len(res_expired_ids)

    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "folders": {
            "wp_workspaces": _entry(wp_bytes),
            "telegram_inbox_media": _entry(tg_media_bytes),
            "messenger_inbox_media": _entry(ms_media_bytes),
            "research_artifacts": _entry(research_artifacts_bytes),
            "web_search_cache": _entry(web_search_cache_bytes),
            "telemetry_jsonl": _entry(telemetry_bytes),
            "audit_jsonl": _entry(audit_bytes),
            "sqlite_dbs": _entry(sqlite_bytes),
        },
        "expired_plans_count": expired_plans_count,
        "untracked_pv_count": untracked_pv_count,
        "expired_research_artifacts_count": expired_research_artifacts_count,
    }


def _collect_recent_errors(root: Path, limit: int) -> tuple[list[dict[str, Any]], int]:
    path = root / "runtime_errors.jsonl"
    if not path.is_file() or path.is_symlink():
        return [], 0
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            raw_code = obj.get("status_code")
            code_val = (
                int(raw_code)
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else None
            )
            entries.append(
                {
                    "ts": _clean_label(obj.get("ts"), max_len=40),
                    "source": _clean_label(obj.get("source"), max_len=60) or "unknown",
                    "error_class": _clean_label(obj.get("error_class"), max_len=80) or "Error",
                    "status_code": code_val,
                    "detail": _sanitize_error_detail(obj.get("detail"), max_len=180),
                }
            )
    except OSError:
        return [], 0
    total = len(entries)
    entries.reverse()  # newest first
    cap = max(0, int(limit))
    return entries[:cap], total


def _configured_waitress_threads() -> int:
    raw = os.environ.get("DASHBOARD_THREADS", "12")
    try:
        val = int(str(raw).strip() or 12)
    except ValueError:
        val = 12
    return max(4, min(32, val))


def collect_runtime_snapshot(
    *,
    data_dir: Path | None = None,
    recent_error_limit: int = 15,
) -> dict[str, Any]:
    """Collect a read-only, secret-free runtime health snapshot for dashboard, CLI, and /ops."""
    root = _resolve_data_dir(data_dir)
    now_dt = datetime.now(UTC)
    now_ts = now_dt.timestamp()

    poller, tg_queue = _collect_telegram_db(root, now_ts)
    ms_queue = _collect_messenger_dbs(root)
    research_queue = _collect_research_db(root)
    breakers = _collect_breakers(root, now_ts)
    open_breakers = sum(
        1 for item in breakers.values() if str(item.get("state") or "closed") != "closed"
    )
    chatgpt_oauth = _collect_chatgpt_oauth_meta(root, now_ts)
    search_router = _collect_search_router(root, now_ts)
    disk_usage = _collect_disk_usage(root, now_dt)
    recent_errors, recent_errors_total = _collect_recent_errors(root, recent_error_limit)

    return {
        "generated_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_dir": str(root),
        "poller": poller,
        "queues": {
            "telegram": tg_queue,
            "messenger": ms_queue,
            "research": research_queue,
        },
        "model_router": {
            "breakers": breakers,
            "open_breakers_count": open_breakers,
            "chatgpt_oauth": chatgpt_oauth,
        },
        "search_router": search_router,
        "disk_usage": disk_usage,
        "recent_errors": recent_errors,
        "recent_errors_total": recent_errors_total,
        "waitress": {
            "configured_threads": _configured_waitress_threads(),
        },
    }


def _format_duration_sv(seconds: int) -> str:
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    hours = secs // 3600
    mins = (secs % 3600) // 60
    return f"{hours}h {mins}m"


def format_ops_telegram_summary(snapshot: dict[str, Any]) -> str:
    """Format a concise Swedish runtime health summary for Oscar's ``/ops`` command."""
    poller = snapshot.get("poller") or {}
    queues = snapshot.get("queues") or {}
    tg = queues.get("telegram") or {}
    ms = queues.get("messenger") or {}
    res_q = queues.get("research") or {}
    mr = snapshot.get("model_router") or {}
    breakers = mr.get("breakers") or {}
    oauth = mr.get("chatgpt_oauth") or {}
    sr = snapshot.get("search_router") or {}
    sr_breakers = sr.get("breakers") or {}
    disk = snapshot.get("disk_usage") or {}
    folders = disk.get("folders") or {}
    waitress = snapshot.get("waitress") or {}
    recent = snapshot.get("recent_errors") or []
    recent_total = int(snapshot.get("recent_errors_total") or len(recent))

    p_status = poller.get("status")
    if p_status == "healthy":
        poller_line = f"OK · TTL {_format_duration_sv(int(poller.get('ttl_seconds') or 0))}"
    elif p_status == "expired":
        poller_line = "UTGÅNGEN lease"
    else:
        poller_line = "saknar aktiv lease"

    open_list = [
        f"{k}={v.get('state')} ({v.get('remaining_s', 0)}s)"
        for k, v in breakers.items()
        if str(v.get("state") or "closed") != "closed"
    ]
    breaker_line = "OK (0 öppna)" if not open_list else ", ".join(open_list[:3])

    oa_status = str(oauth.get("status") or "disconnected")
    if oa_status == "connected":
        ttl_s = int(oauth.get("ttl_seconds") or 0)
        ttl_part = f" (TTL {_format_duration_sv(ttl_s)})" if ttl_s > 0 else ""
        oauth_line = f"ansluten{ttl_part}"
    elif oa_status == "needs_reauth":
        oauth_line = "kräver ny inloggning (/oauth)"
    elif oa_status == "expired":
        oauth_line = "utgången"
    else:
        oauth_line = "ej ansluten"

    wp_mb = float((folders.get("wp_workspaces") or {}).get("mb") or 0.0)
    media_mb = float((folders.get("telegram_inbox_media") or {}).get("mb") or 0.0) + float(
        (folders.get("messenger_inbox_media") or {}).get("mb") or 0.0
    )
    res_mb = float((folders.get("research_artifacts") or {}).get("mb") or 0.0)
    logs_mb = float((folders.get("telemetry_jsonl") or {}).get("mb") or 0.0) + float(
        (folders.get("audit_jsonl") or {}).get("mb") or 0.0
    )
    db_mb = float((folders.get("sqlite_dbs") or {}).get("mb") or 0.0)

    sr_open_list = [
        f"{k}={v.get('state')} ({v.get('remaining_s', 0)}s)"
        for k, v in sr_breakers.items()
        if str(v.get("state") or "closed") != "closed"
    ]
    sr_breaker_line = "OK (0 öppna)" if not sr_open_list else ", ".join(sr_open_list[:3])
    sr_mode = "mock" if sr.get("mock_mode") else ("på" if sr.get("enabled") else "av")

    lines = [
        f"Azom Runtime /ops ({snapshot.get('generated_at', '')})",
        f"Poller: {poller_line}",
        (
            f"Telegram-kö: kö {tg.get('queued', 0)} · kör {tg.get('running', 0)} · "
            f"fel {tg.get('failed', 0)} · avstämning {tg.get('needs_reconciliation', 0)} · "
            f"öppna förslag {tg.get('open_proposals', 0)}"
        ),
        (
            f"Messenger-kö: kö {ms.get('pending', 0)} · kör {ms.get('running', 0)} · "
            f"fel {ms.get('failed', 0)} · öppna förslag {ms.get('open_proposals', 0)}"
        ),
        (
            f"Research-kö: kö {res_q.get('queued', 0)} · kör {res_q.get('running', 0)} · "
            f"klara {res_q.get('completed', 0)} · fel {res_q.get('failed', 0)} · "
            f"avbrutna {res_q.get('cancelled', 0)}"
        ),
        f"Model router: breakers {breaker_line}",
        (
            f"Sökrouter ({sr_mode}): breakers {sr_breaker_line} · "
            f"frågor {sr.get('daily_queries_used', 0)}/{sr.get('daily_queries_cap', 0)} · "
            f"${float(sr.get('monthly_external_usd') or 0.0):.2f}/"
            f"${float(sr.get('monthly_external_usd_cap') or 0.0):.2f}"
        ),
        f"ChatGPT OAuth: {oauth_line}",
        (
            f"Disk: {float(disk.get('total_mb') or 0.0):.2f} MB "
            f"(wp {wp_mb:.2f} · media {media_mb:.2f} · research {res_mb:.2f} · "
            f"loggar {logs_mb:.2f} · db {db_mb:.2f} MB)"
        ),
        (
            f"Städbart: {int(disk.get('expired_plans_count') or 0)} utgångna WP-planer · "
            f"{int(disk.get('untracked_pv_count') or 0)} pv-* arbetsytor · "
            f"{int(disk.get('expired_research_artifacts_count') or 0)} utgångna research-artefakter"
        ),
        f"Dashboard Waitress: {int(waitress.get('configured_threads') or 12)} trådar",
        f"Senaste driftfel: {recent_total} i buffert",
    ]
    for err in recent[:3]:
        code = f" HTTP {err['status_code']}" if err.get("status_code") is not None else ""
        lines.append(
            f"• {err.get('ts', '')} · {err.get('source', '')} · {err.get('error_class', '')}{code}"
        )
    lines.append("Städa: python -m ecom_ops --actor oscar ops gc --dry-run")
    return "\n".join(lines)
