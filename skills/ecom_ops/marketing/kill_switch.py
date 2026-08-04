"""Kill-switches and mutate allow gates for marketing writes."""

from __future__ import annotations

import os

from ecom_ops.marketing.config import MarketingConfig, load_marketing_config

_TRUE = frozenset({"1", "true", "yes", "on"})


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def ads_mutate_allowed(config: MarketingConfig | None = None) -> tuple[bool, str]:
    cfg = config or load_marketing_config()
    if _env_on(cfg.ads_mutate_kill_env):
        return False, "ads_mutate_kill"
    if not cfg.ads_mutate_enabled:
        return False, "ads_mutate_disabled"
    return True, "eligible"


def ga_mutate_allowed(config: MarketingConfig | None = None) -> tuple[bool, str]:
    cfg = config or load_marketing_config()
    if _env_on(cfg.ga_mutate_kill_env):
        return False, "ga_mutate_kill"
    if not cfg.ga_mutate_enabled:
        return False, "ga_mutate_disabled"
    return True, "eligible"


def mp_allowed(config: MarketingConfig | None = None) -> tuple[bool, str]:
    cfg = config or load_marketing_config()
    if _env_on(cfg.mp_kill_env):
        return False, "mp_kill"
    if not cfg.measurement_protocol_enabled:
        return False, "mp_disabled"
    return True, "eligible"


def merchant_write_allowed(config: MarketingConfig | None = None) -> tuple[bool, str]:
    cfg = config or load_marketing_config()
    if _env_on(cfg.ads_mutate_kill_env):
        return False, "ads_mutate_kill"
    if not cfg.merchant_write_enabled:
        return False, "merchant_write_disabled"
    return True, "eligible"
