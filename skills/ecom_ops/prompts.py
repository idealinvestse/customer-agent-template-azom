"""Prompt registry loader (P4.1).

Loads versioned prompts from ``config/prompts.yaml`` so prompt changes are
config-driven, not hardcoded. Falls back to built-in defaults if the file
or a key is missing.

Usage::

    from ecom_ops.prompts import get_prompt
    system, version = get_prompt("classify")
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Built-in fallbacks (kept in sync with config/prompts.yaml defaults)
_BUILTIN: dict[str, dict[str, str]] = {
    "classify": {
        "version": "1.0",
        "system": (
            "You classify Nordic e-commerce support emails for Azom. "
            "Reply with ONLY a single JSON object, no markdown: "
            '{"category":"<one of: order_status,shipping,product,return,billing,technical,abuse,other>", "confidence": <float 0..1>}. '
            "Use abuse for legal threats, chargebacks, self-harm, or violence. "
            "Prefer order_status when the customer asks where an order is."
        ),
    },
    "draft": {
        "version": "1.0",
        "system": (
            "You write short, professional customer-support email drafts for Azom "
            "(Nordic e-commerce). Output only the email body — no subject line, "
            "no markdown fences. Sign as Azom Support. Do not invent tracking "
            "numbers, refunds, or legal promises. Use only order facts from the "
            "provided order context when present. Keep under 180 words."
        ),
    },
    "chat": {
        "version": "1.0",
        "system": (
            "Du är AzomOps — Jonatans/Oscars dedikerade Telegram-kollega för Azom "
            "(Woo SE/NO/DK). Prata som i en vanlig OpenClaw-dialog: flytande svenska, "
            "naturligt, hjälpsamt, utan robotic bullet-spam."
        ),
    },
}


def _prompts_path() -> Path:
    cfg_dir = Path(os.environ.get("AZOM_CONFIG_DIR", "config"))
    return cfg_dir / "prompts.yaml"


@lru_cache(maxsize=1)
def _load_prompts() -> dict[str, Any]:
    path = _prompts_path()
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_prompt(name: str) -> tuple[str, str]:
    """Return (system_prompt, version) for the named prompt.

    Falls back to built-in defaults if config is missing or key absent.
    """
    data = _load_prompts()
    entry = data.get(name) if isinstance(data, dict) else None
    if isinstance(entry, dict) and entry.get("system"):
        return str(entry["system"]), str(entry.get("version", "unknown"))
    builtin = _BUILTIN.get(name)
    if builtin:
        return builtin["system"], builtin["version"]
    raise KeyError(f"Unknown prompt: {name}")


def reload_prompts() -> None:
    """Clear the cache so subsequent get_prompt calls re-read the file."""
    _load_prompts.cache_clear()
