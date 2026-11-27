"""Shared HTTP transport protocol and retryable requests adapter for WooCommerce and WordPress."""

from __future__ import annotations

import os
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

from ecom_ops.security import SecurityError

_SECRET_TEXT_PATTERNS = (
    re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@"),
    re.compile(r"(?i)((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)((?:consumer_key|consumer_secret|password|token|secret|api_key)\s*[=:]\s*)[^\s&;,\"']+"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b(?:sk|ck|cs|pk)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9]{4}(?: [A-Za-z0-9]{4}){5}\b"),
)
_IDEMPOTENT_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})
_NON_SECRET_ENV_KEYS = frozenset({"WOO_BASE_URL", "WP_BASE_URL", "WP_USERNAME", "WORDPRESS_USERNAME", "AZOM_USE_MOCK"})


def _redact_error_text(text: str) -> str:
    cleaned = str(text or "")
    cleaned = _SECRET_TEXT_PATTERNS[0].sub(r"\1***@", cleaned)
    cleaned = _SECRET_TEXT_PATTERNS[1].sub(r"\1***", cleaned)
    cleaned = _SECRET_TEXT_PATTERNS[2].sub(r"\1***", cleaned)
    for pat in _SECRET_TEXT_PATTERNS[3:]:
        cleaned = pat.sub("***", cleaned)
    for key, val in os.environ.items():
        if key in _NON_SECRET_ENV_KEYS or key.startswith(("WOO_BASE_URL_", "WP_BASE_URL_", "WP_USERNAME_")):
            continue
        if (
            key.startswith(("WOO_", "WP_SECRET_", "WP_BRIDGE_SECRET_", "WP_APP_PASSWORD"))
            or any(m in key for m in ("SECRET", "PASSWORD", "TOKEN", "API_KEY"))
        ):
            s = str(val or "").strip()
            if len(s) >= 6 and s in cleaned:
                cleaned = cleaned.replace(s, "***")
    return cleaned


class HttpTransport(Protocol):
    requires_auth: bool

    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any: ...


class RequestsTransport:
    """Live HTTP transport with session reuse + retry/backoff for 429/5xx.

    Honors ``Retry-After`` and ``RateLimit-Retry-After`` headers when present.
    Retries are bounded (default 3) with exponential backoff.
    """

    requires_auth: bool = True

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        default_timeout: float = 30.0,
        service_name: str = "WooCommerce",
        max_retry_after: float = 30.0,
    ) -> None:
        if session is None:
            session = requests.Session()
            session.trust_env = False
        self.session = session
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.default_timeout = float(default_timeout)
        self.service_name = str(service_name or "HTTP")
        self.max_retry_after = max(0.0, float(max_retry_after))

    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        verb = str(method or "GET").strip().upper()
        idempotent = verb in _IDEMPOTENT_VERBS
        last_exc: Exception | None = None
        resp: Any = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    verb,
                    url,
                    auth=auth,
                    json=json,
                    params=params,
                    timeout=timeout or self.default_timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                last_exc = exc
                # Never auto-retry non-idempotent verbs on network/read timeouts.
                if not idempotent or attempt >= self.max_retries:
                    msg = _redact_error_text(f"{self.service_name} transport error: {exc}")
                    raise SecurityError(msg) from exc
                self._sleep_backoff(attempt)
                continue

            if idempotent:
                retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
            elif verb == "POST":
                retry_statuses = frozenset({429})
            else:
                retry_statuses = frozenset()
            if resp.status_code in retry_statuses:
                if attempt >= self.max_retries:
                    break
                retry_after = self._retry_after(resp)
                wait = (
                    retry_after
                    if retry_after is not None
                    else self._sleep_backoff(attempt, return_wait=True)
                )
                time.sleep(wait)
                continue

            if resp.status_code >= 300:
                msg = _redact_error_text(f"{self.service_name} API error {resp.status_code}: {resp.text[:300]}")
                raise SecurityError(msg)
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                msg = _redact_error_text(f"{self.service_name} API returned non-JSON body: {resp.text[:200]}")
                raise SecurityError(msg) from exc

        if last_exc is not None:
            raise SecurityError(_redact_error_text(f"{self.service_name} transport error: {last_exc}"))
        raise SecurityError(
            _redact_error_text(
                f"{self.service_name} API error {resp.status_code} after {self.max_retries} retries: "
                f"{resp.text[:300]}"
            )
        )

    def _retry_after(self, resp: requests.Response) -> float | None:
        """Return seconds to wait from Retry-After / RateLimit-Retry-After.

        Supports both delta-seconds (RFC 7231 §7.1.3) and HTTP-date format,
        capped at ``self.max_retry_after``.
        """
        for header in ("Retry-After", "RateLimit-Retry-After"):
            val = resp.headers.get(header)
            if not val:
                continue
            try:
                return min(self.max_retry_after, max(0.0, float(val)))
            except ValueError:
                pass
            try:
                dt = parsedate_to_datetime(val)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    now = datetime.now(UTC)
                    wait = (dt - now).total_seconds()
                    return min(self.max_retry_after, max(0.0, wait))
            except (TypeError, ValueError):
                pass
        return None

    def _sleep_backoff(self, attempt: int, *, return_wait: bool = False) -> float | None:
        base = self.backoff_base * (2**attempt)
        wait = base * (0.75 + 0.5 * random.random()) if base > 0 else 0.0
        if not return_wait:
            time.sleep(wait)
            return None
        return wait


__all__ = ["HttpTransport", "RequestsTransport"]
