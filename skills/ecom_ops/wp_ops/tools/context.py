"""Persisted site-context validation and TTL matching for WordPress agent tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ecom_ops.wp_ops.tools.types import SITE_ID_RE, WP_TOOL_MAX_AGE_SECONDS
from ecom_ops.wp_ops.tools.validation import is_expired, safe_operation_id


def context_for_storage(
    *,
    actor: str,
    channel: str,
    conversation_id: str,
    site_id: str | None,
    operation_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    if not site_id or not SITE_ID_RE.fullmatch(site_id):
        return None
    return {
        "actor_id": actor,
        "channel": channel,
        "conversation_id": conversation_id[:120],
        "site_id": site_id,
        "operation_ids": [value for value in operation_ids[:20] if safe_operation_id(value)],
        "updated_at": datetime.now(UTC).isoformat(),
    }


def context_matches(
    context: Any, *, actor: str, channel: str, conversation_id: str
) -> bool:
    """Check ownership and TTL before a persisted site context is reused."""
    if not isinstance(context, dict):
        return False
    return bool(
        context.get("actor_id") == actor
        and context.get("channel") == channel
        and context.get("conversation_id") == conversation_id[:120]
        and not is_expired(context.get("updated_at"), age_seconds=WP_TOOL_MAX_AGE_SECONDS)
        and isinstance(context.get("site_id"), str)
        and SITE_ID_RE.fullmatch(context["site_id"])
    )


def valid_context(
    context: dict[str, Any] | None, *, actor: str, channel: str, conversation_id: str
) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    if (
        context.get("actor_id") != actor
        or context.get("channel") != channel
        or context.get("conversation_id") != conversation_id[:120]
        or is_expired(context.get("updated_at"), age_seconds=WP_TOOL_MAX_AGE_SECONDS)
    ):
        return {}
    site_id = context.get("site_id")
    if not isinstance(site_id, str) or not SITE_ID_RE.fullmatch(site_id):
        return {}
    raw_ids = context.get("operation_ids")
    ids = [value for value in raw_ids[:20] if safe_operation_id(value)] if isinstance(raw_ids, list) else []
    return {"site_id": site_id, "operation_ids": ids}


_valid_context = valid_context
_expired = is_expired

__all__ = [
    "_expired",
    "_valid_context",
    "context_for_storage",
    "context_matches",
    "is_expired",
    "valid_context",
]
