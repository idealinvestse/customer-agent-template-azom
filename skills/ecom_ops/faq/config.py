"""Load config/faq.yaml with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FaqConfig:
    enabled: bool
    inject_into_draft: bool
    max_hits: int
    max_chars_per_hit: int
    wp_parent_slug: str
    wp_parent_title: dict[str, str]
    publish_kill_env: str
    live_markets_allowed: tuple[str, ...]


@lru_cache(maxsize=1)
def load_faq_config() -> FaqConfig:
    path = _config_dir() / "faq.yaml"
    raw: dict[str, Any] = {}
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            raw = loaded

    titles = raw.get("wp_parent_title") or {}
    if not isinstance(titles, dict):
        titles = {}
    title_map = {str(k).lower(): str(v) for k, v in titles.items()}

    inject = bool(raw.get("inject_into_draft", False))
    env_inject = _env_bool("AZOM_FAQ_INJECT_INTO_DRAFT")
    if env_inject is not None:
        inject = env_inject

    return FaqConfig(
        enabled=bool(raw.get("enabled", True)),
        inject_into_draft=inject,
        max_hits=max(1, min(int(raw.get("max_hits", 3)), 10)),
        max_chars_per_hit=max(100, min(int(raw.get("max_chars_per_hit", 600)), 4000)),
        wp_parent_slug=str(raw.get("wp_parent_slug") or "faq").strip() or "faq",
        wp_parent_title=title_map
        or {"se": "Vanliga frågor", "no": "Vanlige spørsmål", "dk": "Ofte stillede spørgsmål"},
        publish_kill_env=str(raw.get("publish_kill_env") or "AZOM_FAQ_PUBLISH_KILL"),
        live_markets_allowed=tuple(
            m.lower()
            for m in _as_str_list(raw.get("live_markets_allowed") or ["se"])
        ),
    )


def clear_faq_config_cache() -> None:
    load_faq_config.cache_clear()
