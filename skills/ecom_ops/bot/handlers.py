"""Telegram message handlers — OpenClaw-style multi-turn + write rails."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ecom_ops.bot.actors import (
    ChannelActorDenied,
    TelegramActorDenied,
    channel_peer_allowed,
    messenger_mutations_allowed,
    resolve_channel_actor,
)
from ecom_ops.bot.catalog import execute_page_update, execute_product_update
from ecom_ops.bot.chat_agent import (
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
    BotReply,
    approve_case_actions,
    as_reply,
    bot_reply,
    order_status_confirm_actions,
    product_desc_confirm_actions,
    proposal_confirm_actions,
    triage_cases_actions,
    yes_no_actions,
)
from ecom_ops.bot.site_content import (
    execute_plugin_status,
    execute_post_update,
    execute_site_update,
)
from ecom_ops.bot.store import ConversationStore, clamp_messages
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.rbac import Actor, Permission, resolve_actor
from ecom_ops.security import validate_order_id
from ecom_ops.wp_ops.agent_tools import WpAgentTools, context_for_storage, context_matches

ORDER_FAST_RE = re.compile(r"^\s*(?:order|ordernr|#)\s*(\d{4,12})\s*$", re.I)
_PROPOSAL_YES_RE = re.compile(r"^proposal:yes:(pr-[0-9a-f]{12})$", re.I)
_PROPOSAL_NO_RE = re.compile(r"^proposal:no:(pr-[0-9a-f]{12})$", re.I)

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
    ) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (text or "").strip()
        if not raw:
            return BotReply(text="Skriv /help eller /commands — eller bara fråga.")

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

        # Continue multi-turn flows (confirm writes, escalate, order id prompt)
        state = self.store.get(chat_id)
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
                )
            )

        # Ultra-short order shortcut
        fast = ORDER_FAST_RE.match(raw)
        if fast:
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
        )

    def handle_callback(
        self,
        chat_id: str | int,
        data: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (data or "").strip()

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
                if not re.fullmatch(r"fe-[a-f0-9]{12}", plan_id):
                    return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
                from ecom_ops.wp_ops import WpOpsService

                WpOpsService().deny_file_plan(plan_id, actor=actor)
            return BotReply(text="Avbrutet. Ingen fil ändrades.")
        if raw.startswith("wpfile:stage:"):
            plan_id = raw.split(":", 2)[2]
            if not re.fullmatch(r"fe-[a-f0-9]{12}", plan_id):
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
        return BotReply(
            text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
        )

    def _touch_session_order(self, chat_id: str | int, order_id: str) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        session["last_order_id"] = str(order_id)
        self._merge_state(chat_id, session=session)

    def _clear_mismatched_wp_context(self, chat_id: str | int, actor: str) -> None:
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        context = session.get("wp_ops_context")
        if context is None:
            return
        if context_matches(
            context,
            actor=actor,
            channel=self.channel,
            conversation_id=f"{self.channel}:{chat_id}",
        ):
            return
        session.pop("wp_ops_context", None)
        self._merge_state(
            chat_id,
            session=session,
            messages=state.get("messages"),
            tool_digest=state.get("tool_digest"),
        )

    def _touch_session_case(self, chat_id: str | int, case_id8: str) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        session["last_case_id8"] = str(case_id8)
        self._merge_state(chat_id, session=session)

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
    ) -> str | BotReply:
        flow = state.get("flow")
        step = state.get("step")
        session = dict(state.get("session") or {})
        slots = dict(state.get("slots") or {})

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
        )

    def _start_escalate_confirm(
        self,
        chat_id: str | int,
        message: str,
        *,
        task_id: str | None = None,
        prompt_text: str | None = None,
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
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
    ) -> BotReply:
        if not self._pending_allowed(actor, pending):
            return BotReply(text=f"{text}\n\n{self._deny_pending_reply(pending)}")
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
                                prev = self.store.get(chat_id) or {}
                                session = dict(prev.get("session") or {})
                                session["last_market"] = market
                                self._merge_state(chat_id, session=session)
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
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        actor = self._resolve_actor(chat_id)
        conversation_id = f"{self.channel}:{chat_id}"
        wp_context = session.get("wp_ops_context")
        pop_session_keys: list[str] = []
        if wp_context is not None and not context_matches(
            wp_context, actor=actor, channel=self.channel, conversation_id=conversation_id
        ):
            session.pop("wp_ops_context", None)
            pop_session_keys.append("wp_ops_context")
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
        result = run_chat(message, **chat_kwargs)

        session_updates: dict[str, Any] = {}
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

        if result.offer_escalate:
            if "eskalera" not in text.lower():
                text = f"{text}\n\nVill du eskalera till Oscar?"
            return self._start_escalate_confirm(
                chat_id,
                message,
                task_id=task_id,
                prompt_text=text,
            )

        if result.file_stage_plan_id and re.fullmatch(r"fe-[a-f0-9]{12}", result.file_stage_plan_id):
            plan_id = result.file_stage_plan_id
            return bot_reply(
                text,
                actions=yes_no_actions(
                    yes_data=f"wpfile:stage:{plan_id}",
                    no_data=f"wpfile:no:{plan_id}",
                ),
                needs_typing=True,
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

        return bot_reply(text, actions=actions, needs_typing=True)

    def _wp_context_command(
        self,
        chat_id: str | int,
        text: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> BotReply | None:
        """Minimal user controls for the sticky, actor-scoped WordPress site."""
        match = re.fullmatch(r"/wp(?:\s+(.*))?", text.strip(), re.I)
        if not match:
            return None
        parts = (match.group(1) or "").strip().split()
        action = parts[0].lower() if parts else "help"
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        if action == "clear" and len(parts) == 1:
            session.pop("wp_ops_context", None)
            self._merge_state(
                chat_id,
                session=session,
                messages=state.get("messages"),
                tool_digest=state.get("tool_digest"),
            )
            return BotReply(text="WordPress-sajtkontexten är rensad.")
        if action == "help" and len(parts) == 1:
            return BotReply(text="/wp sites · /wp use <site-id> · /wp clear")
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
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
            session["wp_ops_context"] = context
            self._merge_state(
                chat_id,
                session=session,
                messages=state.get("messages"),
                tool_digest=state.get("tool_digest"),
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
        return BotReply(text="Använd /wp sites, /wp use <site-id> eller /wp clear.")
