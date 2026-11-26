"""Compatibility facade for read-only WordPress component metadata reads.

The implementation lives in ``ecom_ops.integrations.wp.component_reads``.
"""

from __future__ import annotations

from ecom_ops.integrations.wp.component_reads import (
    _client_for_site,
    _public_row,
    load_plugin_rows,
    load_theme_rows,
    wp_client_for_site,
)

__all__ = [
    "_client_for_site",
    "_public_row",
    "load_plugin_rows",
    "load_theme_rows",
    "wp_client_for_site",
]
