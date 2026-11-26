"""Compatibility facade for WordPress review plans.

Canonical implementations live in ``ecom_ops.wp_ops.review.{store, risk,
check_executor, preview_coordinator, deploy, plans}``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.checks import run_catalog
from ecom_ops.wp_ops.review.plans import ReviewPlans
from ecom_ops.wp_ops.review.preview_coordinator import (
    _load_page_draft,
    _load_product_edits,
    safe_preview_html_path,
)
from ecom_ops.wp_ops.review.preview_coordinator import (
    collect_extra_css_files as _collect_extra_css_files,
)
from ecom_ops.wp_ops.review.preview_coordinator import (
    list_artifact_pngs as _list_artifact_pngs,
)
from ecom_ops.wp_ops.review.preview_coordinator import (
    safe_preview_html_path as _safe_preview_html_path,
)
from ecom_ops.wp_ops.review.risk import (
    review_risk,
    text_is_review_approval,
)
from ecom_ops.wp_ops.review.risk import (
    tree_risk as _tree_risk,
)
from ecom_ops.wp_ops.review.store import (
    _ARTIFACT_PNG,
    _BASELINE_REUSE_TTL_S,
    _FORK_BLOCKED_STATUSES,
    _NON_EDITABLE,
    _PLAN_GUARD,
    _PLAN_LOCKS,
    _PNG_MAGIC,
    _PREVIEW_TTL,
    _PUBLIC,
    _SANDBOX_ONLY_FILES,
    _STAGE_OK,
    _STORE,
    _TEMPLATE_HEADER,
    _TERMINAL,
    _TTL,
    _UNREACHABLE,
    _VARIANT_GROUP_RE,
    PlanStore,
    _clean_variant_group,
    _diff_ops,
    _fail,
    _generation,
    _LockBoth,
    _ok,
    _parse_time,
    _plan_lock,
    _prepared,
    _resolve_parent_theme,
    _sha,
    _text_files,
    _unified,
)
from ecom_ops.wp_ops.review.workspace_io import (
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
)

__all__ = [
    "PlanStore",
    "ReviewError",
    "ReviewPlans",
    "VerifiedPlugin",
    "VerifiedTheme",
    "_ARTIFACT_PNG",
    "_BASELINE_REUSE_TTL_S",
    "_FORK_BLOCKED_STATUSES",
    "_LockBoth",
    "_NON_EDITABLE",
    "_PLAN_GUARD",
    "_PLAN_LOCKS",
    "_PNG_MAGIC",
    "_PREVIEW_TTL",
    "_PUBLIC",
    "_SANDBOX_ONLY_FILES",
    "_STAGE_OK",
    "_STORE",
    "_TEMPLATE_HEADER",
    "_TERMINAL",
    "_TTL",
    "_UNREACHABLE",
    "_VARIANT_GROUP_RE",
    "_clean_variant_group",
    "_collect_extra_css_files",
    "_diff_ops",
    "_fail",
    "_generation",
    "_list_artifact_pngs",
    "_load_page_draft",
    "_load_product_edits",
    "_ok",
    "_parse_time",
    "_plan_lock",
    "_prepared",
    "_resolve_parent_theme",
    "_safe_preview_html_path",
    "_sha",
    "_text_files",
    "_tree_risk",
    "_unified",
    "review_risk",
    "run_catalog",
    "safe_preview_html_path",
    "text_is_review_approval",
]
