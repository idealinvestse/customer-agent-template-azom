"""Upfront RBAC on write confirm flows in BotHandler."""

from __future__ import annotations

from ecom_ops.bot.dialog_actions import PendingAction
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.rbac import Actor, clear_rbac_cache


def test_pending_allowed_for_viewer():
    handler = BotHandler(store=ConversationStore())
    viewer = Actor(name="jonatan", role="viewer")
    operator = Actor(name="agent", role="operator")
    pending_order = PendingAction(
        kind="order_status", payload={"order_id": "1001", "status": "completed"}
    )
    pending_product = PendingAction(
        kind="product_desc", payload={"product_id": "42", "publish": False}
    )
    pending_regen = PendingAction(kind="case_regenerate", payload={"case_id": "abcd1234"})
    assert not handler._pending_allowed(viewer, pending_order)
    assert not handler._pending_allowed(viewer, pending_product)
    assert handler._pending_allowed(viewer, pending_regen)
    assert handler._pending_allowed(operator, pending_order)
    assert handler._pending_allowed(operator, pending_product)
    product_update = PendingAction(
        kind="product_update",
        payload={"product_id": "501", "fields": {"name": "X"}},
    )
    page_update = PendingAction(
        kind="page_update",
        payload={"page_id": "2", "fields": {"title": "Om"}},
    )
    assert not handler._pending_allowed(viewer, product_update)
    assert not handler._pending_allowed(viewer, page_update)
    assert handler._pending_allowed(operator, product_update)
    assert handler._pending_allowed(operator, page_update)
    post_update = PendingAction(
        kind="post_update", payload={"post_id": "1", "fields": {"title": "Hej"}}
    )
    site_update = PendingAction(
        kind="site_update", payload={"fields": {"title": "Azom"}}
    )
    plugin_status = PendingAction(
        kind="plugin_status",
        payload={"plugin": "akismet/akismet", "status": "active"},
    )
    oscar = Actor(name="oscar", role="full_admin")
    assert not handler._pending_allowed(viewer, post_update)
    assert not handler._pending_allowed(viewer, site_update)
    assert not handler._pending_allowed(viewer, plugin_status)
    assert handler._pending_allowed(operator, post_update)
    assert handler._pending_allowed(operator, site_update)
    assert not handler._pending_allowed(operator, plugin_status)
    assert handler._pending_allowed(oscar, plugin_status)


def test_exec_pending_enforces_pending_allowed_messenger(tmp_path, monkeypatch):
    clear_rbac_cache()
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("MESSENGER_ACTOR_MAP", raising=False)
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    store = ConversationStore(path=tmp_path / "m.json")
    handler = BotHandler(store=store, channel="messenger")
    pending = PendingAction(
        kind="order_status", payload={"order_id": "1001", "status": "completed"}
    )
    reply = handler._exec_pending("psid-jonatan", pending)
    assert "operator" in reply.text.lower() or "oscar" in reply.text.lower()


def test_start_pending_denies_order_write_for_jonatan(tmp_path, monkeypatch):
    clear_rbac_cache()
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ConversationStore(path=tmp_path / "tg.json")
    handler = BotHandler(store=store)
    pending = PendingAction(
        kind="order_status", payload={"order_id": "1001", "status": "completed"}
    )
    viewer = Actor(name="jonatan", role="viewer")
    reply = handler._start_pending(
        "chat1", pending, "Föreslår ändring", actor=viewer
    )
    assert "operator" in reply.text.lower() or "oscar" in reply.text.lower()
    assert reply.actions is None
    page = PendingAction(
        kind="page_update", payload={"page_id": "2", "fields": {"title": "Om"}}
    )
    denied = handler._start_pending("chat1", page, "Föreslår sida", actor=viewer)
    assert "WP_OPS_OPERATE" in denied.text
    assert denied.actions is None
