"""Resolve compact Woo order context for support drafts."""

from __future__ import annotations

from typing import Any


def format_order_context_block(order: Any) -> str:
    """Safe fields only — never invent tracking numbers."""
    return (
        f"[Order {order.id}]\n"
        f"Status: {order.status}\n"
        f"Total: {order.total} {order.currency}"
    )


def resolve_order_context(
    order_id: str | None,
    *,
    use_mock: bool | None = None,
) -> str | None:
    """Fetch Woo order and return a compact context block, or None on miss/error."""
    if not order_id:
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock)
        order = woo.get_order(str(order_id))
        return format_order_context_block(order)
    except Exception:
        return None


def draft_has_order_block(draft: str | None, order_id: str | None = None) -> bool:
    """True when draft already includes an order context prepend block."""
    text = (draft or "").strip()
    if not text:
        return False
    if order_id and f"[Order {order_id}]" in text:
        return True
    return text.startswith("[Order ")
