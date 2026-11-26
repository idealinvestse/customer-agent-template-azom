"""Unit tests for PR 3.2 split modules (file_paths, file_transport, search, file_plans)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.wp_ops import (
    file_changes,
    file_paths,
    file_plans,
    file_transport,
    files,
    search,
)

ROOT = Path(__file__).resolve().parents[2]


def test_file_paths_resolve_dir_transport_messages_and_expiry() -> None:
    assert file_paths.resolve_dir("/srv/www/shop", "theme", "azom-child") == (
        "/srv/www/shop/wp-content/themes/azom-child"
    )
    assert file_paths.resolve_dir("/srv/www/shop", "theme", "azom-child", "assets/css") == (
        "/srv/www/shop/wp-content/themes/azom-child/assets/css"
    )
    with pytest.raises(file_paths.PathRejected):
        file_paths.resolve_dir("/srv/www/shop", "theme", "azom-child", "../other")
    with pytest.raises(file_paths.PathRejected):
        file_paths.resolve_dir("/srv/www/shop", "theme", "azom-child", ".env")

    assert file_paths.transport_message("SshWriteDisabled") == (
        "Live SFTP is read-only; file writes are disabled."
    )
    assert file_paths.transport_message("UnknownCode") == "File transport failed."

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert not file_paths.plan_expired({"created_at": (now - timedelta(hours=1)).isoformat()}, now)
    assert file_paths.plan_expired({"created_at": (now - timedelta(hours=13)).isoformat()}, now)
    assert file_paths.plan_expired({"expires_at": (now - timedelta(seconds=1)).isoformat()}, now)


def test_file_transport_protocol_flags_and_payload_store(tmp_path: Path) -> None:
    tree = file_transport.LocalTree("/srv/www/shop", tmp_path)
    assert tree.is_ssh is False
    assert file_transport.transport_is_remote(tree) is False

    class _FakeRemote:
        is_ssh = True

    class _UnknownTransport:
        pass

    assert file_transport.transport_is_remote(_FakeRemote()) is True
    assert file_transport.transport_is_remote(_UnknownTransport()) is True

    theme_dir = tmp_path / "wp-content" / "themes" / "azom-child"
    theme_dir.mkdir(parents=True)
    assert tree.parent_dir_exists("/srv/www/shop/wp-content/themes/azom-child/new.css") is True
    assert tree.parent_dir_exists("/srv/www/shop/wp-content/themes/missing/new.css") is False

    store = file_plans.FilePayloadStore(tmp_path)
    plan_id = "fe-0123456789ab"
    store.write(plan_id, "body {}", "--- a\n+++ b\n")
    assert store.read(plan_id) == {"new_text": "body {}", "diff": "--- a\n+++ b\n"}
    store.delete(plan_id)
    with pytest.raises(file_paths.TransportError):
        store.read(plan_id)


def test_file_plans_and_search_avoid_private_editor_and_transport_reachins() -> None:
    plans_src = (ROOT / "skills" / "ecom_ops" / "wp_ops" / "file_plans.py").read_text(encoding="utf-8")
    search_src = (ROOT / "skills" / "ecom_ops" / "wp_ops" / "search.py").read_text(encoding="utf-8")
    for src in (plans_src, search_src):
        assert "editor._" not in src
        assert "transport._local" not in src


def test_files_and_file_changes_facades_preserve_identity() -> None:
    assert files.FileEditor is file_plans.FileEditor
    assert files.LocalTree is file_transport.LocalTree
    assert files.FileTransport is file_transport.FileTransport
    assert files.PathRejected is file_paths.PathRejected
    assert files.TransportError is file_paths.TransportError
    assert files.resolve_file is file_paths.resolve_file
    assert files.transport_message is file_paths.transport_message

    assert file_changes.FileChangeOps is file_plans.FileChangeOps
    assert file_changes.FilePayloadStore is file_plans.FilePayloadStore
    assert file_changes.FILE_TOOL_NAMES is file_paths.FILE_TOOL_NAMES
    assert file_changes.sensitive_relative is file_paths.sensitive_relative
    assert file_changes.plan_expired is file_paths.plan_expired
    assert file_changes.search_texts is search.search_texts
    assert file_changes.validate_safe_regex is search.validate_safe_regex
    assert file_changes.ComponentSearcher is search.ComponentSearcher
