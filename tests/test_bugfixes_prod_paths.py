"""Regression tests for prod-path bugs (mock defaults, RBAC, probes, Telegram)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.actions.mail import MailService
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import main
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailProvider,
)
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient


def test_mail_service_default_respects_env_mock(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    svc = MailService()
    assert isinstance(svc.client.transport, InMemoryMailTransport)


def test_mail_service_default_not_forced_mock_when_live(monkeypatch):
    """Without AZOM_USE_MOCK, MailService must not hardcode use_mock=True."""
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("MAIL_PROVIDER", "generic_imap")
    monkeypatch.setenv("MAIL_USERNAME", "u@azom.se")
    monkeypatch.setenv("MAIL_FROM", "support@azom.se")
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("IMAP_HOST", "localhost")
    calls: list[dict] = []

    def fake_client_from_env(**kwargs):
        calls.append(kwargs)
        return MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="u@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )

    monkeypatch.setattr("ecom_ops.actions.mail.client_from_env", fake_client_from_env)
    MailService()
    assert calls, "client_from_env should be called"
    assert calls[0].get("use_mock") is not True


def test_jonatan_approve_sends_as_jonatan_not_agent(case_store_path, monkeypatch, telemetry):
    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            from_addr="support@azom.se",
        ),
        transport=transport,
    )
    store = CaseStore(path=case_store_path)
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order?",
        from_addr="kund@example.com",
        body="help",
        category="other",
        draft_reply="Tack, vi hjälper dig.",
        order_id=None,
        message_id="<approve-actor@x>",
    )
    mail = MailService(client=client, telemetry=telemetry)
    seen: list[object] = []
    orig_send = mail.send

    def wrap_send(**kwargs):
        seen.append(kwargs.get("actor"))
        return orig_send(**kwargs)

    monkeypatch.setattr(mail, "send", wrap_send)
    svc = CaseService(store=store, mail=mail, telemetry=telemetry)
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok, result.message
    assert seen, "mail.send should be called"
    actor = seen[0]
    name = actor.name if hasattr(actor, "name") else str(actor)
    assert name == "jonatan", f"expected approving actor jonatan, got {name!r}"
    assert any(m.to_addrs and "kund@example.com" in m.to_addrs for m in transport.outbox)


def test_poll_all_mailbox_failures_returns_not_ok(case_store_path, monkeypatch):
    from ecom_ops.cases.mailboxes import MailboxConfig

    store = CaseStore(path=case_store_path)

    class BoomClient:
        def fetch(self, **kwargs):
            raise RuntimeError("mailbox down")

    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env",
        lambda **kw: BoomClient(),
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.enabled_mailboxes",
        lambda: [
            MailboxConfig(
                id="support_default",
                label="Support",
                address="support@azom.se",
            )
        ],
    )
    svc = CaseService(store=store)
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok is False
    assert getattr(result, "errors", 0) >= 1


def test_cli_version_without_woo_credentials(monkeypatch, capsys):
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.delenv("WOO_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("WOO_CONSUMER_SECRET", raising=False)
    code = main(["version"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "2.0.0"


def test_telegram_rejects_unknown_chat_when_allowlist_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")
    handler = BotHandler()
    reply = handler.handle(999, "/help")
    assert "inte behörig" in reply.lower() or "not authorized" in reply.lower() or "allowlist" in reply.lower()


def test_telegram_allows_listed_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")
    handler = BotHandler()
    reply = handler.handle(111, "/help")
    assert "hjälp" in reply.lower() or "help" in reply.lower() or "/" in reply


def test_woo_list_orders_for_probe():
    client = WooCommerceClient(
        base_url="https://mock.local",
        transport=InMemoryWooTransport(),
    )
    orders = client.list_orders(per_page=1)
    assert isinstance(orders, list)
    assert len(orders) >= 1


@pytest.fixture
def case_store_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return tmp_path / "cases.db"
