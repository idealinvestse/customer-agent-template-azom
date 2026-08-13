"""Load config/faq_ingest.yaml."""

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


@dataclass(frozen=True)
class FaqIngestConfig:
    dataset_max_age_days: int
    dataset_exclude_categories: tuple[str, ...]
    dataset_min_reply_chars: int
    max_pages_per_site: int
    max_posts_per_site: int
    ingest_pages: bool
    ingest_posts: bool
    guide_allowlist_hosts: tuple[str, ...]
    guide_urls: tuple[str, ...]
    max_guide_bytes: int
    max_products_per_market: int
    llm_suggest_enabled: bool
    ingest_kill_env: str


@lru_cache(maxsize=1)
def load_faq_ingest_config() -> FaqIngestConfig:
    path = _config_dir() / "faq_ingest.yaml"
    raw: dict[str, Any] = {}
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            raw = loaded
    return FaqIngestConfig(
        dataset_max_age_days=max(1, int(raw.get("dataset_max_age_days", 365))),
        dataset_exclude_categories=tuple(
            c.lower()
            for c in _as_str_list(raw.get("dataset_exclude_categories") or ["abuse"])
        ),
        dataset_min_reply_chars=max(1, int(raw.get("dataset_min_reply_chars", 40))),
        max_pages_per_site=max(1, min(int(raw.get("max_pages_per_site", 50)), 500)),
        max_posts_per_site=max(1, min(int(raw.get("max_posts_per_site", 50)), 500)),
        ingest_pages=bool(raw.get("ingest_pages", True)),
        ingest_posts=bool(raw.get("ingest_posts", True)),
        guide_allowlist_hosts=tuple(
            h.lower()
            for h in _as_str_list(raw.get("guide_allowlist_hosts") or [])
        ),
        guide_urls=tuple(_as_str_list(raw.get("guide_urls") or [])),
        max_guide_bytes=max(1000, int(raw.get("max_guide_bytes", 500_000))),
        max_products_per_market=max(
            1, min(int(raw.get("max_products_per_market", 200)), 2000)
        ),
        llm_suggest_enabled=bool(raw.get("llm_suggest_enabled", False)),
        ingest_kill_env=str(raw.get("ingest_kill_env") or "AZOM_FAQ_INGEST_KILL"),
    )


def clear_faq_ingest_config_cache() -> None:
    load_faq_ingest_config.cache_clear()


def faq_ingest_killed() -> bool:
    cfg = load_faq_ingest_config()
    env = cfg.ingest_kill_env or "AZOM_FAQ_INGEST_KILL"
    return os.environ.get(env, "").strip().lower() in {"1", "true", "yes", "on"}


def effective_use_mock(use_mock: bool | None) -> bool:
    """Resolve CLI ``--mock`` or ``AZOM_USE_MOCK`` (None = read env)."""
    if use_mock is not None:
        return bool(use_mock)
    return os.environ.get("AZOM_USE_MOCK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
