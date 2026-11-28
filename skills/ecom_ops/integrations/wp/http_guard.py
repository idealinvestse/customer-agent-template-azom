"""SSRF, host-pinned redirect, and bounded read-only HTTPS GET guards for WordPress ops."""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ecom_ops.integrations import _http_guard as _shared_http_guard
from ecom_ops.wp_ops.credentials import bind_credential
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError

_PREVIEW_PATH_RE = re.compile(r"^/[A-Za-z0-9/_\-.%]{0,200}$")

BLOCKED_WP_PATHS: tuple[str, ...] = (
    "wp-admin",
    "wp-login",
    "admin-ajax",
    "wc-ajax",
    "/wp-json",
    "rest_route",
    "xmlrpc",
    "wp-cron",
    "add-to-cart",
    "/cart",
    "/checkout",
    "/my-account",
    "/handlekurv",
    "/kasse",
    "/min-konto",
    "/varukorg",
    "/kassa",
    "/kassan",
    "/mitt-konto",
    "/betalning",
    "/kurv",
)
_SITE_BLOCKED_SUBSTRINGS = BLOCKED_WP_PATHS
_REFUSED_PATH_TOKENS: tuple[str, ...] = tuple(
    "wp-json" if token == "/wp-json" else token for token in BLOCKED_WP_PATHS
)

STATIC_ASSET_HOSTS: frozenset[str] = frozenset(
    {
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "ajax.googleapis.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "use.typekit.net",
        "p.typekit.net",
        "assets.website-files.com",
        "uploads-ssl.webflow.com",
        "d3e54v103j8qbb.cloudfront.net",
    }
)
_STATIC_ASSET_HOSTS = STATIC_ASSET_HOSTS

TRACKER_HOSTS: tuple[str, ...] = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "hotjar.com",
    "clarity.ms",
    "tiktok.com",
    "snapchat.com",
    "bing.com",
    "pinterest.com",
)
_TRACKER_HOSTS = TRACKER_HOSTS

PREVIEW_USER_AGENT = "Mozilla/5.0 (AzomOps read-only preview)"
_PREVIEW_USER_AGENT = PREVIEW_USER_AGENT
MAX_HTML_BYTES = 3_000_000
_MAX_HTML_BYTES = MAX_HTML_BYTES

_LIVE_HTML_CACHE_TTL_S = 300.0
_LIVE_HTML_CACHE_MAX = 8
_LIVE_HTML_CACHE_LOCK = threading.Lock()
_LIVE_HTML_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def unquote_iteratively(value: str, *, max_rounds: int = 6) -> str:
    current = str(value or "")
    for _ in range(max_rounds):
        nxt = urllib.parse.unquote(current)
        if nxt == current:
            break
        current = nxt
    return current


_unquote_iteratively = unquote_iteratively


def validate_preview_path(path: str | None) -> str:
    """Validate and normalize a public GET preview path."""
    raw = "/" if path is None or str(path).strip() == "" else str(path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    if "?" in raw or "#" in raw or "\\" in raw or ".." in raw or "//" in raw:
        raise ReviewError("PreviewPathRefused")
    if _PREVIEW_PATH_RE.fullmatch(raw) is None:
        raise ReviewError("PreviewPathRefused")
    decoded = unquote_iteratively(raw)
    if "?" in decoded or "#" in decoded or "\\" in decoded or ".." in decoded or "//" in decoded:
        raise ReviewError("PreviewPathRefused")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise ReviewError("PreviewPathRefused")
    for text in (raw.lower(), decoded.lower()):
        if any(token in text for token in _REFUSED_PATH_TOKENS):
            raise ReviewError("PreviewPathRefused")
    return raw


def allowed_site_hosts(site_host: str) -> frozenset[str]:
    host = str(site_host or "").strip().lower().rstrip(".")
    if not host:
        return frozenset()
    if host.startswith("www."):
        bare = host[4:]
        return frozenset({host, bare}) if bare else frozenset({host})
    return frozenset({host, f"www.{host}"})


_allowed_site_hosts = allowed_site_hosts


def host_matches_suffix(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


_host_matches_suffix = host_matches_suffix


def is_ip_literal(host: str) -> bool:
    """Return True if *host* is an IPv4/IPv6 literal (including legacy numeric/hex/octal forms)."""
    return _shared_http_guard.is_ip_literal(host)


_is_ip_literal = is_ip_literal


def safe_https_parsed(parsed: urllib.parse.ParseResult) -> tuple[bool, str, str]:
    """Validate a parsed URL for WP preview/REST reads, preserving legacy reason tokens."""
    return _shared_http_guard.classify_https_parse_result(
        parsed,
        allow_non_443=False,
        block_example_test_tlds=False,
    )


_safe_https_parsed = safe_https_parsed


def host_resolves_public(host: str) -> bool:
    """Return True iff *host* resolves solely to globally routable public IPs."""
    try:
        return bool(_shared_http_guard.resolve_and_validate_public_ips(host, 443))
    except (OSError, ValueError):
        return False


_host_resolves_public = host_resolves_public


def _resolve_public_host_fn(
    override: Callable[[str], bool] | None = None,
) -> Callable[[str], bool]:
    if override is not None:
        return override
    facade = sys.modules.get("ecom_ops.integrations.wp_live_preview")
    if facade is not None:
        hook = getattr(facade, "_host_resolves_public", None)
        if callable(hook):
            return hook
    return host_resolves_public


_STATIC_ASSET_EXTS: frozenset[str] = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".map",
    }
)
_CHECKOUT_CART_PATH_TOKENS: frozenset[str] = frozenset(
    {
        "/cart",
        "/checkout",
        "/my-account",
        "/handlekurv",
        "/kasse",
        "/min-konto",
        "/varukorg",
        "/kassa",
        "/kassan",
        "/mitt-konto",
        "/betalning",
        "/kurv",
    }
)


def _is_wp_static_asset(parsed: urllib.parse.ParseResult) -> bool:
    """Return True if *parsed* points to a read-only static asset under ``/wp-content/`` or ``/wp-includes/``."""
    raw_path = (parsed.path or "").lower()
    decoded_path = unquote_iteratively(parsed.path or "").lower()
    for p in (raw_path, decoded_path):
        if ".." in p or "\\" in p:
            return False
        if not (p.startswith("/wp-content/") or p.startswith("/wp-includes/")):
            return False
    dot_idx = decoded_path.rfind(".")
    slash_idx = decoded_path.rfind("/")
    if dot_idx <= slash_idx:
        return False
    return decoded_path[dot_idx:] in _STATIC_ASSET_EXTS


def blocked_site_path_reason(
    parsed: urllib.parse.ParseResult,
    *,
    allow_wp_json: bool = False,
    allow_static_assets: bool = False,
) -> str | None:
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"
    decoded_pq = unquote_iteratively(path_and_query)
    is_static = allow_static_assets and _is_wp_static_asset(parsed)
    for candidate in (path_and_query.lower(), decoded_pq.lower()):
        for blocked in BLOCKED_WP_PATHS:
            if allow_wp_json and blocked == "/wp-json":
                continue
            if is_static and blocked in _CHECKOUT_CART_PATH_TOKENS:
                continue
            if blocked in candidate:
                return f"site_path_blocked:{blocked.strip('/')}"
    return None


_blocked_site_path_reason = blocked_site_path_reason


def classify_request(method: str, url: str, *, site_host: str) -> tuple[bool, str]:
    """Decide whether headless preview Chrome may issue this subresource request."""
    verb = str(method or "").strip().upper()
    if verb not in {"GET", "HEAD"}:
        return False, "method_blocked"
    raw_url = str(url or "").strip()
    lower_url = raw_url.lower()
    if lower_url.startswith(("data:", "blob:", "about:")):
        return True, "inline"
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except ValueError:
        return False, "invalid_url"
    ok_url, host, err_reason = safe_https_parsed(parsed)
    if not ok_url:
        return False, err_reason
    if any(host_matches_suffix(host, tracker) for tracker in TRACKER_HOSTS):
        return False, "tracker_blocked"

    site_hosts = allowed_site_hosts(site_host)
    if host in site_hosts:
        blocked_reason = blocked_site_path_reason(parsed, allow_static_assets=True)
        if blocked_reason:
            return False, blocked_reason
        return True, "site_asset"
    if host in STATIC_ASSET_HOSTS:
        return True, "cdn_asset"
    return False, "foreign_host_blocked"


class AllowlistedSiteRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects that leave the allowlisted HTTPS hosts or hit blocked paths."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        allow_wp_json: bool = False,
        check_blocked_paths: bool = True,
        public_host_check: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts
        self._allow_wp_json = allow_wp_json
        self._check_blocked_paths = check_blocked_paths
        self._public_host_check = public_host_check

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urllib.parse.urlparse(str(newurl or ""))
        ok_url, host, err_reason = safe_https_parsed(parsed)
        check_public = _resolve_public_host_fn(self._public_host_check)
        if not ok_url or host not in self._allowed_hosts or not check_public(host):
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"Redirect refused ({err_reason or 'foreign_host'}): {host}",
                headers,
                fp,
            )
        if self._check_blocked_paths:
            blocked_reason = blocked_site_path_reason(parsed, allow_wp_json=self._allow_wp_json)
            if blocked_reason:
                raise urllib.error.HTTPError(
                    newurl,
                    code,
                    f"Redirect to blocked WordPress path refused ({blocked_reason})",
                    headers,
                    fp,
                )
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            orig_parsed = urllib.parse.urlparse(str(getattr(req, "full_url", "") or ""))
            orig_host = (orig_parsed.hostname or "").strip().lower().rstrip(".")
            if orig_host:
                try:
                    orig_host = orig_host.encode("idna").decode("ascii").lower().rstrip(".")
                except UnicodeError:
                    pass
            try:
                orig_port = orig_parsed.port or 443
            except ValueError:
                orig_port = -1
            try:
                new_port = parsed.port or 443
            except ValueError:
                new_port = -1
            if orig_parsed.scheme.lower() != "https" or orig_host != host or orig_port != new_port:
                for hdr_dict in (new_req.headers, getattr(new_req, "unredirected_hdrs", {})):
                    for key in list(hdr_dict.keys()):
                        if str(key).lower() in _shared_http_guard._SENSITIVE_AUTH_HEADERS:
                            hdr_dict.pop(key, None)
        return new_req


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that pins DNS resolution to validated public IPs via ``PinnedHTTPSConnection``."""

    def __init__(self, public_host_check: Callable[[str], bool] | None = None) -> None:
        super().__init__()
        self._public_host_check = public_host_check

    def https_open(self, req: urllib.request.Request) -> Any:
        parsed = urllib.parse.urlparse(str(getattr(req, "full_url", "") or ""))
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        port = parsed.port or 443
        check_public = _resolve_public_host_fn(self._public_host_check)
        if not host or not check_public(host):
            raise urllib.error.URLError(f"Unsafe host blocked: {host}")
        pinned_ips = _shared_http_guard.resolve_and_validate_public_ips(host, port)

        def _factory(conn_host: str, *, timeout: float = 15.0, **_kwargs: Any) -> Any:
            del conn_host
            return _shared_http_guard.PinnedHTTPSConnection(
                host,
                port=port,
                pinned_ips=pinned_ips,
                connect_timeout=min(5.0, float(timeout or 15.0)),
                read_timeout=float(timeout or 15.0),
            )

        return self.do_open(_factory, req)


_AllowlistedSiteRedirectHandler = AllowlistedSiteRedirectHandler


def clear_live_html_cache() -> None:
    """Clear the bounded in-process live HTML fetch cache."""
    with _LIVE_HTML_CACHE_LOCK:
        _LIVE_HTML_CACHE.clear()


def fetch_live_html(
    site: Site,
    path: str = "/",
    *,
    timeout: float = 15.0,
    max_bytes: int = MAX_HTML_BYTES,
    use_cache: bool = False,
    public_host_check: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Fetch public live HTML via HTTPS GET with no cookies and no site credentials."""
    clean_path = validate_preview_path(path)
    parsed_base = urllib.parse.urlparse(str(site.canonical_url or ""))
    ok_url, site_host, _err = safe_https_parsed(parsed_base)
    check_public = _resolve_public_host_fn(public_host_check)
    if not ok_url or not check_public(site_host):
        raise ReviewError("LiveFetchFailed")

    cache_key = (f"{site.id}:{site_host}", clean_path)
    now_mono = time.monotonic()
    if use_cache:
        with _LIVE_HTML_CACHE_LOCK:
            cached = _LIVE_HTML_CACHE.get(cache_key)
            if cached is not None and (now_mono - cached[0]) <= _LIVE_HTML_CACHE_TTL_S:
                return dict(cached[1])

    target_url = urllib.parse.urlunsplit(("https", parsed_base.netloc, clean_path, "", ""))
    allowed_hosts = allowed_site_hosts(site_host)
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        AllowlistedSiteRedirectHandler(allowed_hosts, public_host_check=public_host_check),
    ]
    if public_host_check is None:
        handlers.append(_PinnedHTTPSHandler())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        target_url,
        method="GET",
        headers={
            "User-Agent": PREVIEW_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with opener.open(req, timeout=float(timeout)) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            final_url = str(resp.geturl() or target_url)
            final_parsed = urllib.parse.urlparse(final_url)
            ok_final, final_host, _ferr = safe_https_parsed(final_parsed)
            if not ok_final or final_host not in allowed_hosts or blocked_site_path_reason(final_parsed):
                raise ReviewError("LiveFetchFailed")
            raw = resp.read(max_bytes + 1)
    except ReviewError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError) as exc:
        raise ReviewError("LiveFetchFailed") from exc

    if len(raw) > max_bytes:
        raise ReviewError("HtmlTooLarge")
    html = raw.decode("utf-8", errors="replace")
    result = {
        "status": status,
        "final_url": final_url,
        "html": html,
        "bytes": len(raw),
    }
    if use_cache:
        with _LIVE_HTML_CACHE_LOCK:
            if len(_LIVE_HTML_CACHE) >= _LIVE_HTML_CACHE_MAX and cache_key not in _LIVE_HTML_CACHE:
                oldest_key = min(_LIVE_HTML_CACHE.items(), key=lambda item: item[1][0])[0]
                _LIVE_HTML_CACHE.pop(oldest_key, None)
            _LIVE_HTML_CACHE[cache_key] = (time.monotonic(), dict(result))
    return result


def fetch_site_rest_json(
    site: Site,
    endpoint_path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 8.0,
    use_wp_auth: bool = False,
    max_bytes: int = 1_048_576,
    public_host_check: Callable[[str], bool] | None = None,
) -> Any | None:
    """Read-only SSRF-safe HTTPS GET to a registered site's REST endpoint."""
    parsed_base = urllib.parse.urlparse(str(site.canonical_url or ""))
    ok_url, site_host, _err = safe_https_parsed(parsed_base)
    check_public = _resolve_public_host_fn(public_host_check)
    if not ok_url or not check_public(site_host):
        return None

    query = urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    )
    target_url = urllib.parse.urlunsplit(
        ("https", parsed_base.netloc, endpoint_path, query, "")
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (AzomOps read-only product)",
        "Accept": "application/json",
    }
    if use_wp_auth:
        try:
            bound = bind_credential(site)
            if bound.present and ":" in bound.value:
                token = base64.b64encode(bound.value.encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
        except Exception:
            pass

    allowed_hosts = allowed_site_hosts(site_host)
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        AllowlistedSiteRedirectHandler(
            allowed_hosts,
            allow_wp_json=True,
            public_host_check=public_host_check,
        ),
    ]
    if public_host_check is None:
        handlers.append(_PinnedHTTPSHandler())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(target_url, method="GET", headers=headers)
    try:
        with opener.open(req, timeout=float(timeout)) as resp:
            final_url = str(resp.geturl() or "")
            parsed_final = urllib.parse.urlparse(final_url)
            ok_final, final_host, _ferr = safe_https_parsed(parsed_final)
            if not ok_final or final_host not in allowed_hosts or not check_public(final_host):
                return None
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError):
        return None
