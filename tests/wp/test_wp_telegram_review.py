"""Tests for Telegram WP review callbacks, commands, attachments, and sendPhoto delivery."""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    MOCK_REGISTRY,
    make_png,
    mock_wp_content,
    passing_catalog,
    write_theme_file,
)

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot import job_store as job_store_mod
from ecom_ops.bot.chat_agent import (
    ChatResult,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.reply import (
    BotReply,
    safe_workspace_attachments,
)
from ecom_ops.bot.store import ConversationStore
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme


def test_telegram_wpreview_callbacks_job_store_preflight_handlers_and_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_env, actor_maps
) -> None:
    wp_env(mock="1", files_ssh="0", sandbox="0", reset_router=True)
    actor_maps(telegram={42: "oscar", 77: "jonatan", 88: "agent"})
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test")

    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Button tweak",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/tweak.php", "<?php\n// tweak\n", actor="oscar"
        ).ok
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", passing_catalog)
    assert plans.run_checks("mock-staging", plan_id, actor="oscar").data["status"] == "tests_passed"

    stage_cb = f"wpreview:stage:{plan_id}"
    no_cb = f"wpreview:no:{plan_id}"
    assert job_store_mod._ACTION_CALLBACK.match(stage_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match(no_cb) is None
    assert bot_main._ACTION_CALLBACK.fullmatch(stage_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(no_cb) is None

    conv_store = ConversationStore(path=tmp_path / "conv.json")
    handler = BotHandler(store=conv_store)
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *_a, **_k: ChatResult(text="Kontroller klara.", review_stage_plan_id=plan_id),
    )
    chat_reply = handler.handle(42, "kör kontroller")
    assert chat_reply.reply_markup is not None
    buttons = [btn for row in chat_reply.reply_markup["inline_keyboard"] for btn in row]
    assert any(btn.get("callback_data") == stage_cb for btn in buttons)
    assert any(btn.get("callback_data") == no_cb for btn in buttons)
    assert any(
        btn.get("url") == f"https://ops.example.test/wp/mock-staging/reviews/{plan_id}"
        for btn in buttons
    )
    cached_state = conv_store.get(42) or {}
    assert plan_id in (cached_state.get("session") or {}).get("wp_review_hashes", {})

    job_store = TelegramJobStore(path=tmp_path / "jobs.sqlite3")
    with job_store._connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_conversations(chat_id, state_json, updated_at) "
            "VALUES(?, ?, ?)",
            ("42", json.dumps(cached_state), time.time()),
        )

    accepted = job_store.accept_update(
        {
            "update_id": 1001,
            "callback_query": {
                "id": "cb1",
                "data": stage_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    assert accepted["disposition"] == "queued"
    job = job_store.get_job(accepted["job_id"])
    assert job is not None
    assert job["kind"] == "action"
    assert job["payload"]["approval_snapshot_required"] is True
    assert job["payload"]["approval_snapshot"] == TelegramJobStore.approval_snapshot(cached_state)
    assert bot_main._action_lock_key(job, handler) == "wp-file-edits"
    assert bot_main._preflight_action_job(handler, job) is None

    stale_job = {
        **job,
        "payload": {**job["payload"], "approval_snapshot": "0" * 64},
    }
    assert bot_main._preflight_action_job(handler, stale_job) is not None

    dup = job_store.accept_update(
        {
            "update_id": 1002,
            "callback_query": {
                "id": "cb2",
                "data": stage_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    assert dup["disposition"] == "duplicate_action"

    no_accepted = job_store.accept_update(
        {
            "update_id": 1003,
            "callback_query": {
                "id": "cb3",
                "data": no_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    no_job = job_store.get_job(no_accepted["job_id"])
    assert no_job is not None and no_job["kind"] == "callback"

    plans_oscar = handler.handle(42, "/wp plans mock-staging")
    assert plan_id in plans_oscar.text
    assert "Button tweak" in plans_oscar.text

    plans_jonatan = handler.handle(77, "/wp plans")
    assert plan_id in plans_jonatan.text
    assert "Button tweak" not in plans_jonatan.text

    plan_jonatan = handler.handle(77, f"/wp plan {plan_id}")
    assert plan_id in plan_jonatan.text
    assert plan_jonatan.reply_markup is None
    assert "kräver Oscar" in plan_jonatan.text

    plan_oscar = handler.handle(42, f"/wp plan {plan_id}")
    assert plan_oscar.reply_markup is not None
    assert any(
        btn.get("callback_data") == stage_cb
        for row in plan_oscar.reply_markup["inline_keyboard"]
        for btn in row
    )

    for denied_chat in (77, 88):
        denied_cb = handler.handle_callback(denied_chat, stage_cb)
        assert "kräver Oscar" in denied_cb.text
    assert not (
        mock_wp_content(tmp_path, "mock-staging")
        / "themes"
        / "azom-child"
        / "includes"
        / "tweak.php"
    ).exists()

    cancelled = handler.handle_callback(42, no_cb)
    assert "Avbrutet" in cancelled.text
    assert plan_id not in ((conv_store.get(42) or {}).get("session") or {}).get(
        "wp_review_hashes", {}
    )

    assert handler.handle(42, f"/wp plan {plan_id}").reply_markup is not None
    staged = handler.handle_callback(42, stage_cb)
    assert "skrivna i staging" in staged.text
    assert (
        mock_wp_content(tmp_path, "mock-staging")
        / "themes"
        / "azom-child"
        / "includes"
        / "tweak.php"
    ).read_text(encoding="utf-8") == "<?php\n// tweak\n"


def test_telegram_attachments_send_photo_and_bot_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()

    png_bytes = make_png(4, 4)
    art_dir = tmp_path / "wp_workspaces" / "mock-staging" / "wr-0123456789ab" / "artifacts"
    art_dir.mkdir(parents=True)
    valid_png = art_dir / "work-desktop.png"
    valid_png.write_bytes(png_bytes)

    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes)
    wrong_name = art_dir / "evil.png"
    wrong_name.write_bytes(png_bytes)
    bad_magic = art_dir / "work-mobile.png"
    bad_magic.write_bytes(b"not-a-png-header")
    traversal = art_dir / ".." / ".." / ".." / "outside.png"

    validated = safe_workspace_attachments(
        [str(valid_png), str(outside), str(traversal), str(wrong_name), str(bad_magic)],
        data_dir=tmp_path,
    )
    assert validated == (str(valid_png.resolve()),)

    # _send_photo constructs multipart/form-data POST to /sendPhoto
    captured: dict[str, Any] = {}

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 15.0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeResp(b'{"ok":true,"result":{"message_id":77}}')

    monkeypatch.setattr(bot_main.urllib.request, "urlopen", _fake_urlopen)
    photo_res = bot_main._send_photo("tok123", 555, valid_png, caption="Sandlåda: work-desktop.png")
    assert photo_res["ok"] is True
    assert captured["url"] == "https://api.telegram.org/bottok123/sendPhoto"
    assert captured["method"] == "POST"
    ctype = next(v for k, v in captured["headers"].items() if k.lower() == "content-type")
    assert ctype.startswith("multipart/form-data; boundary=")
    body = captured["data"]
    assert b'name="chat_id"\r\n\r\n555\r\n' in body
    assert b'name="caption"\r\n\r\nSandl' in body
    assert b'name="photo"; filename="work-desktop.png"\r\nContent-Type: image/png\r\n\r\n' + png_bytes in body

    # _result_for_reply / _reply_from_result round-trips attachments
    orig = BotReply(text="Hej", attachments=(str(valid_png),))
    payload = bot_main._result_for_reply(orig)
    assert payload["attachments"] == [str(valid_png)]
    restored = bot_main._reply_from_result(payload)
    assert restored.attachments == (str(valid_png),)

    # BotHandler forwards safe_workspace_attachments from ChatResult.attachments -> BotReply.attachments
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *a, **kw: ChatResult(
            text="Kontroll klar",
            attachments=(str(valid_png), str(outside), str(bad_magic)),
        ),
    )
    handler = BotHandler(store=ConversationStore(path=tmp_path / "conv.json"))
    reply = handler.handle(42, "status")
    assert reply.attachments == (str(valid_png.resolve()),)
