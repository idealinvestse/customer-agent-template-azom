"""Read-only HTTP client for public api.wordpress.org plugin/theme metadata.

Strict fail-closed gates: only executes when ``AZOM_WPORG_INTEL=1`` and
``AZOM_USE_MOCK`` is off, and only queries ``https://api.wordpress.org/``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ecom_ops.wp_ops.config import wporg_intel_live_enabled

_DEFAULT_TIMEOUT_SECONDS = 5.0
_MAX_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_BYTES = 256 * 1024
_MAX_ALLOWED_BYTES = 512 * 1024
_WPORG_PREFIX = "https://api.wordpress.org/"


class WpOrgClientError(RuntimeError):
    """Raised when a bounded WordPress.org metadata fetch fails or is refused."""


def fetch_wporg_json(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Fetch a bounded JSON payload from ``https://api.wordpress.org/``."""
    if not wporg_intel_live_enabled():
        raise WpOrgClientError("WordPress.org live intel is disabled")
    if not isinstance(url, str) or not url.startswith(_WPORG_PREFIX):
        raise ValueError("Refused non-wordpress.org intel endpoint")

    safe_timeout = min(max(float(timeout), 0.5), _MAX_TIMEOUT_SECONDS)
    safe_limit = min(max(int(max_bytes), 1024), _MAX_ALLOWED_BYTES)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "azom-ecom-ops/3.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=safe_timeout) as resp:
            raw = resp.read(safe_limit + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WpOrgClientError(f"WordPress.org fetch failed ({type(exc).__name__})") from exc

    if len(raw) > safe_limit:
        raise WpOrgClientError("WordPress.org response exceeded size limit")

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise WpOrgClientError("WordPress.org returned invalid JSON") from exc

    if not isinstance(payload, dict):
        return {"error": "Unexpected response shape"}
    return payload
