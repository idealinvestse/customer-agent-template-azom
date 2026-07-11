"""P9: schema versioning + legacy deprecation warnings."""

from __future__ import annotations

import warnings

from ecom_ops.cases.store import SCHEMA_VERSION, CaseStore


def test_new_db_records_schema_version(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    assert store.schema_version() == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 2


def test_migrate_from_v1_db_without_version_table(tmp_path, monkeypatch):
    """Old DB with cases table but no schema_migrations gets upgraded."""
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    db = tmp_path / "old.db"
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE cases (
            id TEXT PRIMARY KEY,
            mailbox_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            from_addr TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            status TEXT NOT NULL DEFAULT 'open',
            order_id TEXT,
            draft_reply TEXT,
            message_id TEXT UNIQUE,
            site TEXT NOT NULL DEFAULT 'azom',
            market TEXT,
            language TEXT NOT NULL DEFAULT 'sv',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE case_messages (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            from_addr TEXT NOT NULL,
            to_addr TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            message_id TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    store = CaseStore(path=db)
    assert store.schema_version() == SCHEMA_VERSION
    # New columns from v2 migration exist
    with store._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(cases)").fetchall()}
    assert "priority" in cols
    assert "escalation_id" in cols


def test_legacy_order_status_module_warns():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import importlib

        import ecom_ops.order_status_update as mod

        importlib.reload(mod)
        deprec = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprec, "expected DeprecationWarning from legacy shim"
