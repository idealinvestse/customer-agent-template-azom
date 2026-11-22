"""Telegram shop content: posts, site text, themes, and plugin on/off."""

from __future__ import annotations

from ecom_ops.bot.chat_agent import run_chat
from ecom_ops.bot.dialog_actions import PendingAction
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.rbac import Actor, clear_rbac_cache


def _no_llm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)


def test_list_posts_skips_the_model(monkeypatch):
    _no_llm(monkeypatch)
    result = run_chat("visa inlägg")
    assert "Hej från Azom" in result.text
    assert "1" in result.text
    assert result.pending_action is None
    assert result.sticky_post_id == "1"


def test_post_rename_is_pending_until_confirm(monkeypatch):
    _no_llm(monkeypatch)

    def boom(*_args, **_kwargs):
        raise AssertionError("write")

    monkeypatch.setattr(
        "ecom_ops.integrations.wordpress.WordPressClient.update_post",
        boom,
    )
    result = run_chat("ändra inlägg 1 titel till Ny rubrik")
    assert result.pending_action is not None
    assert result.pending_action.kind == "post_update"
    assert result.pending_action.payload["post_id"] == "1"
    assert result.pending_action.payload["fields"]["title"] == "Ny rubrik"
    assert "Ja" in result.text


def test_sticky_post_rename(monkeypatch):
    _no_llm(monkeypatch)
    result = run_chat(
        "ändra inlägget titel till Ny",
        session={"last_post_id": "1"},
    )
    assert result.pending_action is not None
    assert result.pending_action.payload["post_id"] == "1"
    assert result.pending_action.payload["fields"]["title"] == "Ny"


def test_page_rename_is_not_stolen_by_posts(monkeypatch):
    _no_llm(monkeypatch)
    result = run_chat("ändra sidan Om oss titel till Om Azom")
    assert result.pending_action is not None
    assert result.pending_action.kind == "page_update"
    assert result.pending_action.payload["page_id"] == "2"


def test_site_title_is_pending_and_not_written(monkeypatch):
    _no_llm(monkeypatch)

    def boom(*_args, **_kwargs):
        raise AssertionError("write")

    monkeypatch.setattr(
        "ecom_ops.integrations.wordpress.WordPressClient.update_site_identity",
        boom,
    )
    result = run_chat("ändra sajttitel till Butiken")
    assert result.pending_action is not None
    assert result.pending_action.kind == "site_update"
    assert result.pending_action.payload["fields"] == {"title": "Butiken"}
    assert "Ja" in result.text
    shown = run_chat("vad heter sajten")
    assert "Azom" in shown.text
    assert "Nordisk e-handel" in shown.text


def test_tagline_proposal(monkeypatch):
    _no_llm(monkeypatch)
    result = run_chat("ändra tagline till Kort rad")
    assert result.pending_action.kind == "site_update"
    assert result.pending_action.payload["fields"]["description"] == "Kort rad"


def test_theme_list_skips_the_model_and_refuses_a_switch(monkeypatch):
    _no_llm(monkeypatch)
    result = run_chat("visa teman")
    assert "twentytwentyfive" in result.text
    assert "aktiv" in result.text
    assert result.pending_action is None
    refused = run_chat("byt tema till twentytwentyfour")
    assert refused.pending_action is None
    assert "Temabyte" in refused.text


def test_plugin_list_and_toggle_wait_for_ja(monkeypatch):
    _no_llm(monkeypatch)

    def boom(*_args, **_kwargs):
        raise AssertionError("write")

    monkeypatch.setattr(
        "ecom_ops.integrations.wordpress.WordPressClient.set_plugin_status",
        boom,
    )
    listed = run_chat("visa plugins")
    assert "woocommerce/woocommerce" in listed.text
    assert "aktiv" in listed.text
    assert "akismet/akismet" in listed.text
    assert listed.pending_action is None
    result = run_chat("aktivera plugin akismet")
    assert result.pending_action is not None
    assert result.pending_action.kind == "plugin_status"
    assert result.pending_action.payload["plugin"] == "akismet/akismet"
    assert result.pending_action.payload["status"] == "active"
    assert "Ja" in result.text
    off = run_chat("stäng av plugin woocommerce")
    assert off.pending_action.payload["status"] == "inactive"
    assert off.pending_action.payload["plugin"] == "woocommerce/woocommerce"


def test_plugin_install_update_delete_and_files_are_refused(monkeypatch):
    _no_llm(monkeypatch)
    for phrase in (
        "installera plugin foo",
        "uppdatera plugin woocommerce",
        "radera plugin akismet",
        "ändra functions.php",
    ):
        result = run_chat(phrase)
        assert result.pending_action is None, phrase
        assert "inte" in result.text.lower() or "väntar" in result.text.lower()


def test_model_plugin_write_is_not_executed(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    from ecom_ops.bot.chat_agent import _fulfill_model_tool_call

    raw = (
        '<tool_call>{"name": "set_plugin_status", "arguments": '
        '{"plugin": "akismet/akismet", "status": "active"}}</tool_call>'
    )
    text, shown = _fulfill_model_tool_call(raw, domain=None, offset=0)
    assert text is not None
    assert "inte gjord" in text
    assert "tool_call" not in text
    assert shown is None


def test_executes_only_the_confirmed_field(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    clear_rbac_cache()
    from ecom_ops.bot.site_content import (
        execute_plugin_status,
        execute_post_update,
        execute_site_update,
    )

    transport = InMemoryWpTransport()
    client = WordPressClient(base_url="https://mock.local", transport=transport)

    def same_client(**_kwargs):
        return client

    monkeypatch.setattr("ecom_ops.integrations.wordpress.wp_client_from_env", same_client)
    ok, _msg = execute_post_update(
        post_id="1",
        fields={"title": "Ny", "status": "trash"},
        actor="oscar",
    )
    assert not ok
    assert transport.posts[1]["title"]["rendered"] == "Hej från Azom"
    ok, msg = execute_post_update(
        post_id="1",
        fields={"title": "Ny rubrik"},
        actor="oscar",
    )
    assert ok
    assert "1" in msg
    assert transport.posts[1]["title"]["rendered"] == "Ny rubrik"
    ok, _msg = execute_site_update(
        fields={"title": "Butiken", "email": "a@b.co"},
        actor="agent",
    )
    assert not ok
    assert transport.settings["title"] == "Azom"
    ok, _msg = execute_site_update(fields={"description": "Kort"}, actor="agent")
    assert ok
    assert transport.settings["description"] == "Kort"
    assert transport.settings["language"] == "nb"
    denied, _msg = execute_plugin_status(
        plugin="akismet/akismet",
        status="active",
        actor="agent",
    )
    assert not denied
    assert transport.plugins["akismet/akismet"]["status"] == "inactive"
    ok, msg = execute_plugin_status(
        plugin="akismet/akismet",
        status="active",
        actor="oscar",
    )
    assert ok
    assert "aktiv" in msg
    assert transport.plugins["akismet/akismet"]["status"] == "active"
    assert transport.plugins["akismet/akismet"]["version"] == "5.3"


def test_jonatan_cannot_confirm_plugin_or_post(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    clear_rbac_cache()
    store = ConversationStore(path=tmp_path / "tg.json")
    handler = BotHandler(store=store)
    viewer = Actor(name="jonatan", role="viewer")
    plugin = PendingAction(
        kind="plugin_status",
        payload={"plugin": "akismet/akismet", "status": "active"},
    )
    denied = handler._start_pending("1", plugin, "Föreslår plugin", actor=viewer)
    assert "WP_OPS_ADMIN" in denied.text
    assert denied.actions is None
    post = PendingAction(
        kind="post_update",
        payload={"post_id": "1", "fields": {"title": "Ny"}},
    )
    denied_post = handler._start_pending("1", post, "Föreslår inlägg", actor=viewer)
    assert "WP_OPS_OPERATE" in denied_post.text
    reply = handler._exec_pending("1", plugin)
    assert "WP_OPS_ADMIN" in reply.text


def test_oscar_confirm_toggles_the_plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "55:oscar")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    clear_rbac_cache()

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    transport = InMemoryWpTransport()
    client = WordPressClient(base_url="https://mock.local", transport=transport)

    def same_client(**_kwargs):
        return client

    monkeypatch.setattr("ecom_ops.integrations.wordpress.wp_client_from_env", same_client)
    store = ConversationStore(path=tmp_path / "oscar.json")
    handler = BotHandler(store=store)
    first = handler.handle(55, "aktivera plugin akismet")
    assert first.actions
    assert transport.plugins["akismet/akismet"]["status"] == "inactive"
    second = handler.handle_callback(55, "action:yes")
    assert "är aktiv" in second.text
    assert transport.plugins["akismet/akismet"]["status"] == "active"
