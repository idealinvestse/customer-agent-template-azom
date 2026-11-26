"""Foundational non-handler modules for WordPress agent tools."""

from __future__ import annotations

from ecom_ops.wp_ops.tools.context import context_for_storage, context_matches, valid_context
from ecom_ops.wp_ops.tools.guides import WP_DRIFT_TOOL_GUIDE, WP_TOOL_GUIDE
from ecom_ops.wp_ops.tools.html_parse import (
    HtmlMetaParser,
    ProductLinkParser,
    extract_html_by_selector,
    extract_json_ld_products,
    extract_product_dom_fields,
    parse_html_meta,
    parse_product_links,
    strip_html_plain,
)
from ecom_ops.wp_ops.tools.scrub import (
    scrub_bounded,
    scrub_keep_newlines,
    scrub_nested,
    site_bound_secrets,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_READS,
    FILE_LOOP_MAX_RECOVERY_RETRIES,
    FILE_LOOP_MAX_RESULT_BYTES,
    FILE_LOOP_MAX_SEARCHES,
    FILE_LOOP_MAX_SECONDS,
    FILE_LOOP_MAX_TOTAL_BYTES,
    FILE_READ_DEFAULT,
    FILE_READ_MAX,
    WP_TOOL_MAX_AGE_SECONDS,
    WP_TOOL_MAX_CALLS,
    WP_TOOL_MAX_RESULT_BYTES,
    WP_TOOL_MAX_SECONDS,
    WP_TOOL_MAX_TOTAL_BYTES,
    ToolContext,
    ToolLoopResult,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import (
    error_payload,
    is_expired,
    parse_tool_call,
    safe_failure,
    utf8_window,
)
from ecom_ops.wp_ops.tools.workspace_io import (
    authorize_editor_site,
    read_workspace_json,
    upsert_workspace_json,
)

__all__ = [
    "FILE_LOOP_MAX_CALLS",
    "FILE_LOOP_MAX_READS",
    "FILE_LOOP_MAX_RECOVERY_RETRIES",
    "FILE_LOOP_MAX_RESULT_BYTES",
    "FILE_LOOP_MAX_SEARCHES",
    "FILE_LOOP_MAX_SECONDS",
    "FILE_LOOP_MAX_TOTAL_BYTES",
    "FILE_READ_DEFAULT",
    "FILE_READ_MAX",
    "HtmlMetaParser",
    "ProductLinkParser",
    "ToolContext",
    "ToolLoopResult",
    "ToolOutput",
    "WP_DRIFT_TOOL_GUIDE",
    "WP_TOOL_GUIDE",
    "WP_TOOL_MAX_AGE_SECONDS",
    "WP_TOOL_MAX_CALLS",
    "WP_TOOL_MAX_RESULT_BYTES",
    "WP_TOOL_MAX_SECONDS",
    "WP_TOOL_MAX_TOTAL_BYTES",
    "authorize_editor_site",
    "context_for_storage",
    "context_matches",
    "error_payload",
    "extract_html_by_selector",
    "extract_json_ld_products",
    "extract_product_dom_fields",
    "is_expired",
    "parse_html_meta",
    "parse_product_links",
    "parse_tool_call",
    "read_workspace_json",
    "safe_failure",
    "scrub_bounded",
    "scrub_keep_newlines",
    "scrub_nested",
    "site_bound_secrets",
    "strip_html_plain",
    "upsert_workspace_json",
    "utf8_window",
    "valid_context",
]
