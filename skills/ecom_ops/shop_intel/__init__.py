"""Azom Shop Intelligence package: read-only, zero-PII store learning & security drift."""

from __future__ import annotations

from ecom_ops.shop_intel.analyzer import (
    analyze_shop_signals,
    compute_drift_and_security,
    render_dossier_markdown,
)
from ecom_ops.shop_intel.collectors import collect_shop_signals, extract_url_host
from ecom_ops.shop_intel.config import (
    DEFAULT_CONTEXT_BUDGETS,
    ShopIntelConfig,
    clear_shop_intel_config_cache,
    load_shop_intel_config,
    shop_intel_inject_enabled,
    shop_intel_kill_active,
    shop_intel_live_enabled,
)
from ecom_ops.shop_intel.context_builder import build_tailored_context
from ecom_ops.shop_intel.models import (
    SHOP_INTEL_DIFF_ID_RE,
    SHOP_INTEL_FINDING_ID_RE,
    SHOP_INTEL_INTENT_RE,
    SHOP_INTEL_RUN_ID_RE,
    VALID_DOMAINS,
    VALID_DOMAINS_SET,
    VALID_MODES,
    VALID_PURPOSES,
    VALID_SEVERITIES,
    IntelDiff,
    IntelFact,
    IntelRun,
    IntelSchedule,
    SecurityFinding,
    canonical_sha256,
    new_diff_id,
    new_finding_id,
    new_run_id,
)
from ecom_ops.shop_intel.service import (
    ShopIntelService,
    get_shop_intel_service,
    reset_shop_intel_service,
)
from ecom_ops.shop_intel.store import ShopIntelStore, scrub_payload, scrub_text

__all__ = [
    "DEFAULT_CONTEXT_BUDGETS",
    "IntelDiff",
    "IntelFact",
    "IntelRun",
    "IntelSchedule",
    "SHOP_INTEL_DIFF_ID_RE",
    "SHOP_INTEL_FINDING_ID_RE",
    "SHOP_INTEL_INTENT_RE",
    "SHOP_INTEL_RUN_ID_RE",
    "SecurityFinding",
    "ShopIntelConfig",
    "ShopIntelService",
    "ShopIntelStore",
    "VALID_DOMAINS",
    "VALID_DOMAINS_SET",
    "VALID_MODES",
    "VALID_PURPOSES",
    "VALID_SEVERITIES",
    "analyze_shop_signals",
    "build_tailored_context",
    "canonical_sha256",
    "clear_shop_intel_config_cache",
    "collect_shop_signals",
    "compute_drift_and_security",
    "extract_url_host",
    "get_shop_intel_service",
    "load_shop_intel_config",
    "new_diff_id",
    "new_finding_id",
    "new_run_id",
    "render_dossier_markdown",
    "reset_shop_intel_service",
    "scrub_payload",
    "scrub_text",
    "shop_intel_inject_enabled",
    "shop_intel_kill_active",
    "shop_intel_live_enabled",
]
