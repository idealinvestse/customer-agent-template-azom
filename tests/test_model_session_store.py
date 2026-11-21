"""Session store: atomic writes, mode, lock, and secret-free public view."""

from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from ecom_ops.model_router.store import ModelSession, ModelSessionStore


def _session(**kwargs) -> ModelSession:
    base = dict(
        provider="openai",
        account_id="default",
        client_id="client",
        access_token="access-secret",
        refresh_token="refresh-secret",
        id_token="id-secret",
        code_verifier="verifier-secret",
        oauth_state="state-secret",
        expires_at=9_999_999_999,
        earliest_refresh_at=0,
        generation=1,
        status="connected",
    )
    base.update(kwargs)
    return ModelSession(**base)


def test_roundtrip_and_public_view_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    store.save(_session())
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-secret"
    assert loaded.refresh_token == "refresh-secret"
    assert loaded.generation == 1
    public = loaded.public_view()
    blob = str(public)
    assert "access-secret" not in blob
    assert "refresh-secret" not in blob
    assert "id-secret" not in blob
    assert "verifier-secret" not in blob
    assert "state-secret" not in blob
    assert public["has_access_token"] is True
    assert public["has_refresh_token"] is True
    if os.name == "posix":
        mode = stat.S_IMODE(store.session_path.stat().st_mode)
        assert mode == 0o600


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    store.save(_session(generation=2))
    leftovers = list(store.session_path.parent.glob("*.tmp"))
    assert leftovers == []
    assert store.load().generation == 2
    store.save(_session(generation=3))
    assert store.load().generation == 3


def test_clear_removes_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    store.save(_session())
    store.clear()
    assert store.load() is None
    assert not store.session_path.exists()


def test_lock_serializes_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    order: list[str] = []

    def hold() -> None:
        with store.lock(timeout=5):
            order.append("start")
            time.sleep(0.3)
            order.append("end")

    def wait() -> None:
        time.sleep(0.05)
        with store.lock(timeout=5):
            order.append("second")

    first = threading.Thread(target=hold)
    second = threading.Thread(target=wait)
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    assert order == ["start", "end", "second"]


def test_lock_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    started = threading.Event()

    def hold() -> None:
        with store.lock(timeout=5):
            started.set()
            time.sleep(0.6)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(2)
    with pytest.raises(TimeoutError):
        with store.lock(timeout=0.15):
            pass
    thread.join(timeout=3)
