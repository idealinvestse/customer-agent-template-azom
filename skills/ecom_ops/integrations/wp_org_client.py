"""Compatibility facade for bounded read-only api.wordpress.org metadata fetches."""

from __future__ import annotations

from ecom_ops.integrations.wp.wporg_client import (
    _DEFAULT_MAX_BYTES,
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_ALLOWED_BYTES,
    _MAX_TIMEOUT_SECONDS,
    _WPORG_PREFIX,
    WpOrgClientError,
    fetch_wporg_json,
)

__all__ = [
    "_DEFAULT_MAX_BYTES",
    "_DEFAULT_TIMEOUT_SECONDS",
    "_MAX_ALLOWED_BYTES",
    "_MAX_TIMEOUT_SECONDS",
    "_WPORG_PREFIX",
    "WpOrgClientError",
    "fetch_wporg_json",
]
