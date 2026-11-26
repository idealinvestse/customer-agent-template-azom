"""Actor- and channel-scoped WordPress session state helpers for bot handlers.

Centralizes ``wp_ops_context``, ``active_wp_review``, and ``wp_review_hashes``
validation, hash-pinning, and actor/channel-switch cleanup rails.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.agent_tools import context_matches

FILE_PLAN_ID_RE = re.compile(r"^fe-[a-f0-9]{12}$")
REVIEW_PLAN_ID_RE = re.compile(r"^wr-[a-f0-9]{12}$")
SITE_ID_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_REVIEW_HASH_CACHE = 24
ACTIVE_WP_REVIEW_TTL_SECONDS = 12 * 60 * 60


def _validate_image_paths(paths: Any, *, limit: int) -> list[str]:
    from ecom_ops.bot.chat_agent import _validate_chat_image_paths

    return _validate_chat_image_paths(paths, limit=limit)


def _normalize_page_draft_meta(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    draft_meta = {
        k: str(raw.get(k) or "").strip()[:80]
        for k in ("slug", "page_type", "preview_kind")
        if str(raw.get(k) or "").strip()
    }
    return draft_meta or None


def valid_active_wp_review(
    review: Any,
    *,
    actor: str,
    channel: str,
    conversation_id: str,
) -> dict[str, Any] | None:
    """Validate ownership, site_id, and TTL of persisted ``session['active_wp_review']``."""
    if not isinstance(review, dict):
        return None
    if (
        review.get("actor_id") != actor
        or review.get("channel") != channel
        or review.get("conversation_id") != conversation_id[:120]
    ):
        return None
    site_id = str(review.get("site_id") or "").strip()
    if not site_id or not SITE_ID_TOKEN_RE.fullmatch(site_id):
        return None
    updated_raw = review.get("updated_at")
    if not isinstance(updated_raw, str) or not updated_raw.strip():
        return None
    try:
        updated_at = datetime.fromisoformat(updated_raw)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if (datetime.now(UTC) - updated_at).total_seconds() > ACTIVE_WP_REVIEW_TTL_SECONDS:
            return None
    except ValueError:
        return None
    plan_ids = [
        str(pid).strip().lower()
        for pid in (review.get("plan_ids") or ())
        if isinstance(pid, str) and REVIEW_PLAN_ID_RE.fullmatch(pid.strip().lower())
    ]
    latest = str(review.get("latest_plan_id") or "").strip().lower()
    if latest and not REVIEW_PLAN_ID_RE.fullmatch(latest):
        latest = ""
    if not latest and plan_ids:
        latest = plan_ids[-1]
    if latest and latest not in plan_ids:
        plan_ids.append(latest)
    preview_path = str(review.get("preview_path") or "").strip()
    product_id = str(review.get("product_id") or "").strip()
    page_draft = _normalize_page_draft_meta(review.get("page_draft"))
    if not (latest or preview_path or product_id or page_draft):
        return None
    out: dict[str, Any] = {
        "actor_id": actor,
        "channel": channel,
        "conversation_id": conversation_id[:120],
        "site_id": site_id,
        "plan_ids": plan_ids[-6:],
        "latest_plan_id": latest,
        "variant_group": str(review.get("variant_group") or "").strip()[:80],
        "product_id": product_id[:24],
        "preview_path": preview_path[:300],
        "focus_selector": str(review.get("focus_selector") or "").strip()[:160],
        "theme_slug": str(review.get("theme_slug") or "").strip()[:80],
        "changed_files": [
            str(p).strip()[:160]
            for p in (review.get("changed_files") or ())
            if isinstance(p, str) and str(p).strip()
        ][:12],
        "last_image_paths": _validate_image_paths(review.get("last_image_paths"), limit=2),
        "preview_images": _validate_image_paths(review.get("preview_images"), limit=4),
        "updated_at": updated_raw,
    }
    if page_draft is not None:
        out["page_draft"] = page_draft
    return out


def merge_active_wp_review(
    prior: dict[str, Any] | None,
    *,
    actor: str,
    channel: str,
    conversation_id: str,
    site_id: str | None = None,
    meta: dict[str, Any] | None = None,
    review_plan_id: str | None = None,
    inbound_images: list[str] | tuple[str, ...] = (),
    preview_attachments: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Merge new WP loop / review metadata into ``active_wp_review``."""
    base = dict(prior) if isinstance(prior, dict) else {}
    m = meta if isinstance(meta, dict) else {}
    resolved_site = str(m.get("site_id") or site_id or base.get("site_id") or "").strip()
    if not resolved_site or not SITE_ID_TOKEN_RE.fullmatch(resolved_site):
        return prior

    plan_ids: list[str] = []
    for cand in (
        *(base.get("plan_ids") or ()),
        *(m.get("plan_ids") or ()),
        m.get("latest_plan_id"),
        review_plan_id,
    ):
        if isinstance(cand, str):
            norm = cand.strip().lower()
            if REVIEW_PLAN_ID_RE.fullmatch(norm) and norm not in plan_ids:
                plan_ids.append(norm)
    latest = str(
        m.get("latest_plan_id") or review_plan_id or base.get("latest_plan_id") or ""
    ).strip().lower()
    if not REVIEW_PLAN_ID_RE.fullmatch(latest):
        latest = plan_ids[-1] if plan_ids else ""

    preview_path = str(m.get("preview_path") or base.get("preview_path") or "").strip()[:300]
    product_id = str(m.get("product_id") or base.get("product_id") or "").strip()[:24]
    page_draft = _normalize_page_draft_meta(
        m.get("page_draft") if isinstance(m.get("page_draft"), dict) else base.get("page_draft")
    )
    if not (latest or preview_path or product_id or page_draft):
        return prior

    variant_group = str(m.get("variant_group") or base.get("variant_group") or "").strip()[:80]
    focus_selector = str(m.get("focus_selector") or base.get("focus_selector") or "").strip()[:160]
    theme_slug = str(m.get("theme_slug") or base.get("theme_slug") or "").strip()[:80]
    raw_changed = m.get("changed_files") or base.get("changed_files") or ()
    changed_files = [
        str(p).strip()[:160] for p in raw_changed if isinstance(p, str) and str(p).strip()
    ][:12]
    last_images = (
        _validate_image_paths(inbound_images, limit=2)
        if inbound_images
        else _validate_image_paths(base.get("last_image_paths"), limit=2)
    )
    raw_preview_imgs = m.get("preview_images") or preview_attachments or base.get("preview_images")
    preview_images = _validate_image_paths(raw_preview_imgs, limit=4)

    out: dict[str, Any] = {
        "actor_id": actor,
        "channel": channel,
        "conversation_id": conversation_id[:120],
        "site_id": resolved_site,
        "plan_ids": plan_ids[-6:],
        "latest_plan_id": latest,
        "variant_group": variant_group,
        "product_id": product_id,
        "preview_path": preview_path,
        "focus_selector": focus_selector,
        "theme_slug": theme_slug,
        "changed_files": changed_files,
        "last_image_paths": last_images,
        "preview_images": preview_images,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if page_draft is not None:
        out["page_draft"] = page_draft
    return out


def pick_followup_images(review: dict[str, Any] | None) -> list[str]:
    """Select up to 2 valid PNGs (latest focus/change preview + original screenshot)."""
    if not isinstance(review, dict):
        return []
    preview_pool = _validate_image_paths(review.get("preview_images"), limit=4)
    chosen_preview = ""
    for suffix in ("focus-desktop.png", "change-desktop.png", "preview-desktop.png"):
        hit = next((p for p in preview_pool if Path(p).name == suffix), "")
        if hit:
            chosen_preview = hit
            break
    if not chosen_preview and preview_pool:
        chosen_preview = preview_pool[0]
    inbound_pool = _validate_image_paths(review.get("last_image_paths"), limit=2)
    chosen_inbound = inbound_pool[-1] if inbound_pool else ""
    out: list[str] = []
    for candidate in (chosen_preview, chosen_inbound):
        if candidate and candidate not in out:
            out.append(candidate)
    return out[:2]


def with_review_hashes(
    session: dict[str, Any],
    *,
    actor: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    site_id = str(plan.get("site_id") or "")
    base_sha = str(plan.get("base_sha256") or "")
    work_sha = str(plan.get("work_sha256") or "")
    if not (REVIEW_PLAN_ID_RE.fullmatch(plan_id) and site_id and base_sha and work_sha):
        return session
    existing = dict(session.get("wp_review_hashes") or {})
    existing.pop(plan_id, None)
    existing[plan_id] = {
        "actor": actor,
        "site_id": site_id,
        "base_sha256": base_sha,
        "work_sha256": work_sha,
    }
    while len(existing) > MAX_REVIEW_HASH_CACHE:
        oldest = next(iter(existing))
        existing.pop(oldest, None)
    session["wp_review_hashes"] = existing
    return session


def drop_review_hash(session: dict[str, Any], plan_id: str) -> bool:
    cached = session.get("wp_review_hashes")
    if not isinstance(cached, dict) or plan_id not in cached:
        return False
    updated = dict(cached)
    updated.pop(plan_id, None)
    if updated:
        session["wp_review_hashes"] = updated
    else:
        session.pop("wp_review_hashes", None)
    return True


def prune_review_hashes(
    session: dict[str, Any],
    *,
    actor: str,
) -> tuple[dict[str, Any], bool]:
    """Drop ``wp_review_hashes`` entries belonging to another actor; return ``(kept, changed)``."""
    hashes = session.get("wp_review_hashes")
    if not isinstance(hashes, dict):
        return {}, False
    kept = {
        pid: row
        for pid, row in hashes.items()
        if isinstance(row, dict) and row.get("actor") == actor
    }
    if len(kept) == len(hashes):
        return kept, False
    if kept:
        session["wp_review_hashes"] = kept
    else:
        session.pop("wp_review_hashes", None)
    return kept, True


def _is_valid_pin(row: Any, *, actor: str, site_id: str) -> bool:
    return (
        isinstance(row, dict)
        and row.get("actor") == actor
        and row.get("site_id") == site_id
        and bool(str(row.get("base_sha256") or "").strip())
        and bool(str(row.get("work_sha256") or "").strip())
    )


def resolve_pinned_hashes(
    session: dict[str, Any],
    proposal_store: Any,
    chat_id: str | int,
    *,
    actor: str,
    site_id: str,
    plan_id: str,
) -> tuple[str, str] | None:
    """Resolve actor- and site-matched ``(base_sha256, work_sha256)`` from session or proposal store."""
    cached = (session.get("wp_review_hashes") or {}).get(plan_id)
    if _is_valid_pin(cached, actor=actor, site_id=site_id):
        return str(cached["base_sha256"]), str(cached["work_sha256"])
    if proposal_store is not None and hasattr(proposal_store, "get_pinned_review_hashes"):
        db_pin = proposal_store.get_pinned_review_hashes(chat_id, plan_id)
        if _is_valid_pin(db_pin, actor=actor, site_id=site_id):
            return str(db_pin["base_sha256"]), str(db_pin["work_sha256"])
    return None


def prune_mismatched_wp_session(
    session: dict[str, Any],
    *,
    actor: str,
    channel: str,
    conversation_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Prune mismatched ``wp_ops_context``, ``active_wp_review``, and ``wp_review_hashes`` in-place.

    Returns ``(session_updates, pop_session_keys)`` ready for atomic store persistence.
    """
    updates: dict[str, Any] = {}
    pop_keys: list[str] = []

    context = session.get("wp_ops_context")
    if context is not None and not context_matches(
        context,
        actor=actor,
        channel=channel,
        conversation_id=conversation_id,
    ):
        session.pop("wp_ops_context", None)
        pop_keys.append("wp_ops_context")

    kept, hashes_changed = prune_review_hashes(session, actor=actor)
    if hashes_changed:
        if kept:
            updates["wp_review_hashes"] = kept
        else:
            pop_keys.append("wp_review_hashes")

    active_review = session.get("active_wp_review")
    if (
        active_review is not None
        and valid_active_wp_review(
            active_review,
            actor=actor,
            channel=channel,
            conversation_id=conversation_id,
        )
        is None
    ):
        session.pop("active_wp_review", None)
        pop_keys.append("active_wp_review")

    return updates, pop_keys


__all__ = [
    "ACTIVE_WP_REVIEW_TTL_SECONDS",
    "FILE_PLAN_ID_RE",
    "MAX_REVIEW_HASH_CACHE",
    "REVIEW_PLAN_ID_RE",
    "SITE_ID_TOKEN_RE",
    "drop_review_hash",
    "merge_active_wp_review",
    "pick_followup_images",
    "prune_mismatched_wp_session",
    "prune_review_hashes",
    "resolve_pinned_hashes",
    "valid_active_wp_review",
    "with_review_hashes",
]
