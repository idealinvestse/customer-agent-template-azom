"""Read-only Google Search Console (GSC) Search Analytics client.

Mock-first and fail-closed:
- Uses isolated OAuth credentials (``GSC_OAUTH_*`` / ``AZOM_GSC_*``) and read-only
  scope ``https://www.googleapis.com/auth/webmasters.readonly``. Never reuses
  Gmail, GA4, Google Ads, or Codex OAuth tokens.
- In mock mode (``AZOM_USE_MOCK=1`` or unset), returns deterministic Nordic
  CarPlay Search Analytics fixtures for ``se``, ``no``, and ``dk`` covering
  strike-distance queries (pos 4-15), low-CTR high-impression pages, and
  impression-weighted keyword cannibalization pairs.
- Live egress requires ``AZOM_USE_MOCK=0``, ``AZOM_GSC_ENABLED=1``,
  ``AZOM_GSC_KILL`` unset, and isolated GSC OAuth credentials.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ecom_ops.integrations._http_guard import pinned_https_request
from ecom_ops.integrations._http_transport import _redact_error_text
from ecom_ops.oauth.gsc import (
    GSC_READONLY_SCOPE as _OAUTH_GSC_SCOPE,
)
from ecom_ops.oauth.gsc import (
    GscOAuthStore,
    gsc_oauth_client_id,
    gsc_oauth_client_secret,
    gsc_oauth_configured,
    gsc_oauth_redirect_uri,
)
from ecom_ops.runtime_env import env_flag
from ecom_ops.security import SecurityError
from ecom_ops.wp_ops.config import is_mock_mode

GSC_READONLY_SCOPE = _OAUTH_GSC_SCOPE
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GSC_API_ORIGIN = "https://searchconsole.googleapis.com"
_OAUTH_ORIGIN = "https://oauth2.googleapis.com"
_VALID_MARKETS = frozenset({"se", "no", "dk"})

HttpRequestFn = Callable[..., tuple[int, dict[str, str], bytes, str]]


def _infer_market(site_url: str | None = None, market: str | None = None) -> str:
    if market:
        clean = str(market).strip().lower()
        if clean in _VALID_MARKETS:
            return clean
    if site_url:
        host = (urlparse(str(site_url).strip()).hostname or str(site_url)).lower()
        if host.endswith(".se") or ".se/" in host or "-se" in host:
            return "se"
        if host.endswith(".dk") or ".dk/" in host or "-dk" in host:
            return "dk"
        if host.endswith(".no") or ".no/" in host or "-no" in host:
            return "no"
    return "no"


def _resolve_property_url(site_url: str | None = None, market: str | None = None) -> str:
    mkt = _infer_market(site_url, market).upper()
    for key in (
        f"AZOM_GSC_PROPERTY_URL_{mkt}",
        f"GSC_PROPERTY_URL_{mkt}",
        "AZOM_GSC_PROPERTY_URL",
        "GSC_PROPERTY_URL",
    ):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    if site_url and str(site_url).strip():
        raw = str(site_url).strip()
        if raw.startswith("sc-domain:"):
            return raw
        return raw.rstrip("/") + "/"
    default_domains = {"SE": "https://azom.se/", "NO": "https://azom.no/", "DK": "https://azom.dk/"}
    return default_domains.get(mkt, "https://azom.no/")


def _isolated_gsc_credentials() -> dict[str, str]:
    """Read ONLY isolated GSC env vars or GscOAuthStore (never Gmail / GA4 / Marketing)."""
    client_id = gsc_oauth_client_id()
    client_secret = gsc_oauth_client_secret()
    refresh_token = (
        os.environ.get("GSC_OAUTH_REFRESH_TOKEN")
        or os.environ.get("AZOM_GSC_REFRESH_TOKEN")
        or ""
    ).strip()
    access_token = (
        os.environ.get("GSC_OAUTH_ACCESS_TOKEN")
        or os.environ.get("AZOM_GSC_ACCESS_TOKEN")
        or ""
    ).strip()
    if not refresh_token or not access_token:
        bundle = GscOAuthStore().load()
        if bundle:
            if not refresh_token and bundle.refresh_token:
                refresh_token = bundle.refresh_token
            if not access_token and bundle.access_token:
                if bundle.expires_at is None or time.time() < float(bundle.expires_at) - 60:
                    access_token = bundle.access_token
                elif not refresh_token:
                    access_token = bundle.access_token
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "access_token": access_token,
    }


def _has_gsc_credentials(creds: dict[str, str]) -> bool:
    if creds.get("access_token"):
        return True
    return bool(
        creds.get("refresh_token")
        and creds.get("client_id")
        and creds.get("client_secret")
    )


def gsc_kill_active() -> bool:
    """Return True when the ``AZOM_GSC_KILL`` kill switch is active."""
    return env_flag("AZOM_GSC_KILL")


def gsc_enabled() -> bool:
    """Return True only in live mode when ``AZOM_GSC_ENABLED=1`` and not killed."""
    if is_mock_mode():
        return False
    if gsc_kill_active():
        return False
    return env_flag("AZOM_GSC_ENABLED")


def gsc_status(
    site_url: str | None = None,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    """Return current GSC connector readiness without leaking credentials."""
    prop_url = _resolve_property_url(site_url, market)
    oauth_cfg = gsc_oauth_configured()
    oauth_conn = GscOAuthStore().has_tokens()
    redirect_uri = gsc_oauth_redirect_uri()
    base_meta = {
        "oauth_configured": oauth_cfg,
        "oauth_connected": oauth_conn,
        "redirect_uri": redirect_uri,
    }
    if gsc_kill_active():
        return {
            "connected": False,
            "mode": "disabled",
            "property_url": prop_url,
            "note": "GSC är avstängd via nödstopp (AZOM_GSC_KILL=1).",
            "scope": GSC_READONLY_SCOPE,
            **base_meta,
        }
    if is_mock_mode():
        return {
            "connected": True,
            "mode": "mock",
            "property_url": prop_url,
            "note": "Mock-läge aktivt (deterministiska nordiska GSC-fixturer, endast läsning).",
            "scope": GSC_READONLY_SCOPE,
            **base_meta,
        }
    if not env_flag("AZOM_GSC_ENABLED"):
        return {
            "connected": False,
            "mode": "disabled",
            "property_url": prop_url,
            "note": "Live GSC kräver AZOM_GSC_ENABLED=1 i processmiljön (fail-closed).",
            "scope": GSC_READONLY_SCOPE,
            **base_meta,
        }
    creds = _isolated_gsc_credentials()
    if not _has_gsc_credentials(creds):
        return {
            "connected": False,
            "mode": "live",
            "property_url": prop_url,
            "note": (
                "Isolerade GSC OAuth-uppgifter saknas "
                "(kräver GSC_OAUTH_ACCESS_TOKEN eller GSC_OAUTH_CLIENT_ID + "
                "GSC_OAUTH_CLIENT_SECRET + GSC_OAUTH_REFRESH_TOKEN)."
            ),
            "scope": GSC_READONLY_SCOPE,
            **base_meta,
        }
    return {
        "connected": True,
        "mode": "live",
        "property_url": prop_url,
        "note": "Live GSC ansluten (endast läsning, webmasters.readonly).",
        "scope": GSC_READONLY_SCOPE,
        **base_meta,
    }


def _mock_rows_for_market(market: str, base_origin: str) -> list[dict[str, Any]]:
    """Build deterministic Nordic CarPlay GSC rows covering strike-distance, low-CTR, and cannibalization."""
    origin = base_origin.rstrip("/")
    mkt = market if market in _VALID_MARKETS else "no"

    if mkt == "se":
        raw = [
            # Cannibalization pair 1 + strike distance (pos 4-15)
            ("trådlös carplay adapter", f"{origin}/produkt/carplay-adapter-pro/", 92, 2450, 0.0376, 5.4),
            ("trådlös carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 61, 1890, 0.0323, 6.8),
            # Low-CTR high-impression product pages
            ("apple carplay adapter bil", f"{origin}/produkt/carplay-adapter-pro/", 31, 2600, 0.0119, 3.6),
            ("carplay mini dongle", f"{origin}/produkt/carplay-mini-dongle/", 16, 1480, 0.0108, 4.7),
            # Strike-distance queries (pos 4-15)
            ("android auto carplay box", f"{origin}/produkt/android-auto-carplay-ai-box/", 44, 1320, 0.0333, 8.2),
            ("volvo xc60 carplay adapter", f"{origin}/blogg/monteringsguide-volvo-xc60-carplay/", 28, 1650, 0.0170, 9.6),
            ("apple carplay kompatibilitet", f"{origin}/carplay-kompatibilitet/", 58, 1740, 0.0333, 5.1),
            # Cannibalization pair 2 (mini dongle vs pro adapter)
            ("bästa trådlös carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 47, 1410, 0.0333, 7.3),
            ("bästa trådlös carplay adapter", f"{origin}/produkt/carplay-mini-dongle/", 19, 980, 0.0194, 11.4),
            # Branded top-3 query (baseline control)
            ("azom carplay", f"{origin}/", 240, 620, 0.3871, 1.2),
        ]
    elif mkt == "dk":
        raw = [
            # Cannibalization pair 1 + strike distance
            ("trådløs carplay adapter", f"{origin}/produkt/carplay-adapter-pro/", 74, 2120, 0.0349, 6.1),
            ("trådløs carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 49, 1640, 0.0299, 7.4),
            # Low-CTR high-impression pages
            ("apple carplay adapter bil", f"{origin}/produkt/carplay-adapter-pro/", 24, 2150, 0.0112, 3.9),
            ("carplay dongle til bil", f"{origin}/produkt/carplay-mini-dongle/", 14, 1340, 0.0104, 5.2),
            # Strike-distance queries (pos 4-15)
            ("android auto carplay boks", f"{origin}/produkt/android-auto-carplay-ai-box/", 35, 1190, 0.0294, 9.8),
            ("volvo xc60 apple carplay", f"{origin}/blogg/monteringsguide-volvo-xc60-carplay/", 22, 1420, 0.0155, 11.2),
            ("apple carplay kompatibilitet bil", f"{origin}/carplay-kompatibilitet/", 46, 1380, 0.0333, 4.9),
            # Cannibalization pair 2
            ("bedste trådløs carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 39, 1260, 0.0310, 8.1),
            ("bedste trådløs carplay adapter", f"{origin}/produkt/carplay-mini-dongle/", 15, 840, 0.0179, 12.6),
            # Branded baseline
            ("azom carplay danmark", f"{origin}/", 185, 510, 0.3627, 1.3),
        ]
    else:  # "no"
        raw = [
            # Cannibalization pair 1 + strike distance
            ("trådløs carplay adapter", f"{origin}/produkt/carplay-adapter-pro/", 118, 3100, 0.0381, 5.2),
            ("trådløs carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 76, 2240, 0.0339, 6.6),
            # Low-CTR high-impression pages
            ("apple carplay adapter bil", f"{origin}/produkt/carplay-adapter-pro/", 38, 3150, 0.0121, 3.4),
            ("carplay mini dongle norge", f"{origin}/produkt/carplay-mini-dongle/", 19, 1720, 0.0110, 4.8),
            # Strike-distance queries (pos 4-15)
            ("android auto carplay boks", f"{origin}/produkt/android-auto-carplay-ai-box/", 52, 1590, 0.0327, 8.7),
            ("volvo xc60 carplay adapter", f"{origin}/blogg/monteringsguide-volvo-xc60-carplay/", 31, 1940, 0.0160, 10.1),
            ("apple carplay kompatibilitet", f"{origin}/carplay-kompatibilitet/", 67, 1980, 0.0338, 4.6),
            # Cannibalization pair 2
            ("beste trådløs carplay adapter", f"{origin}/blogg/basta-tradlos-carplay-adapter/", 54, 1680, 0.0321, 7.2),
            ("beste trådløs carplay adapter", f"{origin}/produkt/carplay-mini-dongle/", 23, 1120, 0.0205, 11.9),
            # Branded baseline
            ("azom carplay", f"{origin}/", 310, 780, 0.3974, 1.1),
        ]

    return [
        {
            "query": query,
            "page": page,
            "clicks": clicks,
            "impressions": impressions,
            "ctr": round(ctr, 4),
            "position": round(position, 2),
        }
        for query, page, clicks, impressions, ctr, position in raw
    ]


class GoogleSearchConsoleClient:
    """Read-only Google Search Console Search Analytics client (mock-first, fail-closed)."""

    def __init__(
        self,
        *,
        use_mock: bool = True,
        property_url: str = "",
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        access_token: str = "",
        http_request: HttpRequestFn | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.use_mock = bool(use_mock)
        self.property_url = str(property_url or "").strip()
        self._client_id = str(client_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._refresh_token = str(refresh_token or "").strip()
        self._access_token = str(access_token or "").strip()
        self._access_token_expires_at: float = float("inf") if self._access_token else 0.0
        self._http_request: HttpRequestFn = http_request or pinned_https_request
        self._timeout = max(1.0, float(timeout))

    def _obtain_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise SecurityError(
                "Isolated GSC OAuth credentials missing "
                "(set GSC_OAUTH_CLIENT_ID, GSC_OAUTH_CLIENT_SECRET, GSC_OAUTH_REFRESH_TOKEN)."
            )
        form_body = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            }
        ).encode("utf-8")
        try:
            status, _headers, raw, _final_url = self._http_request(
                _GOOGLE_TOKEN_URL,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                body=form_body,
                connect_timeout=5.0,
                read_timeout=self._timeout,
                max_redirects=0,
                allowed_auth_origins=(_OAUTH_ORIGIN,),
            )
        except Exception as exc:
            raise SecurityError(
                _redact_error_text(f"GSC OAuth token refresh transport error: {exc}")
            ) from exc

        if status < 200 or status >= 300:
            raise SecurityError(f"GSC OAuth token refresh failed (HTTP {status}).")
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise SecurityError("GSC OAuth token endpoint returned non-JSON response.") from exc
        token = str(decoded.get("access_token") or "").strip() if isinstance(decoded, dict) else ""
        if not token:
            raise SecurityError("GSC OAuth token refresh returned no access_token.")
        expires_in = 3000
        try:
            expires_in = max(60, int(decoded.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3000
        self._access_token = token
        self._access_token_expires_at = now + max(30, expires_in - 60)
        return token

    def query_search_analytics(
        self,
        *,
        site_url: str,
        market: str = "no",
        days: int = 28,
        row_limit: int = 100,
    ) -> dict[str, Any]:
        """Query Search Analytics (dimensions: query, page) in mock or live mode."""
        if gsc_kill_active():
            raise SecurityError("GSC kill switch (AZOM_GSC_KILL) is active.")

        clean_days = max(1, min(int(days or 28), 365))
        clean_limit = max(1, min(int(row_limit or 100), 1000))
        mkt = _infer_market(site_url, market)
        prop_url = self.property_url or _resolve_property_url(site_url, mkt)

        if self.use_mock:
            base_origin = str(site_url or "").strip()
            if not base_origin or base_origin.startswith("sc-domain:"):
                base_origin = {"se": "https://azom.se", "dk": "https://azom.dk"}.get(
                    mkt, "https://azom.no"
                )
            rows = _mock_rows_for_market(mkt, base_origin)[:clean_limit]
            return {
                "ok": True,
                "mode": "mock",
                "property_url": prop_url,
                "days": clean_days,
                "rows": rows,
            }

        # Live mode: strict fail-closed posture
        if is_mock_mode() or not env_flag("AZOM_GSC_ENABLED"):
            raise SecurityError(
                "Live GSC Search Analytics is disabled "
                "(requires AZOM_USE_MOCK=0 and AZOM_GSC_ENABLED=1)."
            )

        token = self._obtain_access_token()
        end_date = date.today() - timedelta(days=2)
        start_date = end_date - timedelta(days=clean_days - 1)
        encoded_site = quote(prop_url, safe="")
        endpoint = f"{_GSC_API_ORIGIN}/webmasters/v3/sites/{encoded_site}/searchAnalytics/query"
        req_payload = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": ["query", "page"],
            "rowLimit": clean_limit,
            "dataState": "final",
        }
        body_bytes = json.dumps(req_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        try:
            status, _headers, raw, _final_url = self._http_request(
                endpoint,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=body_bytes,
                connect_timeout=5.0,
                read_timeout=self._timeout,
                max_redirects=0,
                allowed_auth_origins=(_GSC_API_ORIGIN,),
            )
        except Exception as exc:
            raise SecurityError(
                _redact_error_text(f"GSC Search Analytics transport error: {exc}")
            ) from exc

        if status < 200 or status >= 300:
            raise SecurityError(f"GSC Search Analytics API error (HTTP {status}).")

        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise SecurityError("GSC Search Analytics returned non-JSON response.") from exc

        if not isinstance(decoded, dict):
            raise SecurityError("GSC Search Analytics returned unexpected response shape.")

        parsed_rows: list[dict[str, Any]] = []
        for item in list(decoded.get("rows") or []):
            if not isinstance(item, dict):
                continue
            keys = list(item.get("keys") or [])
            query_str = str(keys[0]) if len(keys) > 0 else ""
            page_str = str(keys[1]) if len(keys) > 1 else ""
            try:
                clicks = float(item.get("clicks") or 0.0)
                impressions = float(item.get("impressions") or 0.0)
                ctr = float(item.get("ctr") or 0.0)
                position = float(item.get("position") or 0.0)
            except (TypeError, ValueError):
                continue
            parsed_rows.append(
                {
                    "query": query_str,
                    "page": page_str,
                    "clicks": int(clicks) if clicks.is_integer() else round(clicks, 2),
                    "impressions": int(impressions) if impressions.is_integer() else round(impressions, 2),
                    "ctr": round(ctr, 4),
                    "position": round(position, 2),
                }
            )

        return {
            "ok": True,
            "mode": "live",
            "property_url": prop_url,
            "days": clean_days,
            "rows": parsed_rows[:clean_limit],
        }


def gsc_client_from_env(
    *,
    use_mock: bool | None = None,
    market: str | None = None,
    site_url: str = "",
    http_request: HttpRequestFn | None = None,
) -> GoogleSearchConsoleClient:
    """Build a ``GoogleSearchConsoleClient`` from isolated GSC env configuration."""
    effective_mock = is_mock_mode() if use_mock is None else bool(use_mock)
    prop_url = _resolve_property_url(site_url or None, market)
    if effective_mock:
        return GoogleSearchConsoleClient(
            use_mock=True,
            property_url=prop_url,
            http_request=http_request,
        )
    creds = _isolated_gsc_credentials()
    return GoogleSearchConsoleClient(
        use_mock=False,
        property_url=prop_url,
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        refresh_token=creds["refresh_token"],
        access_token=creds["access_token"],
        http_request=http_request,
    )


__all__ = [
    "GSC_READONLY_SCOPE",
    "GoogleSearchConsoleClient",
    "gsc_client_from_env",
    "gsc_enabled",
    "gsc_kill_active",
    "gsc_status",
]
