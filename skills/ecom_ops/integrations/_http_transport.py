"""Shared HTTP transport protocol and retryable requests adapter for WooCommerce and WordPress."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

from ecom_ops.security import SecurityError


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
    ) -> None:
        self.session = session or requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.default_timeout = float(default_timeout)
        self.service_name = str(service_name or "HTTP")

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
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    auth=auth,
                    json=json,
                    params=params,
                    timeout=timeout or self.default_timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise SecurityError(f"{self.service_name} transport error: {exc}") from exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code in {429, 500, 502, 503, 504}:
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

            if resp.status_code >= 400:
                raise SecurityError(
                    f"{self.service_name} API error {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise SecurityError(
                    f"{self.service_name} API returned non-JSON body: {resp.text[:200]}"
                ) from exc

        if last_exc is not None:
            raise SecurityError(f"{self.service_name} transport error: {last_exc}")
        raise SecurityError(
            f"{self.service_name} API error {resp.status_code} after {self.max_retries} retries: "
            f"{resp.text[:300]}"
        )

    def _retry_after(self, resp: requests.Response) -> float | None:
        """Return seconds to wait from Retry-After / RateLimit-Retry-After.

        Supports both delta-seconds (RFC 7231 §7.1.3) and HTTP-date format.
        """
        for header in ("Retry-After", "RateLimit-Retry-After"):
            val = resp.headers.get(header)
            if not val:
                continue
            try:
                return max(0.0, float(val))
            except ValueError:
                pass
            try:
                dt = parsedate_to_datetime(val)
                if dt is not None:
                    now = datetime.now(UTC)
                    wait = (dt - now).total_seconds()
                    return max(0.0, wait)
            except (TypeError, ValueError):
                pass
        return None

    def _sleep_backoff(self, attempt: int, *, return_wait: bool = False) -> float | None:
        wait = self.backoff_base * (2**attempt)
        if not return_wait:
            time.sleep(wait)
            return None
        return wait


__all__ = ["HttpTransport", "RequestsTransport"]
