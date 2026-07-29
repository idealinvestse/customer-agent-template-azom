"""Per-mailbox env_prefix credential resolution tests."""

from __future__ import annotations

import textwrap

import pytest

from ecom_ops.cases.mailboxes import MailboxConfig, load_mailboxes
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailProvider,
    config_from_env,
)


@pytest.fixture
def case_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return CaseStore(path=tmp_path / "cases.db")


def test_config_from_env_uses_mailbox_prefix(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "shared@azom.se")
    monkeypatch.setenv("MAIL_SE_USERNAME", "se@azom.se")
    monkeypatch.setenv("MAIL_SE_PASSWORD", "se-secret")
    monkeypatch.setenv("MAIL_SE_FROM", "se@azom.se")

    cfg = config_from_env(env_prefix="MAIL_SE_")
    assert cfg.username == "se@azom.se"
    assert cfg.password == "se-secret"
    assert cfg.from_addr == "se@azom.se"


def test_config_from_env_prefix_falls_back_mail_credentials(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "shared@azom.se")
    monkeypatch.setenv("MAIL_PASSWORD", "shared-secret")
    monkeypatch.setenv("MAIL_FROM", "shared@azom.se")

    cfg = config_from_env(env_prefix="MAIL_SE_")
    assert cfg.username == "shared@azom.se"
    assert cfg.password == "shared-secret"
    assert cfg.from_addr == "shared@azom.se"


def test_config_from_env_prefix_host_fallback(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "u@azom.se")
    monkeypatch.setenv("SMTP_HOST", "smtp.shared.example")
    monkeypatch.setenv("IMAP_HOST", "imap.shared.example")

    cfg = config_from_env(env_prefix="MAIL_SE_")
    assert cfg.smtp_host == "smtp.shared.example"
    assert cfg.imap_host == "imap.shared.example"


def test_config_from_env_prefix_prefers_prefixed_host(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "u@azom.se")
    monkeypatch.setenv("SMTP_HOST", "smtp.shared.example")
    monkeypatch.setenv("MAIL_SE_SMTP_HOST", "smtp.se.example")

    cfg = config_from_env(env_prefix="MAIL_SE_")
    assert cfg.smtp_host == "smtp.se.example"


def test_load_mailboxes_parses_env_prefix(tmp_path):
    yaml_path = tmp_path / "mailboxes.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """\
            mailboxes:
              - id: support_se
                label: Support SE
                address: support@azom.se
                enabled: true
                provider: generic_imap
                env_prefix: MAIL_SE_
            """
        ),
        encoding="utf-8",
    )
    boxes = load_mailboxes(yaml_path)
    assert len(boxes) == 1
    assert boxes[0].env_prefix == "MAIL_SE_"


def test_poll_passes_mailbox_env_prefix(case_store, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")

    mb = MailboxConfig(
        id="support_se",
        label="Support SE",
        address="support@azom.se",
        provider="generic_imap",
        env_prefix="MAIL_SE_",
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.enabled_mailboxes",
        lambda path=None: [mb],
    )

    captured: dict = {}

    def fake_client_from_env(**kwargs):
        captured.update(kwargs)
        return MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="se@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )

    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", fake_client_from_env
    )

    svc = CaseService(store=case_store)
    result = svc.poll(actor="agent", use_mock=False)

    assert result.ok
    assert captured.get("env_prefix") == "MAIL_SE_"
    assert captured.get("provider") == "generic_imap"
