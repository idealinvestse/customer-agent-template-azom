"""RBAC helpers for FAQ ingest (Oscar live; agent mock-only)."""

from __future__ import annotations

from ecom_ops.rbac import Actor, Permission, require_permission, resolve_actor


def resolve_ingest_actor(actor: Actor | str | None) -> Actor:
    return actor if isinstance(actor, Actor) else resolve_actor(actor)


def require_faq_ingest(
    actor: Actor | str | None,
    *,
    use_mock: bool = False,
) -> Actor:
    """Require FAQ_PUBLISH, or allow agent/operator when use_mock."""
    actor_obj = resolve_ingest_actor(actor)
    if use_mock and actor_obj.role in {"operator", "full_admin"}:
        return actor_obj
    require_permission(actor_obj, Permission.FAQ_PUBLISH)
    return actor_obj


def require_faq_promote(actor: Actor | str | None) -> Actor:
    """Promote to config/faq always needs FAQ_PUBLISH (Oscar)."""
    actor_obj = resolve_ingest_actor(actor)
    require_permission(actor_obj, Permission.FAQ_PUBLISH)
    return actor_obj
