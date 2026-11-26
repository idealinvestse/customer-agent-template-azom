"""Foundational types, budget constants, and regex patterns for WordPress agent tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ecom_ops.wp_ops.models import SITE_ID_RE

WP_TOOL_MAX_CALLS = 3
WP_TOOL_MAX_SECONDS = 30.0
WP_TOOL_MAX_RESULT_BYTES = 4096
WP_TOOL_MAX_TOTAL_BYTES = 10_240
WP_TOOL_MAX_AGE_SECONDS = 12 * 60 * 60

FILE_LOOP_MAX_CALLS = 16
FILE_LOOP_MAX_RECOVERY_RETRIES = 4
FILE_LOOP_MAX_READS = 10
FILE_LOOP_MAX_SEARCHES = 5
FILE_LOOP_MAX_SECONDS = 60.0
FILE_LOOP_MAX_RESULT_BYTES = 28_672
FILE_LOOP_MAX_TOTAL_BYTES = 81_920

FILE_READ_DEFAULT = 8192
FILE_READ_MAX = 20480

OPERATION_ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|"
    r"wp-plan-[0-9a-f]{16})$"
)
COMPONENTS = frozenset({"core", "plugins", "themes"})
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
INTEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
FILE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,219}$")
FILE_PLAN_RE = re.compile(r"^fe-[a-f0-9]{12}$")
REVIEW_PLAN_RE = re.compile(r"^wr-[a-f0-9]{12}$")
VARIANT_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DASHBOARD_REVIEW_PATH_RE = re.compile(
    r"^(?:https?://(?:127\.0\.0\.1|localhost)(?::\d+)?)?/?wp/([a-z][a-z0-9-]{0,62})/reviews/(wr-[a-f0-9]{12})(?:/preview)?/?$",
    re.I,
)
FILE_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
CODE_INDEX_EXTENSIONS = frozenset(
    {".php", ".phtml", ".inc", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".json"}
)
HOOK_RE = re.compile(
    r"""\b(?:add_action|add_filter|do_action|apply_filters)\s*\(\s*(['"])([A-Za-z0-9_.:/-]{1,100})\1"""
)
WC_TESTED_RE = re.compile(r"(?im)^\s*(?:\*\s*)?WC\s+tested\s+up\s+to:\s*([^\r\n*]+)")
PAGE_DRAFT_FILE = "page_draft.json"
PAGE_DRAFT_TYPES = frozenset({"thankyou", "landing", "campaign", "comparison", "upsell", "faq", "custom"})
SAMPLE_SCENARIOS = frozenset({"thankyou", "landing", "campaign", "comparison", "upsell", "custom"})
PAGE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
PAGE_DRAFT_MAX_HTML_BYTES = 48 * 1024
PAGE_DRAFT_MAX_CSS_BYTES = 24 * 1024
RECOVERY_ERROR_CLASSES = frozenset(
    {"SiteRequired", "InvalidToolArguments", "InvalidSearch", "FileMissing", "InvalidPreviewPath"}
)
PREVIEW_FOCUS_SECTIONS = ("top", "header", "hero", "main", "footer", "bottom")
SCAFFOLD_ONLY_FILES = frozenset({"style.css", "functions.php", "screenshot.png", "readme.txt"})
PRODUCT_SLUG_RE = re.compile(r"^[a-z0-9%][a-z0-9._%-]{0,120}$")
PRODUCT_ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
PRODUCT_LINK_PATH_RE = re.compile(
    r"^/(?:produkt|product|produkter|products|vare)/([a-z0-9%][a-z0-9._%-]{0,120})/?$",
    re.I,
)
PRODUCT_EDIT_LIMITS: dict[str, int] = {
    "title": 200,
    "short_description": 2000,
    "description": 8000,
    "price": 64,
    "regular_price": 64,
    "sale_price": 64,
    "button_text": 120,
    "badge_text": 120,
}
PRODUCT_EDITS_FILE = "product_edits.json"


@dataclass(frozen=True)
class ToolContext:
    """Actor and conversation scope for one WordPress tool invocation."""

    actor: str
    site_id: str | None = None
    sticky_site_id: str | None = None
    channel: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class ToolOutput:
    data: dict[str, Any]
    site_id: str | None = None
    operation_ids: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    review_stage_plan_id: str | None = None


@dataclass(frozen=True)
class ToolLoopResult:
    handled: bool
    content: str
    results: tuple[dict[str, Any], ...] = ()
    site_id: str | None = None
    operation_ids: tuple[str, ...] = ()
    calls: int = 0
    tool_seconds: float = 0.0
    followup_cost_usd: float = 0.0
    model_error: bool = False
    stage_plan_id: str | None = None
    attachments: tuple[str, ...] = ()
    review_stage_plan_id: str | None = None
    model_error_class: str | None = None
    model_error_code: str | None = None
    active_review_meta: dict[str, Any] | None = None


__all__ = [
    "CODE_INDEX_EXTENSIONS",
    "COMPONENTS",
    "DASHBOARD_REVIEW_PATH_RE",
    "FILE_LOOP_MAX_CALLS",
    "FILE_LOOP_MAX_READS",
    "FILE_LOOP_MAX_RECOVERY_RETRIES",
    "FILE_LOOP_MAX_RESULT_BYTES",
    "FILE_LOOP_MAX_SEARCHES",
    "FILE_LOOP_MAX_SECONDS",
    "FILE_LOOP_MAX_TOTAL_BYTES",
    "FILE_PATH_RE",
    "FILE_PLAN_RE",
    "FILE_READ_DEFAULT",
    "FILE_READ_MAX",
    "FILE_SHA_RE",
    "HOOK_RE",
    "INTEL_SLUG_RE",
    "OPERATION_ID_RE",
    "PAGE_DRAFT_FILE",
    "PAGE_DRAFT_MAX_CSS_BYTES",
    "PAGE_DRAFT_MAX_HTML_BYTES",
    "PAGE_DRAFT_TYPES",
    "PAGE_SLUG_RE",
    "PREVIEW_FOCUS_SECTIONS",
    "PRODUCT_EDITS_FILE",
    "PRODUCT_EDIT_LIMITS",
    "PRODUCT_ID_RE",
    "PRODUCT_LINK_PATH_RE",
    "PRODUCT_SLUG_RE",
    "RECOVERY_ERROR_CLASSES",
    "REVIEW_PLAN_RE",
    "SAMPLE_SCENARIOS",
    "SCAFFOLD_ONLY_FILES",
    "SITE_ID_RE",
    "SLUG_RE",
    "ToolContext",
    "ToolLoopResult",
    "ToolOutput",
    "VARIANT_GROUP_RE",
    "WC_TESTED_RE",
    "WP_TOOL_MAX_AGE_SECONDS",
    "WP_TOOL_MAX_CALLS",
    "WP_TOOL_MAX_RESULT_BYTES",
    "WP_TOOL_MAX_SECONDS",
    "WP_TOOL_MAX_TOTAL_BYTES",
]
