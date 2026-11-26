"""Compatibility facade for typed WordPress theme/plugin file editing.

Canonical implementations live in ``ecom_ops.wp_ops.file_paths``,
``ecom_ops.wp_ops.file_transport``, and ``ecom_ops.wp_ops.file_plans``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.file_paths import (
    _EXTENSIONS,
    _KINDS,
    _MAX_BYTES,
    _PLAN_ID,
    _PLAN_TTL,
    _SEGMENT,
    _SLUG,
    PathRejected,
    TransportError,
    _digest,
    component_dir,
    components_parent,
    resolve_file,
    transport_message,
)
from ecom_ops.wp_ops.file_paths import (
    plan_expired as _plan_expired,
)
from ecom_ops.wp_ops.file_plans import FileEditor, _redact_secret_lines
from ecom_ops.wp_ops.file_transport import FileTransport, LocalTree

__all__ = [
    "FileEditor",
    "FileTransport",
    "LocalTree",
    "PathRejected",
    "TransportError",
    "_EXTENSIONS",
    "_KINDS",
    "_MAX_BYTES",
    "_PLAN_ID",
    "_PLAN_TTL",
    "_SEGMENT",
    "_SLUG",
    "_digest",
    "_plan_expired",
    "_redact_secret_lines",
    "component_dir",
    "components_parent",
    "resolve_file",
    "transport_message",
]
