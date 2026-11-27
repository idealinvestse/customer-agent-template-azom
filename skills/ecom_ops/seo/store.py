"""Durable SQLite WAL store for SEO Studio (audits, fix plans, strategies, campaigns, link packages)."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.config import data_dir_path

_DB_NAME = "seo_studio.sqlite3"
_LOCK = threading.Lock()


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
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except json.JSONDecodeError:
        return None


class SeoStudioStore:
    """SQLite WAL persistence at ``AZOM_DATA_DIR/seo_studio.sqlite3`` (``0o600`` best-effort)."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.db_path = self.data_dir / _DB_NAME
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_audits (
                        audit_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        grade TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_audits_site_created "
                    "ON seo_audits (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_fix_plans (
                        plan_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        fix_type TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        plan_sha256 TEXT NOT NULL,
                        before_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_fix_plans_site_created "
                    "ON seo_fix_plans (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_fix_batches (
                        batch_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        audit_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        status TEXT NOT NULL,
                        plan_sha256 TEXT NOT NULL,
                        item_count INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_fix_batches_site_created "
                    "ON seo_fix_batches (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_strategies (
                        strategy_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        title TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_strategies_site_created "
                    "ON seo_strategies (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_campaigns (
                        campaign_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        strategy_id TEXT,
                        name TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_campaigns_site_created "
                    "ON seo_campaigns (site_id, created_at DESC)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_link_packages (
                        package_id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        market TEXT NOT NULL,
                        campaign_id TEXT,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        guardrail_blocked INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_seo_link_packages_site_created "
                    "ON seo_link_packages (site_id, created_at DESC)"
                )
            _chmod_sqlite_files(self.db_path)

    # --- Audits ----------------------------------------------------------- #

    def save_audit(self, audit: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_audits (
                        audit_id, site_id, market, scope, score, grade, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(audit["audit_id"]),
                        str(audit["site_id"]).strip().lower(),
                        str(audit["market"]).strip().lower(),
                        str(audit["scope"]).strip().lower(),
                        int(audit["score"]),
                        str(audit["grade"]),
                        _json_dumps(audit),
                        str(audit["created_at"]),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def get_audit(self, audit_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_audits WHERE audit_id = ?",
                (str(audit_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_audits(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_audits WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_audits ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    # --- Fix plans -------------------------------------------------------- #

    def save_fix_plan(self, plan: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_fix_plans (
                        plan_id, site_id, market, fix_type, target_type, target_id,
                        status, plan_sha256, before_sha256, payload_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(plan["plan_id"]),
                        str(plan["site_id"]).strip().lower(),
                        str(plan["market"]).strip().lower(),
                        str(plan["fix_type"]).strip().lower(),
                        str(plan["target_type"]).strip().lower(),
                        str(plan["target_id"]),
                        str(plan["status"]),
                        str(plan["plan_sha256"]),
                        str(plan["before_sha256"]),
                        _json_dumps(plan),
                        str(plan["created_at"]),
                        str(plan["expires_at"]),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def compare_and_swap_fix_plan(
        self,
        plan_id: str,
        *,
        expected_status: str,
        new_plan: dict[str, Any],
    ) -> bool:
        """Atomically transition a fix plan from ``expected_status`` to ``new_plan['status']``."""
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    """
                    UPDATE seo_fix_plans
                    SET status = ?, payload_json = ?
                    WHERE plan_id = ? AND status = ?
                    """,
                    (
                        str(new_plan["status"]),
                        _json_dumps(new_plan),
                        str(plan_id or "").strip(),
                        str(expected_status),
                    ),
                )
                updated = (cur.rowcount or 0) == 1
            if updated:
                _chmod_sqlite_files(self.db_path)
            return updated

    def get_fix_plan(self, plan_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_fix_plans WHERE plan_id = ?",
                (str(plan_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_fix_plans(self, site_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_fix_plans WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_fix_plans ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    # --- Fix batches (seob-*) --------------------------------------------- #

    def save_fix_batch(self, batch: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_fix_batches (
                        batch_id, site_id, audit_id, market, scope, status,
                        plan_sha256, item_count, payload_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(batch["batch_id"]),
                        str(batch["site_id"]).strip().lower(),
                        str(batch.get("audit_id") or ""),
                        str(batch.get("market") or "no").strip().lower(),
                        str(batch.get("scope") or "all").strip().lower(),
                        str(batch.get("status") or "pending"),
                        str(batch["plan_sha256"]),
                        int(batch.get("item_count") or len(batch.get("items") or [])),
                        _json_dumps(batch),
                        str(batch["created_at"]),
                        str(batch.get("expires_at") or ""),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def get_fix_batch(self, batch_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_fix_batches WHERE batch_id = ?",
                (str(batch_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_fix_batches(self, site_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_fix_batches WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_fix_batches ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    def compare_and_swap_fix_batch(
        self,
        batch_id: str,
        *,
        expected_status: str,
        new_batch: dict[str, Any],
    ) -> bool:
        """Atomically transition a fix batch from ``expected_status`` to ``new_batch['status']``."""
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    """
                    UPDATE seo_fix_batches
                    SET status = ?, payload_json = ?
                    WHERE batch_id = ? AND status = ?
                    """,
                    (
                        str(new_batch["status"]),
                        _json_dumps(new_batch),
                        str(batch_id or "").strip(),
                        str(expected_status),
                    ),
                )
                updated = (cur.rowcount or 0) == 1
            if updated:
                _chmod_sqlite_files(self.db_path)
            return updated

    # --- Strategies ------------------------------------------------------- #

    def save_strategy(self, strategy: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_strategies (
                        strategy_id, site_id, market, title, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(strategy["strategy_id"]),
                        str(strategy["site_id"]).strip().lower(),
                        str(strategy["market"]).strip().lower(),
                        str(strategy["title"]),
                        _json_dumps(strategy),
                        str(strategy["created_at"]),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_strategies WHERE strategy_id = ?",
                (str(strategy_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_strategies(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_strategies WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_strategies ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    # --- Campaigns -------------------------------------------------------- #

    def save_campaign(self, campaign: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_campaigns (
                        campaign_id, site_id, market, strategy_id, name, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(campaign["campaign_id"]),
                        str(campaign["site_id"]).strip().lower(),
                        str(campaign["market"]).strip().lower(),
                        campaign.get("strategy_id"),
                        str(campaign["name"]),
                        _json_dumps(campaign),
                        str(campaign["created_at"]),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_campaigns WHERE campaign_id = ?",
                (str(campaign_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_campaigns(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_campaigns WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_campaigns ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]

    # --- Link Packages ---------------------------------------------------- #

    def save_link_package(self, pkg: dict[str, Any]) -> None:
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO seo_link_packages (
                        package_id, site_id, market, campaign_id, name, status,
                        guardrail_blocked, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(pkg["package_id"]),
                        str(pkg["site_id"]).strip().lower(),
                        str(pkg["market"]).strip().lower(),
                        pkg.get("campaign_id"),
                        str(pkg["name"]),
                        str(pkg["status"]),
                        1 if pkg.get("guardrail_blocked") else 0,
                        _json_dumps(pkg),
                        str(pkg["created_at"]),
                        str(pkg.get("updated_at") or pkg["created_at"]),
                    ),
                )
            _chmod_sqlite_files(self.db_path)

    def compare_and_swap_link_package(
        self,
        package_id: str,
        *,
        expected_status: str,
        new_pkg: dict[str, Any],
    ) -> bool:
        """Atomically transition a link package from ``expected_status`` to ``new_pkg['status']``."""
        with _LOCK:
            with contextlib.closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    """
                    UPDATE seo_link_packages
                    SET name = ?, status = ?, guardrail_blocked = ?, payload_json = ?, updated_at = ?
                    WHERE package_id = ? AND status = ?
                    """,
                    (
                        str(new_pkg["name"]),
                        str(new_pkg["status"]),
                        1 if new_pkg.get("guardrail_blocked") else 0,
                        _json_dumps(new_pkg),
                        str(new_pkg.get("updated_at") or new_pkg["created_at"]),
                        str(package_id or "").strip(),
                        str(expected_status),
                    ),
                )
                updated = (cur.rowcount or 0) == 1
            if updated:
                _chmod_sqlite_files(self.db_path)
            return updated

    def get_link_package(self, package_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM seo_link_packages WHERE package_id = ?",
                (str(package_id or "").strip(),),
            ).fetchone()
        return _json_loads(row["payload_json"]) if row is not None else None

    def list_link_packages(self, site_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with contextlib.closing(self._connect()) as conn:
            if site_id:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_link_packages WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (str(site_id).strip().lower(), lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM seo_link_packages ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [obj for r in rows if (obj := _json_loads(r["payload_json"])) is not None]


__all__ = ["SeoStudioStore"]
