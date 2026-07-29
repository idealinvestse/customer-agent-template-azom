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


def test_smtp_imap_refresh_persists_gmail_token(tmp_path, monkeypatch):
    from ecom_ops.oauth.gmail import GmailOAuthStore, GmailTokenBundle
    from ecom_ops.integrations.mail_providers.models import MailConfig, MailProvider
    from ecom_ops.integrations.mail_providers.smtp_imap import SmtpImapTransport

    store = GmailOAuthStore(data_dir=tmp_path)
    store.save_tokens(
        GmailTokenBundle(
            access_token="old-access",
            refresh_token="refresh-1",
            expires_at=0,
            token_type="Bearer",
            scope="mail",
        )
    )
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    cfg = MailConfig(
        provider=MailProvider.GMAIL,
        username="a@gmail.com",
        password="",
        from_addr="a@gmail.com",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_use_ssl=True,
        oauth_refresh_token="refresh-1",
        oauth_client_id="cid",
        oauth_client_secret="sec",
        oauth_access_token="",
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(
        "ecom_ops.integrations.mail_providers.smtp_imap.requests.post",
        lambda *a, **k: _Resp(),
    )

    transport = SmtpImapTransport(cfg)
    assert transport._ensure_oauth_token() == "new-access"
    loaded = GmailOAuthStore(data_dir=tmp_path).load_tokens()
    assert loaded is not None
    assert loaded.access_token == "new-access"
    assert loaded.refresh_token == "refresh-1"


def test_smtp_imap_refreshes_when_cached_access_token_expired(tmp_path, monkeypatch):
    """Expired in-memory / config access token must not skip refresh."""
    from ecom_ops.oauth.gmail import GmailOAuthStore, GmailTokenBundle
    from ecom_ops.integrations.mail_providers.models import MailConfig, MailProvider
    from ecom_ops.integrations.mail_providers.smtp_imap import SmtpImapTransport

    store = GmailOAuthStore(data_dir=tmp_path)
    store.save_tokens(
        GmailTokenBundle(
            access_token="stale-access",
            refresh_token="refresh-1",
            expires_at=1.0,  # long expired
            token_type="Bearer",
            scope="mail",
        )
    )
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    cfg = MailConfig(
        provider=MailProvider.GMAIL,
        username="a@gmail.com",
        from_addr="a@gmail.com",
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_use_ssl=True,
        oauth_refresh_token="refresh-1",
        oauth_client_id="cid",
        oauth_client_secret="sec",
        oauth_access_token="stale-access",
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "fresh-access", "expires_in": 3600}

    monkeypatch.setattr(
        "ecom_ops.integrations.mail_providers.smtp_imap.requests.post",
        lambda *a, **k: _Resp(),
    )

    transport = SmtpImapTransport(cfg)
    assert transport._ensure_oauth_token() == "fresh-access"
