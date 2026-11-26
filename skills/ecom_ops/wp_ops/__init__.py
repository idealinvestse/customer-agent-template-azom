"""WordPress site operations (read-only checks, backups, review workspaces, and agent tools)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ecom_ops.wp_ops.service import WpOpsService

__all__ = ["WpOpsService"]


def __getattr__(name: str) -> Any:
    if name == "WpOpsService":
        from ecom_ops.wp_ops.service import WpOpsService

        return WpOpsService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
