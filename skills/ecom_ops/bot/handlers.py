"""Telegram message handlers — OpenClaw-style dialogue + Azom ops flows."""

from __future__ import annotations

import os
import re
from typing import Any

from ecom_ops.actions.support import SupportService
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.woocommerce import client_from_env
from ecom_ops.security import validate_order_id

ORDER_TEXT_RE = re.compile(r"\b(?:order|ordernr|#)\s*(\d{4,12})\b", re.I)


def telegram_chat_allowed(chat_id: str | int) -> bool:
    """Enforce TELEGRAM_ALLOWED_CHAT_IDS when set (comma-separated)."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return True
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(chat_id) in allowed


class BotHandler:
    """OpenClaw-compatible commands + multi-turn Azom flows."""

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
        state = {
            "flow": updates.get("flow", prev.get("flow")),
            "step": updates.get("step", prev.get("step")),
            "slots": updates.get("slots", prev.get("slots") or {}),
            "session": session,
        }
        # Drop idle empty sessions so /cancel tests and TTL stay clean
        if not state.get("flow") and not state.get("session"):
            self.store.clear(chat_id)
            return
        self.store.set(chat_id, state)

    def handle(self, chat_id: str | int, text: str) -> str:
        if not telegram_chat_allowed(chat_id):
            return (
                "Du är inte behörig att använda denna bot. "
                "Be Oscar lägga till din chat-id i TELEGRAM_ALLOWED_CHAT_IDS."
            )
        raw = (text or "").strip()
        if not raw:
            return "Skriv /help eller /commands."

        # OpenClaw slash commands first (standalone)
        oc = dispatch_openclaw_command(chat_id, raw, self.store)
        if oc is not None:
            if oc == "__ORDER_PROMPT__":
                self._merge_state(
                    chat_id, flow="order_lookup", step="await_id", slots={}
                )
                return "Ange ordernummer (t.ex. 1001):"
            if oc.startswith("__ORDER__:"):
                return self._lookup_order(oc.split(":", 1)[1])
            return oc

        # Continue multi-turn flows
        state = self.store.get(chat_id)
        if state and state.get("flow"):
            return self._continue_flow(chat_id, raw, state)

        if ORDER_TEXT_RE.search(raw):
            oid = ORDER_TEXT_RE.search(raw).group(1)  # type: ignore[union-attr]
            return self._lookup_order(oid)

        return self._start_support_draft(chat_id, raw)

    def _continue_flow(self, chat_id: str | int, text: str, state: dict[str, Any]) -> str:
        flow = state.get("flow")
        step = state.get("step")
        slots = dict(state.get("slots") or {})
        session = dict(state.get("session") or {})

        if flow == "order_lookup" and step == "await_id":
            try:
                oid = validate_order_id(text.strip())
            except Exception:
                return "Ogiltigt ordernummer. Försök igen eller /stop."
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            return self._lookup_order(oid)

        if flow == "support_draft" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j"}:
                ticket = self.escalation.escalate_critical(
                    "Telegram support draft escalated by Jonatan",
                    details={
                        "chat_id": str(chat_id),
                        "message": slots.get("message", "")[:500],
                        "draft": (slots.get("reply") or "")[:500],
                    },
                )
                self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
                return f"Eskalerat till Oscar. Ticket: {ticket.id}"
            if answer in {"nej", "no", "n"}:
                self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
                return "Draft sparad lokalt men inte eskalerad. /help · /cases"
            return "Svara ja eller nej, eller /stop."

        self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
        return self._start_support_draft(chat_id, text)

    def _start_support_draft(self, chat_id: str | int, message: str) -> str:
        result = SupportService().handle(message, actor="agent", language="sv")
        reply = result.reply or "(inget svar genererat)"
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        self._merge_state(
            chat_id,
            flow="support_draft",
            step="confirm",
            slots={
                "message": message,
                "reply": reply,
                "category": result.category.value,
            },
            session=session,
        )
        verbose = session.get("verbose") in {"on", "full"}
        header = f"Support-draft ({result.category.value})"
        if verbose:
            header += f" · model={session.get('model', 'default')}"
        return (
            f"{header}:\n\n{reply}\n\n"
            "Skicka till Oscar? (ja/nej) — eller öppna dashboard /cases"
        )

    def _lookup_order(self, order_id: str) -> str:
        try:
            oid = validate_order_id(order_id)
            woo = client_from_env(use_mock=None)
            order = woo.get_order(oid)
            return (
                f"Order {order.id}\n"
                f"Status: {order.status}\n"
                f"Total: {order.total} {order.currency}\n"
                f"(read-only – ändra via operator/CLI)"
            )
        except Exception as exc:
            return f"Kunde inte hämta order: {exc}"
