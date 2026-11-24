"""Read-only WordPress component metadata adapter.

Callers must first bind the registered site, verify its configured host, and
confirm that live reads are explicitly enabled. This module has no write API.
"""

from __future__ import annotations

from typing import Any


def load_theme_rows() -> list[dict[str, str]]:
    from ecom_ops.integrations.wordpress import wp_client_from_env

    client = wp_client_from_env(use_mock=False)
    return [_public_row(row) for row in client.list_themes() if isinstance(row, dict)]


def load_plugin_rows() -> list[dict[str, str]]:
    from ecom_ops.integrations.wordpress import wp_client_from_env

    client = wp_client_from_env(use_mock=False)
    return [_public_row(row) for row in client.list_plugins() if isinstance(row, dict)]


def _public_row(row: dict[str, Any]) -> dict[str, str]:
    name = row.get("name")
    if isinstance(name, dict):
        name = name.get("rendered") or ""
    values = {
        "stylesheet": row.get("stylesheet"),
        "plugin": row.get("plugin"),
        "template": row.get("template"),
        "status": row.get("status"),
        "name": name,
    }
    return {key: value if isinstance(value, str) else "" for key, value in values.items()}
