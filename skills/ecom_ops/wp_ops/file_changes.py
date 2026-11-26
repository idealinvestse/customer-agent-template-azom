"""Compatibility facade for site-bound theme and plugin change plans.

Canonical implementations live in ``ecom_ops.wp_ops.file_paths``,
``ecom_ops.wp_ops.file_policy``, ``ecom_ops.wp_ops.search``, and
``ecom_ops.wp_ops.file_plans``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.config import rest_reads_allowed as _config_rest_reads_allowed
from ecom_ops.wp_ops.file_paths import (
    _MAX_BYTES,
    _SKIP_DIRS,
    FILE_TOOL_NAMES,
    plan_expired,
    resolve_dir,
    sensitive_relative,
)
from ecom_ops.wp_ops.file_plans import (
    FileChangeOps,
    FilePayloadStore,
    delete_payload,
    payload_path,
    read_payload,
    write_payload,
)
from ecom_ops.wp_ops.file_policy import (
    is_safe_pure_css_file,
    production_deploy_block,
    risk_for,
)
from ecom_ops.wp_ops.file_policy import (
    normalize_css_for_check as _normalize_css_for_check,
)
from ecom_ops.wp_ops.search import (
    ComponentSearcher,
    _normalize_extensions,
    _scrub_search_text,
    search_texts,
    validate_safe_regex,
)


def rest_reads_allowed() -> bool:
    """Live content reads only when mock mode is explicitly off."""
    return _config_rest_reads_allowed()


_directory = resolve_dir

__all__ = [
    "ComponentSearcher",
    "FILE_TOOL_NAMES",
    "FileChangeOps",
    "FilePayloadStore",
    "_MAX_BYTES",
    "_SKIP_DIRS",
    "_directory",
    "_normalize_css_for_check",
    "_normalize_extensions",
    "_scrub_search_text",
    "delete_payload",
    "is_safe_pure_css_file",
    "payload_path",
    "plan_expired",
    "production_deploy_block",
    "read_payload",
    "rest_reads_allowed",
    "risk_for",
    "search_texts",
    "sensitive_relative",
    "validate_safe_regex",
    "write_payload",
]
