"""B1: probe_mail must use live transport when AZOM_USE_MOCK=0."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


@pytest.fixture
def _probes_path(monkeypatch):
    monkeypatch.syspath_prepend(str(DASH_DIR))
    sys.modules.pop("secret_probes", None)
    yield
    sys.modules.pop("secret_probes", None)


def test_probe_mail_mock_passes_use_mock_true(monkeypatch, tmp_path, _probes_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAIL_USERNAME", "a@b.co")
    monkeypatch.setenv("MAIL_FROM", "a@b.co")

    captured: dict = {}
    mock_client = MagicMock()
    mock_client.fetch.return_value = []

    def _fake_client_from_env(*, use_mock=None, **kwargs):
        captured["use_mock"] = use_mock
        return mock_client

    monkeypatch.setattr(
        "ecom_ops.integrations.mail.client_from_env",
        _fake_client_from_env,
    )

    from secret_probes import probe_mail

    result = probe_mail()
    assert captured.get("use_mock") is True
    assert result.status == "ok"
    mock_client.fetch.assert_called()


def test_probe_mail_prod_forces_live_client(monkeypatch, tmp_path, _probes_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAIL_USERNAME", "a@b.co")
    monkeypatch.setenv("MAIL_FROM", "a@b.co")

    captured: dict = {}
    mock_client = MagicMock()
    mock_client.fetch.return_value = [object()]

    def _fake_client_from_env(*, use_mock=None, **kwargs):
        captured["use_mock"] = use_mock
        return mock_client

    monkeypatch.setattr(
        "ecom_ops.integrations.mail.client_from_env",
        _fake_client_from_env,
    )

    from secret_probes import probe_mail

    result = probe_mail()
    assert captured.get("use_mock") is False
    mock_client.fetch.assert_called_once()
    call_kw = mock_client.fetch.call_args.kwargs
    assert call_kw.get("limit") == 1
    assert result.status == "ok"
    assert "1 message" in result.message or "1" in result.message


def test_probe_mail_prod_auth_error_is_error(monkeypatch, tmp_path, _probes_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAIL_USERNAME", "a@b.co")
    monkeypatch.setenv("MAIL_FROM", "a@b.co")

    mock_client = MagicMock()
    mock_client.fetch.side_effect = RuntimeError("IMAP AUTH failed")

    monkeypatch.setattr(
        "ecom_ops.integrations.mail.client_from_env",
        lambda **kwargs: mock_client,
    )

    from secret_probes import probe_mail

    result = probe_mail()
    assert result.status == "error"
    assert "AUTH" in result.message or "failed" in result.message.lower()


def test_probe_mail_missing_credentials(monkeypatch, tmp_path, _probes_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MAIL_USERNAME", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)

    from secret_probes import probe_mail

    result = probe_mail()
    assert result.status == "missing"
