"""Google Ads + GA4 marketing domain helpers."""

from ecom_ops.marketing.config import MarketingConfig, load_marketing_config
from ecom_ops.marketing.kill_switch import (
    ads_mutate_allowed,
    ga_mutate_allowed,
    mp_allowed,
)

__all__ = [
    "MarketingConfig",
    "load_marketing_config",
    "ads_mutate_allowed",
    "ga_mutate_allowed",
    "mp_allowed",
]
