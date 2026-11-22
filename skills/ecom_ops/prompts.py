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
import re
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
        "version": "1.2",
        "system": (
            "You write short, professional customer-support email drafts for Azom "
            "(Nordic bilmultimedia e-commerce: SE/NO/DK). Output only the email body — "
            "no subject line, no markdown fences. Sign as Azom Support. Do not invent "
            "tracking numbers, refunds, legal outcomes, or compatibility guarantees. "
            "For return/reklamation: ask for order number when missing, reason, packaging "
            "state, and photos if damaged; ångerrätt/angrerett as process info only — "
            "never promise automatic repayment. For billing: ask order number, payment "
            "date and reference; manual review only, never a payment outcome. "
            "Keep under 180 words."
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


def get_prompt(name: str, *, language: str | None = None) -> tuple[str, str]:
    """Return (system_prompt, version) for the named prompt.

    Optional ``language`` looks up ``{name}_{lang}`` first (e.g. draft_da).
    Falls back to built-in defaults if config is missing or key absent.
    """
    data = _load_prompts()
    lang = (language or "").strip().lower()
    if lang in {"no", "nb"}:
        lang = "nb"
    elif lang in {"da", "dk"}:
        lang = "da"
    keys = [f"{name}_{lang}", name] if lang else [name]
    for key in keys:
        entry = data.get(key) if isinstance(data, dict) else None
        if isinstance(entry, dict) and entry.get("system"):
            return str(entry["system"]), str(entry.get("version", "unknown"))
        builtin = _BUILTIN.get(key)
        if builtin:
            return builtin["system"], builtin["version"]
    raise KeyError(f"Unknown prompt: {name}")


def reload_prompts() -> None:
    """Clear the cache so subsequent get_prompt calls re-read the file."""
    _load_prompts.cache_clear()


_PROMPT_NAMES = ("classify", "draft", "chat")
_MAX_PROMPT_CHARS = 8000


def _bump_version(current: str) -> str:
    match = re.search(r"(\d+)$", (current or "").strip())
    if not match:
        return "1"
    number = int(match.group(1)) + 1
    prefix = (current or "")[: match.start()]
    return f"{prefix}{number}"


def save_prompt_texts(updates: dict[str, str]) -> dict[str, str]:
    """Replace system text for classify, draft, and chat.

    Returns ``{name: new_version}`` for texts that changed. Empty text and
    text over 8000 characters raise ``ValueError``. Unchanged text keeps
    its version.
    """
    data = dict(_load_prompts())
    changed: dict[str, str] = {}
    for name in _PROMPT_NAMES:
        if name not in updates:
            continue
        text = str(updates[name]).strip()
        if not text:
            raise ValueError(f"{name} är tom")
        if len(text) > _MAX_PROMPT_CHARS:
            raise ValueError(f"{name} är längre än 8000 tecken")
        entry = data.get(name) if isinstance(data.get(name), dict) else {}
        current = str(entry.get("system") or "").strip()
        if current == text:
            continue
        version = _bump_version(str(entry.get("version") or _BUILTIN[name]["version"]))
        new_entry = dict(entry)
        new_entry["system"] = text
        new_entry["version"] = version
        data[name] = new_entry
        changed[name] = version
    if not changed:
        return {}
    path = _prompts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reload_prompts()
    return changed
