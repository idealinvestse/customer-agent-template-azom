"""Telegram actor mapping: chat_id → RBAC actor."""

from __future__ import annotations

from ecom_ops.bot.actors import resolve_telegram_actor
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.actions.mail import MailService
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailProvider,
)


def test_resolve_telegram_actor_from_map(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "111:jonatan,222:oscar,333:agent")
    assert resolve_telegram_actor(111) == "jonatan"
    assert resolve_telegram_actor("222") == "oscar"
    assert resolve_telegram_actor(333) == "agent"


def test_resolve_telegram_actor_default_jonatan(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    assert resolve_telegram_actor(999) == "jonatan"


def test_whoami_shows_mapped_actor(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "55:oscar")
    store = ConversationStore(path=tmp_path / "tg.json")
    reply = dispatch_openclaw_command(55, "/whoami", store)
    assert "55" in reply
    assert "oscar" in reply.lower()


def test_cases_approve_uses_mapped_actor(tmp_path, monkeypatch, telemetry):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Map actor",
        from_addr="kund@example.com",
        body="help",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<map-actor@x>",
    )
    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP, from_addr="support@azom.se"
        ),
        transport=transport,
    )
    seen: list[str] = []
    mail = MailService(client=client, telemetry=telemetry)
    orig = mail.send

    def wrap(**kwargs):
        actor = kwargs.get("actor")
        seen.append(actor.name if hasattr(actor, "name") else str(actor))
        return orig(**kwargs)

    mail.send = wrap  # type: ignore[method-assign]

    def fake_case_service(*_a, **_k):
        return CaseService(store=store, mail=mail, telemetry=telemetry)

    monkeypatch.setattr(
        "ecom_ops.cases.service.CaseService",
        fake_case_service,
    )
    conv = ConversationStore(path=tmp_path / "tg.json")
    reply = dispatch_openclaw_command(42, f"/cases approve {case.id[:8]}", conv)
    assert "Skickat" in reply or "replied" in reply.lower(), reply
    assert seen and seen[0] == "oscar"
