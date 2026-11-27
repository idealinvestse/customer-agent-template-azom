"""Search provider adapters and shared pinned HTTPS JSON transport helpers."""

from __future__ import annotations

import email.utils
import http.client
import json
import math
import os
import ssl
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search.errors import (
    SearchAuthError,
    SearchConfigError,
    SearchRateLimitError,
    SearchRouteError,
    SearchUnavailableError,
)
from ecom_ops.research.config import load_research_config
from ecom_ops.research.firewall import OutboundQueryFirewall, QueryTaintContext

_DEFAULT_TIMEOUT_CONNECT_S = 5.0
_DEFAULT_TIMEOUT_READ_S = 10.0


def env_secret(name: str) -> str:
    """Read a trimmed API key from process env without logging it."""
    return str(os.environ.get(name) or "").strip()


def require_api_key(route_id: str, env_name: str) -> str:
    """Return the configured API key or raise ``SearchConfigError``."""
    val = env_secret(env_name)
    if not val:
        raise SearchConfigError(f"{route_id}: missing {env_name}", code="missing_key")
    return val


def scrub_query_for_provider(
    query: str,
    *,
    taint: QueryTaintContext | None = None,
) -> str:
    """Run ``OutboundQueryFirewall.check_query`` defensively before any provider call."""
    return OutboundQueryFirewall().check_query(query, taint=taint)


def parse_retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Parse a numeric or HTTP-date ``Retry-After`` header in seconds, or return None."""
    if not headers:
        return None
    for key, val in headers.items():
        if str(key).lower() == "retry-after":
            raw = str(val).strip()
            if not raw:
                return None
            try:
                sec = float(raw)
                return sec if math.isfinite(sec) and sec >= 0 else None
            except ValueError:
                pass
            try:
                dt = email.utils.parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                delta = (dt - datetime.now(UTC)).total_seconds()
                return max(0.0, delta) if math.isfinite(delta) else None
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def http_status_to_search_error(
    route_id: str,
    status: int,
    headers: Mapping[str, str] | None = None,
) -> SearchRouteError:
    """Map an HTTP status code to a sanitized ``SearchRouteError`` without body text."""
    if status in (401, 403):
        return SearchAuthError(
            f"{route_id}: authentication failed (HTTP {status})",
            code="auth",
            http_status=status,
        )
    if status == 429:
        retry_after_s = parse_retry_after_seconds(headers)
        return SearchRateLimitError(
            f"{route_id}: rate limited (HTTP 429)",
            code="rate_limit",
            retry_after_s=retry_after_s,
            http_status=429,
        )
    return SearchUnavailableError(
        f"{route_id}: provider returned HTTP {status}",
        code=f"http_{status}",
        http_status=status,
    )


def pinned_json_request(
    route_id: str,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    allowed_auth_origins: Collection[str],
    max_redirects: int = 2,
) -> dict[str, Any]:
    """Perform a pinned HTTPS JSON request and return parsed JSON without logging raw error bodies."""
    cfg = load_research_config()
    connect_timeout = float(cfg.egress.connect_timeout_s or _DEFAULT_TIMEOUT_CONNECT_S)
    read_timeout = float(cfg.egress.fetch_timeout_s or _DEFAULT_TIMEOUT_READ_S)

    req_headers = {"Accept": "application/json", **(dict(headers) if headers else {})}
    body_bytes: bytes | None = None
    if json_body is not None:
        req_headers.setdefault("Content-Type", "application/json")
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

    try:
        status, resp_headers, payload, _final_url = _http_guard.pinned_https_request(
            url,
            method=method,
            headers=req_headers,
            body=body_bytes,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_redirects=max_redirects,
            max_raw_bytes=int(cfg.egress.max_raw_bytes),
            max_decompressed_bytes=int(cfg.egress.max_decompressed_bytes),
            allowed_content_types=("application/json", "application/problem+json"),
            allowed_auth_origins=allowed_auth_origins,
        )
    except (OSError, TimeoutError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
        raise SearchUnavailableError(
            f"{route_id}: transport error ({type(exc).__name__})",
            code="transport_error",
        ) from exc

    if status < 200 or status >= 300:
        # Never decode, log, or surface raw provider HTTP error bodies.
        raise http_status_to_search_error(route_id, status, resp_headers)

    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SearchUnavailableError(
            f"{route_id}: invalid JSON payload",
            code="invalid_json",
            http_status=status,
        ) from exc
    if not isinstance(data, dict):
        raise SearchUnavailableError(
            f"{route_id}: unexpected JSON root type",
            code="invalid_json",
            http_status=status,
        )
    return data


__all__ = [
    "env_secret",
    "http_status_to_search_error",
    "parse_retry_after_seconds",
    "pinned_json_request",
    "require_api_key",
    "scrub_query_for_provider",
]
