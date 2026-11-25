"""Read-only WordPress component metadata adapter.

Callers must first bind the registered site, verify its configured host, and
confirm that live reads are explicitly enabled. This module has no write API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ecom_ops.integrations.wordpress import WordPressClient
    from ecom_ops.wp_ops.models import Site


def _client_for_site(site: Site | None) -> WordPressClient:
    from ecom_ops.integrations.wordpress import WordPressClient, wp_client_from_env

    if site is not None:
        from ecom_ops.wp_ops.credentials import bind_credential

        bound = bind_credential(site)
        if bound.present and ":" in bound.value:
            username, app_password = bound.value.split(":", 1)
            user_clean = username.strip()
            pass_clean = app_password.strip()
            if user_clean and pass_clean:
                return WordPressClient(
                    base_url=site.canonical_url,
                    username=user_clean,
                    app_password=pass_clean,
                )
    return wp_client_from_env(use_mock=False)


def load_theme_rows(*, site: Site | None = None) -> list[dict[str, str]]:
    client = _client_for_site(site)
    return [_public_row(row) for row in client.list_themes() if isinstance(row, dict)]


def load_plugin_rows(*, site: Site | None = None) -> list[dict[str, str]]:
    client = _client_for_site(site)
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
        "version": row.get("version"),
        "requires_wp": row.get("requires_wp"),
        "requires_php": row.get("requires_php"),
    }
    return {key: value if isinstance(value, str) else "" for key, value in values.items()}
