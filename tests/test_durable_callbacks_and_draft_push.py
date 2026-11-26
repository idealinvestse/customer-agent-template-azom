"""Tests for C1 WP draft push callback, C5 Messenger media jail, and C6 durable legacy callback bindings."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecom_ops.bot import chat_agent, handlers
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import _ACTION_CALLBACK, TelegramJobStore
from ecom_ops.bot.reply import (
    approve_case_actions,
    bot_reply,
    order_status_confirm_actions,
    product_desc_confirm_actions,
)
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.service import WpOpsService


def _tiny_png(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return str(path.resolve())


@pytest.fixture()
def env_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "100,200,300")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "100:oscar,200:agent,300:jonatan")
    return tmp_path


def test_wpreview_push_draft_button_pinning_rbac_and_fail_closed(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    plan_id = "wr-123456abcdef"
    cb = f"wpreview:push_draft:{plan_id}"
    assert _ACTION_CALLBACK.match(cb) is not None

    accepted = jstore.accept_update(
        {
            "update_id": 9101,
            "callback_query": {
                "id": "cb-draft",
                "data": cb,
                "message": {"message_id": 50, "chat": {"id": 100}},
            },
        }
    )
    assert accepted["disposition"] == "queued"
    job = jstore.get_job(accepted["job_id"])
    assert job is not None and job["kind"] == "action"
    assert job["payload"]["approval_snapshot_required"] is True

    monkeypatch.setattr(
        ReviewPlans,
        "get",
        lambda self, pid, site_id=None: {
            "plan_id": pid,
            "site_id": "mock-shop",
            "slug": "azom-theme",
            "page_draft": {"slug": "tack-ny", "page_type": "thankyou"},
            "base_sha256": "a" * 64,
            "work_sha256": "b" * 64,
        },
    )

    # 1. Button emitted when run_chat returns page_draft review for Oscar (100), hidden for agent (200)
    monkeypatch.setattr(
        handlers,
        "run_chat",
        lambda *a, **k: chat_agent.ChatResult(
            text="Utkast klart.",
            review_stage_plan_id=plan_id,
            active_wp_review={
                "site_id": "mock-shop",
                "latest_plan_id": plan_id,
                "page_draft": {"slug": "tack-ny", "page_type": "thankyou", "preview_kind": "standalone_page"},
            },
        ),
    )
    r_oscar = bot.handle(100, "skapa tacksida")
    payloads_oscar = [b.payload for row in r_oscar.actions.rows for b in row if b.payload]
    assert cb in payloads_oscar

    r_agent = bot.handle(200, "skapa tacksida")
    payloads_agent = [b.payload for row in r_agent.actions.rows for b in row if b.payload]
    assert cb not in payloads_agent

    # 2. Viewer (300) and Operator (200) are RBAC-denied on wpreview:push_draft:
    for denied_chat in (200, 300):
        res_denied = bot.handle_callback(denied_chat, cb)
        assert "Oscar" in res_denied.text and "WP_OPS_ADMIN" in res_denied.text

    # 3. Missing pinned hash fails closed with MissingApprovalHash
    res_unpinned = bot.handle_callback(100, "wpreview:push_draft:wr-abcdef001122")
    assert "MissingApprovalHash" in res_unpinned.text

    # 4. Tampered work/ returns DigestMismatch cleanly without crashing
    push_calls: list[dict[str, Any]] = []

    def fake_push(self, site_id, pid, *, actor, base_sha256, work_sha256, client=None):
        push_calls.append(
            {
                "site_id": site_id,
                "plan_id": pid,
                "actor": actor,
                "base_sha256": base_sha256,
                "work_sha256": work_sha256,
            }
        )
        return SimpleNamespace(
            ok=False,
            data={"error_class": "DigestMismatch"},
            message="work_sha256 mismatch",
        )

    monkeypatch.setattr(WpOpsService, "push_page_draft", fake_push, raising=False)
    res_mismatch = bot.handle_callback(100, cb)
    assert "DigestMismatch" in res_mismatch.text
    assert push_calls[-1]["base_sha256"] == "a" * 64
    assert push_calls[-1]["work_sha256"] == "b" * 64

    # 5. Valid Oscar push returns draft status and wp-admin edit_url (never auto-published)
    def fake_push_ok(self, site_id, pid, *, actor, base_sha256, work_sha256, client=None):
        return {
            "ok": True,
            "site_id": site_id,
            "plan_id": pid,
            "draft_page": {
                "id": 77,
                "slug": "tack-ny",
                "status": "draft",
                "link": "https://mock-shop.example.test/?page_id=77&preview=true",
                "edit_url": "https://mock-shop.example.test/wp-admin/post.php?post=77&action=edit",
            },
        }

    monkeypatch.setattr(WpOpsService, "push_page_draft", fake_push_ok, raising=False)
    res_ok = bot.handle_callback(100, cb)
    assert "status=draft" in res_ok.text
    assert "post.php?post=77&action=edit" in res_ok.text


def test_durable_legacy_callback_binding_market_pin_replay_expiry_and_actor(
    env_setup, monkeypatch
):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    # Seed market = "se" when emitting order:set button
    cstore.append_turn_atomic(100, session_updates={"last_market": "se"})
    reply_order = bot._bind_actions_in_reply(
        100,
        bot_reply("Bekräfta?", actions=order_status_confirm_actions("1001", "completed")),
    )
    order_cb = reply_order.actions.rows[0][0].payload
    assert order_cb == "order:set:1001:completed"

    # Switch session market AFTER button was emitted: execution MUST use pinned "se"!
    cstore.append_turn_atomic(100, session_updates={"last_market": "no"})
    seen_domains: list[str | None] = []
    monkeypatch.setattr(
        handlers,
        "execute_order_status",
        lambda order_id, status, actor, domain=None: (
            seen_domains.append(domain) or True,
            f"Order #{order_id} -> {status} ({domain})",
        ),
    )

    first = bot.handle_callback(100, order_cb)
    assert "Klart." in first.text
    assert seen_domains == ["se"]

    # Double-click replay MUST fail closed!
    replay = bot.handle_callback(100, order_cb)
    assert "redan använd" in replay.text.lower()
    assert len(seen_domains) == 1

    # Expired binding fails closed!
    bot._bind_actions_in_reply(
        100,
        bot_reply("Bekräfta?", actions=order_status_confirm_actions("1002", "completed")),
    )
    with jstore._connection() as conn:
        conn.execute(
            "UPDATE bot_proposals SET expires_at=? WHERE callback_key=?",
            (time.time() - 5, "order:set:1002:completed"),
        )
    expired_reply = bot.handle_callback(100, "order:set:1002:completed")
    assert "gått ut" in expired_reply.text.lower()

    # Wrong actor replay fails closed without consuming legitimate actor's binding!
    bot._bind_actions_in_reply(
        100,
        bot_reply("Beskrivning?", actions=product_desc_confirm_actions("55", publish=False)),
        actor="oscar",
    )
    status_wrong, _ = jstore.consume_callback_proposal(
        100, "product:desc:55:0", actor="agent"
    )
    assert status_wrong == "wrong_actor"
    monkeypatch.setattr(
        handlers,
        "execute_product_desc",
        lambda product_id, language, publish, actor: (True, f"Skapade för {product_id}"),
    )
    ok_desc = bot.handle_callback(100, "product:desc:55:0")
    assert "Klart." in ok_desc.text

    # cases:approve binding + single-use replay rejection
    cases_db = CaseStore(path=env_setup / "cases.sqlite3")
    case = cases_db.create_case(
        mailbox_id="support_default",
        subject="Fråga",
        from_addr="a@example.invalid",
        body="Hej",
        category="order_status",
        draft_reply="Svar",
        order_id="1001",
        message_id="<bind@example.invalid>",
        status="open",
    )
    bot._bind_actions_in_reply(
        100,
        bot_reply("Ärende", actions=approve_case_actions(case.id)),
        actor="oscar",
    )
    monkeypatch.setattr(
        bot,
        "_dispatch_oc_with_progress",
        lambda cid, cmd, on_progress=None: f"Skickat ({cmd})",
    )
    case_cb = f"cases:approve:{case.id[:8]}"
    assert "Skickat" in bot.handle_callback(100, case_cb).text
    assert "redan använd" in bot.handle_callback(100, case_cb).text.lower()


def test_messenger_inbox_media_allowed_while_outside_dir_rejected(
    env_setup, tmp_path_factory
):
    messenger_png = _tiny_png(
        env_setup / "messenger_inbox_media" / "psid1" / "shot.png"
    )
    outside_dir = tmp_path_factory.mktemp("outside_msg")
    outside_png = _tiny_png(outside_dir / "outside.png")

    assert handlers._normalize_inbound_image_paths([messenger_png, outside_png]) == [
        messenger_png
    ]
    assert chat_agent._validate_chat_image_paths([messenger_png, outside_png]) == [
        messenger_png
    ]


def test_unbound_and_gc_purged_legacy_callback_fails_closed_when_proposal_store_set(
    env_setup, monkeypatch
):
    from ecom_ops.ops_gc import run_ops_gc

    cstore = ConversationStore(env_setup / "state_gc.json")
    jstore = TelegramJobStore(env_setup / "telegram_jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    executed: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        handlers,
        "execute_order_status",
        lambda order_id, status, actor, domain=None: (
            executed.append((order_id, status, domain)) or True,
            "ok",
        ),
    )

    # 1. Forged/unbound legacy callback MUST fail closed when proposal_store is active
    forged = bot.handle_callback(100, "order:set:9999:cancelled")
    assert "saknas eller har löpt ut" in forged.text.lower()
    assert executed == []

    # 2. Expired binding purged by run_ops_gc MUST NOT resurrect after market switch
    cstore.append_turn_atomic(100, session_updates={"last_market": "se"})
    bot._bind_actions_in_reply(
        100,
        bot_reply("Bekräfta?", actions=order_status_confirm_actions("1001", "cancelled")),
    )
    with jstore._connection() as conn:
        conn.execute(
            "UPDATE bot_proposals SET expires_at=?, created_at=? WHERE callback_key=?",
            (time.time() - 10, time.time() - 90_000, "order:set:1001:cancelled"),
        )
    gc_res = run_ops_gc(data_dir=env_setup, dry_run=False, proposal_max_age_hours=1)
    assert gc_res["purged_proposals"] >= 1
    cstore.append_turn_atomic(100, session_updates={"last_market": "no"})

    resurrect = bot.handle_callback(100, "order:set:1001:cancelled")
    assert "saknas eller har löpt ut" in resurrect.text.lower()
    assert executed == []


