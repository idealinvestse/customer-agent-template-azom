"""FAQ WordPress sync-draft / publish HITL."""

from __future__ import annotations

import pytest

from ecom_ops.faq.config import clear_faq_config_cache
from ecom_ops.faq.publish import clear_mock_wp_clients, publish_page, sync_draft
from ecom_ops.faq.publish_map import FaqPublishMap
from ecom_ops.faq.store import clear_faq_store_cache
from ecom_ops.rbac import resolve_actor


@pytest.fixture(autouse=True)
def _faq_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_FAQ_PUBLISH_KILL", raising=False)
    clear_faq_config_cache()
    clear_faq_store_cache()
    clear_mock_wp_clients()


def test_sync_draft_requires_faq_publish():
    result = sync_draft("se", actor="jonatan", use_mock=True)
    assert result.ok is False
    assert "faq_publish" in result.message.lower() or "lacks permission" in result.message.lower()


def test_sync_draft_and_publish_mock(tmp_path):
    pmap = FaqPublishMap(path=tmp_path / "faq_publish.db")
    sync = sync_draft(
        "se", actor="oscar", use_mock=True, publish_map=pmap
    )
    assert sync.ok, sync.message
    assert sync.wp_page_id
    assert sync.status == "draft"

    pub = publish_page(
        "se", status="publish", actor="oscar", use_mock=True, publish_map=pmap
    )
    assert pub.ok, pub.message
    assert pub.status == "publish"


def test_kill_switch_blocks_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_FAQ_PUBLISH_KILL", "1")
    clear_faq_config_cache()
    pmap = FaqPublishMap(path=tmp_path / "faq_publish.db")
    result = sync_draft("se", actor="oscar", use_mock=True, publish_map=pmap)
    assert result.ok is False
    assert "kill-switch" in result.message.lower()


def test_live_no_blocked_without_allowlist(monkeypatch, tmp_path):
    """NO live publish denied when not in live_markets_allowed."""
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    pmap = FaqPublishMap(path=tmp_path / "faq_publish.db")
    # Force non-mock path but kill WP by expecting market gate first
    result = sync_draft("no", actor="oscar", use_mock=False, publish_map=pmap)
    assert result.ok is False
    assert "not allowed" in result.message.lower()


def test_oscar_has_faq_publish():
    actor = resolve_actor("oscar")
    from ecom_ops.rbac import Permission

    assert actor.has(Permission.FAQ_PUBLISH)
    assert actor.has(Permission.FAQ_READ)
    jon = resolve_actor("jonatan")
    assert jon.has(Permission.FAQ_READ)
    assert not jon.has(Permission.FAQ_PUBLISH)
