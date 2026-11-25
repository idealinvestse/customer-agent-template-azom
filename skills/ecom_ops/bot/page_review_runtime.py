"""Bind a homepage review client outside the WordPress policy package."""

from __future__ import annotations

import os
from typing import Any

from ecom_ops.rbac import Permission, resolve_actor
from ecom_ops.wp_ops.agent_tools import component_rest_source
from ecom_ops.wp_ops.credentials import bind_credential
from ecom_ops.wp_ops.file_changes import rest_reads_allowed
from ecom_ops.wp_ops.registry import load_sites, site_by_id


def page_review_client(site_id: str, actor: str) -> Any | None:
    """Create a read client only for an authorized, registered live site.

    The caller supplies a site id selected by the conversation context. No URL,
    host, credentials, or arbitrary transport comes from model arguments.
    """
    try:
        person = resolve_actor(actor)
        if not person.has(Permission.WP_OPS_READ):
            return None
        site = site_by_id(load_sites(), str(site_id or ""))
        if site is None or os.environ.get("AZOM_USE_MOCK", "1").strip() == "1":
            return None
        if not rest_reads_allowed():
            return None
        bound = bind_credential(site)
        if bound.present and ":" in bound.value:
            username, app_password = bound.value.split(":", 1)
            if username.strip() and app_password.strip():
                from ecom_ops.integrations.wordpress import WordPressClient

                return WordPressClient(
                    base_url=site.canonical_url,
                    username=username.strip(),
                    app_password=app_password.strip(),
                )
        if component_rest_source(site.canonical_url) != "live":
            return None
        from ecom_ops.integrations.wordpress import wp_client_from_env

        return wp_client_from_env(use_mock=False)
    except Exception:
        return None
