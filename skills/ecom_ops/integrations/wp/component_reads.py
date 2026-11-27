"""Read-only WordPress component metadata adapter.

Callers must first bind the registered site, verify its configured host, and
confirm that live reads are explicitly enabled. This module has no write API.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ecom_ops.integrations.wordpress import WordPressClient
    from ecom_ops.wp_ops.models import Site

_FACADE_MODULE = "ecom_ops.integrations.wp_component_reads"


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)
    return default


def wp_client_for_site(
    site: Site | None,
    *,
    fallback_to_env: bool = True,
) -> WordPressClient | None:
    """Bind a per-site WordPressClient using ``site.secret_ref`` (``user:app_password``).

    When ``site`` has no valid ``user:app_password`` secret ref, falls back to
    ``wp_client_from_env(use_mock=False)`` if ``fallback_to_env`` is True;
    otherwise returns ``None``.
    """
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
    if not fallback_to_env:
        return None
    if site is not None and site.canonical_url:
        from urllib.parse import urlparse

        host = (urlparse(str(site.canonical_url)).hostname or "").lower()
        tail = host.rsplit(".", 1)[-1] if "." in host else ""
        if tail in {"no", "se", "dk", "fi"}:
            return wp_client_from_env(base_url=str(site.canonical_url), use_mock=False, domain=tail)
    return wp_client_from_env(use_mock=False)


_client_for_site = wp_client_for_site


def load_theme_rows(*, site: Site | None = None) -> list[dict[str, str]]:
    client = _facade_attr("wp_client_for_site", wp_client_for_site)(site)
    assert client is not None
    return [_public_row(row) for row in client.list_themes() if isinstance(row, dict)]


def load_plugin_rows(*, site: Site | None = None) -> list[dict[str, str]]:
    client = _facade_attr("wp_client_for_site", wp_client_for_site)(site)
    assert client is not None
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


__all__ = [
    "_client_for_site",
    "_public_row",
    "load_plugin_rows",
    "load_theme_rows",
    "wp_client_for_site",
]
