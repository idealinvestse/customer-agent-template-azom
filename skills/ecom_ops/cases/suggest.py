"""Suggest-approve eligibility and cases AI guardrail config."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_SUGGEST_CATEGORIES = ("order_status", "shipping")
_DEFAULT_NEVER = ("abuse", "return", "billing")
_DEFAULT_MIN_CONF = 0.8


@dataclass(frozen=True)
class CasesAiConfig:
    suggest_approve_categories: tuple[str, ...]
    suggest_approve_min_confidence: float
    suggest_approve_require_order_id: bool
    never_suggest_categories: tuple[str, ...]
    auto_send_enabled: bool
    auto_send_categories: tuple[str, ...]
    auto_send_min_confidence: float
    max_auto_sends_per_day: int
    kill_switch_env: str

    def kill_switch_active(self) -> bool:
        raw = (os.environ.get(self.kill_switch_env) or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}


def _config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def _as_str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value)
    return default


def load_cases_ai_config() -> CasesAiConfig:
    """Load config/cases_ai.yaml with safe defaults (auto-send off)."""
    path = _config_dir() / "cases_ai.yaml"
    raw: dict[str, Any] = {}
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            raw = loaded

    return CasesAiConfig(
        suggest_approve_categories=_as_str_tuple(
            raw.get("suggest_approve_categories"), _DEFAULT_SUGGEST_CATEGORIES
        ),
        suggest_approve_min_confidence=float(
            raw.get("suggest_approve_min_confidence", _DEFAULT_MIN_CONF)
        ),
        suggest_approve_require_order_id=bool(
            raw.get("suggest_approve_require_order_id", True)
        ),
        never_suggest_categories=_as_str_tuple(
            raw.get("never_suggest_categories"), _DEFAULT_NEVER
        ),
        auto_send_enabled=bool(raw.get("auto_send_enabled", False)),
        auto_send_categories=_as_str_tuple(
            raw.get("auto_send_categories"), ("order_status",)
        ),
        auto_send_min_confidence=float(raw.get("auto_send_min_confidence", 0.92)),
        max_auto_sends_per_day=int(raw.get("max_auto_sends_per_day", 10)),
        kill_switch_env=str(raw.get("kill_switch_env") or "AZOM_AUTO_SEND_KILL"),
    )


def is_suggest_approve_eligible(
    *,
    category: str,
    confidence: float,
    order_id: str | None,
    escalated: bool,
    config: CasesAiConfig | None = None,
) -> bool:
    """Return True when Jonatan can use reduced-friction suggest-approve UX."""
    cfg = config or load_cases_ai_config()
    cat = (category or "").strip().lower()
    if escalated:
        return False
    if cat in {c.lower() for c in cfg.never_suggest_categories}:
        return False
    if cat not in {c.lower() for c in cfg.suggest_approve_categories}:
        return False
    if confidence < cfg.suggest_approve_min_confidence:
        return False
    if cfg.suggest_approve_require_order_id and not (order_id or "").strip():
        return False
    # shipping only when order_id present (require_order_id already covers)
    return True
