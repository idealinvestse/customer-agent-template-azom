"""Fail-closed configuration and env-gate helpers for Shop Intelligence.

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
or ``subprocess`` imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.config import _config_dir
from ecom_ops.runtime_env import env_flag
from ecom_ops.shop_intel.models import VALID_DOMAINS, VALID_PURPOSES
from ecom_ops.wp_ops.config import is_mock_mode

DEFAULT_CONTEXT_BUDGETS: dict[str, int] = {
    "support_draft": 2200,
    "product_copy": 2200,
    "wp_coder": 3200,
    "chat_general": 1800,
    "seo": 2200,
    "security_audit": 2400,
}

_CACHE_KEY: tuple[str, int | None] | None = None
_CACHE_CONFIG: ShopIntelConfig | None = None


@dataclass(frozen=True)
class ShopIntelConfig:
    schema_version: int = 1
    live_enabled: bool = False
    kill_switch_env: str = "AZOM_SHOP_INTEL_KILL"
    live_enable_env: str = "AZOM_SHOP_INTEL_LIVE_ENABLED"
    max_products_sample: int = 200
    max_pages_sample: int = 50
    max_runs_per_site: int = 30
    retention_days: int = 90
    default_schedule_interval_hours: int = 24
    context_budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CONTEXT_BUDGETS))
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)


def _builtin_raw_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "live_enabled": False,
        "kill_switch_env": "AZOM_SHOP_INTEL_KILL",
        "live_enable_env": "AZOM_SHOP_INTEL_LIVE_ENABLED",
        "max_products_sample": 200,
        "max_pages_sample": 50,
        "max_runs_per_site": 30,
        "retention_days": 90,
        "default_schedule_interval_hours": 24,
        "context_budgets": dict(DEFAULT_CONTEXT_BUDGETS),
        "domains": {dom: {"enabled": True} for dom in VALID_DOMAINS},
    }


def _bounded_int(val: Any, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _build_config(raw: dict[str, Any]) -> ShopIntelConfig:
    budgets = dict(DEFAULT_CONTEXT_BUDGETS)
    raw_budgets = raw.get("context_budgets")
    if isinstance(raw_budgets, dict):
        for purpose in VALID_PURPOSES:
            if purpose in raw_budgets:
                budgets[purpose] = _bounded_int(
                    raw_budgets[purpose], budgets[purpose], minimum=200, maximum=16000
                )

    domains: dict[str, dict[str, Any]] = {}
    raw_domains = raw.get("domains")
    for dom in VALID_DOMAINS:
        entry = raw_domains.get(dom) if isinstance(raw_domains, dict) else None
        if isinstance(entry, dict):
            domains[dom] = {
                "enabled": bool(entry.get("enabled", True)),
                "description": str(entry.get("description") or ""),
            }
        else:
            domains[dom] = {"enabled": True, "description": ""}

    return ShopIntelConfig(
        schema_version=_bounded_int(raw.get("schema_version"), 1, minimum=1, maximum=10),
        live_enabled=bool(raw.get("live_enabled", False)),
        kill_switch_env=str(raw.get("kill_switch_env") or "AZOM_SHOP_INTEL_KILL").strip(),
        live_enable_env=str(raw.get("live_enable_env") or "AZOM_SHOP_INTEL_LIVE_ENABLED").strip(),
        max_products_sample=_bounded_int(raw.get("max_products_sample"), 200, minimum=1, maximum=2000),
        max_pages_sample=_bounded_int(raw.get("max_pages_sample"), 50, minimum=1, maximum=500),
        max_runs_per_site=_bounded_int(raw.get("max_runs_per_site"), 30, minimum=1, maximum=500),
        retention_days=_bounded_int(raw.get("retention_days"), 90, minimum=1, maximum=3650),
        default_schedule_interval_hours=_bounded_int(
            raw.get("default_schedule_interval_hours"), 24, minimum=1, maximum=720
        ),
        context_budgets=budgets,
        domains=domains,
    )


def load_shop_intel_config(path: Path | None = None) -> ShopIntelConfig:
    """Load ``config/shop_intel.yaml`` with fail-closed defaults and mtime caching."""
    global _CACHE_KEY, _CACHE_CONFIG
    cfg_path = Path(path) if path is not None else (_config_dir() / "shop_intel.yaml")
    try:
        mtime = int(cfg_path.stat().st_mtime_ns) if cfg_path.is_file() else None
    except OSError:
        mtime = None
    key = (str(cfg_path), mtime)
    if _CACHE_KEY == key and _CACHE_CONFIG is not None:
        return _CACHE_CONFIG

    raw = _builtin_raw_config()
    if cfg_path.is_file():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                raw.update(loaded)
        except Exception:
            raw = _builtin_raw_config()

    cfg = _build_config(raw)
    _CACHE_KEY = key
    _CACHE_CONFIG = cfg
    return cfg


def clear_shop_intel_config_cache() -> None:
    """Clear cached ``ShopIntelConfig`` (tests)."""
    global _CACHE_KEY, _CACHE_CONFIG
    _CACHE_KEY = None
    _CACHE_CONFIG = None


def shop_intel_kill_active() -> bool:
    """True when ``AZOM_SHOP_INTEL_KILL`` is set truthy in process env."""
    cfg = load_shop_intel_config()
    return env_flag(cfg.kill_switch_env, default=False)


def shop_intel_live_enabled() -> bool:
    """Fail-closed live gate: requires ``AZOM_USE_MOCK=0``, kill unset, and ``AZOM_SHOP_INTEL_LIVE_ENABLED=1``."""
    if is_mock_mode():
        return False
    if shop_intel_kill_active():
        return False
    cfg = load_shop_intel_config()
    return env_flag(cfg.live_enable_env, default=cfg.live_enabled)


def shop_intel_inject_enabled() -> bool:
    """Return True unless ``AZOM_SHOP_INTEL_INJECT`` explicitly disables context injection."""
    raw = os.environ.get("AZOM_SHOP_INTEL_INJECT")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


__all__ = [
    "DEFAULT_CONTEXT_BUDGETS",
    "ShopIntelConfig",
    "clear_shop_intel_config_cache",
    "is_mock_mode",
    "load_shop_intel_config",
    "shop_intel_inject_enabled",
    "shop_intel_kill_active",
    "shop_intel_live_enabled",
]
