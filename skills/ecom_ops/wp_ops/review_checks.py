"""Compatibility facade for named WordPress review checks.

Canonical implementation lives in ``ecom_ops.wp_ops.review.checks``.
"""

from __future__ import annotations

from ecom_ops.wp_ops.review.checks import (
    JOBS,
    PROFILE,
    PROFILE_VERSION,
    bind_artifacts,
    public_policy,
    requirements,
    run_catalog,
    run_is_publishable,
    run_job,
    stamp_run,
    suite_status,
)

__all__ = [
    "JOBS",
    "PROFILE",
    "PROFILE_VERSION",
    "bind_artifacts",
    "public_policy",
    "requirements",
    "run_catalog",
    "run_is_publishable",
    "run_job",
    "stamp_run",
    "suite_status",
]
