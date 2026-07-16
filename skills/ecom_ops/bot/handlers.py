"""Telegram message handlers — OpenClaw-style multi-turn + write rails."""

from __future__ import annotations

import os
import re
from typing import Any

from ecom_ops.bot.actors import TelegramActorDenied, resolve_telegram_actor
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
from ecom_ops.bot.reply import (
    BotReply,
    approve_case_keyboard,
    as_reply,
    order_status_confirm_keyboard,
    product_desc_confirm_keyboard,
    triage_cases_keyboard,
    yes_no_keyboard,
)
from ecom_ops.bot.store import ConversationStore, clamp_messages
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.security import validate_order_id

ORDER_FAST_RE = re.compile(r"^\s*(?:order|ordernr|#)\s*(\d{4,12})\s*$", re.I)


def telegram_chat_allowed(chat_id: str | int) -> bool:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return True
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(chat_id) in allowed


class BotHandler:
    """OpenClaw-compatible commands + hybrid chat + site write confirms."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.store = store or ConversationStore()
        self.escalation = escalation or default_escalation

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

    def handle(self, chat_id: str | int, text: str) -> BotReply:
        if not telegram_chat_allowed(chat_id):
            return BotReply(
                text=(
                    "Du är inte behörig att använda denna bot. "
                    "Be Oscar lägga till din chat-id i TELEGRAM_ALLOWED_CHAT_IDS."
                )
            )
        try:
            resolve_telegram_actor(chat_id)
        except TelegramActorDenied:
            return BotReply(
                text=(
                    "Din chat saknar actor-mapping. "
                    "Be Oscar lägga till dig i TELEGRAM_ACTOR_MAP "
                    "(t.ex. <chat_id>:jonatan)."
                )
            )
        raw = (text or "").strip()
        if not raw:
            return BotReply(text="Skriv /help eller /commands — eller bara fråga.")

        # OpenClaw slash commands first
        oc = dispatch_openclaw_command(chat_id, raw, self.store)
        if oc is not None:
            if oc == "__ORDER_PROMPT__":
                self._merge_state(
                    chat_id, flow="order_lookup", step="await_id", slots={}
                )
                return BotReply(text="Ange ordernummer (t.ex. 1001):")
            if isinstance(oc, str) and oc.startswith("__ORDER__:"):
                oid = oc.split(":", 1)[1]
                self._touch_session_order(chat_id, oid)
                return BotReply(text=tool_lookup_order(oid))
            return as_reply(oc)

        # Continue multi-turn flows (confirm writes, escalate, order id prompt)
        state = self.store.get(chat_id)
        if state and state.get("flow"):
            return as_reply(self._continue_flow(chat_id, raw, state))

        # Ultra-short order shortcut
        fast = ORDER_FAST_RE.match(raw)
        if fast:
            self._touch_session_order(chat_id, fast.group(1))
            return BotReply(text=tool_lookup_order(fast.group(1)))

        if wants_hard_escalate_confirm(raw):
            return self._start_escalate_confirm(chat_id, raw)

        return self._run_llm_chat(chat_id, raw)

    def handle_callback(self, chat_id: str | int, data: str) -> BotReply:
        if not telegram_chat_allowed(chat_id):
            return BotReply(text="Inte behörig.")
        raw = (data or "").strip()
        if raw == "escalate:yes":
            return as_reply(self._confirm_escalate(chat_id, yes=True))
        if raw == "escalate:no":
            return as_reply(self._confirm_escalate(chat_id, yes=False))
        if raw.startswith("cases:show:"):
            id8 = raw.split(":", 2)[2].strip()
            self._touch_session_case(chat_id, id8)
            return as_reply(
                dispatch_openclaw_command(chat_id, f"/cases show {id8}", self.store)
                or f"Hittade inte {id8}."
            )
        if raw.startswith("cases:approve:"):
            id8 = raw.split(":", 2)[2].strip()
            return as_reply(
                dispatch_openclaw_command(
                    chat_id, f"/cases approve {id8}", self.store
                )
                or "Kunde inte godkänna."
            )
        if raw.startswith("cases:regen:"):
            id8 = raw.split(":", 2)[2].strip()
            return self._exec_pending(
                chat_id,
                PendingAction(kind="case_regenerate", payload={"case_id": id8}),
            )
        if raw.startswith("order:set:"):
            # order:set:{oid}:{status}
            parts = raw.split(":")
            if len(parts) >= 4:
                oid, status = parts[2], parts[3]
                return self._exec_pending(
                    chat_id,
                    PendingAction(
                        kind="order_status",
                        payload={"order_id": oid, "status": status},
                    ),
                )
        if raw == "order:cancel":
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen orderstatus ändrad.")
        if raw.startswith("product:desc:"):
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
        if raw == "action:yes":
            return self._confirm_pending_text(chat_id, yes=True)
        if raw == "action:no":
            return self._confirm_pending_text(chat_id, yes=False)
        return BotReply(text="Okänd knapp. /help")

    def _touch_session_order(self, chat_id: str | int, order_id: str) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        session["last_order_id"] = str(order_id)
        self._merge_state(chat_id, session=session)

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
            return tool_lookup_order(oid)

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
            return BotReply(
                text="Svara ja eller nej (eller tryck knappen), eller /stop.",
                reply_markup=yes_no_keyboard(
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
            markup = self._markup_for_pending(pending)
            return BotReply(
                text="Svara ja/nej eller tryck knappen för att bekräfta ändringen.",
                reply_markup=markup,
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
        return BotReply(
            text=(
                "Eskalera till Oscar?\n\n"
                f"Meddelande: {message[:300]}\n\n"
                "Bekräfta med Ja/Nej."
            ),
            reply_markup=yes_no_keyboard(
                yes_data="escalate:yes", no_data="escalate:no"
            ),
        )

    def _confirm_escalate(self, chat_id: str | int, *, yes: bool) -> str:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        session = dict(state.get("session") or {})
        if yes:
            try:
                actor = resolve_telegram_actor(chat_id)
            except TelegramActorDenied:
                return (
                    "Din chat saknar actor-mapping. "
                    "Be Oscar uppdatera TELEGRAM_ACTOR_MAP."
                )
            ticket = self.escalation.escalate_critical(
                f"Telegram escalation by {actor}",
                details={
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

    def _markup_for_pending(
        self, pending: PendingAction | None
    ) -> dict[str, Any] | None:
        if not pending:
            return yes_no_keyboard(yes_data="action:yes", no_data="action:no")
        if pending.kind == "order_status":
            return order_status_confirm_keyboard(
                pending.payload.get("order_id", ""),
                pending.payload.get("status", ""),
            )
        if pending.kind == "product_desc":
            return product_desc_confirm_keyboard(
                pending.payload.get("product_id") or "0",
                publish=bool(pending.payload.get("publish")),
            )
        if pending.kind == "case_regenerate":
            id8 = str(pending.payload.get("case_id") or "")[:8]
            return yes_no_keyboard(
                yes_data=f"cases:regen:{id8}",
                no_data="action:no",
            )
        return yes_no_keyboard(yes_data="action:yes", no_data="action:no")

    def _start_pending(self, chat_id: str | int, pending: PendingAction, text: str) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        if pending.kind == "order_status" and pending.payload.get("order_id"):
            session["last_order_id"] = str(pending.payload["order_id"])
        if pending.kind == "case_regenerate" and pending.payload.get("case_id"):
            session["last_case_id8"] = str(pending.payload["case_id"])[:8]
        self._merge_state(
            chat_id,
            flow="pending_action",
            step="confirm",
            slots={"pending": pending.to_dict(), "prompt": text[:500]},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )
        return BotReply(text=text, reply_markup=self._markup_for_pending(pending), needs_typing=True)

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
            actor = resolve_telegram_actor(chat_id)
        except TelegramActorDenied:
            self._clear_pending(chat_id)
            return BotReply(
                text=(
                    "Din chat saknar actor-mapping. "
                    "Be Oscar uppdatera TELEGRAM_ACTOR_MAP."
                )
            )
        ok = False
        msg = "Okänd action"
        if pending.kind == "order_status":
            ok, msg = execute_order_status(
                order_id=str(pending.payload.get("order_id") or ""),
                status=str(pending.payload.get("status") or ""),
                actor=actor,
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
        elif pending.kind == "case_regenerate":
            # Prefer full id via store if only id8
            case_key = str(pending.payload.get("case_id") or "")
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
        self._clear_pending(chat_id)
        prefix = "Klart." if ok else "Misslyckades."
        # RBAC denies are common for jonatan on order update
        if not ok and ("lacks permission" in msg.lower() or "access" in msg.lower()):
            msg = (
                f"{msg}\n\n"
                "Tips: mappa din chat till en operator/Oscar via TELEGRAM_ACTOR_MAP "
                "(Jonatan har CASE_REPLY men inte order/product write)."
            )
        return BotReply(text=f"{prefix}\n{msg}")

    def _run_llm_chat(self, chat_id: str | int, message: str) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        history = list(prev.get("messages") or [])
        prior_digest = str(prev.get("tool_digest") or "")
        result = run_chat(
            message,
            history=history,
            session=session,
            prior_digest=prior_digest,
            sticky_order_id=session.get("last_order_id"),
            sticky_case_id8=session.get("last_case_id8"),
        )
        if result.sticky_order_id:
            session["last_order_id"] = str(result.sticky_order_id)
        if result.sticky_case_id8:
            session["last_case_id8"] = str(result.sticky_case_id8)

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
        markup = None

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
            markup = yes_no_keyboard(
                yes_data="escalate:yes", no_data="escalate:no"
            )
            if "eskalera" not in text.lower():
                text = f"{text}\n\nVill du eskalera till Oscar?"
            return BotReply(text=text, reply_markup=markup, needs_typing=True)

        if result.pending_action is not None:
            return self._start_pending(chat_id, result.pending_action, text)

        if result.soft_escalate_nudge and SOFT_ESCALATE_NUDGE not in text:
            text = f"{text}\n\n{SOFT_ESCALATE_NUDGE}"

        if result.case_id8:
            markup = approve_case_keyboard(result.case_id8)
        elif result.suggest_case_ids:
            markup = triage_cases_keyboard(result.suggest_case_ids)

        return BotReply(text=text, reply_markup=markup, needs_typing=True)
