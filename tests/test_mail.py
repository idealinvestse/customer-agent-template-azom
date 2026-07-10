"""Mail connector + action tests."""

from __future__ import annotations

from ecom_ops.actions.mail import MailService
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
    build_xoauth2_string,
    client_from_env,
    config_from_env,
)
from ecom_ops.security import SecurityError
import pytest


def test_xoauth2_string():
    s = build_xoauth2_string("user@example.com", "tok123")
    assert s.startswith("user=user@example.com")
    assert "Bearer tok123" in s
    assert s.endswith("\x01\x01")


def test_mock_send_and_fetch(mail_client, telemetry, escalation):
    svc = MailService(client=mail_client, telemetry=telemetry, escalation=escalation)
    sent = svc.send(
        to="customer@example.com",
        subject="Order update",
        body="Din order är på väg.",
        actor="agent",
    )
    assert sent.ok
    assert sent.to == ["customer@example.com"]
    assert sent.subject == "Order update"

    fetched = svc.fetch(actor="agent")
    assert fetched.ok
    assert fetched.count >= 1
    assert any("order" in m["subject"].lower() for m in fetched.messages)


def test_jonatan_can_fetch_not_send(mail_client, telemetry, escalation):
    svc = MailService(client=mail_client, telemetry=telemetry, escalation=escalation)
    fetch = svc.fetch(actor="jonatan")
    assert fetch.ok

    send = svc.send(
        to="a@b.co",
        subject="Nope",
        body="Should fail",
        actor="jonatan",
    )
    assert not send.ok
    assert send.escalated
    assert send.ticket_id


def test_invalid_email_rejected(mail_client, telemetry, escalation):
    svc = MailService(client=mail_client, telemetry=telemetry, escalation=escalation)
    result = svc.send(to="not-an-email", subject="X", body="Y", actor="agent")
    assert not result.ok
    assert not result.escalated


def test_reply(mail_client, telemetry, escalation):
    svc = MailService(client=mail_client, telemetry=telemetry, escalation=escalation)
    result = svc.reply(
        to="customer@example.com",
        subject="Order 1001 status?",
        body="Vi kollar ordern.",
        actor="agent",
    )
    assert result.ok
    assert result.to == ["customer@example.com"]


def test_inmemory_mark_read():
    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(provider=MailProvider.GENERIC_IMAP, from_addr="s@azom.se"),
        transport=transport,
    )
    before = client.fetch_unread()
    assert before
    uid = before[0].uid
    assert uid
    client.mark_read(uid)
    after = client.fetch_unread()
    assert all(m.uid != uid for m in after)


def test_provider_defaults_gmail(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "gmail")
    monkeypatch.setenv("MAIL_USERNAME", "me@gmail.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    cfg = config_from_env()
    assert cfg.provider == MailProvider.GMAIL
    assert cfg.smtp_host == "smtp.gmail.com"
    assert cfg.imap_host == "imap.gmail.com"


def test_provider_defaults_outlook(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "outlook")
    cfg = config_from_env()
    assert cfg.provider == MailProvider.OUTLOOK
    assert cfg.smtp_host == "smtp.office365.com"


def test_invalid_provider(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "fax-machine")
    with pytest.raises(SecurityError):
        config_from_env()


def test_client_from_env_mock():
    client = client_from_env(use_mock=True)
    status = client.send(
        to="a@b.co",
        subject="Hi",
        body="Hello",
    )
    assert status["status"] == "sent"
    msgs = client.fetch_unread()
    assert isinstance(msgs, list)


def test_graph_transport_mock_session():
    """Graph send/fetch against a fake requests Session."""

    class FakeResp:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text or str(payload)

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.posts = []
            self.gets = []
            self.patches = []

        def post(self, url, headers=None, json=None, data=None, timeout=30):
            self.posts.append((url, headers, json, data))
            if "oauth2" in url and "token" in url:
                return FakeResp(200, {"access_token": "graph-token"})
            if "sendMail" in url:
                return FakeResp(202, {})
            return FakeResp(400, text="unexpected post")

        def get(self, url, headers=None, params=None, timeout=30):
            self.gets.append((url, headers, params))
            return FakeResp(
                200,
                {
                    "value": [
                        {
                            "id": "g1",
                            "subject": "Hello Graph",
                            "bodyPreview": "preview",
                            "body": {"contentType": "Text", "content": "body text"},
                            "from": {
                                "emailAddress": {"address": "c@example.com"}
                            },
                            "toRecipients": [
                                {"emailAddress": {"address": "support@azom.se"}}
                            ],
                            "ccRecipients": [],
                            "receivedDateTime": "2026-01-01T00:00:00Z",
                            "isRead": False,
                            "internetMessageId": "<g1@x>",
                        }
                    ]
                },
            )

        def patch(self, url, headers=None, json=None, timeout=30):
            self.patches.append((url, headers, json))
            return FakeResp(200, {})

    from ecom_ops.integrations.mail import GraphMailTransport

    cfg = MailConfig(
        provider=MailProvider.EXCHANGE_GRAPH,
        graph_tenant_id="tenant",
        graph_client_id="cid",
        graph_client_secret="csecret",
        graph_user="support@azom.se",
    )
    session = FakeSession()
    transport = GraphMailTransport(cfg, session=session)
    send_status = transport.send(
        MailMessage(
            subject="Test",
            body="Hi",
            from_addr="support@azom.se",
            to_addrs=["c@example.com"],
        )
    )
    assert send_status["status"] == "sent"
    msgs = transport.fetch(limit=5)
    assert len(msgs) == 1
    assert msgs[0].subject == "Hello Graph"
    transport.mark_read("g1")
    assert session.patches


def test_cli_mail_send_and_fetch(capsys):
    from ecom_ops.cli import main

    code = main(
        [
            "--mock",
            "mail",
            "send",
            "--to",
            "customer@example.com",
            "--subject",
            "Test",
            "--body",
            "Hej",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out or '"ok": true'.replace(" ", "") in out.replace(" ", "")

    code = main(["--mock", "mail", "fetch"])
    assert code == 0
