"""Opt-in self-hosted SearXNG JSON search adapter (``route_id="searxng"``).

Enabled ONLY when ``SEARXNG_BASE_URL`` is configured in process environment.
Accepts either:
- Public HTTPS URL (validated & executed via ``_http_guard.pinned_https_request``), or
- Explicit loopback ``http://127.0.0.1:<port>`` or ``http://[::1]:<port>`` ONLY when
  ``<port>`` is neither Azom's ``DASHBOARD_PORT`` (default 8080) nor the parsed port
  of ``AZOM_DASHBOARD_PUBLIC_URL``, pinned strictly to ``/search?format=json``,
  with ``max_redirects=0`` and ``ProxyHandler({})``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search.base import (
    SearchCapabilities,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from ecom_ops.integrations.search.errors import SearchConfigError, SearchUnavailableError
from ecom_ops.integrations.search.providers import (
    env_secret,
    http_status_to_search_error,
    pinned_json_request,
    scrub_query_for_provider,
)
from ecom_ops.research.config import load_research_config

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


class _RefuseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforce max_redirects=0 on loopback SearXNG requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirects are disabled for SearXNG",
            headers,
            fp,
        )


def _dashboard_forbidden_ports() -> set[int]:
    ports: set[int] = set()
    raw_dash_port = str(os.environ.get("DASHBOARD_PORT") or "8080").strip()
    try:
        ports.add(int(raw_dash_port))
    except ValueError:
        ports.add(8080)
    pub_raw = str(os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip()
    if pub_raw:
        try:
            pub_parsed = urllib.parse.urlparse(pub_raw if "://" in pub_raw else f"https://{pub_raw}")
            if pub_parsed.port is not None:
                ports.add(int(pub_parsed.port))
            elif pub_parsed.scheme.lower() == "https":
                ports.add(443)
            elif pub_parsed.scheme.lower() == "http":
                ports.add(80)
        except ValueError:
            pass
    return ports


def _parse_searxng_base_url(raw_url: str) -> tuple[str, str, str]:
    """Validate ``SEARXNG_BASE_URL`` and return ``(mode, origin, pinned_search_base)``.

    ``mode`` is ``"https_public"`` or ``"http_loopback"``.
    Raises ``SearchConfigError`` if missing or invalid.
    """
    raw = str(raw_url or "").strip()
    if not raw:
        raise SearchConfigError("searxng: SEARXNG_BASE_URL is not configured", code="missing_config")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "\\" in raw:
        raise SearchConfigError("searxng: invalid SEARXNG_BASE_URL", code="invalid_config")
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError as exc:
        raise SearchConfigError("searxng: malformed SEARXNG_BASE_URL", code="invalid_config") from exc

    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise SearchConfigError("searxng: userinfo not allowed in SEARXNG_BASE_URL", code="invalid_config")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise SearchConfigError("searxng: SEARXNG_BASE_URL must be an origin without path/query", code="invalid_config")

    scheme = parsed.scheme.lower()
    if scheme == "https":
        try:
            safe = _http_guard.safe_https_parsed(raw, allow_non_443=False)
        except ValueError as exc:
            raise SearchConfigError("searxng: unsafe HTTPS SEARXNG_BASE_URL", code="invalid_config") from exc
        origin = f"https://{safe.netloc}"
        return "https_public", origin, f"{origin}/search"

    if scheme == "http":
        host = (parsed.hostname or "").strip().lower()
        if host not in _LOOPBACK_HOSTS:
            raise SearchConfigError(
                "searxng: HTTP is allowed only for explicit 127.0.0.1 or [::1] loopback",
                code="invalid_config",
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise SearchConfigError("searxng: invalid loopback port", code="invalid_config") from exc
        if port is None or port < 1 or port > 65535:
            raise SearchConfigError("searxng: explicit port required for loopback", code="invalid_config")
        if port in _dashboard_forbidden_ports():
            raise SearchConfigError(
                "searxng: loopback port collides with dashboard port",
                code="invalid_config",
            )
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        origin = f"http://{netloc}"
        return "http_loopback", origin, f"{origin}/search"

    raise SearchConfigError("searxng: unsupported SEARXNG_BASE_URL scheme", code="invalid_config")


def _loopback_json_get(url: str, *, headers: dict[str, str], read_timeout: float, max_raw_bytes: int) -> dict[str, Any]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirectHandler(),
    )
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with opener.open(req, timeout=read_timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw_ct = str(resp.getheader("Content-Type") or "")
            mime = raw_ct.split(";", 1)[0].strip().lower()
            if 200 <= status < 300 and mime not in {"application/json", "application/problem+json"}:
                raise SearchUnavailableError("searxng: disallowed Content-Type", code="invalid_mime")
            payload = resp.read(max_raw_bytes + 1)
            if len(payload) > max_raw_bytes:
                raise SearchUnavailableError("searxng: response exceeded size cap", code="response_too_large")
    except urllib.error.HTTPError as exc:
        hdrs = {str(k).lower(): str(v) for k, v in (exc.headers.items() if exc.headers else ())}
        raise http_status_to_search_error("searxng", int(exc.code), hdrs) from exc
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise SearchUnavailableError(
            f"searxng: loopback request failed ({type(exc).__name__})",
            code="transport_error",
        ) from exc

    if status < 200 or status >= 300:
        raise http_status_to_search_error("searxng", status)

    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SearchUnavailableError("searxng: invalid JSON payload", code="invalid_json", http_status=status) from exc
    if not isinstance(data, dict):
        raise SearchUnavailableError("searxng: unexpected JSON root type", code="invalid_json", http_status=status)
    return data


class SearxngSearchRoute:
    """Opt-in SearXNG JSON API adapter."""

    route_id = "searxng"
    capabilities = SearchCapabilities(
        supports_country=False,
        supports_language=True,
        supports_freshness=True,
        supports_domain_filter=False,
        supports_content=False,
        supports_extract=False,
        supports_deep_mode=False,
    )

    def is_configured(self) -> bool:
        raw = env_secret("SEARXNG_BASE_URL")
        if not raw:
            return False
        try:
            _parse_searxng_base_url(raw)
            return True
        except SearchConfigError:
            return False

    def status(self) -> dict[str, Any]:
        raw = env_secret("SEARXNG_BASE_URL")
        mode = None
        if raw:
            try:
                mode, _, _ = _parse_searxng_base_url(raw)
            except SearchConfigError:
                mode = "invalid"
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "mode": mode,
        }

    def search(self, request: SearchRequest) -> SearchResult:
        raw_base = env_secret("SEARXNG_BASE_URL")
        mode, origin, search_base = _parse_searxng_base_url(raw_base)
        clean_query = scrub_query_for_provider(request.query, taint=request.taint)

        params: dict[str, str] = {
            "format": "json",
            "q": clean_query,
        }
        if request.language:
            lang = str(request.language).strip().lower().split("-")[0]
            if lang and lang.isalpha() and len(lang) <= 3:
                params["language"] = lang
        if request.freshness_days is not None:
            if request.freshness_days <= 1:
                params["time_range"] = "day"
            elif request.freshness_days <= 7:
                params["time_range"] = "week"
            elif request.freshness_days <= 31:
                params["time_range"] = "month"
            else:
                params["time_range"] = "year"

        target_url = f"{search_base}?{urllib.parse.urlencode(params)}"
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = env_secret("SEARXNG_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        cfg = load_research_config()
        started = time.monotonic()
        if mode == "https_public":
            data = pinned_json_request(
                self.route_id,
                target_url,
                method="GET",
                headers=headers,
                allowed_auth_origins=(origin,),
                max_redirects=0,
            )
        else:
            data = _loopback_json_get(
                target_url,
                headers=headers,
                read_timeout=float(cfg.egress.fetch_timeout_s or 10.0),
                max_raw_bytes=int(cfg.egress.max_raw_bytes),
            )
        latency_ms = int((time.monotonic() - started) * 1000)

        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchUnavailableError("searxng: invalid results field", code="invalid_json")

        hits: list[SearchHit] = []
        for idx, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            snippet = str(item.get("content") or "").strip()
            score_val = item.get("score")
            score = float(score_val) if isinstance(score_val, (int, float)) else max(0.1, 1.0 - idx * 0.05)
            pub = item.get("publishedDate") or item.get("pubdate")
            hits.append(
                SearchHit(
                    url=url,
                    title=str(item.get("title") or "").strip(),
                    snippet=snippet[:1200],
                    published_at=str(pub).strip() if pub else None,
                    provider="searxng",
                    score=score,
                    raw_excerpts=(snippet,) if snippet else (),
                    content=None,
                )
            )
            if len(hits) >= request.max_results:
                break

        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="searxng",
            hits=tuple(hits),
            provenance="live_api",
            latency_ms=latency_ms,
            cached=False,
            estimated_cost_usd=0.0,
        )


__all__ = ["SearxngSearchRoute"]
