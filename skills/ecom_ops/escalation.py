"""Escalation to Oscar for critical ops and code edits."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ecom_ops.config import load_app_config
from ecom_ops.security import redact_secrets

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EscalationReason(str, Enum):
    CRITICAL = "critical"
    CODE_EDIT = "code_edit"
    SSH_UNSAFE = "ssh_unsafe"
    ACCESS_DENIED = "access_denied"
    BUDGET = "budget"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass
class EscalationTicket:
    id: str
    reason: EscalationReason
    severity: Severity
    assignee: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason"] = self.reason.value
        data["severity"] = self.severity.value
        data["details"] = redact_secrets(self.details)
        return data


NotifyFn = Callable[[EscalationTicket], None]


def _default_store_path() -> Path:
    override = os.environ.get("AZOM_ESCALATION_DIR")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "escalations.jsonl"


def _append_ticket(ticket: EscalationTicket, path: Path | None = None) -> Path:
    store = path or _default_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ticket.to_dict(), ensure_ascii=False) + "\n")
    return store


def _log_notify(ticket: EscalationTicket) -> None:
    logger.warning(
        "ESCALATION -> %s | %s | %s | %s",
        ticket.assignee,
        ticket.severity.value,
        ticket.reason.value,
        ticket.summary,
    )


def telegram_notify_factory(chat_id: str | int, *, token_env: str = "TELEGRAM_BOT_TOKEN") -> NotifyFn:
    """Build a Telegram notifier for escalation tickets (P3.4).

    Sends a short message to the configured Oscar chat. Failures are logged
    but never raised (escalation must not fail because the notifier is down).
    """
    import urllib.request

    def notify(ticket: EscalationTicket) -> None:
        token = (os.environ.get(token_env) or "").strip()
        if not token:
            logger.info("Telegram escalation notify skipped (no token)")
            return
        text = (
            f"⚠️ Eskalering → {ticket.assignee}\n"
            f"Severity: {ticket.severity.value}\n"
            f"Reason: {ticket.reason.value}\n"
            f"Summary: {ticket.summary[:300]}"
        )
        url = (
            f"https://api.telegram.org/bot{token}/sendMessage"
            f"?chat_id={chat_id}&text=" + __import__("urllib.parse", fromlist=["quote"]).quote(text)
        )
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    logger.warning("Telegram notify HTTP %s", resp.status)
        except Exception as exc:
            logger.warning("Telegram escalation notify failed: %s", exc)

    return notify


class EscalationService:
    """Create escalation tickets assigned to Oscar (or config override)."""

    def __init__(
        self,
        *,
        store_path: Path | None = None,
        notifiers: list[NotifyFn] | None = None,
    ) -> None:
        self.store_path = store_path
        self.notifiers = notifiers or [_log_notify]
        try:
            cfg = load_app_config()
            self.critical_assignee = cfg.rbac.escalation_critical
            self.code_edit_assignee = cfg.rbac.escalation_code_edit
        except Exception:  # pragma: no cover - config optional in unit tests
            self.critical_assignee = "oscar"
            self.code_edit_assignee = "oscar"

    def escalate(
        self,
        *,
        reason: EscalationReason,
        summary: str,
        details: dict[str, Any] | None = None,
        severity: Severity | None = None,
        assignee: str | None = None,
    ) -> EscalationTicket:
        if assignee is None:
            if reason == EscalationReason.CODE_EDIT:
                assignee = self.code_edit_assignee
            else:
                assignee = self.critical_assignee

        if severity is None:
            severity = (
                Severity.CRITICAL
                if reason
                in {
                    EscalationReason.CRITICAL,
                    EscalationReason.SSH_UNSAFE,
                    EscalationReason.CODE_EDIT,
                }
                else Severity.HIGH
            )

        ticket = EscalationTicket(
            id=str(uuid.uuid4()),
            reason=reason,
            severity=severity,
            assignee=assignee,
            summary=summary,
            details=details or {},
        )
        path = _append_ticket(ticket, self.store_path)
        ticket.details.setdefault("_store", str(path))
        for notify in self.notifiers:
            notify(ticket)
        return ticket

    def escalate_critical(
        self, summary: str, details: dict[str, Any] | None = None
    ) -> EscalationTicket:
        return self.escalate(
            reason=EscalationReason.CRITICAL,
            summary=summary,
            details=details,
            severity=Severity.CRITICAL,
        )

    def escalate_code_edit(
        self, summary: str, details: dict[str, Any] | None = None
    ) -> EscalationTicket:
        return self.escalate(
            reason=EscalationReason.CODE_EDIT,
            summary=summary,
            details=details,
            severity=Severity.CRITICAL,
        )


# Module-level default for scripts.
# Wire Telegram notifier when AZOM_ESCALATION_TELEGRAM_CHAT_ID is set (P3.4).
_default_notifiers: list[NotifyFn] = [_log_notify]
_escalation_chat = (os.environ.get("AZOM_ESCALATION_TELEGRAM_CHAT_ID") or "").strip()
if _escalation_chat:
    _default_notifiers.append(telegram_notify_factory(_escalation_chat))
default_escalation = EscalationService(notifiers=_default_notifiers)
