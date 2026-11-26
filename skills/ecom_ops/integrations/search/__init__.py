"""Isolated external web search and public page-fetch integrations (lazy exports)."""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ALLOWED_FETCH_MIME_TYPES": ("ecom_ops.integrations.search.fetcher", "ALLOWED_FETCH_MIME_TYPES"),
    "FetchResult": ("ecom_ops.integrations.search.base", "FetchResult"),
    "LIVE_ROUTE_IDS": ("ecom_ops.integrations.search.router", "LIVE_ROUTE_IDS"),
    "SafePublicHttpClient": ("ecom_ops.integrations.search.fetcher", "SafePublicHttpClient"),
    "SearchAuthError": ("ecom_ops.integrations.search.errors", "SearchAuthError"),
    "SearchCapabilities": ("ecom_ops.integrations.search.base", "SearchCapabilities"),
    "SearchConfigError": ("ecom_ops.integrations.search.errors", "SearchConfigError"),
    "SearchHit": ("ecom_ops.integrations.search.base", "SearchHit"),
    "SearchPolicyError": ("ecom_ops.integrations.search.errors", "SearchPolicyError"),
    "SearchRateLimitError": ("ecom_ops.integrations.search.errors", "SearchRateLimitError"),
    "SearchRequest": ("ecom_ops.integrations.search.base", "SearchRequest"),
    "SearchResult": ("ecom_ops.integrations.search.base", "SearchResult"),
    "SearchRoute": ("ecom_ops.integrations.search.base", "SearchRoute"),
    "SearchRouteError": ("ecom_ops.integrations.search.errors", "SearchRouteError"),
    "SearchRouter": ("ecom_ops.integrations.search.router", "SearchRouter"),
    "SearchUnavailableError": ("ecom_ops.integrations.search.errors", "SearchUnavailableError"),
    "extract_clean_text_from_html": (
        "ecom_ops.integrations.search.fetcher",
        "extract_clean_text_from_html",
    ),
    "get_search_router": ("ecom_ops.integrations.search.router", "get_search_router"),
    "reset_search_router": ("ecom_ops.integrations.search.router", "reset_search_router"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    mod = importlib.import_module(mod_name)
    value = getattr(mod, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = sorted(_LAZY_EXPORTS)
