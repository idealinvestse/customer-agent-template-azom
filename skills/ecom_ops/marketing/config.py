"""Load config/marketing.yaml with safe defaults (mutates off)."""

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


def _env_id_list(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class MarketingConfig:
    ga4_property_ids: tuple[str, ...]
    google_ads_customer_ids: tuple[str, ...]
    google_ads_login_customer_id: str
    ads_mutate_enabled: bool
    ga_mutate_enabled: bool
    measurement_protocol_enabled: bool
    merchant_write_enabled: bool
    ads_mutate_kill_env: str
    ga_mutate_kill_env: str
    mp_kill_env: str
    purchase_divergence_warn_pct: float
    purchase_divergence_error_pct: float
    waste_min_cost_micros: int
    pacing_alert_ratio: float
    default_currency: str
    default_lookback_days: int


@lru_cache(maxsize=1)
def load_marketing_config() -> MarketingConfig:
    path = _config_dir() / "marketing.yaml"
    raw: dict[str, Any] = {}
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            raw = loaded

    env_ga = _env_id_list("AZOM_GA4_PROPERTY_IDS")
    env_ads = _env_id_list("AZOM_GADS_CUSTOMER_IDS")
    ga_ids = env_ga if env_ga is not None else _as_str_list(raw.get("ga4_property_ids"))
    ads_ids = (
        env_ads
        if env_ads is not None
        else _as_str_list(raw.get("google_ads_customer_ids"))
    )
    login = (
        os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
        or str(raw.get("google_ads_login_customer_id") or "").strip()
    )
    return MarketingConfig(
        ga4_property_ids=tuple(ga_ids),
        google_ads_customer_ids=tuple(ads_ids),
        google_ads_login_customer_id=login.replace("-", ""),
        ads_mutate_enabled=bool(raw.get("ads_mutate_enabled", False)),
        ga_mutate_enabled=bool(raw.get("ga_mutate_enabled", False)),
        measurement_protocol_enabled=bool(
            raw.get("measurement_protocol_enabled", False)
        ),
        merchant_write_enabled=bool(raw.get("merchant_write_enabled", False)),
        ads_mutate_kill_env=str(
            raw.get("ads_mutate_kill_env") or "AZOM_ADS_MUTATE_KILL"
        ),
        ga_mutate_kill_env=str(raw.get("ga_mutate_kill_env") or "AZOM_GA_MUTATE_KILL"),
        mp_kill_env=str(raw.get("mp_kill_env") or "AZOM_MP_KILL"),
        purchase_divergence_warn_pct=float(
            raw.get("purchase_divergence_warn_pct", 25)
        ),
        purchase_divergence_error_pct=float(
            raw.get("purchase_divergence_error_pct", 50)
        ),
        waste_min_cost_micros=int(raw.get("waste_min_cost_micros", 5_000_000)),
        pacing_alert_ratio=float(raw.get("pacing_alert_ratio", 1.15)),
        default_currency=str(raw.get("default_currency") or "SEK"),
        default_lookback_days=int(raw.get("default_lookback_days", 7)),
    )


def clear_marketing_config_cache() -> None:
    load_marketing_config.cache_clear()
