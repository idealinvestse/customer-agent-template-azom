"""Tests for Wave 2 ChangeLedger, SnapshotStore, and ChangeRollbackService."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _helpers import mock_wp_content

from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.rollback_service import ChangeRollbackService
from ecom_ops.wp_ops.service import WpOpsService


def test_change_ledger_snapshot_integrity_tamper_and_secret_scrub(tmp_path: Path) -> None:
    ledger = ChangeLedger(data_dir=tmp_path)
    row = ledger.record_change(
        site_id="mock-shop",
        environment="staging",
        category="wp_settings",
        transport="wp_rest",
        actor="oscar",
        summary="Uppdaterade sajtinställningar token=ghp_SecretToken123456789",
        target_ref="settings",
        before_data={"title": "Azom", "api_key": "sk_live_secretvalue999"},
        after_data={"title": "Azom SE", "api_key": "sk_live_secretvalue999"},
    )
    cid = row["change_id"]
    assert cid.startswith("chg-")
    assert "ghp_SecretToken" not in row["summary"]

    verified = ledger.verify_snapshot(row["site_id"], cid)
    assert verified["verified"] is True

    diff = ledger.read_snapshot_diff(row["site_id"], cid)
    assert "sk_live_secretvalue" not in str(diff)

    # Tamper with after.json -> verify_snapshot detects mismatch
    after_path = ledger.snapshots.change_dir(row["site_id"], cid) / "after.json"
    after_path.write_text('{"title": "TAMPERED"}', encoding="utf-8")
    tampered = ledger.verify_snapshot(row["site_id"], cid)
    assert tampered["verified"] is False
    assert tampered["error_class"] == "SnapshotCorrupted"


def test_rollback_service_guards_and_file_entity_rollbacks(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    # Seed child theme file in data_dir mock wp-content
    content_file = mock_wp_content(tmp_path, "mock-staging") / "themes" / "azom-child" / "style.css"
    content_file.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = b"/* Azom Child */\nbody { color: #111; }\n"
    after_bytes = b"/* Azom Child */\nbody { color: #222; }\n"
    content_file.write_bytes(after_bytes)

    ledger = ChangeLedger(data_dir=tmp_path)
    file_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="file_edit",
        transport="mock_local",
        actor="oscar",
        summary="Ändrade style.css färg",
        target_ref="theme:azom-child:style.css",
        file_entries=[{"path": "style.css", "kind": "theme", "slug": "azom-child", "op": "modified"}],
        before_files={"style.css": before_bytes},
        after_files={"style.css": after_bytes},
        metadata={"kind": "theme", "slug": "azom-child"},
    )
    cid = file_row["change_id"]
    svc = ChangeRollbackService(data_dir=tmp_path)

    # Jonatan (viewer) cannot execute rollback
    denied = svc.execute_rollback(
        "mock-staging",
        cid,
        actor="jonatan",
        expected_before_sha256=file_row["before_sha256"],
    )
    assert denied.ok is False and denied.data["error_class"] == "AccessDenied"

    # Hash mismatch is rejected
    bad_hash = svc.execute_rollback(
        "mock-staging",
        cid,
        actor="oscar",
        expected_before_sha256="0" * 64,
    )
    assert bad_hash.ok is False and bad_hash.data["error_class"] == "DigestMismatch"

    # Kill switch blocks even Oscar
    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    killed = svc.execute_rollback(
        "mock-staging",
        cid,
        actor="oscar",
        expected_before_sha256=file_row["before_sha256"],
    )
    assert killed.ok is False and killed.data["error_class"] == "DeployKillSwitchActive"
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # Live mode without AZOM_WP_RESTORE_ENABLED=1 refuses
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_RESTORE_ENABLED", raising=False)
    live_blocked = svc.execute_rollback(
        "mock-staging",
        cid,
        actor="oscar",
        expected_before_sha256=file_row["before_sha256"],
    )
    assert live_blocked.ok is False and live_blocked.data["error_class"] == "RestoreGateDisabled"

    # In mock mode, Oscar executes verified file rollback cleanly
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    ok_res = svc.execute_rollback(
        "mock-staging",
        cid,
        actor="oscar",
        expected_before_sha256=file_row["before_sha256"],
    )
    assert ok_res.ok is True
    assert ok_res.data["rollback_change_id"].startswith("chg-")
    assert content_file.read_bytes() == before_bytes
    updated = ledger.get_change("mock-staging", cid)
    assert updated is not None and updated["status"] == "rolled_back"


def test_rollback_service_entity_restore_for_product_and_page(
    tmp_path: Path, wp_env
) -> None:
    wp_env(mock="1", files_ssh="0")
    os.environ["AZOM_USE_MOCK"] = "1"
    ledger = ChangeLedger(data_dir=tmp_path)
    svc = ChangeRollbackService(data_dir=tmp_path)

    for cat, target, before_data, after_data in (
        (
            "product",
            "product:501",
            {"id": 501, "name": "Azom Pro Headset", "short_description": "Gammal text", "regular_price": "1990"},
            {"id": 501, "name": "Azom Pro Headset", "short_description": "Ny text", "regular_price": "2490"},
        ),
        (
            "page",
            "page:2",
            {"id": 2, "title": "Om oss", "content": "<p>Original</p>", "status": "publish"},
            {"id": 2, "title": "Om oss", "content": "<p>Ändrad</p>", "status": "publish"},
        ),
        (
            "post",
            "post:1",
            {"id": 1, "title": "Guide", "content": "<p>Före</p>", "status": "publish"},
            {"id": 1, "title": "Guide", "content": "<p>Efter</p>", "status": "publish"},
        ),
        (
            "settings",
            "settings",
            {"title": "Azom"},
            {"title": "Azom Beta"},
        ),
        (
            "plugin",
            "plugin:akismet/akismet",
            {"plugin": "akismet/akismet", "status": "inactive"},
            {"plugin": "akismet/akismet", "status": "active"},
        ),
    ):
        row = ledger.record_change(
            site_id="mock-staging",
            environment="staging",
            category=cat,
            transport="woo_rest" if cat.startswith("woo") else "wp_rest",
            actor="oscar",
            summary=f"Test {cat}",
            target_ref=target,
            before_data=before_data,
            after_data=after_data,
        )
        res = svc.execute_rollback(
            "mock-staging",
            row["change_id"],
            actor="oscar",
            expected_before_sha256=row["before_sha256"],
        )
        assert res.ok is True, f"Failed rollback for {cat}: {res}"


def test_legacy_restore_and_file_ops_rollback_stay_fail_closed(tmp_path: Path, wp_env) -> None:
    wp_env(mock="1", files_ssh="0")
    svc = WpOpsService(data_dir=tmp_path)
    created = svc.create_backup("mock-staging", actor="oscar")
    assert created.ok is True
    bid = created.data["backup"]["id"]
    restored = svc.restore("mock-staging", actor="oscar", change_ref=bid)
    assert restored.ok is False and restored.data["error_class"] == "RestoreNotInMvp"

    rb_res = svc._file_changes().rollback("mock-staging", "fe-000000000001", actor="oscar")
    assert rb_res.ok is False and rb_res.data["error_class"] in {"RestoreNotInMvp", "UnknownPlan"}


def test_ledger_multi_market_and_entity_edge_cases_and_bridge_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from typing import Any

    import yaml
    from _helpers import site_row

    from ecom_ops.security import redact_secrets
    from ecom_ops.wp_ops.change_ledger import resolve_ledger_site_id
    from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WP_SECRET_AZOM_NO", "secret-no")
    monkeypatch.setenv("WP_SECRET_AZOM_SE", "secret-se")
    reg = tmp_path / "wp_sites.yaml"
    reg.write_text(
        yaml.safe_dump(
            {
                "sites": [
                    site_row("azom-no", canonical_url="https://azom.no/", secret_ref="WP_SECRET_AZOM_NO"),
                    site_row("azom-se", canonical_url="https://azom.se/", secret_ref="WP_SECRET_AZOM_SE"),
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # V-04: multi-market domain resolution prefers matching host/TLD
    assert resolve_ledger_site_id(domain="se") == "azom-se"

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "secret-shop")
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "secret-staging")
    ledger = ChangeLedger(data_dir=tmp_path)

    # V-17 + V-19: page_draft cannot auto-rollback
    draft_chg = ledger.record_change(
        site_id="mock-shop",
        environment="production",
        category="page_draft",
        transport="wp_rest",
        actor="oscar",
        summary="Draft created",
        target_ref="77",
        before_data={},
        after_data={"id": 77, "status": "draft"},
    )
    rb_svc = ChangeRollbackService(data_dir=tmp_path)
    assert rb_svc.preview_rollback("mock-shop", draft_chg["change_id"], actor="oscar")["can_rollback"] is False
    draft_rb = rb_svc.execute_rollback(
        "mock-shop",
        draft_chg["change_id"],
        actor="oscar",
        expected_before_sha256=draft_chg["before_sha256"],
        dry_run=False,
    )
    assert draft_rb.ok is False
    assert draft_rb.data["error_class"] in {"RollbackUnsupported", "NotRollbackable", "RollbackFailed"}

    # V-23: entity rollback supports empty string, >8 KiB content, and explicit manage_stock=False
    big_content = "<p>" + ("a" * 9000) + "</p>"
    page_chg = ledger.record_change(
        site_id="mock-shop",
        environment="production",
        category="page",
        transport="wp_rest",
        actor="oscar",
        summary="Page edit",
        target_ref="2",
        before_data={"id": 2, "title": "", "content": big_content},
        after_data={"id": 2, "title": "After", "content": "short"},
    )
    updated_calls: list[dict[str, Any]] = []

    class _FakeWp:
        def get_page(self, page_id: int, **_kw: Any) -> Any:
            return SimpleNamespace(id=page_id, title="After", content="short")

        def update_page(self, page_id: int, **kw: Any) -> Any:
            updated_calls.append({"id": page_id, **kw})
            return SimpleNamespace(id=page_id)

    rb_ok = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-shop",
        page_chg["change_id"],
        actor="oscar",
        expected_before_sha256=page_chg["before_sha256"],
        dry_run=False,
        wp_client=_FakeWp(),
    )
    assert rb_ok.ok is True
    assert updated_calls and updated_calls[0]["title"] == ""
    assert len(updated_calls[0]["content"]) == len(big_content)

    # V-17: second rollback on already rolled-back entry is rejected
    second_rb = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-shop",
        page_chg["change_id"],
        actor="oscar",
        expected_before_sha256=page_chg["before_sha256"],
        dry_run=False,
        wp_client=_FakeWp(),
    )
    assert second_rb.ok is False
    assert second_rb.data["error_class"] == "AlreadyRolledBack"

    # V-23: Woo product rollback preserves manage_stock=False explicitly
    prod_calls: list[dict[str, Any]] = []

    class _FakeWoo:
        def get_product(self, product_id: int) -> Any:
            return SimpleNamespace(id=product_id, name="New", regular_price="200", raw={"manage_stock": True})

        def update_product(self, product_id: int, fields: dict[str, Any], **_kw: Any) -> Any:
            prod_calls.append({"id": product_id, "fields": dict(fields)})
            return SimpleNamespace(id=product_id)

    prod_chg = ledger.record_change(
        site_id="mock-shop",
        environment="production",
        category="product",
        transport="woo_rest",
        actor="oscar",
        summary="Product update",
        target_ref="10",
        before_data={"id": 10, "name": "Old", "regular_price": "100", "manage_stock": False},
        after_data={"id": 10, "name": "New", "regular_price": "200", "manage_stock": True},
    )
    prod_rb = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-shop",
        prod_chg["change_id"],
        actor="oscar",
        expected_before_sha256=prod_chg["before_sha256"],
        dry_run=False,
        woo_client=_FakeWoo(),
    )
    assert prod_rb.ok is True
    assert prod_calls[0]["fields"].get("manage_stock") is False

    # V-31 + V-38: kill switch blocks bridge_cache_flush & bridge_maintenance, and redact_secrets(str) scrubs tokens
    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    svc = WpOpsService(data_dir=tmp_path)
    flush_res = svc.bridge_cache_flush("mock-staging", actor="oscar")
    maint_res = svc.bridge_maintenance("mock-staging", enabled=True, actor="oscar")
    assert flush_res.ok is False and flush_res.data["error_class"] in {"KillSwitchActive", "DeployKillSwitchActive"}
    assert maint_res.ok is False and maint_res.data["error_class"] in {"KillSwitchActive", "DeployKillSwitchActive"}
    scrubbed = redact_secrets("Authorization: Basic YWRtaW46c2VjcmV0 WP_BRIDGE_SECRET_NO=topsecret123")
    assert "YWRtaW46c2VjcmV0" not in scrubbed and "topsecret123" not in scrubbed


def test_b1_07_b1_12_b1_14_b1_15_toggle_fail_woo_redaction_preview_mu_root_and_posix_lock(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import SimpleNamespace

    from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
    from ecom_ops.security import redact_secrets
    from ecom_ops.wp_ops import locks as wp_locks

    wp_env(mock="1", files_ssh="0")

    # B1-12: Multi-site WOO_CONSUMER_KEY_<MARKET> and ck_/cs_ tokens redacted everywhere
    redacted_dict = redact_secrets({"WOO_CONSUMER_KEY_NO": "ck_0123456789abcdef0123456789abcdef"})
    assert redacted_dict["WOO_CONSUMER_KEY_NO"] == "***REDACTED***"
    redacted_str = redact_secrets(
        "woo failed ck_0123456789abcdef0123456789abcdef and cs_abcdef0123456789abcdef0123456789"
    )
    assert "ck_0123456789abcdef" not in redacted_str
    assert "cs_abcdef0123456789" not in redacted_str

    ledger = ChangeLedger(data_dir=tmp_path)
    svc = ChangeRollbackService(data_dir=tmp_path)
    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    bc = WpBridgeClient(site_id="mock-staging", transport=tr)

    # B1-07: toggle_plugin auto-revert failure raises RollbackFailed (never marks rolled_back)
    deact_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="plugin_rollback",
        transport="bridge",
        actor="oscar",
        summary="Avaktiverade plugin",
        target_ref="plugin:wordpress-seo",
        before_data={"plugin_file": "wordpress-seo/wp-seo.php", "active": True},
        after_data={"plugin_file": "wordpress-seo/wp-seo.php", "active": False},
        metadata={"recovery_action": "deactivate", "plugin_file": "wordpress-seo/wp-seo.php"},
    )
    tr.fail_probe_for_plugins.add("wordpress-seo/wp-seo.php")
    rb_toggle_fail = svc.execute_rollback(
        "mock-staging",
        deact_row["change_id"],
        actor="oscar",
        expected_before_sha256=deact_row["before_sha256"],
        bridge_client=bc,
    )
    assert rb_toggle_fail.ok is False
    assert rb_toggle_fail.data["error_class"] == "RollbackFailed"
    rec_after = ledger.get_change("mock-staging", deact_row["change_id"])
    assert rec_after is not None and rec_after["status"] != "rolled_back"
    tr.fail_probe_for_plugins.clear()

    # B1-14: preview_rollback checks AZOM_WP_PLUGIN_MUTATE_KILL & non-reversible plugin_rollback
    upd_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="plugin_update",
        transport="bridge",
        actor="oscar",
        summary="Uppdaterade litespeed-cache",
        target_ref="plugin:litespeed-cache",
        before_data={"version": "6.5.0"},
        after_data={"version": "6.5.2"},
        metadata={"slug": "litespeed-cache", "plugin_file": "litespeed-cache/litespeed-cache.php", "backup_id": "pbackup-chg-1-litespeed-cache.zip"},
    )
    monkeypatch.setenv("AZOM_WP_PLUGIN_MUTATE_KILL", "1")
    prev_pkill = svc.preview_rollback("mock-staging", upd_row["change_id"], actor="oscar", bridge_client=bc)
    assert prev_pkill["can_rollback"] is False
    assert prev_pkill["block_reason"] == "PluginMutateKillSwitchActive"
    monkeypatch.delenv("AZOM_WP_PLUGIN_MUTATE_KILL", raising=False)

    snap_rb_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="plugin_rollback",
        transport="bridge",
        actor="oscar",
        summary="Återställde plugin-snapshot",
        target_ref="plugin:litespeed-cache",
        before_data={"version": "6.5.2"},
        after_data={"version": "6.5.0"},
        metadata={"recovery_action": "rollback_snapshot", "slug": "litespeed-cache"},
    )
    prev_nonrev = svc.preview_rollback("mock-staging", snap_rb_row["change_id"], actor="oscar", bridge_client=bc)
    assert prev_nonrev["can_rollback"] is False
    assert prev_nonrev["block_reason"] == "NonReversibleOperation"

    # B1-14: SFTP rollback handles MU-root file & calls best-effort Bridge cache flush
    mu_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="mu_plugin_deploy",
        transport="sftp_deploy",
        actor="oscar",
        summary="Deployade root MU-plugin",
        target_ref="mu_plugin:_root:guard.php",
        file_entries=[{"path": "guard.php", "kind": "mu_plugin", "slug": "_root", "op": "updated"}],
        before_files={"guard.php": b"<?php // old\n"},
        after_files={"guard.php": b"<?php // new\n"},
        metadata={"kind": "mu_plugin", "slug": "_root"},
    )
    sftp_rb_calls: list[tuple[dict[str, bytes], list[str]]] = []

    class _FakeDeployTransport:
        files_root = "/var/www/staging"

        def rollback_bundle(self, before_files: dict[str, bytes], created_paths: list[str]) -> dict[str, object]:
            sftp_rb_calls.append((dict(before_files), list(created_paths)))
            return {"ok": True, "restored": len(before_files), "removed": len(created_paths)}

    flushes_before = tr.cache_flushes
    rb_mu = svc.execute_rollback(
        "mock-staging",
        mu_row["change_id"],
        actor="oscar",
        expected_before_sha256=mu_row["before_sha256"],
        deploy_transport=_FakeDeployTransport(),
        bridge_client=bc,
    )
    assert rb_mu.ok is True
    assert sftp_rb_calls
    assert "/var/www/staging/wp-content/mu-plugins/guard.php" in sftp_rb_calls[0][0]
    assert tr.cache_flushes == flushes_before + 1

    # B1-15: POSIX advisory_file_lock uses LOCK_EX | LOCK_NB and honors timeout_s & raise_on_timeout
    flock_flags: list[int] = []

    class _FakeFcntl:
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(_fd: int, flags: int) -> None:
            flock_flags.append(flags)
            if flags & _FakeFcntl.LOCK_NB:
                raise BlockingIOError("busy")

    monkeypatch.setitem(sys.modules, "fcntl", _FakeFcntl)
    with pytest.raises(BlockingIOError):
        wp_locks._acquire_posix_lock(1, timeout_s=0.01, raise_on_timeout=True)
    assert any(flag == (_FakeFcntl.LOCK_EX | _FakeFcntl.LOCK_NB) for flag in flock_flags)

    assert wp_locks._acquire_posix_lock(1, timeout_s=0.01, raise_on_timeout=False) is False
    del SimpleNamespace


def test_change_ledger_batch_id_migration_and_transactional_batch_rollback(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient

    wp_env(mock="1", files_ssh="0")

    # 1. Safe ALTER TABLE migration on a legacy DB lacking batch_id column
    db_path = tmp_path / "wp_change_ledger.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE wp_changes (
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
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO wp_changes (
            change_id, site_id, environment, category, transport, actor, summary,
            target_ref, before_sha256, after_sha256, created_at
        ) VALUES ('chg-000000000001', 'mock-staging', 'staging', 'settings', 'wp_rest',
                  'oscar', 'Legacy row', 'settings', 'a', 'b', '2026-11-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    ledger = ChangeLedger(data_dir=tmp_path)
    legacy = ledger.get_change("mock-staging", "chg-000000000001")
    assert legacy is not None and legacy["batch_id"] == ""

    with sqlite3.connect(str(db_path)) as check_conn:
        idx_names = {r[1] for r in check_conn.execute("PRAGMA index_list(wp_changes)").fetchall()}
        assert "idx_changes_batch_id" in idx_names

    # 2. Record a 3-change SEO batch on bridge transport & verify reverse-chronological listing
    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    bc = WpBridgeClient(site_id="mock-staging", transport=tr)
    batch_id = "seob-0123456789ab"

    orig_501 = str(tr.seo_entities_store[("product", 501)]["meta_title"])
    orig_502 = str(tr.seo_entities_store[("product", 502)]["meta_description"])
    orig_media = str(tr.seo_media_store[101]["alt_text"])

    bc.seo_update_meta(
        change_id="chg-b1",
        object_type="product",
        object_id=501,
        fields={"meta_title": "Batch Title 1"},
    )
    c1 = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="bridge",
        actor="oscar",
        summary="Batch item 1",
        target_ref="product:501",
        before_data={"meta_title": orig_501},
        after_data={"meta_title": "Batch Title 1"},
        metadata={"object_type": "product", "object_id": 501},
        batch_id=batch_id,
    )

    bc.seo_update_meta(
        change_id="chg-b2",
        object_type="product",
        object_id=502,
        fields={"meta_description": "Batch Desc 2"},
    )
    c2 = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="bridge",
        actor="oscar",
        summary="Batch item 2",
        target_ref="product:502",
        before_data={"meta_description": orig_502},
        after_data={"meta_description": "Batch Desc 2"},
        metadata={"object_type": "product", "object_id": 502},
        batch_id=batch_id,
    )

    bc.seo_update_media(
        change_id="chg-b3",
        media_id=101,
        fields={"alt_text": "Batch Alt 3"},
    )
    c3 = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="media_alt",
        transport="bridge",
        actor="oscar",
        summary="Batch item 3",
        target_ref="media:101",
        before_data={"alt_text": orig_media},
        after_data={"alt_text": "Batch Alt 3"},
        metadata={"media_id": 101},
        batch_id=batch_id,
    )

    batch_rows = ledger.list_batch_changes(batch_id, site_id="mock-staging")
    assert [r["change_id"] for r in batch_rows] == [c3["change_id"], c2["change_id"], c1["change_id"]]
    assert len(ledger.list_changes("mock-staging", batch_id=batch_id)) == 3

    svc = ChangeRollbackService(data_dir=tmp_path)

    # Preview works for Jonatan (WP_OPS_READ)
    prev = svc.preview_batch_rollback("mock-staging", batch_id, actor="jonatan", bridge_client=bc)
    assert prev["ok"] is True
    assert prev["total"] == 3
    assert prev["rollbackable_count"] == 3
    assert prev["can_rollback"] is True
    assert prev["change_ids"] == [c3["change_id"], c2["change_id"], c1["change_id"]]

    # Guards: RBAC, confirm_batch_id mismatch, kill switches
    denied = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="jonatan", confirm_batch_id=batch_id, bridge_client=bc
    )
    assert denied["ok"] is False and denied["error_class"] == "AccessDenied"

    bad_confirm = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="oscar", confirm_batch_id="wrong", bridge_client=bc
    )
    assert bad_confirm["ok"] is False and bad_confirm["error_class"] == "BatchConfirmationMismatch"

    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    seo_killed = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="oscar", confirm_batch_id=batch_id, bridge_client=bc
    )
    assert seo_killed["ok"] is False and seo_killed["error_class"] == "SeoKillSwitchActive"
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)

    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    dep_killed = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="oscar", confirm_batch_id=batch_id, bridge_client=bc
    )
    assert dep_killed["ok"] is False and dep_killed["error_class"] == "DeployKillSwitchActive"
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # Execute batch rollback cleanly in reverse chronological order
    res = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="oscar", confirm_batch_id=batch_id, bridge_client=bc
    )
    assert res["ok"] is True
    assert res["executed"] is True
    assert res["rolled_back_count"] == 3
    assert res["failed_count"] == 0
    assert [r["change_id"] for r in res["rolled_back"]] == [c3["change_id"], c2["change_id"], c1["change_id"]]
    assert tr.seo_entities_store[("product", 501)]["meta_title"] == orig_501
    assert tr.seo_entities_store[("product", 502)]["meta_description"] == orig_502
    assert tr.seo_media_store[101]["alt_text"] == orig_media

    # Second execution refuses cleanly as AlreadyRolledBack
    again = svc.execute_batch_rollback(
        "mock-staging", batch_id, actor="oscar", confirm_batch_id=batch_id, bridge_client=bc
    )
    assert again["ok"] is False and again["error_class"] == "AlreadyRolledBack"
