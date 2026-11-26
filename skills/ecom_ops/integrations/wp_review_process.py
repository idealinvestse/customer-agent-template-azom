"""Compatibility facade for the fixed, bounded PHP syntax check process.

The implementation lives in ``ecom_ops.integrations.wp.php_lint``; this module
re-exports the public API and documented monkeypatch targets.
"""

from __future__ import annotations

import shutil  # noqa: F401
import subprocess  # noqa: F401

from ecom_ops.integrations.wp.php_lint import (
    _artifact,
    _now,
    resolve_php_binary,
    run_php_lint,
)
from ecom_ops.integrations.wp_runtime_fetch import resolve_php_from_manifest

__all__ = [
    "_artifact",
    "_now",
    "resolve_php_binary",
    "resolve_php_from_manifest",
    "run_php_lint",
    "shutil",
    "subprocess",
]
