"""Typed errors for model routing.

Messages stay short and safe. They never include access tokens, refresh
tokens, authorization codes, or raw provider response bodies.
"""

from __future__ import annotations


class ModelRouteError(RuntimeError):
    """Base error for all model-route failures."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ModelConfigError(ModelRouteError):
    """Route is selected but its required configuration is missing."""


class ModelAuthError(ModelRouteError):
    """Authentication, consent, token refresh, or authorization failure."""


class ModelRateLimitError(ModelRouteError):
    """Provider rejected the request because of quota or rate limiting."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after


class ModelUnavailableError(ModelRouteError):
    """Provider is temporarily unavailable or returned an unexpected response."""


class ModelSchemaError(ModelRouteError):
    """Model returned output that failed structured validation."""
