"""Bind a homepage review client and shared review turn runner outside ``wp_ops``."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ecom_ops.bot.reply import safe_workspace_attachments
from ecom_ops.integrations.wp_component_reads import wp_client_for_site
from ecom_ops.rbac import Permission, resolve_actor
from ecom_ops.wp_ops.agent_tools import (
    component_rest_source,
    context_for_storage,
    context_matches,
)
from ecom_ops.wp_ops.file_changes import rest_reads_allowed
from ecom_ops.wp_ops.page_review_intent import extract_page_review_target
from ecom_ops.wp_ops.registry import load_sites, resolve_site_id_from_text, site_by_id

_UNBOUND_SITE_TEXT = (
    "Välj en WordPress-sajt med /wp use. Ingen sida och ingen order hämtades."
)


@dataclass(frozen=True)
class PageReviewTurnResult:
    text: str
    attachments: tuple[str, ...]
    site_id: str
    wp_ops_context: dict[str, Any] | None
    context_bound_from_text: bool = False
    outcome: Any = None


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == name:
            return True
    return False


def page_review_client(site_id: str, actor: str) -> Any | None:
    """Create a read client only for an authorized, registered live site.

    The caller supplies a site id selected by the conversation context. No URL,
    host, credentials, or arbitrary transport comes from model arguments.
    """
    try:
        person = resolve_actor(actor)
        if not person.has(Permission.WP_OPS_READ):
            return None
        site = site_by_id(load_sites(), str(site_id or ""))
        if site is None or os.environ.get("AZOM_USE_MOCK", "1").strip() == "1":
            return None
        if not rest_reads_allowed():
            return None
        client = wp_client_for_site(site, fallback_to_env=False)
        if client is not None:
            return client
        if component_rest_source(site.canonical_url) != "live":
            return None
        from urllib.parse import urlparse

        from ecom_ops.integrations.wordpress import wp_client_from_env

        host = (urlparse(str(site.canonical_url or "")).hostname or "").lower()
        tail = host.rsplit(".", 1)[-1] if "." in host else ""
        dom = tail if tail in {"no", "se", "dk", "fi"} else None
        return wp_client_from_env(base_url=site.canonical_url, use_mock=False, domain=dom)
    except Exception:
        return None


def run_page_review_turn(
    *,
    text: str,
    actor: str,
    channel: str,
    conversation_id: str,
    wp_ops_context: dict[str, Any] | None,
    loop_fn: Callable[..., Any],
    on_progress: Any = None,
    on_checkpoint: Callable[[], list[str]] | None = None,
    require_site_id: bool = False,
) -> PageReviewTurnResult:
    """Resolve site context and run a bounded page source review via ``loop_fn``.

    Accepts ``loop_fn`` explicitly so callers preserve module-level monkeypatch
    contracts on both ``ecom_ops.wp_ops.page_review_loop`` and
    ``ecom_ops.bot.page_review_job``.
    """
    bound = wp_ops_context if isinstance(wp_ops_context, dict) else None
    site_id = ""
    context_bound_from_text = False
    if context_matches(
        bound,
        actor=actor,
        channel=channel,
        conversation_id=conversation_id,
    ):
        site_id = str(bound.get("site_id") or "")
    if not site_id and (text or "").strip():
        candidate = resolve_site_id_from_text(text)
        if candidate:
            try:
                allowed_read = resolve_actor(actor).has(Permission.WP_OPS_READ)
            except Exception:
                allowed_read = False
            if allowed_read:
                site_id = candidate
                bound = context_for_storage(
                    actor=actor,
                    channel=channel,
                    conversation_id=conversation_id,
                    site_id=site_id,
                    operation_ids=(),
                )
                context_bound_from_text = bound is not None
    if require_site_id and not site_id:
        return PageReviewTurnResult(
            text=_UNBOUND_SITE_TEXT,
            attachments=(),
            site_id="",
            wp_ops_context=None,
            context_bound_from_text=False,
        )

    kwargs: dict[str, Any] = {
        "site_id": site_id,
        "actor": actor,
        "client": page_review_client(site_id, actor) if site_id else None,
        "on_progress": on_progress,
        "target_page": extract_page_review_target(text or ""),
    }
    if on_checkpoint is not None and _accepts_kwarg(loop_fn, "on_checkpoint"):
        kwargs["on_checkpoint"] = on_checkpoint
    outcome = loop_fn(**kwargs)
    return PageReviewTurnResult(
        text=outcome.text,
        attachments=safe_workspace_attachments(getattr(outcome, "attachments", ())),
        site_id=site_id,
        wp_ops_context=bound if site_id else None,
        context_bound_from_text=context_bound_from_text,
        outcome=outcome,
    )
