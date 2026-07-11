"""Gmail OAuth token store tests."""

from __future__ import annotations

import json

from ecom_ops.integrations.mail import MailProvider, config_from_env
from ecom_ops.oauth.gmail import GmailOAuthStore


def test_mock_connect_persists_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = GmailOAuthStore()
    bundle = store.mock_connect()
    assert bundle.access_token
    assert store.has_tokens()
    loaded = store.load_tokens()
    assert loaded is not None
    assert loaded.refresh_token == "mock-refresh-token"


def test_state_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = GmailOAuthStore()
    state = store.create_state()
    assert store.validate_state(state)
    store.clear_state()
    assert not store.validate_state(state)


def test_apply_stored_gmail_tokens_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAIL_PROVIDER", "gmail")
    store = GmailOAuthStore()
    store.mock_connect()

    cfg = config_from_env()
    assert cfg.provider == MailProvider.GMAIL
    assert cfg.oauth_access_token == "mock-access-token"
    assert cfg.oauth_refresh_token == "mock-refresh-token"


def test_exchange_code_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = GmailOAuthStore()
    store.create_state()
    bundle = store.exchange_code("fake-code")
    assert bundle.access_token.startswith("mock-")
    store.clear_state()


def test_token_file_permissions_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = GmailOAuthStore()
    store.mock_connect()
    assert store.token_path.is_file()
    data = json.loads(store.token_path.read_text())
    assert "access_token" in data
    assert "refresh_token" in data
