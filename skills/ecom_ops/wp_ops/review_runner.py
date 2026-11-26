"""Compatibility facade for the WordPress review sandbox gate.

Canonical implementation lives in ``ecom_ops.wp_ops.review.runner``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.runner import (
    FORBIDDEN_ENV_NAMES,
    SandboxGate,
    empty_record,
    public_sandbox_record,
    sandbox_enabled,
    synthetic_env,
    unavailable_destroyed_record,
    versions_unknown,
)

__all__ = [
    "FORBIDDEN_ENV_NAMES",
    "SandboxGate",
    "empty_record",
    "public_sandbox_record",
    "sandbox_enabled",
    "synthetic_env",
    "unavailable_destroyed_record",
    "versions_unknown",
]
