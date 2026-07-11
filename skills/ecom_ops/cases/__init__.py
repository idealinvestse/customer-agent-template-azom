"""Case management package."""

from __future__ import annotations

from typing import Any

__all__ = ["Case", "CaseMessage", "CaseService", "CaseStore"]


def __getattr__(name: str) -> Any:
    if name == "CaseService":
        from ecom_ops.cases.service import CaseService

        return CaseService
    if name in {"Case", "CaseMessage", "CaseStore"}:
        from ecom_ops.cases.store import Case, CaseMessage, CaseStore

        return {"Case": Case, "CaseMessage": CaseMessage, "CaseStore": CaseStore}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
