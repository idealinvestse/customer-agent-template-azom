"""Typed error hierarchy for web search routing and page fetching.

Error messages stay short and safe: they never include API keys, tokens,
or raw provider HTTP response bodies.
"""

from __future__ import annotations

from ecom_ops.research.models import SearchPolicyError


class SearchRouteError(RuntimeError):
    """Base error for all search-route and fetch failures."""

    code: str = "route_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.http_status = http_status


class SearchConfigError(SearchRouteError):
    """Search route is disabled, unconfigured, or budget-exhausted."""

    code: str = "config"


class SearchAuthError(SearchRouteError):
    """Search provider authentication or authorization failure (401/403)."""

    code: str = "auth"


class SearchRateLimitError(SearchRouteError):
    """Search provider rejected the request due to rate limits or quota (429)."""

    code: str = "rate_limit"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after_s: float | None = None,
        retry_after: float | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, code=code, http_status=http_status)
        resolved = retry_after_s if retry_after_s is not None else retry_after
        self.retry_after_s = float(resolved) if resolved is not None else None


class SearchUnavailableError(SearchRouteError):
    """Search provider is temporarily unavailable or returned an unexpected response."""

    code: str = "unavailable"


__all__ = [
    "SearchAuthError",
    "SearchConfigError",
    "SearchPolicyError",
    "SearchRateLimitError",
    "SearchRouteError",
    "SearchUnavailableError",
]
