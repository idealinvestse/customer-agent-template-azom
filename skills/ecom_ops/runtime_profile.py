"""Runtime profile helpers (null-send / shadow observation)."""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})


def null_send_active() -> bool:
    """True when AZOM_NULL_SEND enables the null-send profile."""
    return os.environ.get("AZOM_NULL_SEND", "").strip().lower() in _TRUE


def null_send_label() -> str:
    """Always-on status token: null_send=on|off."""
    return "on" if null_send_active() else "off"


def enable_null_send() -> None:
    """Set env for the current process (CLI --null-send)."""
    os.environ["AZOM_NULL_SEND"] = "1"
