"""Shared case pipeline result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTIVE_CASE_STATUSES = ("open", "escalated")


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    message: str
    created: int = 0
    skipped: int = 0
    errors: int = 0
    cases: list[dict[str, Any]] | None = None
    escalated: bool = False
    ticket_id: str | None = None
    per_mailbox: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "created": self.created,
            "skipped": self.skipped,
            "errors": self.errors,
            "cases": self.cases or [],
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
            "per_mailbox": self.per_mailbox or [],
        }


@dataclass(frozen=True)
class CaseActionResult:
    ok: bool
    message: str
    case: dict[str, Any] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "case": self.case,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }
