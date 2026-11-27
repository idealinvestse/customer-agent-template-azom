"""SQLite WAL persistence, zero-PII scrubbing, and atomic dossier artifacts for Shop Intelligence."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.research.sanitizer import sanitize_untrusted_text
from ecom_ops.shop_intel.models import (
    SHOP_INTEL_RUN_ID_RE,
    IntelDiff,
    IntelFact,
    IntelRun,
    IntelSchedule,
    SecurityFinding,
    utc_now_iso,
)
from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.store import atomic_write_text

_DB_NAME = "shop_intel.sqlite3"
_ARTIFACTS_DIR = "shop_intel_artifacts"
_LOCK = threading.Lock()
_SAFE_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_EMAIL_PII_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_NORDIC_PHONE_PII_RE = re.compile(
    r"(?:(?<!\d)(?:\+|00)(?:46|47|45)[\s.-]?(?:\(0\)[\s.-]?)?(?:\d[\s.-]?){7,10}\b|"
    r"(?<!\d)07[02369][\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d))"
)
_SE_PERSONNUMMER_PII_RE = re.compile(
    r"(?<!\d)(?:19|20)?\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-+]\d{4}(?!\d)"
)
_NO_FODSELSNUMMER_PII_RE = re.compile(
    r"(?<!\d)(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])\d{2}\s?\d{5}(?!\d)"
)


def scrub_text(text: Any, *, max_chars: int = 4000) -> str:
    """Neutralize prompt injections, secrets, and customer PII from collected text."""
    raw = str(text or "")
    cleaned = sanitize_untrusted_text(raw, max_chars=max(max_chars, 8000))
    cleaned = redact_secret_text(cleaned)
    cleaned = _EMAIL_PII_RE.sub("[EMAIL_REDACTED]", cleaned)
    cleaned = _SE_PERSONNUMMER_PII_RE.sub("[ID_REDACTED]", cleaned)
    cleaned = _NO_FODSELSNUMMER_PII_RE.sub("[ID_REDACTED]", cleaned)
    cleaned = _NORDIC_PHONE_PII_RE.sub("[PHONE_REDACTED]", cleaned)
    if max_chars > 0 and len(cleaned) > max_chars:
        if max_chars <= 3:
            return cleaned[:max_chars]
        return cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned


def scrub_payload(value: Any, *, max_str_chars: int = 4000, depth: int = 0) -> Any:
    """Recursively scrub strings and keys in nested payloads prior to persistence."""
    if depth > 16:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return scrub_text(value, max_chars=max_str_chars)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            clean_key = scrub_text(str(k), max_chars=200)
            out[clean_key] = scrub_payload(v, max_str_chars=max_str_chars, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_payload(item, max_str_chars=max_str_chars, depth=depth + 1) for item in value]
    return scrub_text(str(value), max_chars=max_str_chars)


def _chmod_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError:
            pass


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_site_id(site_id: str) -> str:
    clean = str(site_id or "").strip().lower()
    if not _SAFE_SITE_RE.fullmatch(clean):
        raise ValueError(f"Invalid site_id: {site_id!r}")
    return clean


class ShopIntelStore:
    """SQLite WAL store at ``AZOM_DATA_DIR/shop_intel.sqlite3`` with atomic dossier artifacts."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.db_path = self.data_dir / _DB_NAME
        self.artifacts_root = self.data_dir / _ARTIFACTS_DIR
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intel_runs (
                        run_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        provenance TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        status TEXT NOT NULL,
                        security_score INTEGER NOT NULL,
                        findings_count INTEGER NOT NULL,
                        diffs_count INTEGER NOT NULL,
                        cost_usd REAL NOT NULL DEFAULT 0.0,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_runs_site_created "
                    "ON intel_runs (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intel_facts (
                        fact_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        site_id TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        fact_key TEXT NOT NULL,
                        source TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        evidence_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_facts_site_domain "
                    "ON intel_facts (site_id, domain, observed_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_facts_run_domain "
                    "ON intel_facts (run_id, domain)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intel_diffs (
                        diff_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        prev_run_id TEXT NOT NULL,
                        curr_run_id TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        category TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_diffs_site_created "
                    "ON intel_diffs (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intel_security_findings (
                        finding_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        site_id TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        category TEXT NOT NULL,
                        code TEXT NOT NULL,
                        status TEXT NOT NULL,
                        title TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_findings_site_created "
                    "ON intel_security_findings (site_id, created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intel_findings_run_sev "
                    "ON intel_security_findings (run_id, severity)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intel_schedules (
                        site_id TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        interval_hours INTEGER NOT NULL DEFAULT 24,
                        mode TEXT NOT NULL DEFAULT 'full',
                        last_run_at TEXT,
                        next_run_at TEXT,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            _chmod_sqlite_files(self.db_path)

    # --- Runs & facts ----------------------------------------------------- #

    def save_run(
        self,
        run: IntelRun,
        *,
        facts: list[IntelFact] | None = None,
        diffs: list[IntelDiff] | None = None,
        findings: list[SecurityFinding] | None = None,
    ) -> None:
        clean_site = _normalize_site_id(run.site_id)
        if not SHOP_INTEL_RUN_ID_RE.fullmatch(run.run_id):
            raise ValueError(f"Invalid run_id: {run.run_id!r}")
        scrubbed_run = scrub_payload(run.to_dict())
        scrubbed_run["site_id"] = clean_site

        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO intel_runs (
                        run_id, site_id, mode, provenance, actor, status,
                        security_score, findings_count, diffs_count, cost_usd,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        clean_site,
                        str(scrubbed_run["mode"]),
                        str(scrubbed_run["provenance"]),
                        str(scrubbed_run["actor"]),
                        str(scrubbed_run["status"]),
                        int(scrubbed_run["security_score"]),
                        int(scrubbed_run["findings_count"]),
                        int(scrubbed_run["diffs_count"]),
                        float(scrubbed_run.get("cost_usd", 0.0)),
                        _json_dumps(scrubbed_run),
                        str(scrubbed_run["created_at"]),
                    ),
                )

                for idx, fact in enumerate(facts or []):
                    sfact = scrub_payload(fact.to_dict())
                    fact_id = f"{run.run_id}:{idx}:{sfact['domain']}:{sfact['key']}"
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO intel_facts (
                            fact_id, run_id, site_id, domain, fact_key, source,
                            confidence, evidence_hash, payload_json, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fact_id,
                            run.run_id,
                            clean_site,
                            str(sfact["domain"]),
                            str(sfact["key"]),
                            str(sfact["source"]),
                            float(sfact.get("confidence", 1.0)),
                            str(sfact.get("evidence_hash") or ""),
                            _json_dumps(sfact),
                            str(sfact.get("observed_at") or run.created_at),
                        ),
                    )

                for diff in diffs or []:
                    sdiff = scrub_payload(diff.to_dict())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO intel_diffs (
                            diff_id, site_id, prev_run_id, curr_run_id,
                            severity, domain, category, summary, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(sdiff["diff_id"]),
                            clean_site,
                            str(sdiff["prev_run_id"]),
                            str(sdiff["curr_run_id"]),
                            str(sdiff["severity"]),
                            str(sdiff["domain"]),
                            str(sdiff["category"]),
                            str(sdiff["summary"]),
                            _json_dumps(sdiff),
                            str(sdiff["created_at"]),
                        ),
                    )

                for finding in findings or []:
                    sfind = scrub_payload(finding.to_dict())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO intel_security_findings (
                            finding_id, run_id, site_id, severity, category,
                            code, status, title, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(sfind["finding_id"]),
                            run.run_id,
                            clean_site,
                            str(sfind["severity"]),
                            str(sfind["category"]),
                            str(sfind["code"]),
                            str(sfind["status"]),
                            str(sfind["title"]),
                            _json_dumps(sfind),
                            str(sfind["created_at"]),
                        ),
                    )
            _chmod_sqlite_files(self.db_path)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        clean_id = str(run_id or "").strip()
        if not SHOP_INTEL_RUN_ID_RE.fullmatch(clean_id):
            return None
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM intel_runs WHERE run_id = ?",
                (clean_id,),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def get_latest_run(self, site_id: str) -> dict[str, Any] | None:
        clean_site = _normalize_site_id(site_id)
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM intel_runs WHERE site_id = ? ORDER BY created_at DESC LIMIT 1",
                (clean_site,),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_runs(self, site_id: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                clean_site = _normalize_site_id(site_id)
                rows = conn.execute(
                    "SELECT payload_json FROM intel_runs WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (clean_site, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM intel_runs ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    def list_facts(
        self,
        *,
        run_id: str | None = None,
        site_id: str | None = None,
        domain: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 500))
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(str(run_id).strip())
        if site_id:
            clauses.append("site_id = ?")
            params.append(_normalize_site_id(site_id))
        if domain:
            clauses.append("domain = ?")
            params.append(str(domain).strip().lower())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT payload_json FROM intel_facts {where} ORDER BY observed_at DESC LIMIT ?"
        params.append(lim)
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    def list_diffs(
        self, site_id: str, *, run_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clean_site = _normalize_site_id(site_id)
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM intel_diffs WHERE site_id = ? AND curr_run_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (clean_site, str(run_id).strip(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM intel_diffs WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (clean_site, lim),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    def list_security_findings(
        self,
        site_id: str,
        *,
        run_id: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_site = _normalize_site_id(site_id)
        lim = max(1, min(int(limit), 500))
        clauses = ["site_id = ?"]
        params: list[Any] = [clean_site]
        if run_id:
            clauses.append("run_id = ?")
            params.append(str(run_id).strip())
        if severity:
            clauses.append("severity = ?")
            params.append(str(severity).strip().lower())
        where = " AND ".join(clauses)
        params.append(lim)
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM intel_security_findings WHERE {where} "
                "ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    # --- Schedules -------------------------------------------------------- #

    def save_schedule(self, schedule: IntelSchedule) -> None:
        clean_site = _normalize_site_id(schedule.site_id)
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO intel_schedules (
                        site_id, enabled, interval_hours, mode,
                        last_run_at, next_run_at, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_site,
                        1 if schedule.enabled else 0,
                        int(schedule.interval_hours),
                        str(schedule.mode),
                        schedule.last_run_at,
                        schedule.next_run_at,
                        scrub_text(schedule.updated_by, max_chars=80),
                        str(schedule.updated_at or utc_now_iso()),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def get_schedule(self, site_id: str) -> dict[str, Any] | None:
        clean_site = _normalize_site_id(site_id)
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM intel_schedules WHERE site_id = ?",
                (clean_site,),
            ).fetchone()
        if row is None:
            return None
        return {
            "site_id": row["site_id"],
            "enabled": bool(row["enabled"]),
            "interval_hours": int(row["interval_hours"]),
            "mode": row["mode"],
            "last_run_at": row["last_run_at"],
            "next_run_at": row["next_run_at"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }

    def list_schedules(self) -> list[dict[str, Any]]:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM intel_schedules ORDER BY site_id ASC").fetchall()
        return [
            {
                "site_id": row["site_id"],
                "enabled": bool(row["enabled"]),
                "interval_hours": int(row["interval_hours"]),
                "mode": row["mode"],
                "last_run_at": row["last_run_at"],
                "next_run_at": row["next_run_at"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    # --- Retention -------------------------------------------------------- #

    def prune_old_runs(
        self,
        site_id: str,
        *,
        max_runs: int = 30,
        retention_days: int = 90,
    ) -> int:
        clean_site = _normalize_site_id(site_id)
        cutoff_iso = (datetime.now(UTC) - timedelta(days=max(1, int(retention_days)))).isoformat()
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT run_id, created_at FROM intel_runs WHERE site_id = ? ORDER BY created_at DESC",
                    (clean_site,),
                ).fetchall()
                to_delete: list[str] = []
                for idx, row in enumerate(rows):
                    if idx >= max(1, int(max_runs)) or str(row["created_at"]) < cutoff_iso:
                        to_delete.append(str(row["run_id"]))
                if not to_delete:
                    return 0
                placeholders = ",".join("?" for _ in to_delete)
                conn.execute(
                    f"DELETE FROM intel_facts WHERE run_id IN ({placeholders})",
                    tuple(to_delete),
                )
                conn.execute(
                    f"DELETE FROM intel_security_findings WHERE run_id IN ({placeholders})",
                    tuple(to_delete),
                )
                conn.execute(
                    f"DELETE FROM intel_diffs WHERE curr_run_id IN ({placeholders})",
                    tuple(to_delete),
                )
                conn.execute(
                    f"DELETE FROM intel_runs WHERE run_id IN ({placeholders})",
                    tuple(to_delete),
                )
            _chmod_sqlite_files(self.db_path)
            return len(to_delete)

    # --- Artifacts -------------------------------------------------------- #

    def artifact_dir(self, site_id: str) -> Path:
        clean_site = _normalize_site_id(site_id)
        root = self.artifacts_root.resolve()
        target = (root / clean_site).resolve()
        if target.parent != root:
            raise ValueError("Invalid site artifact directory")
        return target

    def write_dossier_artifacts(
        self,
        site_id: str,
        *,
        run_payload: dict[str, Any],
        markdown_text: str,
    ) -> dict[str, str]:
        site_dir = self.artifact_dir(site_id)
        json_path = site_dir / "latest.json"
        md_path = site_dir / "latest.md"
        scrubbed_json = scrub_payload(run_payload)
        scrubbed_md = scrub_text(markdown_text, max_chars=48_000)
        atomic_write_text(json_path, _json_dumps(scrubbed_json), mode=0o600)
        atomic_write_text(md_path, scrubbed_md, mode=0o600)
        return {"json": str(json_path), "md": str(md_path)}

    def read_dossier_artifact(self, site_id: str, *, fmt: str = "json") -> str | None:
        site_dir = self.artifact_dir(site_id)
        name = "latest.md" if fmt in {"md", "markdown"} else "latest.json"
        path = site_dir / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")


__all__ = [
    "ShopIntelStore",
    "scrub_payload",
    "scrub_text",
]
