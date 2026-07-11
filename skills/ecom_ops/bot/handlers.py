"""Telegram message handlers — OpenClaw-style dialogue + hybrid LLM chat."""

from __future__ import annotations

import os
import re
from typing import Any

from ecom_ops.bot.actors import resolve_telegram_actor
from ecom_ops.bot.chat_agent import (
    SOFT_ESCALATE_NUDGE,
    run_chat,
    tool_lookup_order,
    wants_escalate,
    wants_hard_escalate_confirm,
)
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.reply import (
    BotReply,
    approve_case_keyboard,
    as_reply,
    triage_cases_keyboard,
    yes_no_keyboard,
)
from ecom_ops.bot.store import ConversationStore, clamp_messages
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.security import validate_order_id

# Bare "#1001" / "order 1001" still gets a fast path; richer NL goes via LLM chat.
ORDER_FAST_RE = re.compile(r"^\s*(?:order|ordernr|#)\s*(\d{4,12})\s*$", re.I)


def telegram_chat_allowed(chat_id: str | int) -> bool:
    """Enforce TELEGRAM_ALLOWED_CHAT_IDS when set (comma-separated)."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return True
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(chat_id) in allowed


class BotHandler:
    """OpenClaw-compatible commands + hybrid chat + multi-turn Azom flows."""

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
        state = {
            "flow": updates.get("flow", prev.get("flow")),
            "step": updates.get("step", prev.get("step")),
            "slots": updates.get("slots", prev.get("slots") or {}),
            "session": session,
            "messages": messages,
            "tool_digest": tool_digest,
        }
        if (
            not state.get("flow")
            and not state.get("session")
            and not state.get("messages")
            and not state.get("tool_digest")
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
                return BotReply(text=tool_lookup_order(oc.split(":", 1)[1]))
            return as_reply(oc)

        # Continue multi-turn flows
        state = self.store.get(chat_id)
        if state and state.get("flow"):
            return as_reply(self._continue_flow(chat_id, raw, state))

        # Ultra-short order shortcut (not full NL — that goes through chat)
        fast = ORDER_FAST_RE.match(raw)
        if fast:
            return BotReply(text=tool_lookup_order(fast.group(1)))

        # Clear escalate → confirm with buttons (skip LLM latency)
        if wants_hard_escalate_confirm(raw):
            return self._start_escalate_confirm(chat_id, raw)

        return self._run_llm_chat(chat_id, raw)

    def handle_callback(self, chat_id: str | int, data: str) -> BotReply:
        """Handle inline keyboard callback_data."""
        if not telegram_chat_allowed(chat_id):
            return BotReply(text="Inte behörig.")
        raw = (data or "").strip()
        if raw == "escalate:yes":
            return as_reply(self._confirm_escalate(chat_id, yes=True))
        if raw == "escalate:no":
            return as_reply(self._confirm_escalate(chat_id, yes=False))
        if raw.startswith("cases:show:"):
            id8 = raw.split(":", 2)[2].strip()
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
        return BotReply(text="Okänd knapp. /help")

    def _continue_flow(
        self, chat_id: str | int, text: str, state: dict[str, Any]
    ) -> str | BotReply:
        flow = state.get("flow")
        step = state.get("step")
        session = dict(state.get("session") or {})

        if flow == "order_lookup" and step == "await_id":
            try:
                oid = validate_order_id(text.strip())
            except Exception:
                return BotReply(
                    text="Ogiltigt ordernummer. Försök igen eller /stop."
                )
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            return tool_lookup_order(oid)

        if flow == "escalate_confirm" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j"}:
                return self._confirm_escalate(chat_id, yes=True)
            if answer in {"nej", "no", "n"}:
                return self._confirm_escalate(chat_id, yes=False)
            # Mid-confirm free text → treat as chat, clear confirm
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
            actor = resolve_telegram_actor(chat_id)
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
        )
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

        # Sticky escalate only for hard user intent
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

        # Soft nudge: no sticky flow
        if result.soft_escalate_nudge and SOFT_ESCALATE_NUDGE not in text:
            text = f"{text}\n\n{SOFT_ESCALATE_NUDGE}"

        if result.case_id8:
            markup = approve_case_keyboard(result.case_id8)
        elif result.suggest_case_ids:
            markup = triage_cases_keyboard(result.suggest_case_ids)

        return BotReply(text=text, reply_markup=markup, needs_typing=True)
