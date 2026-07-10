"""Telegram message handlers with support_draft and order_lookup flows."""

from __future__ import annotations

import re
from typing import Any

from ecom_ops.actions.support import SupportService
from ecom_ops.bot.store import ConversationStore
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.woocommerce import client_from_env
from ecom_ops.security import validate_order_id

ORDER_CMD_RE = re.compile(r"^/order(?:\s+(\d{1,12}))?\s*$", re.I)
ORDER_TEXT_RE = re.compile(r"\b(?:order|ordernr|#)\s*(\d{4,12})\b", re.I)


class BotHandler:
    """Stateless entry with persistent ConversationStore."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.store = store or ConversationStore()
        self.escalation = escalation or default_escalation

    def handle(self, chat_id: str | int, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return "Skicka /help för kommandon."

        lower = raw.lower()
        if lower in {"/start", "/help"}:
            self.store.clear(chat_id)
            return (
                "Azom Ops Bot (Jonatan read-only)\n"
                "/help – denna hjälp\n"
                "/health – SSH health\n"
                "/brief – KPI snapshot\n"
                "/order [id] – orderstatus (read-only)\n"
                "/cancel – avbryt pågående dialog\n"
                "Skriv ett meddelande för support-draft → eskalera till Oscar."
            )

        if lower == "/cancel":
            self.store.clear(chat_id)
            return "Dialog avbruten."

        if lower == "/health":
            return self._cmd_health()

        if lower == "/brief":
            return self._cmd_brief()

        m = ORDER_CMD_RE.match(raw)
        if m:
            oid = m.group(1)
            if oid:
                return self._lookup_order(oid)
            self.store.set(
                chat_id,
                {"flow": "order_lookup", "step": "await_id", "slots": {}},
            )
            return "Ange ordernummer (t.ex. 1001):"

        state = self.store.get(chat_id)
        if state:
            return self._continue_flow(chat_id, raw, state)

        if ORDER_TEXT_RE.search(raw):
            oid = ORDER_TEXT_RE.search(raw).group(1)  # type: ignore[union-attr]
            return self._lookup_order(oid)

        return self._start_support_draft(chat_id, raw)

    def _continue_flow(self, chat_id: str | int, text: str, state: dict[str, Any]) -> str:
        flow = state.get("flow")
        step = state.get("step")
        slots = dict(state.get("slots") or {})

        if flow == "order_lookup" and step == "await_id":
            try:
                oid = validate_order_id(text.strip())
            except Exception:
                return "Ogiltigt ordernummer. Försök igen eller /cancel."
            self.store.clear(chat_id)
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
                self.store.clear(chat_id)
                return f"Eskalerat till Oscar. Ticket: {ticket.id}"
            if answer in {"nej", "no", "n", "n"}:
                self.store.clear(chat_id)
                return "Draft sparad lokalt men inte eskalerad. /help för nya kommandon."
            return "Svara ja eller nej, eller /cancel."

        self.store.clear(chat_id)
        return self._start_support_draft(chat_id, text)

    def _start_support_draft(self, chat_id: str | int, message: str) -> str:
        result = SupportService().handle(message, actor="agent", language="sv")
        reply = result.reply or "(inget svar genererat)"
        self.store.set(
            chat_id,
            {
                "flow": "support_draft",
                "step": "confirm",
                "slots": {
                    "message": message,
                    "reply": reply,
                    "category": result.category.value,
                },
            },
        )
        return (
            f"Support-draft ({result.category.value}):\n\n{reply}\n\n"
            "Skicka till Oscar? (ja/nej)"
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
                f"(read-only – Jonatan kan inte ändra status)"
            )
        except Exception as exc:
            return f"Kunde inte hämta order: {exc}"

    def _cmd_health(self) -> str:
        try:
            from ecom_ops.actions.ssh_ops import SSHOpsService

            results = SSHOpsService().health(actor="jonatan")
            lines = []
            for r in results:
                ok = "ok" if r.ok else "fail"
                cmd = r.result.command if r.result else "?"
                lines.append(f"{cmd}: {ok}")
            return "SSH health:\n" + "\n".join(lines)
        except Exception as exc:
            return f"Health error: {exc}"

    def _cmd_brief(self) -> str:
        try:
            from ecom_ops.config import load_app_config
            from ecom_ops.telemetry import Telemetry

            cfg = load_app_config()
            cost = Telemetry().sum_cost_usd()
            return (
                f"Customer: {cfg.customer.customer}\n"
                f"Domains: {', '.join(cfg.customer.domains)}\n"
                f"LLM cost USD: {cost:.4f} / cap {cfg.limits.openrouter_cap}"
            )
        except Exception as exc:
            return f"Brief error: {exc}"
