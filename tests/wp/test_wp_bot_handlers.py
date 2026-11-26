"""Unit tests for PR 6.3 extracted bot WP session & handler modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.bot import handlers, wp_handlers, wp_session
from ecom_ops.bot.chat_agent import ChatResult
from ecom_ops.bot.store import ConversationStore
from ecom_ops.wp_ops.agent_tools import context_for_storage


def test_wp_session_prunes_mismatched_actor_channel_and_ttl() -> None:
    now = datetime.now(UTC)
    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    assert ctx is not None
    session = {
        "wp_ops_context": ctx,
        "wp_review_hashes": {
            "wr-111111111111": {
                "actor": "oscar",
                "site_id": "mock-staging",
                "base_sha256": "a" * 64,
                "work_sha256": "b" * 64,
            },
            "wr-222222222222": {
                "actor": "jonatan",
                "site_id": "mock-staging",
                "base_sha256": "c" * 64,
                "work_sha256": "d" * 64,
            },
        },
        "active_wp_review": {
            "actor_id": "oscar",
            "channel": "telegram",
            "conversation_id": "telegram:42",
            "site_id": "mock-staging",
            "latest_plan_id": "wr-111111111111",
            "updated_at": now.isoformat(),
        },
    }

    # Same actor/channel keeps oscar's context & active review, drops jonatan's hash
    updates, popped = wp_session.prune_mismatched_wp_session(
        session, actor="oscar", channel="telegram", conversation_id="telegram:42"
    )
    assert popped == []
    assert list(updates["wp_review_hashes"]) == ["wr-111111111111"]
    assert "wp_ops_context" in session
    assert "active_wp_review" in session

    # Switching actor to jonatan clears all 3 keys
    updates2, popped2 = wp_session.prune_mismatched_wp_session(
        session, actor="jonatan", channel="telegram", conversation_id="telegram:42"
    )
    assert updates2 == {}
    assert set(popped2) == {"wp_ops_context", "wp_review_hashes", "active_wp_review"}
    assert session == {}

    # Expired active_wp_review is rejected
    expired_review = {
        "actor_id": "oscar",
        "channel": "telegram",
        "conversation_id": "telegram:42",
        "site_id": "mock-staging",
        "latest_plan_id": "wr-111111111111",
        "updated_at": (now - timedelta(hours=13)).isoformat(),
    }
    assert (
        wp_session.valid_active_wp_review(
            expired_review,
            actor="oscar",
            channel="telegram",
            conversation_id="telegram:42",
        )
        is None
    )


def test_wp_session_resolve_pinned_hashes_fallback_to_proposal_store() -> None:
    class _FakeProposalStore:
        def get_pinned_review_hashes(self, _chat_id, plan_id: str):
            if plan_id == "wr-aaaaaaaaaaaa":
                return {
                    "actor": "oscar",
                    "site_id": "mock-staging",
                    "base_sha256": "1" * 64,
                    "work_sha256": "2" * 64,
                }
            return None

    # Falls back to proposal_store when session lacks pin
    resolved = wp_session.resolve_pinned_hashes(
        {},
        _FakeProposalStore(),
        99,
        actor="oscar",
        site_id="mock-staging",
        plan_id="wr-aaaaaaaaaaaa",
    )
    assert resolved == ("1" * 64, "2" * 64)

    # Wrong actor is denied
    assert (
        wp_session.resolve_pinned_hashes(
            {},
            _FakeProposalStore(),
            99,
            actor="jonatan",
            site_id="mock-staging",
            plan_id="wr-aaaaaaaaaaaa",
        )
        is None
    )


def test_wp_handlers_delegation_and_handlers_run_chat_patchability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "100")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "100:oscar")

    # Verify compatibility aliases on handlers module
    assert handlers._wpreview_stage_actions is wp_handlers.wpreview_stage_actions
    assert handlers._review_plan_ready is wp_handlers.review_plan_ready
    assert handlers._with_review_hashes is wp_session.with_review_hashes
    assert handlers._prune_review_hashes is wp_session.prune_review_hashes

    # Verify /wp help & /wp clear route through wp_handlers cleanly
    store = ConversationStore(tmp_path / "bot_state.db")
    bot = handlers.BotHandler(store=store)
    help_reply = bot.handle(100, "/wp help")
    assert "/wp sites" in help_reply.text

    # Verify handlers.run_chat monkeypatch target stays intact
    captured: list[str] = []

    def _fake_run_chat(msg: str, **_kwargs) -> ChatResult:
        captured.append(msg)
        return ChatResult(text="patched-ok", messages=[{"role": "user", "content": msg}])

    monkeypatch.setattr(handlers, "run_chat", _fake_run_chat)
    chat_reply = bot.handle(100, "hej hur mår du")
    assert captured == ["hej hur mår du"]
    assert chat_reply.text == "patched-ok"
