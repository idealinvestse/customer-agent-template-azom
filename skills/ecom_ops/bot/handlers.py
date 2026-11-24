"""Telegram message handlers — OpenClaw-style multi-turn + write rails."""

from __future__ import annotations

import re
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
    ) -> None:
        self.store = store or ConversationStore()
        self.escalation = escalation or default_escalation
        self.channel = (channel or "telegram").strip().lower()

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

    def handle(
        self,
        chat_id: str | int,
        text: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
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

        wp_command = self._wp_context_command(chat_id, raw)
        if wp_command is not None:
            return wp_command

        # OpenClaw slash commands first
        oc = dispatch_openclaw_command(
            chat_id, raw, self.store, channel=self.channel
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
                    text=tool_lookup_order(oid, domain=self._session_market(chat_id))
                )
            return as_reply(oc)

        # Continue multi-turn flows (confirm writes, escalate, order id prompt)
        state = self.store.get(chat_id)
        if state and state.get("flow"):
            return as_reply(self._continue_flow(chat_id, raw, state))

        # Ultra-short order shortcut
        fast = ORDER_FAST_RE.match(raw)
        if fast:
            self._touch_session_order(chat_id, fast.group(1))
            return BotReply(
                text=tool_lookup_order(
                    fast.group(1), domain=self._session_market(chat_id)
                )
            )

        if wants_hard_escalate_confirm(raw):
            return self._start_escalate_confirm(chat_id, raw)

        return self._run_llm_chat(
            chat_id,
            raw,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_checkpoint=on_checkpoint,
        )

    def handle_callback(self, chat_id: str | int, data: str) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (data or "").strip()
        if raw == "escalate:yes":
            return as_reply(self._confirm_escalate(chat_id, yes=True))
        if raw == "escalate:no":
            return as_reply(self._confirm_escalate(chat_id, yes=False))
        if raw == "cases:list":
            return as_reply(
                dispatch_openclaw_command(
                    chat_id, "/cases list", self.store, channel=self.channel
                )
                or with_recovery("Kunde inte lista ärenden.")
            )
        if raw.startswith("cases:show:"):
            id8 = raw.split(":", 2)[2].strip()
            self._touch_session_case(chat_id, id8)
            return as_reply(
                dispatch_openclaw_command(
                    chat_id, f"/cases show {id8}", self.store, channel=self.channel
                )
                or with_recovery(f"Hittade inte {id8}.")
            )
        if raw.startswith("cases:approve:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            id8 = raw.split(":", 2)[2].strip()
            return as_reply(
                dispatch_openclaw_command(
                    chat_id,
                    f"/cases approve {id8}",
                    self.store,
                    channel=self.channel,
                )
                or with_recovery("Kunde inte godkänna.")
            )
        if raw.startswith("cases:regen:"):
            id8 = raw.split(":", 2)[2].strip()
            return self._exec_pending(
                chat_id,
                PendingAction(kind="case_regenerate", payload={"case_id": id8}),
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

            staged = WpOpsService().stage_file_plan(plan_id, actor=actor)
            if staged.ok and (staged.data or {}).get("executed") is True:
                return BotReply(text="Filen är skriven i staging. Produktionen är oförändrad.")
            error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
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
            return self._confirm_pending_text(chat_id, yes=True)
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

    def _clear_pending(self, chat_id: str | int) -> None:
        prev = self.store.get(chat_id) or {}
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
        self, chat_id: str | int, text: str, state: dict[str, Any]
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
            return tool_lookup_order(oid, domain=self._session_market(chat_id))

        if flow == "escalate_confirm" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j"}:
                return self._confirm_escalate(chat_id, yes=True)
            if answer in {"nej", "no", "n"}:
                return self._confirm_escalate(chat_id, yes=False)
            if not wants_escalate(text) and len(text) > 12:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(chat_id, text)
            return bot_reply(
                "Svara ja eller nej (eller tryck knappen), eller /stop.",
                actions=yes_no_actions(
                    yes_data="escalate:yes", no_data="escalate:no"
                ),
            )

        if flow == "pending_action" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j", "ok", "kör", "bekrafta", "bekräfta"}:
                return self._confirm_pending_text(chat_id, yes=True)
            if answer in {"nej", "no", "n", "avbryt", "cancel"}:
                return self._confirm_pending_text(chat_id, yes=False)
            # Free text mid-confirm → drop pending and chat
            if len(text) > 8:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(chat_id, text)
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
        return self._run_llm_chat(chat_id, text)

    def _start_escalate_confirm(self, chat_id: str | int, message: str) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        self._merge_state(
            chat_id,
            flow="escalate_confirm",
            step="confirm",
            slots={"message": message[:500]},
            session=session,
        )
        return bot_reply(
            (
                "Eskalera till Oscar?\n\n"
                f"Meddelande: {message[:300]}\n\n"
                "Bekräfta med Ja/Nej."
            ),
            actions=yes_no_actions(yes_data="escalate:yes", no_data="escalate:no"),
        )

    def _confirm_escalate(self, chat_id: str | int, *, yes: bool) -> str:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        session = dict(state.get("session") or {})
        if yes:
            try:
                actor = self._resolve_actor(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return (
                    "Din chat saknar actor-mapping. "
                    "Be Oscar uppdatera actor-map för kanalen."
                )
            ticket = self.escalation.escalate_critical(
                f"{self.channel} escalation by {actor}",
                details={
                    "channel": self.channel,
                    "chat_id": str(chat_id),
                    "actor": actor,
                    "message": slots.get("message", "")[:500],
                    "draft": (slots.get("reply") or "")[:500],
                },
            )
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            return f"Eskalerat till Oscar. Ticket: {ticket.id}"
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
        self, chat_id: str | int, pending: PendingAction, text: str, *, actor: Actor
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

    def _confirm_pending_text(self, chat_id: str | int, *, yes: bool) -> BotReply:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        pending = PendingAction.from_dict(slots.get("pending"))
        if not yes or not pending:
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen ändring utförd.")
        return self._exec_pending(chat_id, pending)

    def _exec_pending(self, chat_id: str | int, pending: PendingAction) -> BotReply:
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            self._clear_pending(chat_id)
            return self._deny_actor()
        try:
            actor_obj = resolve_actor(actor)
        except Exception:
            self._clear_pending(chat_id)
            return self._deny_actor()
        if not self._pending_allowed(actor_obj, pending):
            self._clear_pending(chat_id)
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
        ok = False
        msg = "Okänd action"
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
        self._clear_pending(chat_id)
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
        on_progress: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        actor = self._resolve_actor(chat_id)
        conversation_id = f"{self.channel}:{chat_id}"
        wp_context = session.get("wp_ops_context")
        if not context_matches(
            wp_context, actor=actor, channel=self.channel, conversation_id=conversation_id
        ):
            session.pop("wp_ops_context", None)
        history = list(prev.get("messages") or [])
        prior_digest = str(prev.get("tool_digest") or "")
        result = run_chat(
            message,
            history=history,
            session=session,
            prior_digest=prior_digest,
            sticky_order_id=session.get("last_order_id"),
            sticky_case_id8=session.get("last_case_id8"),
            actor=actor,
            channel=self.channel,
            conversation_id=conversation_id,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_checkpoint=on_checkpoint,
        )
        if result.sticky_order_id:
            session["last_order_id"] = str(result.sticky_order_id)
        if result.sticky_case_id8:
            session["last_case_id8"] = str(result.sticky_case_id8)
        if result.sticky_market:
            session["last_market"] = str(result.sticky_market)
        if result.order_list_shown is not None:
            session["order_list_shown"] = int(result.order_list_shown)
        if result.sticky_product_id:
            session["last_product_id"] = str(result.sticky_product_id)
        if result.sticky_page_id:
            session["last_page_id"] = str(result.sticky_page_id)
        if result.sticky_post_id:
            session["last_post_id"] = str(result.sticky_post_id)
        if result.wp_ops_context is not None:
            session["wp_ops_context"] = result.wp_ops_context

        self._merge_state(
            chat_id,
            flow=None,
            step=None,
            slots={},
            session=session,
            messages=result.messages,
            tool_digest=result.tool_digest or prior_digest,
        )

        text = result.text
        actions = None

        if result.offer_escalate:
            self._merge_state(
                chat_id,
                flow="escalate_confirm",
                step="confirm",
                slots={"message": message[:500]},
                session=session,
                messages=result.messages,
                tool_digest=result.tool_digest or prior_digest,
            )
            if "eskalera" not in text.lower():
                text = f"{text}\n\nVill du eskalera till Oscar?"
            return bot_reply(
                text,
                actions=yes_no_actions(
                    yes_data="escalate:yes", no_data="escalate:no"
                ),
                needs_typing=True,
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
                chat_id, result.pending_action, text, actor=actor_obj
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

    def _wp_context_command(self, chat_id: str | int, text: str) -> BotReply | None:
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
        if action == "sites" and len(parts) == 1:
            output = tools.execute(
                "wp_list_sites",
                {},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            if output.data.get("ok") is not True:
                return BotReply(text="Du saknar rättighet att läsa WordPress-sajter.")
            sites = output.data.get("sites", [])
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
            output = tools.execute(
                "wp_site_status",
                {"site_id": site_id},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            if output.data.get("ok") is not True:
                return BotReply(text="Sajten är okänd eller du saknar rättighet att läsa den.")
            context = context_for_storage(
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
                site_id=output.site_id,
                operation_ids=output.operation_ids,
            )
            if context is None:
                return BotReply(text="WordPress-sajten kunde inte väljas.")
            session["wp_ops_context"] = context
            self._merge_state(
                chat_id,
                session=session,
                messages=state.get("messages"),
                tool_digest=state.get("tool_digest"),
            )
            return BotReply(text=f"Aktiv WordPress-sajt: {site_id}. Fråga om status, backup eller uppdateringar.")
        return BotReply(text="Använd /wp sites, /wp use <site-id> eller /wp clear.")
