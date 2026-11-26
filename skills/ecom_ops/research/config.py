"""Fail-closed configuration and gate helpers for web search and deep research.

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

_MOCK_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_CACHE_KEY: tuple[str, int | None] | None = None
_CACHE_CONFIG: ResearchConfig | None = None


@dataclass(frozen=True)
class DepthTierConfig:
    max_rounds: int
    max_queries: int
    max_fetches: int
    max_wall_s: int
    max_llm_calls: int


@dataclass(frozen=True)
class ResearchBudgetsConfig:
    max_queries_per_day: int = 200
    max_fetches_per_day: int = 300
    monthly_external_usd_cap: float = 25.0
    warn_ratio: float = 0.8
    per_turn_quick_search_max: int = 3
    per_turn_quick_fetch_max: int = 2
    max_concurrent_jobs: int = 2
    depth_tiers: dict[str, DepthTierConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchCacheConfig:
    enabled: bool = True
    default_ttl_hours: int = 6
    docs_ttl_hours: int = 24
    news_ttl_hours: int = 1
    max_entries: int = 1000
    max_mb: int = 50


@dataclass(frozen=True)
class ResearchRetentionConfig:
    artifact_ttl_days: int = 14
    artifact_grace_hours: int = 24


@dataclass(frozen=True)
class ResearchEgressConfig:
    https_only: bool = True
    max_redirects: int = 3
    connect_timeout_s: int = 5
    fetch_timeout_s: int = 10
    max_raw_bytes: int = 524_288
    max_decompressed_bytes: int = 1_048_576
    max_extracted_chars: int = 12_000
    min_domain_interval_s: float = 2.0
    max_retry_after_s: float = 5.0
    denylist_domains: tuple[str, ...] = ()
    allowlist_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchConfig:
    schema_version: int = 1
    enabled: bool = False
    kill_switch_env: str = "AZOM_WEB_SEARCH_KILL"
    research_kill_switch_env: str = "AZOM_RESEARCH_KILL"
    default_route_order: tuple[str, ...] = ("parallel", "exa", "brave", "tavily", "searxng")
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    budgets: ResearchBudgetsConfig = field(default_factory=ResearchBudgetsConfig)
    cache: ResearchCacheConfig = field(default_factory=ResearchCacheConfig)
    retention: ResearchRetentionConfig = field(default_factory=ResearchRetentionConfig)
    egress: ResearchEgressConfig = field(default_factory=ResearchEgressConfig)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


def _builtin_raw_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": False,
        "kill_switch_env": "AZOM_WEB_SEARCH_KILL",
        "research_kill_switch_env": "AZOM_RESEARCH_KILL",
        "default_route_order": ["parallel", "exa", "brave", "tavily", "searxng"],
        "providers": {
            "parallel": {"enabled": True, "mode_quick": "fast", "mode_deep": "basic"},
            "exa": {"enabled": True, "search_type_quick": "auto", "search_type_deep": "deep"},
            "brave": {"enabled": True},
            "tavily": {
                "enabled": True,
                "search_depth_quick": "basic",
                "search_depth_deep": "advanced",
            },
            "searxng": {"enabled": False},
        },
        "budgets": {
            "max_queries_per_day": 200,
            "max_fetches_per_day": 300,
            "monthly_external_usd_cap": 25.0,
            "warn_ratio": 0.8,
            "per_turn_quick_search_max": 3,
            "per_turn_quick_fetch_max": 2,
            "max_concurrent_jobs": 2,
            "depth_tiers": {
                "quick": {
                    "max_rounds": 1,
                    "max_queries": 4,
                    "max_fetches": 4,
                    "max_wall_s": 45,
                    "max_llm_calls": 3,
                },
                "standard": {
                    "max_rounds": 2,
                    "max_queries": 8,
                    "max_fetches": 10,
                    "max_wall_s": 150,
                    "max_llm_calls": 6,
                },
                "deep": {
                    "max_rounds": 3,
                    "max_queries": 12,
                    "max_fetches": 15,
                    "max_wall_s": 300,
                    "max_llm_calls": 9,
                },
            },
        },
        "cache": {
            "enabled": True,
            "default_ttl_hours": 6,
            "docs_ttl_hours": 24,
            "news_ttl_hours": 1,
            "max_entries": 1000,
            "max_mb": 50,
        },
        "retention": {
            "artifact_ttl_days": 14,
            "artifact_grace_hours": 24,
        },
        "egress": {
            "https_only": True,
            "max_redirects": 3,
            "connect_timeout_s": 5,
            "fetch_timeout_s": 10,
            "max_raw_bytes": 524_288,
            "max_decompressed_bytes": 1_048_576,
            "max_extracted_chars": 12_000,
            "min_domain_interval_s": 2.0,
            "max_retry_after_s": 5.0,
            "denylist_domains": [],
            "allowlist_domains": [],
        },
        "profiles": {},
    }


def _as_domain_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [p.strip().lower().lstrip(".") for p in value.split(",")]
        return tuple(item for item in items if item)
    if isinstance(value, (list, tuple)):
        items = [str(x).strip().lower().lstrip(".") for x in value]
        return tuple(item for item in items if item)
    return ()


def _parse_depth_tiers(raw_tiers: Any, fallback_tiers: dict[str, Any]) -> dict[str, DepthTierConfig]:
    merged: dict[str, DepthTierConfig] = {}
    src = raw_tiers if isinstance(raw_tiers, dict) else {}
    for tier_name, default_vals in fallback_tiers.items():
        row = src.get(tier_name) if isinstance(src.get(tier_name), dict) else {}
        merged[tier_name] = DepthTierConfig(
            max_rounds=max(1, int(row.get("max_rounds", default_vals["max_rounds"]))),
            max_queries=max(1, int(row.get("max_queries", default_vals["max_queries"]))),
            max_fetches=max(0, int(row.get("max_fetches", default_vals["max_fetches"]))),
            max_wall_s=max(1, int(row.get("max_wall_s", default_vals["max_wall_s"]))),
            max_llm_calls=max(1, int(row.get("max_llm_calls", default_vals["max_llm_calls"]))),
        )
    return merged


def _build_research_config(raw: dict[str, Any]) -> ResearchConfig:
    defaults = _builtin_raw_config()
    budgets_raw = raw.get("budgets") if isinstance(raw.get("budgets"), dict) else {}
    budgets_def = defaults["budgets"]
    warn_ratio = float(budgets_raw.get("warn_ratio", budgets_def["warn_ratio"]))
    if warn_ratio <= 0 or warn_ratio > 1:
        warn_ratio = float(budgets_def["warn_ratio"])

    budgets = ResearchBudgetsConfig(
        max_queries_per_day=max(0, int(budgets_raw.get("max_queries_per_day", budgets_def["max_queries_per_day"]))),
        max_fetches_per_day=max(0, int(budgets_raw.get("max_fetches_per_day", budgets_def["max_fetches_per_day"]))),
        monthly_external_usd_cap=max(
            0.0,
            float(budgets_raw.get("monthly_external_usd_cap", budgets_def["monthly_external_usd_cap"])),
        ),
        warn_ratio=warn_ratio,
        per_turn_quick_search_max=max(
            1, int(budgets_raw.get("per_turn_quick_search_max", budgets_def["per_turn_quick_search_max"]))
        ),
        per_turn_quick_fetch_max=max(
            0, int(budgets_raw.get("per_turn_quick_fetch_max", budgets_def["per_turn_quick_fetch_max"]))
        ),
        max_concurrent_jobs=max(
            1, int(budgets_raw.get("max_concurrent_jobs", budgets_def["max_concurrent_jobs"]))
        ),
        depth_tiers=_parse_depth_tiers(budgets_raw.get("depth_tiers"), budgets_def["depth_tiers"]),
    )

    cache_raw = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
    cache_def = defaults["cache"]
    cache = ResearchCacheConfig(
        enabled=bool(cache_raw.get("enabled", cache_def["enabled"])),
        default_ttl_hours=max(0, int(cache_raw.get("default_ttl_hours", cache_def["default_ttl_hours"]))),
        docs_ttl_hours=max(0, int(cache_raw.get("docs_ttl_hours", cache_def["docs_ttl_hours"]))),
        news_ttl_hours=max(0, int(cache_raw.get("news_ttl_hours", cache_def["news_ttl_hours"]))),
        max_entries=max(1, int(cache_raw.get("max_entries", cache_def["max_entries"]))),
        max_mb=max(1, int(cache_raw.get("max_mb", cache_def["max_mb"]))),
    )

    ret_raw = raw.get("retention") if isinstance(raw.get("retention"), dict) else {}
    ret_def = defaults["retention"]
    retention = ResearchRetentionConfig(
        artifact_ttl_days=max(1, int(ret_raw.get("artifact_ttl_days", ret_def["artifact_ttl_days"]))),
        artifact_grace_hours=max(0, int(ret_raw.get("artifact_grace_hours", ret_def["artifact_grace_hours"]))),
    )

    egress_raw = raw.get("egress") if isinstance(raw.get("egress"), dict) else {}
    egress_def = defaults["egress"]
    egress = ResearchEgressConfig(
        https_only=bool(egress_raw.get("https_only", egress_def["https_only"])),
        max_redirects=max(0, int(egress_raw.get("max_redirects", egress_def["max_redirects"]))),
        connect_timeout_s=max(1, int(egress_raw.get("connect_timeout_s", egress_def["connect_timeout_s"]))),
        fetch_timeout_s=max(1, int(egress_raw.get("fetch_timeout_s", egress_def["fetch_timeout_s"]))),
        max_raw_bytes=max(1024, int(egress_raw.get("max_raw_bytes", egress_def["max_raw_bytes"]))),
        max_decompressed_bytes=max(
            1024, int(egress_raw.get("max_decompressed_bytes", egress_def["max_decompressed_bytes"]))
        ),
        max_extracted_chars=max(
            200, int(egress_raw.get("max_extracted_chars", egress_def["max_extracted_chars"]))
        ),
        min_domain_interval_s=max(
            0.0, float(egress_raw.get("min_domain_interval_s", egress_def["min_domain_interval_s"]))
        ),
        max_retry_after_s=max(
            0.0, float(egress_raw.get("max_retry_after_s", egress_def["max_retry_after_s"]))
        ),
        denylist_domains=_as_domain_tuple(egress_raw.get("denylist_domains")),
        allowlist_domains=_as_domain_tuple(egress_raw.get("allowlist_domains")),
    )

    route_raw = raw.get("default_route_order") or defaults["default_route_order"]
    route_order = tuple(str(r).strip().lower() for r in route_raw if str(r).strip())
    if not route_order:
        route_order = tuple(defaults["default_route_order"])

    providers_raw = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    providers: dict[str, dict[str, Any]] = {}
    for name, def_cfg in defaults["providers"].items():
        merged_p = dict(def_cfg)
        if isinstance(providers_raw.get(name), dict):
            merged_p.update(providers_raw[name])
        providers[name] = merged_p
    for name, p_cfg in providers_raw.items():
        if name not in providers and isinstance(p_cfg, dict):
            providers[str(name)] = dict(p_cfg)

    profiles_raw = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    profiles = {
        str(k): dict(v)
        for k, v in profiles_raw.items()
        if isinstance(v, dict)
    }

    return ResearchConfig(
        schema_version=int(raw.get("schema_version", 1)),
        enabled=bool(raw.get("enabled", False)),
        kill_switch_env=str(raw.get("kill_switch_env") or "AZOM_WEB_SEARCH_KILL").strip(),
        research_kill_switch_env=str(
            raw.get("research_kill_switch_env") or "AZOM_RESEARCH_KILL"
        ).strip(),
        default_route_order=route_order,
        providers=providers,
        budgets=budgets,
        cache=cache,
        retention=retention,
        egress=egress,
        profiles=profiles,
    )


def _research_yaml_path() -> Path:
    return _config_dir() / "research.yaml"


def load_research_config() -> ResearchConfig:
    """Load ``config/research.yaml`` with mtime caching and safe builtin fallback."""
    global _CACHE_KEY, _CACHE_CONFIG
    path = _research_yaml_path()
    try:
        mtime_ns = path.stat().st_mtime_ns if path.is_file() else None
    except OSError:
        mtime_ns = None
    key = (str(path), mtime_ns)
    if _CACHE_KEY == key and _CACHE_CONFIG is not None:
        return _CACHE_CONFIG

    raw: dict[str, Any] = _builtin_raw_config()
    if mtime_ns is not None:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            raw = _builtin_raw_config()

    cfg = _build_research_config(raw)
    _CACHE_KEY = key
    _CACHE_CONFIG = cfg
    return cfg


def clear_research_config_cache() -> None:
    """Clear the cached ``ResearchConfig`` (intended for tests)."""
    global _CACHE_KEY, _CACHE_CONFIG
    _CACHE_KEY = None
    _CACHE_CONFIG = None


def is_mock_mode() -> bool:
    """Return True unless ``AZOM_USE_MOCK`` is explicitly disabled."""
    raw = os.environ.get("AZOM_USE_MOCK")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in _MOCK_FALSE_VALUES


def kill_switch_active(scope: str | None = None) -> bool:
    """Return True when the relevant kill-switch env flag is active."""
    cfg = load_research_config()
    search_kill = env_flag(cfg.kill_switch_env, default=False)
    if scope in {"search", "fetch"}:
        return search_kill
    research_kill = env_flag(cfg.research_kill_switch_env, default=False)
    return search_kill or research_kill


def web_search_enabled() -> bool:
    """Live web search requires mock mode off, explicit enable flag, and kill switch unset."""
    if is_mock_mode():
        return False
    if kill_switch_active("search"):
        return False
    return env_flag("AZOM_WEB_SEARCH_ENABLED", default=False)


def web_fetch_enabled() -> bool:
    """Live web page fetch requires mock mode off, explicit enable flag, and kill switch unset."""
    if is_mock_mode():
        return False
    if kill_switch_active("fetch"):
        return False
    return env_flag("AZOM_WEB_FETCH_ENABLED", default=False)


def research_enabled() -> bool:
    """Live deep research requires mock mode off, explicit enable flag, and kill switches unset."""
    if is_mock_mode():
        return False
    if kill_switch_active("research"):
        return False
    return env_flag("AZOM_RESEARCH_ENABLED", default=False)


def effective_route_order(config: ResearchConfig | None = None) -> tuple[str, ...]:
    """Return route priority order, honoring ``AZOM_SEARCH_ROUTE_ORDER`` if set."""
    cfg = config or load_research_config()
    env_order = os.environ.get("AZOM_SEARCH_ROUTE_ORDER", "").strip()
    if env_order:
        items = tuple(p.strip().lower() for p in env_order.split(",") if p.strip())
        if items:
            return items
    return cfg.default_route_order


def effective_allowlist_domains(config: ResearchConfig | None = None) -> tuple[str, ...]:
    """Return configured domain allowlist, narrowed by ``AZOM_SEARCH_ALLOWED_DOMAINS``."""
    cfg = config or load_research_config()
    cfg_allow = cfg.egress.allowlist_domains
    env_raw = os.environ.get("AZOM_SEARCH_ALLOWED_DOMAINS", "").strip()
    if not env_raw:
        return cfg_allow
    env_allow = _as_domain_tuple(env_raw)
    if not cfg_allow:
        return env_allow
    cfg_set = set(cfg_allow)
    return tuple(d for d in env_allow if d in cfg_set or any(d.endswith(f".{c}") for c in cfg_set))


__all__ = [
    "DepthTierConfig",
    "ResearchBudgetsConfig",
    "ResearchCacheConfig",
    "ResearchConfig",
    "ResearchEgressConfig",
    "ResearchRetentionConfig",
    "clear_research_config_cache",
    "effective_allowlist_domains",
    "effective_route_order",
    "is_mock_mode",
    "kill_switch_active",
    "load_research_config",
    "research_enabled",
    "web_fetch_enabled",
    "web_search_enabled",
]
