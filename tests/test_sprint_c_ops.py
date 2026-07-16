"""Sprint C: poll partial visibility + Telegram actor fail-closed."""

from __future__ import annotations

import pytest

from ecom_ops.bot.actors import TelegramActorDenied, resolve_telegram_actor
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.ops_status import readiness_from_last_poll, write_last_case_poll


def test_actor_default_jonatan_when_map_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.delenv("TELEGRAM_FAIL_CLOSED", raising=False)
    assert resolve_telegram_actor(999) == "jonatan"


def test_actor_fail_closed_when_map_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "111:jonatan")
    assert resolve_telegram_actor(111) == "jonatan"
    with pytest.raises(TelegramActorDenied):
        resolve_telegram_actor(999)


def test_actor_fail_closed_env_empty_map(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.setenv("TELEGRAM_FAIL_CLOSED", "1")
    with pytest.raises(TelegramActorDenied):
        resolve_telegram_actor(1)


def test_handler_denies_unmapped_when_map_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:jonatan")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    h = BotHandler(store=ConversationStore(path=tmp_path / "tg.json"))
    r = h.handle(999, "/help")
    assert "TELEGRAM_ACTOR_MAP" in r.text or "mapping" in r.text.lower()
    ok = h.handle(42, "/help")
    assert "help" in ok.text.lower() or "kommando" in ok.text.lower() or "/" in ok.text


def test_readiness_partial_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    write_last_case_poll(
        ok=True,
        errors=1,
        created=2,
        extra={"partial": True, "mailboxes": 2},
    )
    ready = readiness_from_last_poll()
    assert ready["partial"] is True
    assert ready["ok"] is False
    assert ready.get("detail") and "partial" in str(ready["detail"]).lower()
