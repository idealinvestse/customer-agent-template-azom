"""Telegram message handlers — OpenClaw-style multi-turn + write rails."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ecom_ops.bot.actors import (
    ChannelActorDenied,
    TelegramActorDenied,
    channel_peer_allowed,
    messenger_mutations_allowed,
    resolve_channel_actor,
)
from ecom_ops.bot.catalog import execute_page_update, execute_product_update
from ecom_ops.bot.change_clarifier import (
    CLARIFY_TTL_SECONDS,
    MAX_CLARIFY_ROUNDS,
    ClarificationProposal,
    build_clarified_request_message,
    detect_change_ambiguity,
    render_clarification_message,
)
from ecom_ops.bot.chat_agent import (
    CAPABILITIES_RE,
    CASES_INTENT_RE,
    FALLBACK_BUDGET,
    FALLBACK_NO_KEY,
    MARKETING_INTENT_RE,
    OPS_INTENT_RE,
    RECENT_ORDERS_RE,
    SOFT_ESCALATE_NUDGE,
    ChatResult,
    _accepts_kwarg,
    _inspect_tool_outcome,
    _provenance,
    run_chat,
    tool_lookup_order,
    wants_escalate,
    wants_hard_escalate_confirm,
)
from ecom_ops.bot.dashboard_links import review_plan_url
from ecom_ops.bot.dialog_actions import (
    PendingAction,
    execute_case_regenerate,
    execute_order_status,
    execute_product_desc,
)
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.progress_events import (
    ProgressCallback,
    ProgressEvent,
    emit_progress,
)
from ecom_ops.bot.recovery import (
    FOOTER_UNKNOWN_CALLBACK,
    deny_actor_reply,
    deny_allowlist_reply,
    with_recovery,
)
from ecom_ops.bot.reply import (
    ActionButton,
    ActionMarkup,
    BotReply,
    approve_case_actions,
    as_reply,
    bot_reply,
    clarification_actions,
    order_status_confirm_actions,
    product_desc_confirm_actions,
    proposal_confirm_actions,
    safe_workspace_attachments,
    triage_cases_actions,
    yes_no_actions,
)
from ecom_ops.bot.site_content import (
    execute_plugin_status,
    execute_post_update,
    execute_site_update,
)
from ecom_ops.bot.store import ConversationStore, clamp_messages
from ecom_ops.bot.telegram_oauth import (
    handle_ephemeral_oauth_callback,
    handle_oauth_callback_query,
    handle_oauth_command,
    is_ephemeral_oauth_message,
    match_natural_oauth_intent,
)
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.model_router.base import MAX_MULTIMODAL_IMAGE_BYTES, detect_image_format
from ecom_ops.rbac import Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import validate_order_id
from ecom_ops.wp_ops.agent_tools import WpAgentTools, context_for_storage, context_matches

ORDER_FAST_RE = re.compile(r"^\s*(?:order|ordernr|#)\s*(\d{4,12})\s*$", re.I)
_FILE_PLAN_ID = re.compile(r"fe-[a-f0-9]{12}")
_REVIEW_PLAN_ID = re.compile(r"wr-[a-f0-9]{12}")
_MAX_REVIEW_HASH_CACHE = 8
_MAX_CLARIFY_SESSION_CACHE = 6
_PROPOSAL_YES_RE = re.compile(r"^proposal:yes:(pr-[0-9a-f]{12})$", re.I)
_PROPOSAL_NO_RE = re.compile(r"^proposal:no:(pr-[0-9a-f]{12})$", re.I)
_CLARIFY_ID_RE = re.compile(r"^cl-[0-9a-f]{12}$", re.I)
_CLARIFY_PICK_RE = re.compile(
    r"^clarify:pick:(cl-[0-9a-f]{12}):([a-z0-9_-]{1,12})$", re.I
)
_CLARIFY_GO_RE = re.compile(r"^clarify:go:(cl-[0-9a-f]{12})$", re.I)
_CLARIFY_OTHER_RE = re.compile(r"^clarify:other:(cl-[0-9a-f]{12})$", re.I)
_CLARIFY_CANCEL_RE = re.compile(r"^clarify:cancel:(cl-[0-9a-f]{12})$", re.I)
_CLARIFY_CANCEL_WORDS = frozenset(
    {"avbryt", "cancel", "nej", "no", "n", "skippa", "glöm det", "stopp"}
)
_CLARIFY_CONFIRM_WORDS = frozenset(
    {
        "ja",
        "yes",
        "y",
        "j",
        "stämmer",
        "stammer",
        "ok",
        "okej",
        "kör",
        "kor",
        "bekräfta",
        "bekrafta",
    }
)
_OFFLINE_READ_OPTION_PROMPTS: dict[str, str] = {
    "inspect": "visa sajten",
    "read_first": "lista teman och plugins",
    "list_pages": "lista sidor",
    "find_prod": "lista produkter",
    "show_site": "visa sajten",
    "show_order": "visa senaste ordrar",
    "list_orders": "lista senaste ordrar",
}


def _is_unrelated_lookup(text: str) -> bool:
    """Return True if ``text`` is a standalone read/lookup unrelated to clarification."""
    raw = (text or "").strip()
    if not raw:
        return False
    if ORDER_FAST_RE.match(raw) or re.fullmatch(r"#?\d{4,12}", raw):
        return True
    assess = detect_change_ambiguity(raw)
    if assess.is_change_request:
        return False
    return bool(
        CASES_INTENT_RE.search(raw)
        or MARKETING_INTENT_RE.search(raw)
        or RECENT_ORDERS_RE.search(raw)
        or OPS_INTENT_RE.search(raw)
        or CAPABILITIES_RE.search(raw)
        or re.match(
            r"^\s*(?:visa|lista|kolla|h[äa]mta|vad\s+[äa]r|hur\s+m[åa]nga|hur\s+g[åa]r|granska)\b",
            raw,
            re.I,
        )
    )


_PHOTO_WAIT_REPLY = (
    "Bild mottagen! Skriv vad du vill ändra på det som är inringat så fixar jag det i sandlådan."
)
_MAX_TURN_IMAGES = 4


def _normalize_inbound_image_paths(
    paths: Iterable[Any] | None,
    *,
    limit: int = _MAX_TURN_IMAGES,
) -> list[str]:
    """Keep unique, existing, non-symlink PNG/JPEG/WEBP files within size limit."""
    out: list[str] = []
    seen: set[str] = set()
    for item in paths or ():
        if not isinstance(item, (str, Path)):
            continue
        raw = str(item).strip()
        if not raw or "\x00" in raw:
            continue
        path = Path(raw)
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            if size <= 0 or size > MAX_MULTIMODAL_IMAGE_BYTES:
                continue
            head = path.read_bytes()[:16]
        except OSError:
            continue
        if detect_image_format(head) is None:
            continue
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    if len(out) > limit:
        out = out[-limit:]
    return out


_PENDING_PROGRESS_LABELS: dict[str, tuple[str, str, str]] = {
    "order_status": ("Orderstatus", "Uppdaterar orderstatus i Woo", "Orderstatus uppdaterad"),
    "product_desc": ("Produktbeskrivning", "Genererar produktbeskrivning", "Produktbeskrivning klar"),
    "product_update": ("Produktuppdatering", "Uppdaterar produkt i Woo", "Produkt uppdaterad"),
    "page_update": ("Siduppdatering", "Uppdaterar sida i WordPress", "Sida uppdaterad"),
    "post_update": ("Inläggsuppdatering", "Uppdaterar inlägg i WordPress", "Inlägg uppdaterat"),
    "site_update": ("Sajtinställningar", "Uppdaterar sajtinställningar", "Sajtinställningar uppdaterade"),
    "plugin_status": ("WordPress-plugin", "Ändrar plugin-status", "Plugin-status uppdaterad"),
    "case_regenerate": ("Ärendeutkast", "Skapar nytt svarsutkast", "Nytt utkast sparat"),
}


def _wpreview_stage_actions(
    plan_id: str, *, review_url: str | None = None
) -> ActionMarkup:
    rows: list[list[ActionButton]] = [
        [
            ActionButton(
                label="Stagea ändringar till staging",
                payload=f"wpreview:stage:{plan_id}",
            ),
            ActionButton(label="Avbryt", payload=f"wpreview:no:{plan_id}"),
        ]
    ]
    if review_url:
        rows.append([ActionButton(label="Öppna granskning", url=review_url)])
    return ActionMarkup(rows=rows)


def _review_plan_ready(
    plans: Any,
    plan: dict[str, Any],
    *,
    changed_count: int | None = None,
) -> tuple[bool, str]:
    status = str(plan.get("status") or "")
    if status == "staged":
        return False, "redan i staging"
    if status in {
        "expired",
        "rejected",
        "denied",
        "fork_not_approved",
        "not_resumable",
    }:
        return False, status
    if plan.get("superseded_by"):
        return False, "ersatt av nyare plan"
    if plan.get("risk") == "high":
        return False, "hög risk blockerad"
    if plan.get("environment") != "staging":
        return False, "endast staging kan stageas"
    if status != "tests_passed":
        return False, f"kontroller ej godkända ({status})"
    evidence = plans._require_current_evidence(plan)
    if evidence is not None:
        reason = str((evidence.data or {}).get("error_class") or "EvidenceMissing")
        return False, f"bevis saknas ({reason})"
    if changed_count is not None and changed_count == 0:
        return False, "inga ändringar"
    return True, "redo för staging"


def _format_changed_files(
    changed_files: list[dict[str, Any]], *, limit: int = 15
) -> list[str]:
    lines: list[str] = []
    for item in changed_files[:limit]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        op = str(item.get("status") or item.get("op") or "changed").strip()
        lines.append(f"- [{op}] {path}")
    extra = len(changed_files) - limit
    if extra > 0:
        lines.append(f"- ... och {extra} fler")
    return lines


def _format_check_rows(checks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in checks[:12]:
        if not isinstance(row, dict):
            continue
        job = str(row.get("job") or "").strip()
        status = str(row.get("status") or "unknown").strip()
        if not job:
            continue
        reason = str(row.get("reason") or "").strip()
        suffix = f" ({reason})" if reason and status != "passed" else ""
        lines.append(f"- {job}: {status}{suffix}")
    return lines


def _with_review_hashes(
    session: dict[str, Any], *, actor: str, plan: dict[str, Any]
) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    site_id = str(plan.get("site_id") or "")
    base_sha = str(plan.get("base_sha256") or "")
    work_sha = str(plan.get("work_sha256") or "")
    if not (_REVIEW_PLAN_ID.fullmatch(plan_id) and site_id and base_sha and work_sha):
        return session
    existing = dict(session.get("wp_review_hashes") or {})
    existing.pop(plan_id, None)
    existing[plan_id] = {
        "actor": actor,
        "site_id": site_id,
        "base_sha256": base_sha,
        "work_sha256": work_sha,
    }
    while len(existing) > _MAX_REVIEW_HASH_CACHE:
        oldest = next(iter(existing))
        existing.pop(oldest, None)
    session["wp_review_hashes"] = existing
    return session


def _drop_review_hash(session: dict[str, Any], plan_id: str) -> bool:
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


def _prune_review_hashes(
    session: dict[str, Any], *, actor: str
) -> tuple[dict[str, Any], bool]:
    """Drop wp_review_hashes entries belonging to another actor; return (kept, changed)."""
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


def _slash_command_meta(raw: str) -> tuple[str, str, str, str] | None:
    """Return (step_id, label, detail, tool_name) for operational slash commands."""
    text = (raw or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if cmd == "/health":
        return ("cmd:health", "Systemhälsa", "Kontrollerar systemstatus", "health")
    if cmd == "/brief":
        return ("cmd:brief", "Daglig sammanfattning", "Sammanställer lägesbild", "brief")
    if cmd == "/marketing":
        return ("cmd:marketing", "Marknadsföring", "Hämtar marknadsföringsdata", "marketing")
    if cmd == "/cases":
        if sub in {"approve", "send"}:
            return ("cmd:cases_approve", "Ärende", "Godkänner utkast", "cases_approve")
        if sub == "close":
            return ("cmd:cases_close", "Ärende", "Stänger ärende", "cases_close")
        if sub == "show":
            return ("cmd:cases_show", "Ärende", "Hämtar ärendedetaljer", "case_lookup")
        return ("cmd:cases", "Ärenden", "Hämtar ärenden", "case_list")
    return None


def _reply_text(value: Any) -> str:
    if isinstance(value, BotReply):
        return value.text
    return str(value or "")


def _stored_tool_digest(result: ChatResult, prior_digest: str) -> str:
    """Keep an explicit empty digest when a page review clears the order summary."""
    if result.clear_tool_digest:
        return ""
    return result.tool_digest or prior_digest


def telegram_chat_allowed(chat_id: str | int) -> bool:
    return channel_peer_allowed("telegram", chat_id)


class BotHandler:
    """OpenClaw-compatible commands + hybrid chat + site write confirms."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        escalation: EscalationService | None = None,
        *,
        channel: str = "telegram",
        proposal_store: Any | None = None,
    ) -> None:
        self.store = store or ConversationStore()
        self.escalation = escalation or default_escalation
        self.channel = (channel or "telegram").strip().lower()
        self.proposal_store = proposal_store

    def _peer_allowed(self, peer_id: str | int) -> bool:
        return channel_peer_allowed(self.channel, peer_id)

    def _resolve_actor(self, peer_id: str | int) -> str:
        return resolve_channel_actor(self.channel, peer_id)

    def _deny_allowlist(self) -> BotReply:
        return deny_allowlist_reply(channel=self.channel)

    def _deny_actor(self) -> BotReply:
        return deny_actor_reply(channel=self.channel)

    def _mutations_blocked_reply(self) -> BotReply | None:
        """Block Messenger writes when page token missing outside mock."""
        if self.channel != "messenger":
            return None
        if messenger_mutations_allowed():
            return None
        return BotReply(
            text=(
                "Muterande Messenger-åtgärder är blockerade: "
                "MESSENGER_PAGE_ACCESS_TOKEN saknas (prod). "
                "Be Oscar sätta page token — läsning (/cases list|show, /order) fungerar."
            )
        )

    def _session_market(self, chat_id: str | int) -> str | None:
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        raw = session.get("last_market")
        return str(raw).strip() if raw else None

    def _actor_obj(self, chat_id: str | int) -> Actor:
        return resolve_actor(self._resolve_actor(chat_id))

    def _pending_allowed(self, actor: Actor, pending: PendingAction) -> bool:
        if pending.kind == "order_status":
            return actor.has(Permission.ORDER_STATUS_UPDATE)
        if pending.kind in {"product_desc", "product_update"}:
            return actor.has(Permission.PRODUCT_DESC_WRITE)
        if pending.kind == "page_update":
            return actor.has(Permission.WP_OPS_OPERATE)
        if pending.kind in {"post_update", "site_update"}:
            return actor.has(Permission.WP_OPS_OPERATE)
        if pending.kind == "plugin_status":
            return actor.has(Permission.WP_OPS_ADMIN)
        if pending.kind == "case_regenerate":
            return actor.has(Permission.CASE_REPLY)
        return True

    def _deny_pending_reply(self, pending: PendingAction) -> str:
        if pending.kind == "order_status":
            return (
                "Orderstatus-ändring kräver operator eller Oscar "
                "(Jonatan har CASE_REPLY men inte order write)."
            )
        if pending.kind in {"product_desc", "product_update"}:
            return (
                "Produktändring kräver operator eller Oscar "
                "(Jonatan har inte PRODUCT_DESC_WRITE)."
            )
        if pending.kind == "page_update":
            return (
                "Sidändring kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "post_update":
            return (
                "Inläggsändring kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "site_update":
            return (
                "Sajttitel och tagline kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "plugin_status":
            return "Plugin på eller av kräver Oscar (WP_OPS_ADMIN)."
        return "Du saknar behörighet för den här åtgärden."

    def _merge_state(self, chat_id: str | int, **updates: Any) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(updates.get("session", prev.get("session") or {}))
        if "messages" in updates:
            messages = clamp_messages(updates.get("messages"))
        else:
            messages = clamp_messages(prev.get("messages"))
        if "tool_digest" in updates:
            tool_digest = str(updates.get("tool_digest") or "")
        else:
            tool_digest = str(prev.get("tool_digest") or "")
        slots = updates.get("slots", prev.get("slots") or {})
        state = {
            "flow": updates.get("flow", prev.get("flow")),
            "step": updates.get("step", prev.get("step")),
            "slots": slots,
            "session": session,
            "messages": messages,
            "tool_digest": tool_digest,
        }
        if (
            not state.get("flow")
            and not state.get("session")
            and not state.get("messages")
            and not state.get("tool_digest")
            and not state.get("slots")
        ):
            self.store.clear(chat_id)
            return
        self.store.set(chat_id, state)

    def _update_session_atomic(
        self,
        chat_id: str | int,
        *,
        session_updates: dict[str, Any] | None = None,
        pop_session_keys: Iterable[str] = (),
    ) -> None:
        if hasattr(self.store, "append_turn_atomic"):
            self.store.append_turn_atomic(
                chat_id,
                session_updates=session_updates or None,
                pop_session_keys=pop_session_keys,
            )
            return
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        for key in pop_session_keys:
            session.pop(key, None)
        if session_updates:
            session.update(session_updates)
        self._merge_state(
            chat_id,
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )

    def _persist_review_hashes(
        self, chat_id: str | int, session: dict[str, Any]
    ) -> None:
        remaining = session.get("wp_review_hashes")
        if isinstance(remaining, dict) and remaining:
            self._update_session_atomic(
                chat_id,
                session_updates={"wp_review_hashes": dict(remaining)},
            )
        else:
            self._update_session_atomic(
                chat_id,
                pop_session_keys=("wp_review_hashes",),
            )

    def _lookup_order_with_progress(
        self,
        chat_id: str | int,
        oid: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        prov = _provenance()
        step_id = f"order:{oid}"
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="order_lookup",
                phase="started",
                label=f"Order #{oid}",
                detail="Hämtar från Woo",
                provenance=prov,
                legacy_text=f"Hämtar order #{oid} från Woo...",
            ),
        )
        t0 = time.monotonic()
        try:
            out = tool_lookup_order(oid, domain=self._session_market(chat_id))
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="order_lookup",
                    phase="failed",
                    label=f"Order #{oid}",
                    outcome="hämtning misslyckades",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"Order #{oid}: hämtning misslyckades.",
                ),
            )
            raise
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ok, outcome = _inspect_tool_outcome("order_lookup", out)
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="order_lookup",
                phase="completed" if ok else "failed",
                label=f"Order #{oid}",
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                provenance=prov,
                legacy_text=f"Order #{oid}: {outcome}.",
            ),
        )
        return out

    def _dispatch_oc_with_progress(
        self,
        chat_id: str | int,
        raw: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        meta = _slash_command_meta(raw)
        if meta is None:
            return dispatch_openclaw_command(
                chat_id, raw, self.store, channel=self.channel
            )
        step_id, label, detail, tool_name = meta
        prov = _provenance()
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=tool_name,
                phase="started",
                label=label,
                detail=detail,
                provenance=prov,
                legacy_text=f"{detail}...",
            ),
        )
        t0 = time.monotonic()
        try:
            oc = dispatch_openclaw_command(
                chat_id, raw, self.store, channel=self.channel
            )
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool=tool_name,
                    phase="failed",
                    label=label,
                    outcome="misslyckades",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"{label}: misslyckades.",
                ),
            )
            raise
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if oc is None:
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool=tool_name,
                    phase="failed",
                    label=label,
                    outcome="inget svar",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"{label}: inget svar.",
                ),
            )
            return None
        ok, outcome = _inspect_tool_outcome(tool_name, _reply_text(oc))
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=tool_name,
                phase="completed" if ok else "failed",
                label=label,
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                provenance=prov,
                legacy_text=f"{label}: {outcome}.",
            ),
        )
        return oc

    def handle_ephemeral_oauth_callback(
        self, chat_id: str | int, raw_text: str
    ) -> BotReply:
        """Exchange a pasted ChatGPT OAuth callback URL without storing conversation state."""
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        return handle_ephemeral_oauth_callback(
            chat_id, raw_text, channel=self.channel
        )

    def handle(
        self,
        chat_id: str | int,
        text: str,
        *,
        on_progress: ProgressCallback | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_stream_clear: Callable[[], None] | None = None,
        on_phase_change: Callable[[dict[str, Any]], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
        task_id: str | None = None,
        image_paths: list[str] | tuple[str, ...] | None = None,
    ) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        incoming_images = _normalize_inbound_image_paths(image_paths)
        raw = (text or "").strip()
        if incoming_images and not raw:
            prev = self.store.get(chat_id) or {}
            prev_session = dict(prev.get("session") or {})
            existing_pending = _normalize_inbound_image_paths(
                prev_session.get("pending_image_paths")
            )
            combined = _normalize_inbound_image_paths([*existing_pending, *incoming_images])
            self._update_session_atomic(
                chat_id,
                session_updates={"pending_image_paths": combined},
            )
            return BotReply(text=_PHOTO_WAIT_REPLY)
        if not raw:
            return BotReply(text="Skriv /help eller /commands — eller bara fråga.")

        if is_ephemeral_oauth_message(raw):
            return handle_ephemeral_oauth_callback(
                chat_id, raw, channel=self.channel
            )

        wp_command = self._wp_context_command(chat_id, raw, on_progress=on_progress)
        if wp_command is not None:
            return wp_command

        # OpenClaw slash commands first
        oc = self._dispatch_oc_with_progress(
            chat_id, raw, on_progress=on_progress
        )
        if oc is not None:
            if oc == "__ORDER_PROMPT__":
                self._merge_state(
                    chat_id, flow="order_lookup", step="await_id", slots={}
                )
                return BotReply(text="Ange ordernummer (t.ex. 1001):")
            if isinstance(oc, str) and oc.startswith("__ORDER__:"):
                oid = oc.split(":", 1)[1]
                self._touch_session_order(chat_id, oid)
                return BotReply(
                    text=self._lookup_order_with_progress(
                        chat_id, oid, on_progress=on_progress
                    )
                )
            return as_reply(oc)

        # Attach any pending screenshot(s) from the prior photo-only message and clear once consumed.
        state = self.store.get(chat_id)
        session_dict = dict((state or {}).get("session") or {})
        had_pending_images = bool(session_dict.get("pending_image_paths"))
        pending_images = _normalize_inbound_image_paths(
            session_dict.get("pending_image_paths")
        )
        effective_images = _normalize_inbound_image_paths(
            [*pending_images, *incoming_images]
        )
        if had_pending_images:
            self._update_session_atomic(
                chat_id,
                pop_session_keys=("pending_image_paths",),
            )
            if isinstance(state, dict) and isinstance(state.get("session"), dict):
                state = dict(state)
                cleaned_session = dict(state["session"])
                cleaned_session.pop("pending_image_paths", None)
                state["session"] = cleaned_session

        # Continue multi-turn flows (confirm writes, escalate, order id prompt)
        if state and state.get("flow"):
            return as_reply(
                self._continue_flow(
                    chat_id,
                    raw,
                    state,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                    image_paths=effective_images or None,
                )
            )

        nat_oauth = match_natural_oauth_intent(raw)
        if nat_oauth is not None:
            sub = "" if nat_oauth == "panel" else nat_oauth
            return handle_oauth_command(chat_id, sub, channel=self.channel)

        # Ultra-short order shortcut
        fast = ORDER_FAST_RE.match(raw)
        if fast and not effective_images:
            oid = fast.group(1)
            self._touch_session_order(chat_id, oid)
            return BotReply(
                text=self._lookup_order_with_progress(
                    chat_id, oid, on_progress=on_progress
                )
            )

        if wants_hard_escalate_confirm(raw):
            return self._start_escalate_confirm(chat_id, raw, task_id=task_id)

        return self._run_llm_chat(
            chat_id,
            raw,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_stream_clear=on_stream_clear,
            on_phase_change=on_phase_change,
            on_checkpoint=on_checkpoint,
            task_id=task_id,
            image_paths=effective_images or None,
        )

    def handle_callback(
        self,
        chat_id: str | int,
        data: str,
        *,
        on_progress: ProgressCallback | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_stream_clear: Callable[[], None] | None = None,
        on_phase_change: Callable[[dict[str, Any]], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
        task_id: str | None = None,
    ) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (data or "").strip()

        clarify_pick = _CLARIFY_PICK_RE.fullmatch(raw)
        if clarify_pick:
            cid = clarify_pick.group(1).lower()
            opt_id = clarify_pick.group(2).lower()
            prop = self._consume_clarification(chat_id, cid)
            if prop is None:
                return BotReply(
                    text="Förtydligandet är redan hanterat eller utgånget. Skriv din fråga igen om du vill börja om."
                )
            payload = dict(prop.get("payload") or {})
            proposal = ClarificationProposal.from_dict(payload.get("proposal"))
            opt = (
                next((o for o in proposal.options if o.id == opt_id), None)
                if proposal is not None
                else None
            )
            self._clear_clarify_flow(chat_id, clarify_id=cid)
            if proposal is None or opt is None:
                return BotReply(
                    text="Alternativet är inte längre giltigt. Skriv din fråga igen om du vill börja om."
                )
            prior_picks = list(payload.get("picks") or [])
            picks = [*prior_picks, opt.to_dict()]
            stored_images = _normalize_inbound_image_paths(payload.get("image_paths"))
            original_request = str(
                payload.get("original_request") or proposal.original_request or ""
            )
            round_num = int(payload.get("round") or proposal.round_num or 1)
            next_round = round_num + 1
            confirmed = next_round > MAX_CLARIFY_ROUNDS
            return self._execute_clarified_step(
                chat_id,
                message=original_request or opt.label,
                original_request=original_request,
                picks=picks,
                round_num=next_round,
                confirmed=confirmed,
                picked_option_id=opt.id,
                image_paths=stored_images or None,
                on_progress=on_progress,
                on_text_delta=on_text_delta,
                on_text_reset=on_text_reset,
                on_stream_clear=on_stream_clear,
                on_phase_change=on_phase_change,
                on_checkpoint=on_checkpoint,
                task_id=task_id,
            )

        clarify_go = _CLARIFY_GO_RE.fullmatch(raw)
        if clarify_go:
            cid = clarify_go.group(1).lower()
            prop = self._consume_clarification(chat_id, cid)
            if prop is None:
                return BotReply(
                    text="Förtydligandet är redan hanterat eller utgånget. Skriv din fråga igen om du vill börja om."
                )
            payload = dict(prop.get("payload") or {})
            proposal = ClarificationProposal.from_dict(payload.get("proposal"))
            picks = list(payload.get("picks") or [])
            stored_images = _normalize_inbound_image_paths(payload.get("image_paths"))
            original_request = str(
                payload.get("original_request")
                or (proposal.original_request if proposal else "")
                or ""
            )
            round_num = int(
                payload.get("round") or (proposal.round_num if proposal else 1) or 1
            )
            self._clear_clarify_flow(chat_id, clarify_id=cid)
            msg = original_request or (
                proposal.understanding if proposal else "Fortsätt enligt bekräftad plan"
            )
            return self._execute_clarified_step(
                chat_id,
                message=msg,
                original_request=original_request,
                picks=picks,
                round_num=round_num + 1,
                confirmed=True,
                image_paths=stored_images or None,
                on_progress=on_progress,
                on_text_delta=on_text_delta,
                on_text_reset=on_text_reset,
                on_stream_clear=on_stream_clear,
                on_phase_change=on_phase_change,
                on_checkpoint=on_checkpoint,
                task_id=task_id,
            )

        clarify_other = _CLARIFY_OTHER_RE.fullmatch(raw)
        if clarify_other:
            cid = clarify_other.group(1).lower()
            prop = self._consume_clarification(chat_id, cid)
            if prop is None:
                return BotReply(
                    text="Förtydligandet är redan hanterat eller utgånget. Skriv din fråga igen om du vill börja om."
                )
            payload = dict(prop.get("payload") or {})
            proposal_dict = dict(payload.get("proposal") or {})
            original_request = str(
                payload.get("original_request")
                or proposal_dict.get("original_request")
                or ""
            )
            picks = list(payload.get("picks") or [])
            stored_images = _normalize_inbound_image_paths(payload.get("image_paths"))
            round_num = int(
                payload.get("round") or proposal_dict.get("round_num") or 1
            )
            other_slots: dict[str, Any] = {
                "clarify_id": cid,
                "proposal": proposal_dict,
                "original_request": original_request,
                "picks": picks,
                "round": round_num,
            }
            if stored_images:
                other_slots["image_paths"] = stored_images
            self._set_clarify_flow(
                chat_id,
                flow="change_clarify",
                step="await_other_text",
                slots=other_slots,
            )
            return BotReply(
                text=(
                    "Beskriv kort med egna ord vad du vill ändra "
                    "(t.ex. vilken sida/del, vad som ska ändras och till vad), "
                    "så anpassar jag förslaget. Skriv /stop eller avbryt för att avbryta."
                )
            )

        clarify_cancel = _CLARIFY_CANCEL_RE.fullmatch(raw)
        if clarify_cancel:
            cid = clarify_cancel.group(1).lower()
            cancelled = self._cancel_clarification(chat_id, cid)
            if not cancelled:
                return BotReply(
                    text="Förtydligandet är redan hanterat eller utgånget. Ingen ändring utförd."
                )
            return BotReply(text="Avbrutet — ingen ändring påbörjad.")

        prop_yes = _PROPOSAL_YES_RE.fullmatch(raw)
        if prop_yes:
            proposal_id = prop_yes.group(1).lower()
            if self.proposal_store is None:
                return BotReply(
                    text=with_recovery(
                        "Förslagslagring är inte aktiverad.",
                        footer=FOOTER_UNKNOWN_CALLBACK,
                    )
                )
            prop = self.proposal_store.consume_proposal(proposal_id, chat_id=chat_id)
            if prop is None:
                return BotReply(
                    text="Förslaget är redan hanterat eller utgånget. Ingen ändring utfördes."
                )
            if str(prop.get("kind") or "") == "escalate":
                payload = dict(prop.get("payload") or {})
                return as_reply(
                    self._execute_escalate(
                        chat_id,
                        message=str(payload.get("message") or ""),
                        draft=str(payload.get("reply") or ""),
                        proposal_id=proposal_id,
                        on_progress=on_progress,
                    )
                )
            pending = PendingAction.from_dict(prop.get("payload"))
            if pending is None:
                return BotReply(text="Ogiltigt förslag. Ingen ändring utfördes.")
            if prop.get("domain") and not pending.payload.get("domain"):
                pending.payload["domain"] = str(prop["domain"])
            if prop.get("site_id") and not pending.payload.get("site_id"):
                pending.payload["site_id"] = str(prop["site_id"])
            return self._exec_pending(
                chat_id,
                pending,
                on_progress=on_progress,
                proposal_id=proposal_id,
            )

        prop_no = _PROPOSAL_NO_RE.fullmatch(raw)
        if prop_no:
            proposal_id = prop_no.group(1).lower()
            if self.proposal_store is not None:
                self.proposal_store.cancel_proposal(proposal_id, chat_id=chat_id)
            self._clear_pending(chat_id, proposal_id=proposal_id)
            return BotReply(text="Avbrutet — ingen ändring utförd.")

        if raw == "escalate:yes":
            return as_reply(
                self._confirm_escalate(chat_id, yes=True, on_progress=on_progress)
            )
        if raw == "escalate:no":
            return as_reply(self._confirm_escalate(chat_id, yes=False))
        if raw == "cases:list":
            return as_reply(
                self._dispatch_oc_with_progress(
                    chat_id, "/cases list", on_progress=on_progress
                )
                or with_recovery("Kunde inte lista ärenden.")
            )
        if raw.startswith("cases:show:"):
            id8 = raw.split(":", 2)[2].strip()
            self._touch_session_case(chat_id, id8)
            return as_reply(
                self._dispatch_oc_with_progress(
                    chat_id, f"/cases show {id8}", on_progress=on_progress
                )
                or with_recovery(f"Hittade inte {id8}.")
            )
        if raw.startswith("cases:approve:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            id8 = raw.split(":", 2)[2].strip()
            return as_reply(
                self._dispatch_oc_with_progress(
                    chat_id,
                    f"/cases approve {id8}",
                    on_progress=on_progress,
                )
                or with_recovery("Kunde inte godkänna.")
            )
        if raw.startswith("cases:regen:"):
            id8 = raw.split(":", 2)[2].strip()
            return self._exec_pending(
                chat_id,
                PendingAction(kind="case_regenerate", payload={"case_id": id8}),
                on_progress=on_progress,
            )
        if raw.startswith("order:set:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            # order:set:{oid}:{status}
            parts = raw.split(":")
            if len(parts) >= 4:
                oid, status = parts[2], parts[3]
                return self._exec_pending(
                    chat_id,
                    PendingAction(
                        kind="order_status",
                        payload={
                            "order_id": oid,
                            "status": status,
                            "domain": self._session_market(chat_id),
                        },
                    ),
                    on_progress=on_progress,
                )
        if raw == "order:cancel":
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen orderstatus ändrad.")
        if raw.startswith("product:desc:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            parts = raw.split(":")
            # product:desc:{pid}:{publish_flag}
            if len(parts) >= 4:
                pid, flag = parts[2], parts[3]
                return self._exec_pending(
                    chat_id,
                    PendingAction(
                        kind="product_desc",
                        payload={
                            "product_id": pid if pid != "0" else "",
                            "publish": flag == "1",
                            "language": "sv",
                        },
                    ),
                    on_progress=on_progress,
                )
        if raw == "product:cancel":
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen produktbeskrivning genererad.")
        if raw == "wpfile:no" or raw.startswith("wpfile:no:"):
            plan_id = raw.split(":", 2)[2] if raw.startswith("wpfile:no:") else ""
            if plan_id:
                if not _FILE_PLAN_ID.fullmatch(plan_id):
                    return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
                from ecom_ops.wp_ops import WpOpsService

                WpOpsService().deny_file_plan(plan_id, actor=actor)
            return BotReply(text="Avbrutet. Ingen fil ändrades.")
        if raw.startswith("wpfile:stage:"):
            plan_id = raw.split(":", 2)[2]
            if not _FILE_PLAN_ID.fullmatch(plan_id):
                return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
            from ecom_ops.wp_ops import WpOpsService

            prov = _provenance()
            step_id = f"wpfile:{plan_id}"
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wpfile_stage",
                    phase="started",
                    label="Staging-filändring",
                    detail="Skriver godkänd filändring i staging",
                    provenance=prov,
                    legacy_text="Skriver godkänd filändring i staging...",
                ),
            )
            t0 = time.monotonic()
            staged = WpOpsService().stage_file_plan(plan_id, actor=actor)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if staged.ok and (staged.data or {}).get("executed") is True:
                emit_progress(
                    on_progress,
                    ProgressEvent(
                        step_id=step_id,
                        tool="wpfile_stage",
                        phase="completed",
                        label="Staging-filändring",
                        outcome="fil skriven i staging",
                        elapsed_ms=elapsed_ms,
                        provenance=prov,
                        legacy_text="Staging-filändring: klar.",
                    ),
                )
                return BotReply(text="Filen är skriven i staging. Produktionen är oförändrad.")
            error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wpfile_stage",
                    phase="failed",
                    label="Staging-filändring",
                    outcome=f"skrevs inte ({error})",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"Staging-filändring: misslyckades ({error}).",
                ),
            )
            return BotReply(text=f"Staging skrevs inte ({error}). Ingen fil ändrades.")
        if raw == "wpreview:no" or raw.startswith(("wpreview:no:", "wpreview:stage:")):
            return self._handle_wpreview_callback(
                chat_id, actor, raw, on_progress=on_progress
            )
        if raw == "action:yes":
            blocked = self._mutations_blocked_reply()
            if blocked:
                state = self.store.get(chat_id) or {}
                slots = dict(state.get("slots") or {})
                pending = PendingAction.from_dict(slots.get("pending"))
                if pending and pending.kind in {
                    "order_status",
                    "product_desc",
                    "product_update",
                    "page_update",
                    "post_update",
                    "site_update",
                    "plugin_status",
                }:
                    return blocked
            return self._confirm_pending_text(
                chat_id, yes=True, on_progress=on_progress
            )
        if raw == "action:no":
            return self._confirm_pending_text(chat_id, yes=False)
        if raw.startswith("oauth:"):
            oauth_reply = handle_oauth_callback_query(
                chat_id, raw, channel=self.channel
            )
            if oauth_reply is not None:
                return oauth_reply
        return BotReply(
            text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
        )

    def _touch_session_order(self, chat_id: str | int, order_id: str) -> None:
        self._update_session_atomic(
            chat_id, session_updates={"last_order_id": str(order_id)}
        )

    def _clear_mismatched_wp_context(self, chat_id: str | int, actor: str) -> None:
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        context = session.get("wp_ops_context")
        pop_keys: list[str] = []
        updates: dict[str, Any] = {}
        if context is not None and not context_matches(
            context,
            actor=actor,
            channel=self.channel,
            conversation_id=f"{self.channel}:{chat_id}",
        ):
            session.pop("wp_ops_context", None)
            pop_keys.append("wp_ops_context")
        kept, hashes_changed = _prune_review_hashes(session, actor=actor)
        if hashes_changed:
            if kept:
                updates["wp_review_hashes"] = kept
            else:
                pop_keys.append("wp_review_hashes")
        if not updates and not pop_keys:
            return
        self._update_session_atomic(
            chat_id,
            session_updates=updates or None,
            pop_session_keys=pop_keys,
        )

    def _touch_session_case(self, chat_id: str | int, case_id8: str) -> None:
        self._update_session_atomic(
            chat_id, session_updates={"last_case_id8": str(case_id8)}
        )

    def _clear_pending(
        self, chat_id: str | int, *, proposal_id: str | None = None
    ) -> None:
        prev = self.store.get(chat_id) or {}
        if proposal_id is not None:
            slots = dict(prev.get("slots") or {})
            stored_pid = str(slots.get("proposal_id") or "").strip().lower()
            if stored_pid and stored_pid != proposal_id.strip().lower():
                return
        session = dict(prev.get("session") or {})
        self._merge_state(
            chat_id,
            flow=None,
            step=None,
            slots={},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )

    def _continue_flow(
        self,
        chat_id: str | int,
        text: str,
        state: dict[str, Any],
        *,
        on_progress: ProgressCallback | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_stream_clear: Callable[[], None] | None = None,
        on_phase_change: Callable[[dict[str, Any]], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
        task_id: str | None = None,
        image_paths: list[str] | tuple[str, ...] | None = None,
    ) -> str | BotReply:
        flow = state.get("flow")
        step = state.get("step")
        session = dict(state.get("session") or {})
        slots = dict(state.get("slots") or {})

        if flow == "change_clarify":
            clarify_id = str(slots.get("clarify_id") or "").strip().lower()
            proposal = ClarificationProposal.from_dict(slots.get("proposal"))
            original_request = str(
                slots.get("original_request")
                or (proposal.original_request if proposal else "")
                or ""
            )
            prior_picks = list(slots.get("picks") or [])
            stored_images = _normalize_inbound_image_paths(
                [*(slots.get("image_paths") or ()), *(image_paths or ())]
            )
            try:
                round_num = max(
                    1,
                    int(slots.get("round") or (proposal.round_num if proposal else 1) or 1),
                )
            except (TypeError, ValueError):
                round_num = 1
            answer = text.strip().lower()

            if answer in _CLARIFY_CANCEL_WORDS:
                if clarify_id:
                    self._cancel_clarification(chat_id, clarify_id)
                self._clear_clarify_flow(chat_id)
                return BotReply(text="Avbrutet — ingen ändring påbörjad.")

            if step == "await_choice":
                if re.fullmatch(r"[1-4]", answer):
                    idx = int(answer)
                    if proposal is not None and 1 <= idx <= len(proposal.options):
                        opt = proposal.options[idx - 1]
                        if clarify_id:
                            self._consume_clarification(chat_id, clarify_id)
                        self._clear_clarify_flow(chat_id)
                        picks = [*prior_picks, opt.to_dict()]
                        next_round = round_num + 1
                        return self._execute_clarified_step(
                            chat_id,
                            message=original_request or opt.label,
                            original_request=original_request,
                            picks=picks,
                            round_num=next_round,
                            confirmed=next_round > MAX_CLARIFY_ROUNDS,
                            picked_option_id=opt.id,
                            image_paths=stored_images or None,
                            on_progress=on_progress,
                            on_text_delta=on_text_delta,
                            on_text_reset=on_text_reset,
                            on_stream_clear=on_stream_clear,
                            on_phase_change=on_phase_change,
                            on_checkpoint=on_checkpoint,
                            task_id=task_id,
                        )
                    if proposal is not None:
                        actions = (
                            clarification_actions(clarify_id, proposal)
                            if _CLARIFY_ID_RE.fullmatch(clarify_id)
                            else None
                        )
                        return bot_reply(
                            f"Välj 1–{len(proposal.options)}, skriv med egna ord eller avbryt.",
                            actions=actions,
                        )
                if _is_unrelated_lookup(text):
                    if clarify_id:
                        self._cancel_clarification(chat_id, clarify_id)
                    self._clear_clarify_flow(chat_id)
                    fast = ORDER_FAST_RE.match(text)
                    if fast:
                        oid = fast.group(1)
                        self._touch_session_order(chat_id, oid)
                        return BotReply(
                            text=self._lookup_order_with_progress(
                                chat_id, oid, on_progress=on_progress
                            )
                        )
                    return self._run_llm_chat(
                        chat_id,
                        text,
                        on_progress=on_progress,
                        on_text_delta=on_text_delta,
                        on_text_reset=on_text_reset,
                        on_stream_clear=on_stream_clear,
                        on_phase_change=on_phase_change,
                        on_checkpoint=on_checkpoint,
                        task_id=task_id,
                        image_paths=image_paths,
                    )
                if answer in _CLARIFY_CONFIRM_WORDS and len(answer) <= 8:
                    actions = (
                        clarification_actions(clarify_id, proposal)
                        if (proposal is not None and _CLARIFY_ID_RE.fullmatch(clarify_id))
                        else None
                    )
                    return bot_reply(
                        "Välj ett av alternativen ovan (1–4 eller knapp), eller skriv med egna ord vad du vill ändra.",
                        actions=actions,
                    )
                if clarify_id:
                    self._consume_clarification(chat_id, clarify_id)
                self._clear_clarify_flow(chat_id)
                return self._execute_clarified_step(
                    chat_id,
                    message=text,
                    original_request=original_request,
                    picks=prior_picks,
                    round_num=round_num + 1,
                    confirmed=round_num >= MAX_CLARIFY_ROUNDS,
                    extra_note=text,
                    image_paths=stored_images or None,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                )

            if step == "await_confirm":
                if answer in _CLARIFY_CONFIRM_WORDS:
                    if clarify_id:
                        self._consume_clarification(chat_id, clarify_id)
                    self._clear_clarify_flow(chat_id)
                    msg = original_request or (
                        proposal.understanding
                        if proposal
                        else "Fortsätt enligt bekräftad plan"
                    )
                    return self._execute_clarified_step(
                        chat_id,
                        message=msg,
                        original_request=original_request,
                        picks=prior_picks,
                        round_num=round_num + 1,
                        confirmed=True,
                        image_paths=stored_images or None,
                        on_progress=on_progress,
                        on_text_delta=on_text_delta,
                        on_text_reset=on_text_reset,
                        on_stream_clear=on_stream_clear,
                        on_phase_change=on_phase_change,
                        on_checkpoint=on_checkpoint,
                        task_id=task_id,
                    )
                if _is_unrelated_lookup(text):
                    if clarify_id:
                        self._cancel_clarification(chat_id, clarify_id)
                    self._clear_clarify_flow(chat_id)
                    fast = ORDER_FAST_RE.match(text)
                    if fast:
                        oid = fast.group(1)
                        self._touch_session_order(chat_id, oid)
                        return BotReply(
                            text=self._lookup_order_with_progress(
                                chat_id, oid, on_progress=on_progress
                            )
                        )
                    return self._run_llm_chat(
                        chat_id,
                        text,
                        on_progress=on_progress,
                        on_text_delta=on_text_delta,
                        on_text_reset=on_text_reset,
                        on_stream_clear=on_stream_clear,
                        on_phase_change=on_phase_change,
                        on_checkpoint=on_checkpoint,
                        task_id=task_id,
                        image_paths=image_paths,
                    )
                if clarify_id:
                    self._consume_clarification(chat_id, clarify_id)
                self._clear_clarify_flow(chat_id)
                return self._execute_clarified_step(
                    chat_id,
                    message=text,
                    original_request=original_request,
                    picks=prior_picks,
                    round_num=round_num + 1,
                    confirmed=round_num >= MAX_CLARIFY_ROUNDS,
                    extra_note=text,
                    image_paths=stored_images or None,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                )

            if step == "await_other_text":
                self._clear_clarify_flow(chat_id)
                return self._execute_clarified_step(
                    chat_id,
                    message=text,
                    original_request=original_request,
                    picks=prior_picks,
                    round_num=round_num + 1,
                    confirmed=round_num >= MAX_CLARIFY_ROUNDS,
                    extra_note=text,
                    image_paths=stored_images or None,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                )

        if flow == "order_lookup" and step == "await_id":
            try:
                oid = validate_order_id(text.strip())
            except Exception:
                return BotReply(
                    text="Ogiltigt ordernummer. Försök igen eller /stop."
                )
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            self._touch_session_order(chat_id, oid)
            return self._lookup_order_with_progress(
                chat_id, oid, on_progress=on_progress
            )

        if flow == "escalate_confirm" and step == "confirm":
            answer = text.strip().lower()
            proposal_id = str(slots.get("proposal_id") or "").strip()
            if answer in {"ja", "yes", "y", "j"}:
                if self.channel == "telegram" and self.proposal_store is not None:
                    return bot_reply(
                        "Tryck på Ja-knappen på det aktuella förslaget för att bekräfta eskaleringen.",
                        actions=proposal_confirm_actions(proposal_id) if proposal_id else None,
                    )
                return self._confirm_escalate(
                    chat_id, yes=True, on_progress=on_progress
                )
            if answer in {"nej", "no", "n"}:
                if self.proposal_store is not None and proposal_id:
                    self.proposal_store.cancel_proposal(proposal_id, chat_id=chat_id)
                return self._confirm_escalate(chat_id, yes=False)
            if not wants_escalate(text) and len(text) > 12:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(
                    chat_id,
                    text,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                    image_paths=image_paths,
                )
            if self.proposal_store is not None and proposal_id:
                return bot_reply(
                    "Tryck på Ja- eller Nej-knappen på det aktuella förslaget, eller /stop.",
                    actions=proposal_confirm_actions(proposal_id),
                )
            return bot_reply(
                "Svara ja eller nej (eller tryck knappen), eller /stop.",
                actions=yes_no_actions(
                    yes_data="escalate:yes", no_data="escalate:no"
                ),
            )

        if flow == "pending_action" and step == "confirm":
            answer = text.strip().lower()
            proposal_id = str(slots.get("proposal_id") or "").strip()
            if answer in {"ja", "yes", "y", "j", "ok", "kör", "bekrafta", "bekräfta"}:
                if self.channel == "telegram" and self.proposal_store is not None:
                    return bot_reply(
                        "Tryck på Ja-knappen på det aktuella förslaget för att bekräfta ändringen.",
                        actions=proposal_confirm_actions(proposal_id) if proposal_id else None,
                    )
                return self._confirm_pending_text(
                    chat_id, yes=True, on_progress=on_progress
                )
            if answer in {"nej", "no", "n", "avbryt", "cancel"}:
                if self.proposal_store is not None and proposal_id:
                    self.proposal_store.cancel_proposal(proposal_id, chat_id=chat_id)
                return self._confirm_pending_text(chat_id, yes=False)
            # Free text mid-confirm → drop pending and chat
            if len(text) > 8:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(
                    chat_id,
                    text,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                    image_paths=image_paths,
                )
            if self.proposal_store is not None and proposal_id:
                return bot_reply(
                    "Tryck på Ja- eller Nej-knappen på det aktuella förslaget för att bekräfta ändringen.",
                    actions=proposal_confirm_actions(proposal_id),
                )
            pending = PendingAction.from_dict(slots.get("pending"))
            try:
                actor_obj = self._actor_obj(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return self._deny_actor()
            return bot_reply(
                "Svara ja/nej eller tryck knappen för att bekräfta ändringen.",
                actions=self._actions_for_pending(pending, actor=actor_obj),
            )

        self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
        return self._run_llm_chat(
            chat_id,
            text,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_stream_clear=on_stream_clear,
            on_phase_change=on_phase_change,
            on_checkpoint=on_checkpoint,
            task_id=task_id,
            image_paths=image_paths,
        )

    def _start_escalate_confirm(
        self,
        chat_id: str | int,
        message: str,
        *,
        task_id: str | None = None,
        prompt_text: str | None = None,
        attachments: tuple[str, ...] = (),
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        attachment_kwargs = {"attachments": attachments} if attachments else {}
        body = prompt_text or (
            "Eskalera till Oscar?\n\n"
            f"Meddelande: {message[:300]}\n\n"
            "Bekräfta med Ja/Nej."
        )
        if self.proposal_store is not None:
            domain = self._session_market(chat_id)
            wp_ctx = session.get("wp_ops_context")
            site_id = (
                str(wp_ctx.get("site_id")).strip()
                if isinstance(wp_ctx, dict) and wp_ctx.get("site_id")
                else None
            )
            prop = self.proposal_store.create_proposal(
                chat_id,
                kind="escalate",
                payload={"message": message[:500]},
                task_id=task_id,
                domain=domain,
                site_id=site_id,
            )
            proposal_id = str(prop.get("proposal_id") or "")
            self._merge_state(
                chat_id,
                flow="escalate_confirm",
                step="confirm",
                slots={"message": message[:500], "proposal_id": proposal_id},
                session=session,
                messages=prev.get("messages"),
                tool_digest=prev.get("tool_digest"),
            )
            return bot_reply(
                body,
                actions=proposal_confirm_actions(proposal_id),
                needs_typing=True,
                **attachment_kwargs,
            )
        self._merge_state(
            chat_id,
            flow="escalate_confirm",
            step="confirm",
            slots={"message": message[:500]},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )
        return bot_reply(
            body,
            actions=yes_no_actions(yes_data="escalate:yes", no_data="escalate:no"),
            needs_typing=True,
            **attachment_kwargs,
        )

    def _execute_escalate(
        self,
        chat_id: str | int,
        *,
        message: str,
        draft: str = "",
        proposal_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return (
                "Din chat saknar actor-mapping. "
                "Be Oscar uppdatera actor-map för kanalen."
            )
        prov = _provenance()
        step_id = "action:escalate"
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="escalate",
                phase="started",
                label="Eskalering",
                detail="Skickar eskalering till Oscar",
                provenance=prov,
                legacy_text="Eskalerar till Oscar...",
            ),
        )
        t0 = time.monotonic()
        try:
            ticket = self.escalation.escalate_critical(
                f"{self.channel} escalation by {actor}",
                details={
                    "channel": self.channel,
                    "chat_id": str(chat_id),
                    "actor": actor,
                    "message": message[:500],
                    "draft": draft[:500],
                },
            )
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="escalate",
                    phase="failed",
                    label="Eskalering",
                    outcome="eskalering misslyckades",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text="Eskalering misslyckades.",
                ),
            )
            raise
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="escalate",
                phase="completed",
                label="Eskalering",
                outcome="eskalerad till Oscar",
                elapsed_ms=elapsed_ms,
                provenance=prov,
                legacy_text="Eskalerat till Oscar.",
            ),
        )
        self._clear_pending(chat_id, proposal_id=proposal_id)
        return f"Eskalerat till Oscar. Ticket: {ticket.id}"

    def _confirm_escalate(
        self,
        chat_id: str | int,
        *,
        yes: bool,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        session = dict(state.get("session") or {})
        if yes:
            return self._execute_escalate(
                chat_id,
                message=str(slots.get("message") or ""),
                draft=str(slots.get("reply") or ""),
                on_progress=on_progress,
            )
        self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
        return "Inte eskalerad. Fortsätt chatta — eller /cases · /help"

    def _actions_for_pending(self, pending: PendingAction | None, *, actor: Actor | None = None):
        if not pending:
            return yes_no_actions(yes_data="action:yes", no_data="action:no")
        if actor is not None and not self._pending_allowed(actor, pending):
            return None
        if pending.kind == "order_status":
            return order_status_confirm_actions(
                str(pending.payload.get("order_id") or ""),
                str(pending.payload.get("status") or ""),
            )
        if pending.kind == "product_desc":
            return product_desc_confirm_actions(
                str(pending.payload.get("product_id") or "0"),
                publish=bool(pending.payload.get("publish")),
            )
        if pending.kind == "case_regenerate":
            id8 = str(pending.payload.get("case_id") or "")[:8]
            return yes_no_actions(yes_data=f"cases:regen:{id8}", no_data="action:no")
        return yes_no_actions(yes_data="action:yes", no_data="action:no")

    def _start_pending(
        self,
        chat_id: str | int,
        pending: PendingAction,
        text: str,
        *,
        actor: Actor,
        task_id: str | None = None,
        attachments: tuple[str, ...] = (),
    ) -> BotReply:
        if not self._pending_allowed(actor, pending):
            return BotReply(text=f"{text}\n\n{self._deny_pending_reply(pending)}")
        attachment_kwargs = {"attachments": attachments} if attachments else {}
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        if pending.kind == "order_status" and pending.payload.get("order_id"):
            session["last_order_id"] = str(pending.payload["order_id"])
        if pending.kind == "case_regenerate" and pending.payload.get("case_id"):
            session["last_case_id8"] = str(pending.payload["case_id"])[:8]
        if pending.kind == "product_update" and pending.payload.get("product_id"):
            session["last_product_id"] = str(pending.payload["product_id"])
        if pending.kind == "page_update" and pending.payload.get("page_id"):
            session["last_page_id"] = str(pending.payload["page_id"])
        if pending.kind == "post_update" and pending.payload.get("post_id"):
            session["last_post_id"] = str(pending.payload["post_id"])

        if self.proposal_store is not None:
            raw_domain = pending.payload.get("domain") or self._session_market(chat_id)
            domain = str(raw_domain).strip() if raw_domain else None
            wp_ctx = session.get("wp_ops_context")
            site_id = (
                str(wp_ctx.get("site_id")).strip()
                if isinstance(wp_ctx, dict) and wp_ctx.get("site_id")
                else None
            )
            frozen_payload = dict(pending.payload)
            if domain and not frozen_payload.get("domain"):
                frozen_payload["domain"] = domain
            if site_id and not frozen_payload.get("site_id"):
                frozen_payload["site_id"] = site_id
            frozen_pending = PendingAction(kind=pending.kind, payload=frozen_payload)
            prop = self.proposal_store.create_proposal(
                chat_id,
                kind=pending.kind,
                payload=frozen_pending.to_dict(),
                task_id=task_id,
                domain=domain,
                site_id=site_id,
            )
            proposal_id = str(prop.get("proposal_id") or "")
            self._merge_state(
                chat_id,
                flow="pending_action",
                step="confirm",
                slots={
                    "pending": frozen_pending.to_dict(),
                    "proposal_id": proposal_id,
                    "prompt": text[:500],
                },
                session=session,
                messages=prev.get("messages"),
                tool_digest=prev.get("tool_digest"),
            )
            return bot_reply(
                text,
                actions=proposal_confirm_actions(proposal_id),
                needs_typing=True,
                **attachment_kwargs,
            )

        self._merge_state(
            chat_id,
            flow="pending_action",
            step="confirm",
            slots={"pending": pending.to_dict(), "prompt": text[:500]},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )
        return bot_reply(
            text,
            actions=self._actions_for_pending(pending, actor=actor),
            needs_typing=True,
            **attachment_kwargs,
        )

    def _confirm_pending_text(
        self,
        chat_id: str | int,
        *,
        yes: bool,
        on_progress: ProgressCallback | None = None,
    ) -> BotReply:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        pending = PendingAction.from_dict(slots.get("pending"))
        if not yes or not pending:
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen ändring utförd.")
        return self._exec_pending(chat_id, pending, on_progress=on_progress)

    def _exec_pending(
        self,
        chat_id: str | int,
        pending: PendingAction,
        *,
        on_progress: ProgressCallback | None = None,
        proposal_id: str | None = None,
    ) -> BotReply:
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            self._clear_pending(chat_id, proposal_id=proposal_id)
            return self._deny_actor()
        try:
            actor_obj = resolve_actor(actor)
        except Exception:
            self._clear_pending(chat_id, proposal_id=proposal_id)
            return self._deny_actor()
        if not self._pending_allowed(actor_obj, pending):
            self._clear_pending(chat_id, proposal_id=proposal_id)
            return BotReply(text=self._deny_pending_reply(pending))
        if pending.kind in {
            "order_status",
            "product_desc",
            "product_update",
            "page_update",
            "post_update",
            "site_update",
            "plugin_status",
        }:
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
        label, detail, ok_outcome = _PENDING_PROGRESS_LABELS.get(
            pending.kind, ("Åtgärd", "Utför godkänd åtgärd", "Åtgärd utförd")
        )
        prov = _provenance()
        step_id = f"action:{pending.kind}"
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=pending.kind,
                phase="started",
                label=label,
                detail=detail,
                provenance=prov,
                legacy_text=f"{detail}...",
            ),
        )
        t0 = time.monotonic()
        ok = False
        msg = "Okänd action"
        try:
            if pending.kind == "order_status":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_order_status(
                    order_id=str(pending.payload.get("order_id") or ""),
                    status=str(pending.payload.get("status") or ""),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
                if ok and pending.payload.get("order_id"):
                    self._touch_session_order(chat_id, str(pending.payload["order_id"]))
            elif pending.kind == "product_desc":
                ok, msg = execute_product_desc(
                    product_id=str(pending.payload.get("product_id") or ""),
                    language=str(pending.payload.get("language") or "sv"),
                    publish=bool(pending.payload.get("publish")),
                    actor=actor,
                )
            elif pending.kind == "product_update":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_product_update(
                    product_id=str(pending.payload.get("product_id") or ""),
                    fields=dict(pending.payload.get("fields") or {}),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
            elif pending.kind == "page_update":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_page_update(
                    page_id=str(pending.payload.get("page_id") or ""),
                    fields=dict(pending.payload.get("fields") or {}),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
            elif pending.kind == "post_update":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_post_update(
                    post_id=str(pending.payload.get("post_id") or ""),
                    fields=dict(pending.payload.get("fields") or {}),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
            elif pending.kind == "site_update":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_site_update(
                    fields=dict(pending.payload.get("fields") or {}),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
            elif pending.kind == "plugin_status":
                domain = pending.payload.get("domain") or self._session_market(chat_id)
                ok, msg = execute_plugin_status(
                    plugin=str(pending.payload.get("plugin") or ""),
                    status=str(pending.payload.get("status") or ""),
                    actor=actor,
                    domain=str(domain) if domain else None,
                )
            elif pending.kind == "case_regenerate":
                # Prefer full id via store if only id8
                case_key = str(pending.payload.get("case_id") or "")
                case = None
                try:
                    from ecom_ops.cases.service import CaseService

                    svc = CaseService()
                    case = svc.store.resolve_id_prefix(case_key) or svc.get(case_key)
                    if case:
                        case_key = case.id
                except Exception:
                    pass
                ok, msg = execute_case_regenerate(case_id=case_key, actor=actor)
                if ok:
                    self._touch_session_case(chat_id, case_key[:8])
                    try:
                        from ecom_ops.order_context import woo_domain_from_market

                        if case and getattr(case, "market", None):
                            market = woo_domain_from_market(case.market)
                            if market:
                                self._update_session_atomic(
                                    chat_id, session_updates={"last_market": market}
                                )
                    except Exception:
                        pass
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool=pending.kind,
                    phase="failed",
                    label=label,
                    outcome="misslyckades",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"{label}: misslyckades.",
                ),
            )
            raise
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=pending.kind,
                phase="completed" if ok else "failed",
                label=label,
                outcome=ok_outcome if ok else "misslyckades",
                elapsed_ms=elapsed_ms,
                provenance=prov,
                legacy_text=f"{label}: {ok_outcome if ok else 'misslyckades'}.",
            ),
        )
        self._clear_pending(chat_id, proposal_id=proposal_id)
        prefix = "Klart." if ok else "Misslyckades."
        # RBAC denies are common for jonatan on order update
        if not ok and ("lacks permission" in msg.lower() or "access" in msg.lower()):
            map_hint = (
                "MESSENGER_ACTOR_MAP"
                if self.channel == "messenger"
                else "TELEGRAM_ACTOR_MAP"
            )
            msg = (
                f"{msg}\n\n"
                f"Tips: mappa din chat till en operator/Oscar via {map_hint} "
                "(Jonatan har CASE_REPLY men inte order/product write)."
            )
        return BotReply(text=f"{prefix}\n{msg}")

    def _set_clarify_flow(
        self,
        chat_id: str | int,
        *,
        flow: str | None,
        step: str | None,
        slots: dict[str, Any],
    ) -> None:
        if hasattr(self.store, "append_turn_atomic"):
            self.store.append_turn_atomic(
                chat_id,
                flow_updates={"flow": flow, "step": step, "slots": slots},
            )
            return
        prev = self.store.get(chat_id) or {}
        self._merge_state(
            chat_id,
            flow=flow,
            step=step,
            slots=slots,
            session=dict(prev.get("session") or {}),
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )

    def _clear_clarify_flow(
        self, chat_id: str | int, *, clarify_id: str | None = None
    ) -> None:
        prev = self.store.get(chat_id) or {}
        if prev.get("flow") != "change_clarify":
            return
        if clarify_id is not None:
            stored_cid = str((prev.get("slots") or {}).get("clarify_id") or "").strip().lower()
            if stored_cid and stored_cid != clarify_id.strip().lower():
                return
        self._set_clarify_flow(chat_id, flow=None, step=None, slots={})

    def _store_session_clarification(
        self,
        chat_id: str | int,
        clarify_id: str,
        payload: dict[str, Any],
        domain: str | None,
        site_id: str | None,
    ) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        cache = dict(session.get("clarify_proposals") or {})
        cache.pop(clarify_id, None)
        cache[clarify_id] = {
            "kind": "change_clarify",
            "state": "open",
            "created_at": time.time(),
            "payload": payload,
            "domain": domain,
            "site_id": site_id,
        }
        while len(cache) > _MAX_CLARIFY_SESSION_CACHE:
            oldest = next(iter(cache))
            cache.pop(oldest, None)
        self._update_session_atomic(chat_id, session_updates={"clarify_proposals": cache})

    def _consume_clarification(
        self, chat_id: str | int, clarify_id: str
    ) -> dict[str, Any] | None:
        cid = clarify_id.strip().lower()
        if self.proposal_store is not None:
            prop = self.proposal_store.consume_proposal(cid, chat_id=chat_id)
            if prop is None or str(prop.get("kind") or "") != "change_clarify":
                return None
            return prop
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        cache = dict(session.get("clarify_proposals") or {})
        entry = cache.get(cid)
        if isinstance(entry, dict):
            if entry.get("state") != "open":
                return None
            created_at = float(entry.get("created_at") or 0)
            if created_at and (time.time() - created_at) > CLARIFY_TTL_SECONDS:
                cache[cid] = {**entry, "state": "expired"}
                self._update_session_atomic(
                    chat_id, session_updates={"clarify_proposals": cache}
                )
                return None
            cache[cid] = {**entry, "state": "consumed"}
            self._update_session_atomic(
                chat_id, session_updates={"clarify_proposals": cache}
            )
            return dict(entry)
        slots = dict(prev.get("slots") or {})
        if (
            prev.get("flow") == "change_clarify"
            and str(slots.get("clarify_id") or "").strip().lower() == cid
        ):
            return {
                "kind": "change_clarify",
                "payload": {
                    "proposal": slots.get("proposal"),
                    "original_request": slots.get("original_request") or "",
                    "picks": list(slots.get("picks") or []),
                    "round": slots.get("round") or 1,
                },
            }
        return None

    def _cancel_clarification(self, chat_id: str | int, clarify_id: str) -> bool:
        cid = clarify_id.strip().lower()
        cancelled = False
        if self.proposal_store is not None:
            cancelled = bool(self.proposal_store.cancel_proposal(cid, chat_id=chat_id))
        else:
            prev = self.store.get(chat_id) or {}
            cache = dict((prev.get("session") or {}).get("clarify_proposals") or {})
            entry = cache.get(cid)
            if isinstance(entry, dict) and entry.get("state") == "open":
                cache[cid] = {**entry, "state": "cancelled"}
                self._update_session_atomic(
                    chat_id, session_updates={"clarify_proposals": cache}
                )
                cancelled = True
            else:
                slots = dict(prev.get("slots") or {})
                if (
                    prev.get("flow") == "change_clarify"
                    and str(slots.get("clarify_id") or "").strip().lower() == cid
                ):
                    cancelled = True
        self._clear_clarify_flow(chat_id, clarify_id=cid)
        return cancelled

    def _start_clarification(
        self,
        chat_id: str | int,
        proposal: ClarificationProposal,
        text: str,
        *,
        original_request: str = "",
        picks: list[dict[str, Any]] | None = None,
        round_num: int = 1,
        task_id: str | None = None,
        attachments: tuple[str, ...] = (),
        image_paths: list[str] | tuple[str, ...] | None = None,
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        orig = (original_request or proposal.original_request or "").strip()
        prior_picks = list(picks or [])
        clean_images = _normalize_inbound_image_paths(image_paths)
        try:
            rnum = max(1, int(round_num or proposal.round_num or 1))
        except (TypeError, ValueError):
            rnum = 1
        proposal.original_request = orig
        proposal.round_num = rnum
        payload_data: dict[str, Any] = {
            "proposal": proposal.to_dict(),
            "original_request": orig,
            "picks": prior_picks,
            "round": rnum,
        }
        if clean_images:
            payload_data["image_paths"] = clean_images
        domain = self._session_market(chat_id)
        wp_ctx = session.get("wp_ops_context")
        site_id = (
            str(wp_ctx.get("site_id")).strip()
            if isinstance(wp_ctx, dict) and wp_ctx.get("site_id")
            else None
        )
        cid = ""
        if self.proposal_store is not None:
            create_kwargs: dict[str, Any] = {
                "kind": "change_clarify",
                "payload": payload_data,
                "task_id": task_id,
                "domain": domain,
                "site_id": site_id,
            }
            if _accepts_kwarg(self.proposal_store.create_proposal, "id_prefix"):
                create_kwargs["id_prefix"] = "cl"
            prop = self.proposal_store.create_proposal(chat_id, **create_kwargs)
            cid = str(prop.get("proposal_id") or "").strip().lower()
        if not _CLARIFY_ID_RE.fullmatch(cid):
            cid = f"cl-{uuid.uuid4().hex[:12]}"
            self._store_session_clarification(chat_id, cid, payload_data, domain, site_id)
        elif self.proposal_store is None:
            self._store_session_clarification(chat_id, cid, payload_data, domain, site_id)

        step = (
            "await_confirm"
            if (proposal.mode == "confirm" or not proposal.options)
            else "await_choice"
        )
        slots: dict[str, Any] = {
            "clarify_id": cid,
            "proposal": proposal.to_dict(),
            "original_request": orig,
            "picks": prior_picks,
            "round": rnum,
        }
        if clean_images:
            slots["image_paths"] = clean_images
        self._set_clarify_flow(
            chat_id,
            flow="change_clarify",
            step=step,
            slots=slots,
        )
        attachment_kwargs = {"attachments": attachments} if attachments else {}
        return bot_reply(
            text,
            actions=clarification_actions(cid, proposal),
            needs_typing=True,
            **attachment_kwargs,
        )

    def _execute_clarified_step(
        self,
        chat_id: str | int,
        *,
        message: str,
        original_request: str,
        picks: list[dict[str, Any]],
        round_num: int,
        confirmed: bool,
        extra_note: str = "",
        picked_option_id: str = "",
        image_paths: list[str] | tuple[str, ...] | None = None,
        on_progress: ProgressCallback | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_stream_clear: Callable[[], None] | None = None,
        on_phase_change: Callable[[dict[str, Any]], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
        task_id: str | None = None,
    ) -> BotReply:
        clarify_ctx = build_clarified_request_message(
            original_request,
            picks,
            extra_note=extra_note,
            confirmed=confirmed,
        )
        reply = self._run_llm_chat(
            chat_id,
            message,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_stream_clear=on_stream_clear,
            on_phase_change=on_phase_change,
            on_checkpoint=on_checkpoint,
            task_id=task_id,
            clarification_confirmed=confirmed,
            clarification_round=round_num,
            clarification_context=clarify_ctx,
            original_request=original_request,
            prior_picks=picks,
            image_paths=image_paths,
        )
        if reply.actions is None and reply.text in {FALLBACK_NO_KEY, FALLBACK_BUDGET}:
            read_prompt = _OFFLINE_READ_OPTION_PROMPTS.get(picked_option_id)
            if read_prompt:
                return self._run_llm_chat(
                    chat_id,
                    read_prompt,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta,
                    on_text_reset=on_text_reset,
                    on_stream_clear=on_stream_clear,
                    on_phase_change=on_phase_change,
                    on_checkpoint=on_checkpoint,
                    task_id=task_id,
                    clarification_confirmed=True,
                    clarification_round=round_num,
                    clarification_context=clarify_ctx,
                    original_request=original_request,
                    prior_picks=picks,
                    image_paths=image_paths,
                )
            if not confirmed and round_num <= MAX_CLARIFY_ROUNDS and picks:
                picked = picks[-1]
                label = str(picked.get("label") or picked.get("id") or "Valt spår")
                summary = str(picked.get("summary") or "").strip()
                understanding = (
                    f"Valt spår: {label}" + (f" — {summary}" if summary and summary != label else "")
                )
                confirm_prop = ClarificationProposal(
                    understanding=understanding,
                    question="Ska jag gå vidare med detta spår eller vill du lägga till exakta värden/ID?",
                    options=(),
                    mode="confirm",
                    original_request=original_request,
                    round_num=round_num,
                    source="fallback",
                )
                return self._start_clarification(
                    chat_id,
                    confirm_prop,
                    render_clarification_message(confirm_prop),
                    original_request=original_request,
                    picks=picks,
                    round_num=round_num,
                    task_id=task_id,
                    image_paths=image_paths,
                )
            return BotReply(
                text=(
                    "Ingen LLM-route är konfigurerad just nu, men din inriktning är registrerad.\n"
                    "Skriv en exakt ändring (t.ex. «sätt order 1001 till completed», "
                    "«ändra produkt 501 pris till 999» eller «ändra sidan 2 titel till Ny rubrik») "
                    "så skapar jag ett bekräftelseförslag."
                )
            )
        return reply

    def _run_llm_chat(
        self,
        chat_id: str | int,
        message: str,
        *,
        on_progress: ProgressCallback | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_stream_clear: Callable[[], None] | None = None,
        on_phase_change: Callable[[dict[str, Any]], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
        task_id: str | None = None,
        clarification_confirmed: bool = False,
        clarification_round: int = 1,
        clarification_context: str = "",
        original_request: str | None = None,
        prior_picks: list[dict[str, Any]] | None = None,
        image_paths: list[str] | tuple[str, ...] | None = None,
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        actor = self._resolve_actor(chat_id)
        conversation_id = f"{self.channel}:{chat_id}"
        wp_context = session.get("wp_ops_context")
        pop_session_keys: list[str] = []
        session_updates: dict[str, Any] = {}
        if "pending_image_paths" in session:
            session.pop("pending_image_paths", None)
            pop_session_keys.append("pending_image_paths")
        if wp_context is not None and not context_matches(
            wp_context, actor=actor, channel=self.channel, conversation_id=conversation_id
        ):
            session.pop("wp_ops_context", None)
            pop_session_keys.append("wp_ops_context")
        kept_hashes, hashes_changed = _prune_review_hashes(session, actor=actor)
        if hashes_changed:
            if kept_hashes:
                session_updates["wp_review_hashes"] = kept_hashes
            else:
                pop_session_keys.append("wp_review_hashes")
        history = list(prev.get("messages") or [])
        prior_digest = str(prev.get("tool_digest") or "")
        chat_kwargs: dict[str, Any] = {
            "history": history,
            "session": session,
            "prior_digest": prior_digest,
            "sticky_order_id": session.get("last_order_id"),
            "sticky_case_id8": session.get("last_case_id8"),
            "actor": actor,
            "channel": self.channel,
            "conversation_id": conversation_id,
            "on_progress": on_progress,
            "on_text_delta": on_text_delta,
            "on_text_reset": on_text_reset,
            "on_checkpoint": on_checkpoint,
        }
        if on_stream_clear is not None and _accepts_kwarg(run_chat, "on_stream_clear"):
            chat_kwargs["on_stream_clear"] = on_stream_clear
        if on_phase_change is not None and _accepts_kwarg(run_chat, "on_phase_change"):
            chat_kwargs["on_phase_change"] = on_phase_change
        if _accepts_kwarg(run_chat, "clarification_confirmed"):
            chat_kwargs["clarification_confirmed"] = bool(clarification_confirmed)
        if _accepts_kwarg(run_chat, "clarification_round"):
            chat_kwargs["clarification_round"] = clarification_round
        if _accepts_kwarg(run_chat, "clarification_context"):
            chat_kwargs["clarification_context"] = clarification_context
        if image_paths and _accepts_kwarg(run_chat, "image_paths"):
            chat_kwargs["image_paths"] = list(image_paths)
        result = run_chat(message, **chat_kwargs)

        review_plan_id = getattr(result, "review_stage_plan_id", None)
        review_plan_row: dict[str, Any] | None = None
        if review_plan_id and _REVIEW_PLAN_ID.fullmatch(review_plan_id):
            try:
                from ecom_ops.wp_ops.review_plan import ReviewPlans

                review_plan_row = ReviewPlans().get(review_plan_id)
                if isinstance(review_plan_row, dict):
                    _with_review_hashes(session, actor=actor, plan=review_plan_row)
                    if isinstance(session.get("wp_review_hashes"), dict):
                        session_updates["wp_review_hashes"] = dict(
                            session["wp_review_hashes"]
                        )
                        if "wp_review_hashes" in pop_session_keys:
                            pop_session_keys.remove("wp_review_hashes")
            except Exception:
                review_plan_row = None

        if result.sticky_order_id:
            session_updates["last_order_id"] = str(result.sticky_order_id)
        if result.sticky_case_id8:
            session_updates["last_case_id8"] = str(result.sticky_case_id8)
        if result.sticky_market:
            session_updates["last_market"] = str(result.sticky_market)
        if result.order_list_shown is not None:
            session_updates["order_list_shown"] = int(result.order_list_shown)
        if result.sticky_product_id:
            session_updates["last_product_id"] = str(result.sticky_product_id)
        if result.sticky_page_id:
            session_updates["last_page_id"] = str(result.sticky_page_id)
        if result.sticky_post_id:
            session_updates["last_post_id"] = str(result.sticky_post_id)
        if result.wp_ops_context is not None:
            session_updates["wp_ops_context"] = result.wp_ops_context
            if "wp_ops_context" in pop_session_keys:
                pop_session_keys.remove("wp_ops_context")
        session.update(session_updates)

        if hasattr(self.store, "append_turn_atomic"):
            if len(result.messages) >= len(history):
                delta_messages = list(result.messages[len(history) :])
            else:
                delta_messages = list(result.messages)
            self.store.append_turn_atomic(
                chat_id,
                new_messages=delta_messages,
                session_updates=session_updates or None,
                pop_session_keys=pop_session_keys,
                tool_digest=result.tool_digest or None,
                clear_tool_digest=bool(result.clear_tool_digest),
            )
        else:
            self._merge_state(
                chat_id,
                flow=None,
                step=None,
                slots={},
                session=session,
                messages=result.messages,
                tool_digest=_stored_tool_digest(result, prior_digest),
            )

        text = result.text
        actions = None
        safe_attachments = (
            safe_workspace_attachments(result.attachments)
            if result.attachments
            else ()
        )
        attachment_kwargs = (
            {"attachments": safe_attachments} if safe_attachments else {}
        )

        if result.offer_escalate:
            if "eskalera" not in text.lower():
                text = f"{text}\n\nVill du eskalera till Oscar?"
            return self._start_escalate_confirm(
                chat_id,
                message,
                task_id=task_id,
                prompt_text=text,
                **attachment_kwargs,
            )

        if result.file_stage_plan_id and _FILE_PLAN_ID.fullmatch(result.file_stage_plan_id):
            plan_id = result.file_stage_plan_id
            return bot_reply(
                text,
                actions=yes_no_actions(
                    yes_data=f"wpfile:stage:{plan_id}",
                    no_data=f"wpfile:no:{plan_id}",
                ),
                needs_typing=True,
                **attachment_kwargs,
            )

        if review_plan_id and _REVIEW_PLAN_ID.fullmatch(review_plan_id):
            site_id = (
                str(review_plan_row.get("site_id") or "")
                if isinstance(review_plan_row, dict)
                else ""
            )
            url = review_plan_url(site_id, review_plan_id) if site_id else None
            return bot_reply(
                text,
                actions=_wpreview_stage_actions(review_plan_id, review_url=url),
                needs_typing=True,
                **attachment_kwargs,
            )

        if result.pending_action is not None:
            try:
                actor_obj = self._actor_obj(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return self._deny_actor()
            return self._start_pending(
                chat_id,
                result.pending_action,
                text,
                actor=actor_obj,
                task_id=task_id,
                **attachment_kwargs,
            )

        clarification = getattr(result, "clarification", None)
        if clarification is not None:
            return self._start_clarification(
                chat_id,
                clarification,
                text,
                original_request=original_request or clarification.original_request or message,
                picks=prior_picks,
                round_num=clarification_round,
                task_id=task_id,
                attachments=safe_attachments,
                image_paths=image_paths,
            )

        if result.soft_escalate_nudge and SOFT_ESCALATE_NUDGE not in text:
            text = f"{text}\n\n{SOFT_ESCALATE_NUDGE}"

        if result.case_id8:
            # Prefer sticky full id from session when prefix matches
            sticky = str(session.get("last_case_id8") or "")
            case_key = (
                sticky
                if sticky.startswith(str(result.case_id8)[:8])
                else str(result.case_id8)
            )
            actions = approve_case_actions(case_key)
        elif result.suggest_case_ids:
            actions = triage_cases_actions(result.suggest_case_ids)

        return bot_reply(
            text, actions=actions, needs_typing=True, attachments=safe_attachments
        )

    def _handle_wpreview_callback(
        self,
        chat_id: str | int,
        actor: str,
        raw: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> BotReply:
        if raw == "wpreview:no" or raw.startswith("wpreview:no:"):
            plan_id = raw.split(":", 2)[2] if raw.startswith("wpreview:no:") else ""
            if plan_id:
                if not _REVIEW_PLAN_ID.fullmatch(plan_id):
                    return BotReply(
                        text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
                    )
                prev = self.store.get(chat_id) or {}
                session = dict(prev.get("session") or {})
                if _drop_review_hash(session, plan_id):
                    self._persist_review_hashes(chat_id, session)
                return BotReply(
                    text=f"Avbrutet ({plan_id}). Ingen ändring skrevs till staging eller produktion."
                )
            return BotReply(
                text="Avbrutet. Ingen ändring skrevs till staging eller produktion."
            )

        plan_id = raw.split(":", 2)[2]
        if not _REVIEW_PLAN_ID.fullmatch(plan_id):
            return BotReply(
                text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
            )
        blocked = self._mutations_blocked_reply()
        if blocked:
            return blocked
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_ADMIN)
            require_permission(actor_obj, Permission.CODE_EDIT)
        except Exception:
            return BotReply(
                text="Staging av granskningsplan kräver Oscar (WP_OPS_ADMIN och CODE_EDIT). Ingen ändring utfördes."
            )

        from ecom_ops.wp_ops.review_plan import ReviewPlans

        plans = ReviewPlans()
        plan = plans.get(plan_id)
        if plan is None:
            return BotReply(
                text=f"Staging skrevs inte (UnknownPlan). Granskningsplanen {plan_id} hittades inte."
            )
        site_id = str(plan.get("site_id") or "")
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        cached = (session.get("wp_review_hashes") or {}).get(plan_id)
        if (
            isinstance(cached, dict)
            and cached.get("actor") == actor
            and cached.get("site_id") == site_id
        ):
            base_sha = str(cached.get("base_sha256") or "")
            work_sha = str(cached.get("work_sha256") or "")
        else:
            base_sha = str(plan.get("base_sha256") or "")
            work_sha = str(plan.get("work_sha256") or "")

        prov = _provenance()
        step_id = f"wpreview:{plan_id}"
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="wpreview_stage",
                phase="started",
                label="Staging-granskningsplan",
                detail="Skriver godkänd granskningsplan i staging",
                provenance=prov,
                legacy_text="Skriver godkänd granskningsplan i staging...",
            ),
        )
        t0 = time.monotonic()
        try:
            ws_status = plans.workspace_status(site_id, plan_id, actor=actor)
            changed_files = (
                list((ws_status.data or {}).get("changed_files") or [])
                if ws_status.ok
                else []
            )
            staged = plans.approve_staging(
                site_id,
                plan_id,
                actor=actor,
                base_sha256=base_sha,
                work_sha256=work_sha,
            )
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wpreview_stage",
                    phase="failed",
                    label="Staging-granskningsplan",
                    outcome="misslyckades",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text="Staging-granskningsplan: misslyckades.",
                ),
            )
            raise
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if staged.ok and (staged.data or {}).get("executed") is True:
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wpreview_stage",
                    phase="completed",
                    label="Staging-granskningsplan",
                    outcome="ändringar skrivna i staging",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text="Staging-granskningsplan: klar.",
                ),
            )
            if _drop_review_hash(session, plan_id):
                self._persist_review_hashes(chat_id, session)
            lines = [
                f"Ändringarna för {plan_id} är skrivna i staging ({site_id}, mock). Produktionen är oförändrad."
            ]
            file_lines = _format_changed_files(changed_files)
            if file_lines:
                lines.append("Ändrade filer:")
                lines.extend(file_lines)
            url = review_plan_url(site_id, plan_id)
            if url:
                lines.append(f"Granskning: {url} (kräver inloggning)")
            else:
                lines.append(f"Granskningssida: /wp/{site_id}/reviews/{plan_id} (kräver inloggning)")
            return BotReply(text="\n".join(lines))

        error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool="wpreview_stage",
                phase="failed",
                label="Staging-granskningsplan",
                outcome=f"skrevs inte ({error})",
                elapsed_ms=elapsed_ms,
                provenance=prov,
                legacy_text=f"Staging-granskningsplan: misslyckades ({error}).",
            ),
        )
        detail = str(staged.message or "").strip()
        msg = f"Staging skrevs inte ({error}). Ingen ändring utfördes."
        if detail and detail != error:
            msg = f"{msg}\n{detail}"
        return BotReply(text=msg)

    def _wp_plans_command(self, actor: str, parts: list[str]) -> BotReply:
        from ecom_ops.wp_ops.review_plan import ReviewPlans

        site_id = parts[1] if len(parts) == 2 else None
        plans = ReviewPlans()
        res = plans.list_plans(site_id, actor=actor)
        if not res.ok:
            err = str((res.data or {}).get("error_class") or "")
            if err == "AccessDenied":
                return BotReply(text="Du saknar rättighet att läsa granskningsplaner.")
            if err == "UnknownSite":
                return BotReply(text=f"Okänd WordPress-sajt: {site_id}.")
            return BotReply(text=f"Kunde inte lista granskningsplaner ({err or 'okänt fel'}).")

        rows = list((res.data or {}).get("plans") or [])
        if not rows:
            scope = f" för {site_id}" if site_id else ""
            return BotReply(text=f"Inga öppna granskningsarbetsytor (wr-*){scope}.")

        can_see_summary = False
        try:
            actor_obj = resolve_actor(actor)
            can_see_summary = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(
                Permission.CODE_EDIT
            )
        except Exception:
            can_see_summary = False

        header = f"Granskningsarbetsytor ({site_id}):" if site_id else "Granskningsarbetsytor (senaste):"
        lines = [header]
        for item in rows[:10]:
            pid = str(item.get("plan_id") or "")
            psite = str(item.get("site_id") or "")
            target = f"{item.get('kind')}:{item.get('slug')}"
            status = str(item.get("status") or "unknown")
            _ready, ready_label = _review_plan_ready(plans, item)
            title = ""
            if can_see_summary:
                raw_row = plans.get(pid, site_id=psite)
                raw_summary = str((raw_row or {}).get("summary") or "").strip()
                if raw_summary:
                    title = f" · {raw_summary[:40]}"
            lines.append(f"- {pid} | {psite} | {target} | {status} | {ready_label}{title}")
        if len(rows) > 10:
            lines.append(f"... och {len(rows) - 10} fler.")
        lines.append("Detaljer: /wp plan <plan-id>")
        return BotReply(text="\n".join(lines))

    def _wp_plan_command(
        self, chat_id: str | int, actor: str, parts: list[str]
    ) -> BotReply:
        plan_id = parts[1].strip()
        if not _REVIEW_PLAN_ID.fullmatch(plan_id):
            return BotReply(text="Ange ett giltigt plan-id (wr-xxxxxxxxxxxx).")
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_READ)
        except Exception:
            return BotReply(text="Du saknar rättighet att läsa granskningsplaner.")

        from ecom_ops.wp_ops.review_plan import ReviewPlans

        plans = ReviewPlans()
        raw_plan = plans.get(plan_id)
        if raw_plan is None:
            return BotReply(text=f"Okänd granskningsplan: {plan_id}.")
        site_id = str(raw_plan.get("site_id") or "")
        shown = plans.show(site_id, plan_id, actor=actor, include_diff=True)
        if not shown.ok:
            err = str((shown.data or {}).get("error_class") or "")
            if err == "AccessDenied":
                return BotReply(text="Du saknar rättighet att läsa granskningsplanen.")
            if err == "Expired":
                return BotReply(text=f"Granskningsplan {plan_id} har gått ut.")
            return BotReply(text=f"Kunde inte visa plan {plan_id} ({err or 'okänt fel'}).")

        view = dict((shown.data or {}).get("plan") or {})
        ws_status = plans.workspace_status(site_id, plan_id, actor=actor)
        changed_files = (
            list((ws_status.data or {}).get("changed_files") or [])
            if ws_status.ok
            else []
        )
        ready, ready_label = _review_plan_ready(
            plans, raw_plan, changed_count=len(changed_files)
        )
        can_stage = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(
            Permission.CODE_EDIT
        )
        artifacts = (
            plans.list_artifacts(site_id, plan_id, actor=actor) if can_stage else []
        )
        url = review_plan_url(site_id, plan_id)

        lines = [
            f"Granskningsplan {plan_id}",
            f"Sajt: {site_id} ({view.get('environment')}) · {view.get('kind')}: {view.get('slug')}",
            f"Status: {view.get('status')} · Risk: {view.get('risk')} · Staging: {ready_label}",
        ]
        summary = str(view.get("summary") or "").strip()
        if summary:
            lines.append(f"Sammanfattning: {summary[:200]}")

        if changed_files:
            lines.append(f"Ändrade filer ({len(changed_files)}):")
            lines.extend(_format_changed_files(changed_files))
        else:
            lines.append("Ändrade filer: inga ännu.")

        check_lines = _format_check_rows(list(view.get("checks") or []))
        if check_lines:
            lines.append("Kontroller:")
            lines.extend(check_lines)
        check_run = view.get("check_run")
        if isinstance(check_run, dict) and check_run.get("run_id"):
            pub = "ja" if check_run.get("published") is True else "nej"
            lines.append(f"Check-run: {check_run.get('run_id')} (publicerad: {pub})")

        sandbox = view.get("sandbox") if isinstance(view.get("sandbox"), dict) else {}
        if sandbox:
            lines.append(
                f"Sandbox: {sandbox.get('state', 'stopped')} ({sandbox.get('engine', 'unavailable')})"
            )
        if artifacts:
            lines.append(f"Artifakter: {', '.join(artifacts[:6])}")
        if url:
            lines.append(f"Granskningslänk: {url} (kräver inloggning)")
        else:
            lines.append(f"Granskningssida: /wp/{site_id}/reviews/{plan_id} (kräver inloggning)")
        lines.append("Produktion deployas aldrig från chatten.")

        if ready and can_stage:
            prev = self.store.get(chat_id) or {}
            session = dict(prev.get("session") or {})
            _with_review_hashes(session, actor=actor, plan=raw_plan)
            self._persist_review_hashes(chat_id, session)
            return bot_reply(
                "\n".join(lines),
                actions=_wpreview_stage_actions(plan_id, review_url=url),
            )
        if ready and not can_stage:
            lines.append("Staging kräver Oscar (WP_OPS_ADMIN + CODE_EDIT).")
        return BotReply(text="\n".join(lines))

    def _wp_context_command(
        self,
        chat_id: str | int,
        text: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> BotReply | None:
        """Minimal user controls for the sticky, actor-scoped WordPress site."""
        match = re.fullmatch(r"/wp(?:@\w+)?(?:\s+(.*))?", text.strip(), re.I)
        if not match:
            return None
        parts = (match.group(1) or "").strip().split()
        action = parts[0].lower() if parts else "help"
        if action == "clear" and len(parts) == 1:
            self._update_session_atomic(
                chat_id,
                pop_session_keys=("wp_ops_context", "wp_review_hashes"),
            )
            return BotReply(text="WordPress-sajtkontexten är rensad.")
        if action == "help" and len(parts) == 1:
            return BotReply(
                text="/wp sites · /wp use <site-id> · /wp plans [site-id] · /wp plan <plan-id> · /wp clear"
            )
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        if action == "plans" and len(parts) in {1, 2}:
            return self._wp_plans_command(actor, parts)
        if action == "plan" and len(parts) == 2:
            return self._wp_plan_command(chat_id, actor, parts)
        tools = WpAgentTools()
        prov = _provenance()
        if action == "sites" and len(parts) == 1:
            step_id = "cmd:wp:sites"
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wp_list_sites",
                    phase="started",
                    label="WordPress-sajter",
                    detail="Hämtar registrerade sajter",
                    provenance=prov,
                    legacy_text="Hämtar WordPress-sajter...",
                ),
            )
            t0 = time.monotonic()
            output = tools.execute(
                "wp_list_sites",
                {},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if output.data.get("ok") is not True:
                emit_progress(
                    on_progress,
                    ProgressEvent(
                        step_id=step_id,
                        tool="wp_list_sites",
                        phase="failed",
                        label="WordPress-sajter",
                        outcome="saknar behörighet",
                        elapsed_ms=elapsed_ms,
                        provenance=prov,
                        legacy_text="WordPress-sajter: saknar behörighet.",
                    ),
                )
                return BotReply(text="Du saknar rättighet att läsa WordPress-sajter.")
            sites = output.data.get("sites", [])
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wp_list_sites",
                    phase="completed",
                    label="WordPress-sajter",
                    outcome=f"{len(sites)} sajter",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"WordPress-sajter: {len(sites)} sajter.",
                ),
            )
            if not sites:
                return BotReply(text="Inga registrerade WordPress-sajter.")
            lines = ["Registrerade WordPress-sajter:"]
            for row in sites[:20]:
                lines.append(
                    f"- {row.get('site_id')} · {row.get('name')} · "
                    f"{row.get('environment')} · status {row.get('status')}"
                )
            return BotReply(text="\n".join(lines))
        if action == "use" and len(parts) == 2:
            site_id = parts[1]
            step_id = f"cmd:wp:use:{site_id}"
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wp_site_status",
                    phase="started",
                    label="WordPress-sajt",
                    detail=f"Kontrollerar {site_id}",
                    provenance=prov,
                    legacy_text=f"Kontrollerar WordPress-sajt {site_id}...",
                ),
            )
            t0 = time.monotonic()
            output = tools.execute(
                "wp_site_status",
                {"site_id": site_id},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if output.data.get("ok") is not True:
                emit_progress(
                    on_progress,
                    ProgressEvent(
                        step_id=step_id,
                        tool="wp_site_status",
                        phase="failed",
                        label="WordPress-sajt",
                        outcome="okänd sajt eller saknar behörighet",
                        elapsed_ms=elapsed_ms,
                        provenance=prov,
                        legacy_text="WordPress-sajt: okänd eller saknar behörighet.",
                    ),
                )
                return BotReply(text="Sajten är okänd eller du saknar rättighet att läsa den.")
            context = context_for_storage(
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
                site_id=output.site_id,
                operation_ids=output.operation_ids,
            )
            if context is None:
                emit_progress(
                    on_progress,
                    ProgressEvent(
                        step_id=step_id,
                        tool="wp_site_status",
                        phase="failed",
                        label="WordPress-sajt",
                        outcome="kunde inte väljas",
                        elapsed_ms=elapsed_ms,
                        provenance=prov,
                        legacy_text="WordPress-sajt: kunde inte väljas.",
                    ),
                )
                return BotReply(text="WordPress-sajten kunde inte väljas.")
            self._update_session_atomic(
                chat_id,
                session_updates={"wp_ops_context": context},
            )
            emit_progress(
                on_progress,
                ProgressEvent(
                    step_id=step_id,
                    tool="wp_site_status",
                    phase="completed",
                    label="WordPress-sajt",
                    outcome=f"aktiv sajt: {site_id}",
                    elapsed_ms=elapsed_ms,
                    provenance=prov,
                    legacy_text=f"Aktiv WordPress-sajt: {site_id}.",
                ),
            )
            return BotReply(text=f"Aktiv WordPress-sajt: {site_id}. Fråga om status, backup eller uppdateringar.")
        return BotReply(
            text="Använd /wp sites, /wp use <site-id>, /wp plans [site-id], /wp plan <plan-id> eller /wp clear."
        )
