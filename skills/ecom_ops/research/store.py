"""Durable SQLite stores, LRU search cache, artifact persistence, and RBAC for research."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
from ecom_ops.research.config import load_research_config
from ecom_ops.research.evaluator import canonicalize_url
from ecom_ops.research.firewall import OutboundQueryFirewall, QueryTaintContext
from ecom_ops.research.models import (
    RESEARCH_ID_RE,
    EvaluatedSource,
    EvidenceStatus,
    ResearchBrief,
    ResearchJob,
    ResearchSpec,
    SearchPolicyError,
    new_research_id,
)
from ecom_ops.research.sanitizer import sanitize_untrusted_text

MAX_STEERS_PER_JOB = 3
_ACTIVE_STATUSES = ("planning", "searching", "fetching", "evaluating", "synthesizing", "running")
_TERMINAL_STATUSES = frozenset({"completed", "partial", "cancelled", "failed"})
_REPLACE_RETRY_DELAYS = (0.02, 0.04, 0.06, 0.08)

_TARGET_WP_RE = re.compile(r"^wp:[a-z0-9][a-z0-9_-]{0,63}:wr-[0-9a-f]{12}$")
_TARGET_WP_SITE_RE = re.compile(r"^wp_site:[a-z0-9][a-z0-9_-]{0,63}$")
_TARGET_CASE_RE = re.compile(r"^case:[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TARGET_MARKETING_RE = re.compile(r"^marketing:[A-Za-z0-9._:-]{1,64}$")
_TARGET_PRODUCT_RE = re.compile(r"^product:[A-Za-z0-9._-]{1,64}$")
_TARGET_CHAT_RE = re.compile(r"^chat:[a-z0-9_-]{1,32}:[A-Za-z0-9._:-]{1,64}$")
_TARGET_SEO_RE = re.compile(r"^seo:(?:audit:seoa-[0-9a-f]{12}|[a-z][a-z0-9-]{0,62})$")


def _default_data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _now() -> float:
    return time.time()


def _ts_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _chmod_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError:
            pass


def _chmod_path(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _sanitize_payload_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_untrusted_text(value, max_chars=24_000)
    if isinstance(value, list):
        return [_sanitize_payload_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _sanitize_payload_value(v) for k, v in value.items()}
    return value


def _is_wp_target_ref(target_ref: str | None) -> bool:
    ref = (target_ref or "").strip()
    return ref.startswith("wp:") or ref.startswith("wp_site:")


def _assert_safe_contained_non_symlink(path: Path, ancestor: Path) -> Path:
    """Refuse symlinks anywhere from ``ancestor`` down to ``path`` and enforce containment."""
    if ancestor.is_symlink():
        raise SearchPolicyError("ArtifactSymlinkRefused")
    ancestor_res = ancestor.resolve()
    rel_parts: list[str]
    try:
        rel_parts = list(path.relative_to(ancestor).parts)
    except ValueError:
        try:
            rel_parts = list(path.resolve().relative_to(ancestor_res).parts)
        except ValueError:
            raise SearchPolicyError("ArtifactPathEscape")

    cursor = ancestor
    for part in rel_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SearchPolicyError("ArtifactSymlinkRefused")
    if path.is_symlink():
        raise SearchPolicyError("ArtifactSymlinkRefused")
    resolved = path.resolve()
    if resolved != ancestor_res and ancestor_res not in resolved.parents:
        raise SearchPolicyError("ArtifactPathEscape")
    return resolved


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        tmp.write_text(text, encoding="utf-8")
        _chmod_path(tmp, mode)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                _chmod_path(path, mode)
                return
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[min(attempt, len(_REPLACE_RETRY_DELAYS) - 1)])
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def authorize_target_ref(actor: str, target_ref: str | None) -> None:
    """Enforce server-side actor resolution and RBAC on ``target_ref``."""
    actor_obj = resolve_actor(actor)
    if target_ref is None or not str(target_ref).strip():
        return
    ref = str(target_ref).strip()

    if ref.startswith(("wp:", "wp_site:")):
        if not (_TARGET_WP_RE.fullmatch(ref) or _TARGET_WP_SITE_RE.fullmatch(ref)):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        if not (
            actor_obj.has(Permission.WP_OPS_OPERATE)
            or actor_obj.has(Permission.WP_OPS_ADMIN)
        ):
            raise AccessDenied(
                f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks WP research target access"
            )
        return

    if ref.startswith("case:"):
        if not _TARGET_CASE_RE.fullmatch(ref):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        if not (
            actor_obj.has(Permission.CASE_REPLY)
            or actor_obj.has(Permission.MAIL_READ)
        ):
            raise AccessDenied(
                f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks case target access"
            )
        return

    if ref.startswith("marketing:"):
        if not _TARGET_MARKETING_RE.fullmatch(ref):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        if not actor_obj.has(Permission.MARKETING_READ):
            raise AccessDenied(
                f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks marketing target access"
            )
        return

    if ref.startswith("product:"):
        if not _TARGET_PRODUCT_RE.fullmatch(ref):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        if not actor_obj.has(Permission.WEB_SEARCH):
            raise AccessDenied(
                f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks product research access"
            )
        return

    if ref.startswith("chat:"):
        if not _TARGET_CHAT_RE.fullmatch(ref):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        return

    if ref.startswith("seo:"):
        if not _TARGET_SEO_RE.fullmatch(ref):
            raise SearchPolicyError(f"Invalid target_ref: {ref}")
        if not (
            actor_obj.has(Permission.MARKETING_READ)
            or actor_obj.has(Permission.WP_OPS_READ)
        ):
            raise AccessDenied(
                f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks SEO research target access"
            )
        return

    raise SearchPolicyError(f"Unsupported target_ref: {ref}")


def authorize_research_access(
    actor: str,
    job: ResearchJob,
    action: Literal["view", "list", "export", "steer", "cancel", "pin"],
) -> bool:
    """Check whether ``actor`` may perform ``action`` on ``job``."""
    try:
        actor_obj = resolve_actor(actor)
    except AccessDenied:
        return False

    actor_name = actor_obj.name.strip().lower()
    job_actor = (job.actor or "").strip().lower()
    effective_ref = job.target_ref or (job.spec.target_ref if job.spec is not None else None)
    is_wp = _is_wp_target_ref(effective_ref)

    if action in {"steer", "cancel", "pin"}:
        if is_wp and not (
            (actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(Permission.CODE_EDIT))
            or (job_actor == actor_name and actor_obj.has(Permission.WP_OPS_OPERATE))
        ):
            return False
        return job_actor == actor_name or actor_obj.has(Permission.RESEARCH_ADMIN)

    if action in {"view", "list", "export"}:
        if is_wp:
            return (
                actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(Permission.CODE_EDIT)
            ) or (
                job_actor == actor_name and actor_obj.has(Permission.WP_OPS_OPERATE)
            )
        return (
            job_actor == actor_name
            or actor_obj.has(Permission.RESEARCH_ADMIN)
            or job.visibility == "team"
        )

    return False


class SearchCache:
    """SQLite WAL TTL + LRU cache for sanitized search and fetch payloads."""

    def __init__(self, path: Path | None = None, *, max_entries: int | None = None) -> None:
        self.path = Path(path) if path is not None else (_default_data_dir() / "web_search_cache.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cfg = load_research_config().cache
        self.max_entries = max(1, int(max_entries if max_entries is not None else cfg.max_entries))
        self._init_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires
                    ON search_cache(expires_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_lru
                    ON search_cache(last_accessed_at, created_at);
                """
            )
        _chmod_sqlite_files(self.path)

    @staticmethod
    def make_key(
        kind: str,
        query_or_url: str,
        *,
        profile_id: str = "",
        locale: str = "",
        filters: dict[str, Any] | None = None,
    ) -> str:
        """Compute deterministic SHA-256 key over normalized cache coordinates."""
        norm_kind = str(kind or "search").strip().lower()
        raw_target = str(query_or_url or "").strip()
        if norm_kind == "fetch":
            norm_target = canonicalize_url(raw_target) or raw_target.lower()
        else:
            norm_target = " ".join(raw_target.split()).lower()

        norm_filters: dict[str, Any] = {}
        if isinstance(filters, dict):
            for k, v in sorted(filters.items()):
                if isinstance(v, (list, tuple, set)):
                    norm_filters[str(k)] = sorted(str(x).strip().lower() for x in v if str(x).strip())
                elif v is not None:
                    norm_filters[str(k)] = v

        canonical_obj = {
            "kind": norm_kind,
            "target": norm_target,
            "profile_id": str(profile_id or "").strip().lower(),
            "locale": str(locale or "").strip().lower(),
            "filters": norm_filters,
        }
        encoded = json.dumps(canonical_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def get(self, key: str, *, now_ts: float | None = None) -> Any | None:
        """Return cached sanitized payload if unexpired, updating LRU timestamp."""
        now = float(now_ts) if now_ts is not None else _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json, expires_at FROM search_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            if float(row["expires_at"]) <= now:
                conn.execute("DELETE FROM search_cache WHERE cache_key=?", (key,))
                conn.commit()
                return None
            conn.execute(
                "UPDATE search_cache SET last_accessed_at=? WHERE cache_key=?",
                (now, key),
            )
            conn.commit()
        return _json_loads(row["payload_json"], None)

    def set(
        self,
        key: str,
        payload: Any,
        *,
        ttl_seconds: float | int,
        kind: str = "search",
        now_ts: float | None = None,
    ) -> None:
        """Sanitize and store ``payload`` under ``key``, enforcing expiry and LRU cap."""
        ttl = float(ttl_seconds)
        if ttl <= 0:
            return
        now = float(now_ts) if now_ts is not None else _now()
        sanitized = _sanitize_payload_value(payload)
        encoded = _json_dumps(sanitized)
        size_bytes = len(encoded.encode("utf-8"))
        expires_at = now + ttl

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM search_cache WHERE expires_at <= ?", (now,))
            conn.execute(
                """
                INSERT INTO search_cache(
                    cache_key, kind, payload_json, size_bytes, created_at, last_accessed_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    kind=excluded.kind,
                    payload_json=excluded.payload_json,
                    size_bytes=excluded.size_bytes,
                    last_accessed_at=excluded.last_accessed_at,
                    expires_at=excluded.expires_at
                """,
                (key, str(kind or "search").strip().lower(), encoded, size_bytes, now, now, expires_at),
            )
            count = int(conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0])
            excess = count - self.max_entries
            if excess > 0:
                conn.execute(
                    """
                    DELETE FROM search_cache WHERE cache_key IN (
                        SELECT cache_key FROM search_cache
                        ORDER BY last_accessed_at ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                )
            conn.commit()
        _chmod_sqlite_files(self.path)

    def purge_expired(self, now_ts: float | None = None) -> int:
        """Delete all expired cache entries and return the number of rows removed."""
        now = float(now_ts) if now_ts is not None else _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM search_cache WHERE expires_at <= ?", (now,))
            deleted = int(cur.rowcount or 0)
            conn.commit()
        return deleted

    def stats(self, now_ts: float | None = None) -> dict[str, int]:
        now = float(now_ts) if now_ts is not None else _now()
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END) AS expired,
                    COALESCE(SUM(size_bytes), 0) AS total_bytes
                FROM search_cache
                """,
                (now,),
            ).fetchone()
        return {
            "total_entries": int(row["total"] or 0),
            "expired_entries": int(row["expired"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
        }


class ResearchStore:
    """Durable SQLite job/steer/event store and symlink-safe artifact persistence."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        artifacts_dir: Path | None = None,
    ) -> None:
        base_dir = _default_data_dir()
        self.path = Path(path) if path is not None else (base_dir / "research_jobs.sqlite3")
        self.artifacts_dir = (
            Path(artifacts_dir) if artifacts_dir is not None else (base_dir / "research_artifacts")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_jobs (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    peer_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    profile_id TEXT NOT NULL DEFAULT 'deep_general',
                    depth TEXT NOT NULL DEFAULT 'standard',
                    topic TEXT NOT NULL DEFAULT '',
                    spec_json TEXT NOT NULL,
                    target_ref TEXT,
                    visibility TEXT NOT NULL DEFAULT 'private',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    status_message_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    evidence_status TEXT,
                    rounds_completed INTEGER NOT NULL DEFAULT 0,
                    queries_used INTEGER NOT NULL DEFAULT 0,
                    fetches_used INTEGER NOT NULL DEFAULT 0,
                    llm_calls_used INTEGER NOT NULL DEFAULT 0,
                    estimated_search_usd REAL NOT NULL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_research_jobs_status_created
                    ON research_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_research_jobs_actor_created
                    ON research_jobs(actor, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_jobs_target_ref
                    ON research_jobs(target_ref);

                CREATE TABLE IF NOT EXISTS research_steers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_research_steers_job
                    ON research_steers(job_id, consumed_at, id);

                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    round_num INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    provenance TEXT NOT NULL DEFAULT 'live',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_events_job_seq
                    ON research_events(job_id, seq);
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(research_jobs)")}
            if "cancel_requested" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
            if "llm_calls_used" not in cols:
                conn.execute("ALTER TABLE research_jobs ADD COLUMN llm_calls_used INTEGER NOT NULL DEFAULT 0")
            if "estimated_search_usd" not in cols:
                conn.execute(
                    "ALTER TABLE research_jobs ADD COLUMN estimated_search_usd REAL NOT NULL DEFAULT 0.0"
                )
        _chmod_sqlite_files(self.path)

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> ResearchJob | None:
        if row is None:
            return None
        spec_dict = _json_loads(row["spec_json"], {})
        spec = ResearchSpec.from_dict(spec_dict) if isinstance(spec_dict, dict) else None
        ev_raw = row["evidence_status"]
        evidence_status: EvidenceStatus | None = None
        if ev_raw:
            try:
                evidence_status = EvidenceStatus(str(ev_raw))
            except ValueError:
                evidence_status = None
        return ResearchJob(
            research_id=str(row["id"]),
            actor=str(row["actor"]),
            status=str(row["status"]),
            spec=spec,
            target_ref=row["target_ref"],
            visibility=str(row["visibility"] or "private"),
            evidence_status=evidence_status,
            pinned=bool(row["pinned"]),
            cancel_requested=bool(row["cancel_requested"]),
            delivery_state=str(row["delivery_state"] or "pending"),
            channel=str(row["channel"] or ""),
            peer_id=str(row["peer_id"] or ""),
            status_message_id=row["status_message_id"],
            lease_owner=row["lease_owner"],
            lease_expires_at=_ts_to_iso(row["lease_expires_at"]),
            current_round=int(row["rounds_completed"] or 0),
            queries_used=int(row["queries_used"] or 0),
            fetches_used=int(row["fetches_used"] or 0),
            llm_calls_used=int(row["llm_calls_used"] or 0),
            estimated_search_usd=float(row["estimated_search_usd"] or 0.0),
            error_class=row["error_code"],
            error_message=row["error_message"],
            created_at=_ts_to_iso(row["created_at"]) or "",
            updated_at=_ts_to_iso(row["updated_at"]) or "",
            completed_at=_ts_to_iso(row["completed_at"]),
        )

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        job_id: str,
        *,
        phase: str,
        message: str,
        round_num: int = 0,
        provenance: str = "live",
        created_at: float | None = None,
    ) -> int:
        ts = float(created_at) if created_at is not None else _now()
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM research_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        clean_msg = sanitize_untrusted_text(message, max_chars=240)
        conn.execute(
            """
            INSERT INTO research_events(job_id, seq, phase, round_num, message, provenance, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                seq,
                str(phase or "info")[:40],
                max(0, int(round_num)),
                clean_msg,
                str(provenance or "live")[:24],
                ts,
            ),
        )
        return seq

    def enqueue_job(
        self,
        spec: ResearchSpec,
        *,
        actor: str,
        channel: str = "",
        peer_id: str = "",
        target_ref: str | None = None,
        visibility: str = "private",
        research_id: str | None = None,
    ) -> ResearchJob:
        """Validate actor/target_ref RBAC and persist a new queued ``ResearchJob``."""
        actor_obj = resolve_actor(actor)
        effective_ref = target_ref if target_ref is not None else spec.target_ref
        effective_ref = str(effective_ref).strip() if effective_ref else None
        authorize_target_ref(actor_obj.name, effective_ref)

        vis = str(visibility or "private").strip().lower()
        if vis not in {"private", "team"}:
            raise SearchPolicyError(f"Invalid visibility: {visibility}")
        if _is_wp_target_ref(effective_ref):
            vis = "private"

        jid = research_id or new_research_id()
        if RESEARCH_ID_RE.fullmatch(jid) is None:
            raise SearchPolicyError(f"Invalid research_id: {jid}")

        clean_topic = sanitize_untrusted_text(spec.topic, max_chars=500)
        spec_dict = spec.to_dict()
        spec_dict["topic"] = clean_topic
        spec_dict["target_ref"] = effective_ref
        now = _now()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO research_jobs(
                    id, actor, channel, peer_id, status, profile_id, depth, topic,
                    spec_json, target_ref, visibility, pinned, cancel_requested,
                    created_at, updated_at, delivery_state
                ) VALUES(?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'pending')
                """,
                (
                    jid,
                    actor_obj.name,
                    str(channel or "")[:32],
                    str(peer_id or "")[:64],
                    str(spec.profile_id or "deep_general")[:64],
                    str(spec.depth or "standard")[:32],
                    clean_topic,
                    _json_dumps(spec_dict),
                    effective_ref,
                    vis,
                    now,
                    now,
                ),
            )
            self._insert_event(
                conn,
                jid,
                phase="queued",
                message="Research job queued",
                round_num=0,
                provenance="system",
                created_at=now,
            )
            row = conn.execute("SELECT * FROM research_jobs WHERE id=?", (jid,)).fetchone()
            conn.commit()
        job = self._row_to_job(row)
        assert job is not None
        return job

    def get_job(self, job_id: str, *, actor: str | None = None) -> ResearchJob | None:
        """Fetch a job by ID, enforcing ``authorize_research_access`` when ``actor`` is given."""
        if not RESEARCH_ID_RE.fullmatch(str(job_id or "")):
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        job = self._row_to_job(row)
        if job is None:
            return None
        if actor is not None and not authorize_research_access(actor, job, "view"):
            raise AccessDenied(f"Actor {actor!r} cannot view research job {job_id}")
        return job

    def list_jobs(
        self,
        *,
        actor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        target_ref: str | None = None,
    ) -> list[ResearchJob]:
        """List jobs applying server-side visibility and non-``wp:*`` RBAC filtering."""
        lim = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[Any] = []

        if status:
            clauses.append("status=?")
            params.append(str(status).strip())
        if target_ref:
            clauses.append("target_ref=?")
            params.append(str(target_ref).strip())

        actor_obj = resolve_actor(actor) if actor is not None else None
        if actor_obj is not None and not actor_obj.has(Permission.RESEARCH_ADMIN):
            if actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(Permission.CODE_EDIT):
                clauses.append(
                    "(actor=? OR visibility='team' OR target_ref LIKE 'wp:%' OR target_ref LIKE 'wp_site:%')"
                )
                params.append(actor_obj.name)
            else:
                clauses.append(
                    "(actor=? OR (visibility='team' AND COALESCE(target_ref, '') NOT LIKE 'wp:%' "
                    "AND COALESCE(target_ref, '') NOT LIKE 'wp_site:%'))"
                )
                params.append(actor_obj.name)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM research_jobs {where_sql} ORDER BY created_at DESC, id DESC LIMIT ?"
        fetch_lim = lim if actor_obj is None else min(500, lim * 4)

        with self._connection() as conn:
            rows = conn.execute(sql, (*params, fetch_lim)).fetchall()

        out: list[ResearchJob] = []
        for row in rows:
            job = self._row_to_job(row)
            if job is None:
                continue
            if actor is not None and not authorize_research_access(actor, job, "list"):
                continue
            out.append(job)
            if len(out) >= lim:
                break
        return out

    def _recover_expired_leases_in_tx(self, conn: sqlite3.Connection, now: float) -> int:
        active_marks = ",".join("?" for _ in _ACTIVE_STATUSES)
        rows = conn.execute(
            f"""
            SELECT id FROM research_jobs
            WHERE status IN ({active_marks})
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (*_ACTIVE_STATUSES, now),
        ).fetchall()
        recovered = 0
        for row in rows:
            jid = str(row["id"])
            conn.execute(
                """
                UPDATE research_jobs
                SET status='queued', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                WHERE id=?
                """,
                (now, jid),
            )
            conn.execute(
                "UPDATE research_steers SET consumed_at=NULL WHERE job_id=?",
                (jid,),
            )
            self._insert_event(
                conn,
                jid,
                phase="recovered",
                message="Worker lease expired; job re-queued",
                round_num=0,
                provenance="system",
                created_at=now,
            )
            recovered += 1
        return recovered

    def claim_next(
        self,
        worker_id: str,
        lease_s: int = 120,
        *,
        max_concurrent: int | None = None,
    ) -> ResearchJob | None:
        """Atomically claim the next queued job up to ``max_concurrent`` active jobs."""
        cap = max(
            1,
            int(
                max_concurrent
                if max_concurrent is not None
                else load_research_config().budgets.max_concurrent_jobs
            ),
        )
        now = _now()
        lease_until = now + max(1, int(lease_s))
        active_marks = ",".join("?" for _ in _ACTIVE_STATUSES)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_expired_leases_in_tx(conn, now)
            active_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM research_jobs
                    WHERE status IN ({active_marks})
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (*_ACTIVE_STATUSES, now),
                ).fetchone()[0]
            )
            if active_count >= cap:
                conn.commit()
                return None

            row = conn.execute(
                "SELECT id FROM research_jobs WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            job_id = str(row["id"])
            changed = conn.execute(
                """
                UPDATE research_jobs
                SET status='planning', lease_owner=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status='queued' AND cancel_requested=0
                """,
                (str(worker_id), lease_until, now, job_id),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None

            self._insert_event(
                conn,
                job_id,
                phase="planning",
                message="Claimed by worker",
                round_num=0,
                provenance="system",
                created_at=now,
            )
            claimed = conn.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
        return self._row_to_job(claimed)

    def heartbeat(self, job_id: str, worker_id: str, lease_s: int = 120) -> bool:
        """Extend the active lease for ``job_id`` owned by ``worker_id``."""
        now = _now()
        lease_until = now + max(1, int(lease_s))
        active_marks = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""
                UPDATE research_jobs
                SET lease_expires_at=?, updated_at=?
                WHERE id=? AND lease_owner=? AND status IN ({active_marks})
                """,
                (lease_until, now, job_id, str(worker_id), *_ACTIVE_STATUSES),
            )
            ok = cur.rowcount == 1
            conn.commit()
        return ok

    def recover_stale_leases(self, now_ts: float | None = None) -> int:
        """Re-queue active jobs whose lease has expired and return recovered count."""
        now = float(now_ts) if now_ts is not None else _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recovered = self._recover_expired_leases_in_tx(conn, now)
            conn.commit()
        return recovered

    def update_progress(
        self,
        job_id: str,
        *,
        status: str | None = None,
        rounds_completed: int | None = None,
        queries_used: int | None = None,
        fetches_used: int | None = None,
        llm_calls_used: int | None = None,
        estimated_search_usd: float | None = None,
        delivery_state: str | None = None,
        status_message_id: str | None = None,
    ) -> bool:
        """Update job phase/counters cleanly."""
        assignments: list[str] = ["updated_at=?"]
        params: list[Any] = [_now()]
        if status is not None:
            assignments.append("status=?")
            params.append(str(status))
        if rounds_completed is not None:
            assignments.append("rounds_completed=?")
            params.append(max(0, int(rounds_completed)))
        if queries_used is not None:
            assignments.append("queries_used=?")
            params.append(max(0, int(queries_used)))
        if fetches_used is not None:
            assignments.append("fetches_used=?")
            params.append(max(0, int(fetches_used)))
        if llm_calls_used is not None:
            assignments.append("llm_calls_used=?")
            params.append(max(0, int(llm_calls_used)))
        if estimated_search_usd is not None:
            assignments.append("estimated_search_usd=?")
            params.append(max(0.0, float(estimated_search_usd)))
        if delivery_state is not None:
            assignments.append("delivery_state=?")
            params.append(str(delivery_state))
        if status_message_id is not None:
            assignments.append("status_message_id=?")
            params.append(str(status_message_id))

        params.append(job_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE research_jobs SET {', '.join(assignments)} WHERE id=?",
                params,
            )
            ok = cur.rowcount == 1
            conn.commit()
        return ok

    def complete_job(
        self,
        job_id: str,
        *,
        status: str = "completed",
        evidence_status: EvidenceStatus | str | None = None,
        brief: ResearchBrief | None = None,
        sources: list[EvaluatedSource] | None = None,
        rounds_completed: int | None = None,
        queries_used: int | None = None,
        fetches_used: int | None = None,
        llm_calls_used: int | None = None,
        estimated_search_usd: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        worker_id: str | None = None,
    ) -> ResearchJob | None:
        """Persist completion state and write sanitized artifacts atomically."""
        now = _now()
        target_status = str(status)
        ev_str = str(evidence_status) if evidence_status is not None else (
            str(brief.evidence_status) if brief is not None else None
        )
        eff_err_code = error_code
        eff_err_msg = error_message
        write_brief = brief
        write_sources = sources

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if str(row["status"]) in _TERMINAL_STATUSES:
                conn.commit()
                return self._row_to_job(row)
            if worker_id is not None and str(row["lease_owner"] or "") != str(worker_id):
                conn.commit()
                return self._row_to_job(row)
            if int(row["cancel_requested"] or 0) == 1 and target_status != "cancelled":
                target_status = "cancelled"
                eff_err_code = eff_err_code or "cancelled"
                eff_err_msg = eff_err_msg or "Cancelled by user request"
                ev_str = row["evidence_status"]
                write_brief = None
                write_sources = None

            clean_err = sanitize_untrusted_text(eff_err_msg, max_chars=400) if eff_err_msg else None
            r_done = int(rounds_completed if rounds_completed is not None else row["rounds_completed"])
            q_used = int(queries_used if queries_used is not None else row["queries_used"])
            f_used = int(fetches_used if fetches_used is not None else row["fetches_used"])
            l_used = int(llm_calls_used if llm_calls_used is not None else row["llm_calls_used"])
            usd_used = float(
                estimated_search_usd if estimated_search_usd is not None else row["estimated_search_usd"]
            )

            conn.execute(
                """
                UPDATE research_jobs
                SET status=?, evidence_status=?, rounds_completed=?, queries_used=?, fetches_used=?,
                    llm_calls_used=?, estimated_search_usd=?, error_code=?, error_message=?,
                    lease_owner=NULL, lease_expires_at=NULL, completed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    target_status,
                    ev_str,
                    r_done,
                    q_used,
                    f_used,
                    l_used,
                    usd_used,
                    str(eff_err_code)[:64] if eff_err_code else None,
                    clean_err,
                    now,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                conn,
                job_id,
                phase=target_status,
                message=f"Research {target_status}",
                round_num=r_done,
                provenance=write_brief.provenance if write_brief is not None else "system",
                created_at=now,
            )
            updated = conn.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()

        job = self._row_to_job(updated)
        if write_brief is not None and job is not None and job.status != "cancelled":
            self.write_artifacts(job, brief=write_brief, sources=write_sources or [])
        return job

    def add_steer(
        self,
        job_id: str,
        actor: str,
        note: str,
        *,
        taint: QueryTaintContext | None = None,
    ) -> int:
        """Append a sanitized steer note for ``job_id`` after ownership/RBAC verification."""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Unknown research job: {job_id}")
        if not authorize_research_access(actor, job, "steer"):
            raise AccessDenied(f"Actor {actor!r} cannot steer research job {job_id}")
        if job.status in _TERMINAL_STATUSES:
            raise ValueError(f"Research job {job_id} is already {job.status}")

        checked_note = OutboundQueryFirewall().check_query(note, taint=taint)
        clean_note = sanitize_untrusted_text(checked_note, max_chars=250)
        if not clean_note:
            raise SearchPolicyError("Empty steer note")

        actor_obj = resolve_actor(actor)
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                raise KeyError(f"Unknown research job: {job_id}")
            if str(row["status"]) in _TERMINAL_STATUSES:
                conn.commit()
                raise ValueError(f"Research job {job_id} is already {row['status']}")
            steer_count = int(
                conn.execute("SELECT COUNT(*) FROM research_steers WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            if steer_count >= MAX_STEERS_PER_JOB:
                conn.commit()
                raise ValueError(f"Max {MAX_STEERS_PER_JOB} steering notes per job reached")
            cur = conn.execute(
                "INSERT INTO research_steers(job_id, actor, note, created_at) VALUES(?, ?, ?, ?)",
                (job_id, actor_obj.name, clean_note, now),
            )
            steer_id = int(cur.lastrowid or 0)
            self._insert_event(
                conn,
                job_id,
                phase="steer_queued",
                message="Follow-up steering note queued",
                round_num=job.current_round,
                provenance="system",
                created_at=now,
            )
            conn.commit()
        return steer_id

    def consume_steers(self, job_id: str) -> list[str]:
        """Atomically mark pending steers for ``job_id`` consumed and return their notes."""
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, note FROM research_steers WHERE job_id=? AND consumed_at IS NULL ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            if not rows:
                conn.commit()
                return []
            ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE research_steers SET consumed_at=? WHERE id IN ({marks})",
                (now, *ids),
            )
            self._insert_event(
                conn,
                job_id,
                phase="steer_applied",
                message=f"Applied {len(ids)} steering note(s)",
                round_num=0,
                provenance="system",
                created_at=now,
            )
            conn.commit()
        return [str(r["note"]) for r in rows]

    def append_event(
        self,
        job_id: str,
        *,
        phase: str,
        message: str,
        round_num: int = 0,
        provenance: str = "live",
    ) -> int:
        """Append a sequential event to ``research_events`` and return its ``seq``."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = self._insert_event(
                conn,
                job_id,
                phase=phase,
                message=message,
                round_num=round_num,
                provenance=provenance,
            )
            conn.commit()
        return seq

    def list_events(self, job_id: str, *, since_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Return ordered events for ``job_id`` with ``seq > since_seq``."""
        lim = max(1, min(int(limit), 500))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, seq, phase, round_num, message, provenance, created_at
                FROM research_events
                WHERE job_id=? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (job_id, int(since_seq), lim),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "job_id": str(r["job_id"]),
                "seq": int(r["seq"]),
                "phase": str(r["phase"]),
                "round_num": int(r["round_num"]),
                "message": str(r["message"]),
                "provenance": str(r["provenance"]),
                "created_at": _ts_to_iso(r["created_at"]),
            }
            for r in rows
        ]

    def request_cancel(self, job_id: str, actor: str) -> str:
        """Request cooperative cancellation (or cancel queued job immediately)."""
        job = self.get_job(job_id)
        if job is None:
            return "not_found"
        if not authorize_research_access(actor, job, "cancel"):
            raise AccessDenied(f"Actor {actor!r} cannot cancel research job {job_id}")

        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM research_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                conn.commit()
                return "not_found"
            cur_status = str(row["status"])
            if cur_status in _TERMINAL_STATUSES:
                conn.commit()
                return "already_terminal"
            if cur_status == "queued":
                conn.execute(
                    """
                    UPDATE research_jobs
                    SET status='cancelled', cancel_requested=1, completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, job_id),
                )
                self._insert_event(
                    conn,
                    job_id,
                    phase="cancelled",
                    message="Research job cancelled before start",
                    round_num=0,
                    provenance="system",
                    created_at=now,
                )
                conn.commit()
                return "cancelled"

            conn.execute(
                "UPDATE research_jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                (now, job_id),
            )
            self._insert_event(
                conn,
                job_id,
                phase="cancel_requested",
                message="Cancellation requested",
                round_num=job.current_round,
                provenance="system",
                created_at=now,
            )
            conn.commit()
        return "cancel_requested"

    def set_pinned(self, job_id: str, actor: str, pinned: bool = True) -> bool:
        """Pin or unpin a research job against retention GC."""
        job = self.get_job(job_id)
        if job is None:
            return False
        if not authorize_research_access(actor, job, "pin"):
            raise AccessDenied(f"Actor {actor!r} cannot pin research job {job_id}")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE research_jobs SET pinned=?, updated_at=? WHERE id=?",
                (1 if pinned else 0, _now(), job_id),
            )
            ok = cur.rowcount == 1
            conn.commit()
        return ok

    def unlink_target_ref(self, target_ref: str) -> int:
        """Atomically unlink ``target_ref`` (e.g. ``case:<uuid>``) from SQLite jobs."""
        ref = str(target_ref or "").strip()
        if not ref:
            return 0
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, spec_json FROM research_jobs WHERE target_ref=?",
                (ref,),
            ).fetchall()
            for row in rows:
                spec_dict = _json_loads(row["spec_json"], {})
                if isinstance(spec_dict, dict) and "target_ref" in spec_dict:
                    spec_dict["target_ref"] = None
                    new_spec_json = _json_dumps(spec_dict)
                else:
                    new_spec_json = str(row["spec_json"])
                conn.execute(
                    """
                    UPDATE research_jobs
                    SET target_ref=NULL, spec_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (new_spec_json, now, str(row["id"])),
                )
            conn.commit()
        return len(rows)

    def _safe_job_artifact_dir(self, job_id: str, *, create: bool = False) -> Path:
        if RESEARCH_ID_RE.fullmatch(str(job_id or "")) is None:
            raise SearchPolicyError(f"Invalid research_id: {job_id}")
        if self.artifacts_dir.is_symlink():
            raise SearchPolicyError("ArtifactSymlinkRefused")
        if create:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            _chmod_path(self.artifacts_dir, 0o700)
        job_dir = self.artifacts_dir / job_id
        if self.artifacts_dir.exists():
            _assert_safe_contained_non_symlink(job_dir, self.artifacts_dir)
        if create:
            job_dir.mkdir(parents=True, exist_ok=True)
            _chmod_path(job_dir, 0o700)
            _assert_safe_contained_non_symlink(job_dir, self.artifacts_dir)
        return job_dir

    def write_artifacts(
        self,
        job: ResearchJob,
        *,
        brief: ResearchBrief,
        sources: list[EvaluatedSource],
    ) -> Path:
        """Write ``brief.md``, ``brief.json``, ``sources.json``, ``meta.json`` safely.

        Invariant (#17.8): ``target_ref`` is strictly omitted from all disk artifacts.
        """
        job_dir = self._safe_job_artifact_dir(job.research_id, create=True)
        brief_payload = brief.to_dict()
        brief_payload.pop("target_ref", None)

        sources_payload: list[dict[str, Any]] = []
        for src in sources:
            entry = src.to_dict()
            entry.pop("target_ref", None)
            sources_payload.append(entry)

        meta_payload = {
            "research_id": job.research_id,
            "actor": job.actor,
            "topic": sanitize_untrusted_text(brief.topic or (job.spec.topic if job.spec else ""), max_chars=500),
            "profile_id": brief.profile_id,
            "depth": job.spec.depth if job.spec is not None else "standard",
            "status": job.status,
            "evidence_status": str(brief.evidence_status),
            "provenance": brief.provenance,
            "visibility": job.visibility,
            "pinned": job.pinned,
            "rounds_completed": job.current_round,
            "queries_used": job.queries_used,
            "fetches_used": job.fetches_used,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

        md_text = brief.markdown.strip() if brief.markdown else self._render_markdown(brief)

        files_to_write = {
            "brief.md": md_text,
            "brief.json": json.dumps(brief_payload, ensure_ascii=False, indent=2),
            "sources.json": json.dumps(sources_payload, ensure_ascii=False, indent=2),
            "meta.json": json.dumps(meta_payload, ensure_ascii=False, indent=2),
        }
        for filename, content in files_to_write.items():
            dest = job_dir / filename
            _assert_safe_contained_non_symlink(dest, job_dir)
            _atomic_write_text(dest, content, mode=0o600)
        return job_dir

    @staticmethod
    def _render_markdown(brief: ResearchBrief) -> str:
        lines = [
            f"# {brief.topic or brief.research_id}",
            "",
            f"- ID: `{brief.research_id}`",
            f"- Profile: `{brief.profile_id}`",
            f"- Evidence: `{brief.evidence_status}` ({brief.provenance})",
            "",
            "## Summary",
            brief.summary or "_No summary available._",
        ]
        if brief.citations:
            lines.extend(["", "## Sources"])
            for cit in brief.citations:
                lines.append(f"- [{cit.id}] {cit.title} ({cit.domain}) — {cit.url}")
        return "\n".join(lines).strip() + "\n"

    def read_brief(self, job_id: str, *, actor: str | None = None) -> dict[str, Any]:
        """Read ``brief.json`` + metadata for ``job_id``, returning ``artifact_unavailable`` if missing."""
        if not RESEARCH_ID_RE.fullmatch(str(job_id or "")):
            return {
                "ok": False,
                "status": "artifact_unavailable",
                "error_class": "artifact_unavailable",
                "research_id": str(job_id or ""),
                "message": "Invalid research ID.",
            }
        if actor is not None:
            job = self.get_job(job_id, actor=actor)
            if job is None:
                return {
                    "ok": False,
                    "status": "not_found",
                    "error_class": "not_found",
                    "research_id": job_id,
                    "message": "Research job not found.",
                }

        job_dir = self._safe_job_artifact_dir(job_id, create=False)
        if not job_dir.is_dir():
            return {
                "ok": False,
                "status": "artifact_unavailable",
                "error_class": "artifact_unavailable",
                "research_id": job_id,
                "message": "Research artifacts are unavailable on disk.",
            }

        brief_path = job_dir / "brief.json"
        _assert_safe_contained_non_symlink(brief_path, job_dir)
        if not brief_path.is_file():
            return {
                "ok": False,
                "status": "artifact_unavailable",
                "error_class": "artifact_unavailable",
                "research_id": job_id,
                "message": "Research brief artifact is unavailable on disk.",
            }

        brief_data = _json_loads(brief_path.read_text(encoding="utf-8"), None)
        if not isinstance(brief_data, dict):
            return {
                "ok": False,
                "status": "artifact_unavailable",
                "error_class": "artifact_unavailable",
                "research_id": job_id,
                "message": "Research brief artifact is unreadable.",
            }

        sources_path = job_dir / "sources.json"
        sources_data: list[dict[str, Any]] = []
        if sources_path.exists():
            _assert_safe_contained_non_symlink(sources_path, job_dir)
            if sources_path.is_file():
                loaded_sources = _json_loads(sources_path.read_text(encoding="utf-8"), [])
                if isinstance(loaded_sources, list):
                    sources_data = loaded_sources

        meta_path = job_dir / "meta.json"
        meta_data: dict[str, Any] = {}
        if meta_path.exists():
            _assert_safe_contained_non_symlink(meta_path, job_dir)
            if meta_path.is_file():
                loaded_meta = _json_loads(meta_path.read_text(encoding="utf-8"), {})
                if isinstance(loaded_meta, dict):
                    meta_data = loaded_meta

        return {
            "ok": True,
            "status": "ok",
            "research_id": job_id,
            "brief": brief_data,
            "sources": sources_data,
            "meta": meta_data,
        }


__all__ = [
    "ResearchStore",
    "SearchCache",
    "authorize_research_access",
    "authorize_target_ref",
]
