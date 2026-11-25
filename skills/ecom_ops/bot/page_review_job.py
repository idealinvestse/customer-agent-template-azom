"""Telegram background job for a homepage source review.

This path does not call the order chat and does not treat a steer as an order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ecom_ops.bot.progress_events import ProgressCallback, ProgressEvent, emit_progress
from ecom_ops.bot.reply import BotReply, safe_workspace_attachments
from ecom_ops.rbac import Permission, resolve_actor
from ecom_ops.wp_ops.agent_tools import context_for_storage, context_matches
from ecom_ops.wp_ops.page_review_intent import extract_page_review_target
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.registry import resolve_site_id_from_text


def run_page_review_job(
    handler: Any,
    job: dict[str, Any],
    *,
    progress: ProgressCallback,
    on_checkpoint: Callable[[], list[str]] | None = None,
) -> BotReply:
    chat_id = str(job.get("chat_id") or "")
    if not handler._peer_allowed(chat_id):
        return BotReply(text="Chatten saknar behörighet. Ingen sida hämtades.")
    try:
        actor = str(handler._resolve_actor(chat_id))
    except Exception:
        return BotReply(text="Actor-behörigheten kunde inte verifieras. Ingen sida hämtades.")
    merge = getattr(handler, "_merge_state", None)
    if callable(merge):
        merge(chat_id, tool_digest="")
    if on_checkpoint:
        steers = on_checkpoint() or []
        if steers:
            emit_progress(
                progress,
                ProgressEvent(
                    step_id="page-review-steer-0",
                    tool="page_review",
                    label="Följdfråga mottagen",
                    phase="steer",
                    outcome="Följdfrågan stannar i sidgranskningen. Orderverktyg kördes inte.",
                    legacy_text="Följdfrågan stannar i sidgranskningen. Orderverktyg kördes inte.",
                ),
            )
    state = handler.store.get(chat_id) or {}
    session = state.get("session") if isinstance(state, dict) else None
    context = session.get("wp_ops_context") if isinstance(session, dict) else None
    channel = str(getattr(handler, "channel", "") or "")
    site_id = ""
    if context_matches(
        context,
        actor=actor,
        channel=channel,
        conversation_id=f"{channel}:{chat_id}",
    ):
        site_id = str(context.get("site_id") or "")
    raw_text = str((job.get("payload") or {}).get("text") or "")
    if not site_id and raw_text.strip():
        candidate = resolve_site_id_from_text(raw_text)
        if candidate:
            try:
                allowed_read = resolve_actor(actor).has(Permission.WP_OPS_READ)
            except Exception:
                allowed_read = False
            if allowed_read:
                site_id = candidate
                update_session = getattr(handler, "_update_session_atomic", None)
                if callable(update_session):
                    bound_ctx = context_for_storage(
                        actor=actor,
                        channel=channel,
                        conversation_id=f"{channel}:{chat_id}",
                        site_id=site_id,
                        operation_ids=(),
                    )
                    if bound_ctx is not None:
                        update_session(chat_id, session_updates={"wp_ops_context": bound_ctx})
    if not site_id:
        return BotReply(
            text="Välj en WordPress-sajt med /wp use. Ingen sida och ingen order hämtades."
        )
    from ecom_ops.bot.page_review_runtime import page_review_client

    target_page = extract_page_review_target(raw_text)
    outcome = run_bounded_page_review_loop(
        site_id=site_id,
        actor=actor,
        client=page_review_client(site_id, actor),
        on_progress=progress,
        on_checkpoint=on_checkpoint,
        target_page=target_page,
    )
    return BotReply(
        text=outcome.text,
        attachments=safe_workspace_attachments(getattr(outcome, "attachments", ())),
    )
