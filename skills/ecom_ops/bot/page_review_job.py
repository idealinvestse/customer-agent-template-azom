"""Telegram background job for a homepage source review.

This path does not call the order chat and does not treat a steer as an order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ecom_ops.bot.page_review_runtime import run_page_review_turn
from ecom_ops.bot.progress_events import ProgressCallback, ProgressEvent, emit_progress
from ecom_ops.bot.reply import BotReply
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop


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
    raw_text = str((job.get("payload") or {}).get("text") or "")

    turn = run_page_review_turn(
        text=raw_text,
        actor=actor,
        channel=channel,
        conversation_id=f"{channel}:{chat_id}",
        wp_ops_context=context if isinstance(context, dict) else None,
        loop_fn=run_bounded_page_review_loop,
        on_progress=progress,
        on_checkpoint=on_checkpoint,
        require_site_id=True,
    )
    if turn.context_bound_from_text and turn.wp_ops_context is not None:
        update_session = getattr(handler, "_update_session_atomic", None)
        if callable(update_session):
            update_session(chat_id, session_updates={"wp_ops_context": turn.wp_ops_context})
    return BotReply(text=turn.text, attachments=turn.attachments)
