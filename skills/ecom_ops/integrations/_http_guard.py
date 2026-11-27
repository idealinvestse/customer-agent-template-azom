"""Shared DNS-pinned HTTPS egress, SSRF, redirect, and decompression guards.

Pure Python stdlib implementation with zero ambient proxy inheritance, single-lookup
DNS validation + dual-stack IP pinning, TLS SNI/hostname verification, manual
hop-by-hop redirects, and bounded streaming decompression.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import urllib.parse
import zlib
from collections.abc import Callable, Collection, Mapping, Sequence

_VALID_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_VALID_TLD_LABEL_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")
_LEGACY_IPV4_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z]{3,10}$")

_BLOCKED_EXACT_HOSTS: frozenset[str] = frozenset({"localhost"})
_RESERVED_TLDS: tuple[str, ...] = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".home.arpa",
    ".invalid",
    ".example",
    ".test",
)
_LOCAL_ONLY_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".home.arpa",
    ".invalid",
)

_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_METADATA_IPV4 = ipaddress.IPv4Address("169.254.169.254")
_METADATA_IPV6 = ipaddress.IPv6Address("fd00:ec2::254")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPAT_NETWORK = ipaddress.IPv6Network("::/96")
_SIXTOFOUR_NETWORK = ipaddress.IPv6Network("2002::/16")
_TEREDO_NETWORK = ipaddress.IPv6Network("2001::/32")

_SENSITIVE_AUTH_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "x-api-key",
        "x-subscription-token",
        "proxy-authorization",
    }
)
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


def is_ip_literal(host: str) -> bool:
    """Return True if *host* is any IPv4/IPv6 literal, including legacy numeric/hex/octal IPv4."""
    raw = str(host or "").strip()
    if not raw:
        return False
    unbracketed = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw.strip("[]")
    try:
        ipaddress.ip_address(unbracketed)
        return True
    except ValueError:
        pass
    if ":" in raw or "[" in raw or "]" in raw:
        return False
    parts = raw.split(".")
    if 1 <= len(parts) <= 4 and all(_LEGACY_IPV4_PART_RE.fullmatch(part) for part in parts):
        return True
    return False


def is_public_global_ip(raw_ip: str) -> bool:
    """Return True iff *raw_ip* is a globally routable public IP (no private/CGNAT/metadata)."""
    text = str(raw_ip or "").strip()
    if not text or "%" in text:
        return False
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv6Address):
        if (
            addr == _METADATA_IPV6
            or addr in _NAT64_WELL_KNOWN
            or addr in _NAT64_LOCAL
            or addr in _IPV4_COMPAT_NETWORK
            or addr in _SIXTOFOUR_NETWORK
            or addr in _TEREDO_NETWORK
            or addr.sixtofour is not None
            or addr.teredo is not None
        ):
            return False
    if not addr.is_global:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        if addr == _METADATA_IPV4 or addr in _CGNAT_NETWORK:
            return False
    return True


def _validate_public_dns_hostname(
    raw_host: str,
    *,
    block_example_test_tlds: bool = True,
) -> tuple[bool, str, str]:
    host = str(raw_host or "").lower()
    if not host:
        return False, "", "host_blocked"
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in host):
        return False, "", "host_blocked"
    if host.endswith(".") or host.startswith(".") or ".." in host:
        return False, "", "host_blocked"
    if host in _BLOCKED_EXACT_HOSTS or is_ip_literal(host):
        return False, "", "host_blocked"
    if host.endswith(".example.test"):
        return False, "", "synthetic_host_blocked"
    blocked_suffixes = _RESERVED_TLDS if block_example_test_tlds else _LOCAL_ONLY_SUFFIXES
    if any(host.endswith(suffix) for suffix in blocked_suffixes):
        return False, "", "host_blocked"
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False, "", "host_blocked"
    if not ascii_host or len(ascii_host) > 253 or is_ip_literal(ascii_host):
        return False, "", "host_blocked"
    if ascii_host in _BLOCKED_EXACT_HOSTS or any(ascii_host.endswith(s) for s in blocked_suffixes):
        return False, "", "host_blocked"
    labels = ascii_host.split(".")
    if (
        len(labels) < 2
        or not all(_VALID_HOST_LABEL_RE.fullmatch(lbl) for lbl in labels)
        or _VALID_TLD_LABEL_RE.fullmatch(labels[-1]) is None
    ):
        return False, "", "host_blocked"
    return True, ascii_host, ""


def classify_https_parse_result(
    parsed: urllib.parse.ParseResult,
    *,
    allow_non_443: bool = False,
    block_example_test_tlds: bool = True,
) -> tuple[bool, str, str]:
    """Validate a parsed URL for strict HTTPS public-host egress, returning ``(ok, host, reason)``."""
    if parsed.scheme != "https":
        return False, "", "non_https_blocked"
    netloc = parsed.netloc or ""
    if parsed.username is not None or parsed.password is not None or "@" in netloc:
        return False, "", "userinfo_blocked"
    if "[" in netloc or "]" in netloc:
        return False, "", "host_blocked"
    try:
        port = parsed.port
    except ValueError:
        return False, "", "invalid_url"
    if port is not None and (port < 1 or port > 65535):
        return False, "", "invalid_url"
    if not allow_non_443 and port not in (None, 443):
        return False, "", "port_blocked"
    return _validate_public_dns_hostname(
        parsed.hostname or "",
        block_example_test_tlds=block_example_test_tlds,
    )


def safe_https_parsed(
    url: str,
    *,
    allow_non_443: bool = False,
) -> urllib.parse.ParseResult:
    """Parse and strictly validate an external HTTPS URL, raising ``ValueError`` if unsafe."""
    if not isinstance(url, str) or not url:
        raise ValueError("Empty URL")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url) or "\\" in url:
        raise ValueError("URL contains whitespace, control characters, or backslash")
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError("Invalid URL syntax") from exc
    ok, host, reason = classify_https_parse_result(
        parsed,
        allow_non_443=allow_non_443,
        block_example_test_tlds=True,
    )
    if not ok:
        raise ValueError(f"Unsafe HTTPS URL ({reason})")
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return parsed._replace(netloc=netloc)


def resolve_and_validate_public_ips(host: str, port: int = 443) -> list[str]:
    """Resolve *host* once via ``socket.getaddrinfo`` and fail closed unless ALL IPs are public."""
    ok, clean_host, reason = _validate_public_dns_hostname(host, block_example_test_tlds=True)
    if not ok:
        raise ValueError(f"Unsafe host for DNS resolution ({reason})")
    infos = socket.getaddrinfo(clean_host, int(port), type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError(f"No DNS records resolved for {clean_host}")
    validated: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            raise ValueError(f"Malformed sockaddr returned for {clean_host}")
        raw_ip = str(sockaddr[0])
        if not is_public_global_ip(raw_ip):
            raise ValueError(f"Non-public IP resolved for {clean_host}")
        if raw_ip not in seen:
            seen.add(raw_ip)
            validated.append(raw_ip)
    if not validated:
        raise ValueError(f"No public IPs resolved for {clean_host}")
    return validated


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` connecting directly to pre-validated IPs without re-querying DNS."""

    def __init__(
        self,
        host: str,
        port: int = 443,
        *,
        pinned_ips: Sequence[str],
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        context: ssl.SSLContext | None = None,
    ) -> None:
        ips = [str(ip) for ip in pinned_ips if str(ip).strip()]
        if not ips or not all(is_public_global_ip(ip) for ip in ips):
            raise ValueError("PinnedHTTPSConnection requires validated public IPs")
        tls_context = context if context is not None else ssl.create_default_context()
        tls_context.check_hostname = True
        tls_context.verify_mode = ssl.CERT_REQUIRED
        super().__init__(host, port=int(port), timeout=float(read_timeout), context=tls_context)
        self._pinned_ips = tuple(ips)
        self._connect_timeout = max(0.1, float(connect_timeout))
        self._read_timeout = max(0.1, float(read_timeout))
        self._tls_context = tls_context

    def connect(self) -> None:
        last_exc: OSError | None = None
        for ip_str in self._pinned_ips:
            addr = ipaddress.ip_address(ip_str)
            family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            raw_sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                raw_sock.settimeout(self._connect_timeout)
                raw_sock.connect((ip_str, int(self.port)))
            except OSError as exc:
                last_exc = exc
                raw_sock.close()
                continue
            try:
                raw_sock.settimeout(self._read_timeout)
                self.sock = self._tls_context.wrap_socket(
                    raw_sock,
                    server_hostname=self.host,
                )
                return
            except Exception:
                raw_sock.close()
                raise
        if last_exc is not None:
            raise last_exc
        raise OSError(f"Failed to connect to pinned IPs for {self.host}")


def _origin_key(parsed: urllib.parse.ParseResult) -> tuple[str, str, int]:
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443)


def _normalize_allowed_origins(
    allowed_auth_origins: Collection[str] | None,
) -> frozenset[tuple[str, str, int]] | None:
    if allowed_auth_origins is None:
        return None
    origins: set[tuple[str, str, int]] = set()
    for origin in allowed_auth_origins:
        parsed = safe_https_parsed(str(origin))
        origins.add(_origin_key(parsed))
    return frozenset(origins)


def _prepare_hop_headers(
    headers: Mapping[str, str] | None,
    *,
    host_header: str,
    strip_auth: bool,
    drop_content_headers: bool,
) -> dict[str, str]:
    out: dict[str, str] = {}
    has_accept_encoding = False
    for key, val in (headers or {}).items():
        k_str = str(key).strip()
        low = k_str.lower()
        if low == "host":
            continue
        if strip_auth and low in _SENSITIVE_AUTH_HEADERS:
            continue
        if drop_content_headers and low in {"content-length", "content-type"}:
            continue
        if low == "accept-encoding":
            has_accept_encoding = True
        out[k_str] = str(val)
    out["Host"] = host_header
    if not has_accept_encoding:
        out["Accept-Encoding"] = "gzip, deflate"
    return out


def _read_bounded_response(
    resp: http.client.HTTPResponse,
    *,
    max_raw_bytes: int,
    max_decompressed_bytes: int,
    chunk_size: int = 16384,
) -> bytes:
    raw_len_hdr = resp.getheader("Content-Length")
    if raw_len_hdr:
        try:
            if int(str(raw_len_hdr).strip()) > max_raw_bytes:
                raise ValueError(f"Response Content-Length exceeds {max_raw_bytes} bytes")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise

    encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
    decomp: zlib._Decompress | None = None
    is_deflate = False
    if encoding in ("", "identity"):
        decomp = None
    elif encoding in ("gzip", "x-gzip"):
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decomp = zlib.decompressobj(zlib.MAX_WBITS)
        is_deflate = True
    else:
        raise ValueError(f"Unsupported Content-Encoding: {encoding}")

    raw_total = 0
    decomp_total = 0
    chunks: list[bytes] = []
    first_deflate_chunk = is_deflate
    deflate_pending = b""

    def _feed_decomp(data: bytes) -> None:
        nonlocal decomp, decomp_total, first_deflate_chunk
        assert decomp is not None
        buf = data
        while buf:
            remaining = max_decompressed_bytes - decomp_total + 1
            try:
                piece = decomp.decompress(buf, max_length=remaining)
            except zlib.error as exc:
                if first_deflate_chunk:
                    first_deflate_chunk = False
                    decomp = zlib.decompressobj(-zlib.MAX_WBITS)
                    try:
                        piece = decomp.decompress(buf, max_length=remaining)
                    except zlib.error as raw_exc:
                        raise ValueError("Invalid compressed response stream") from raw_exc
                else:
                    raise ValueError("Invalid compressed response stream") from exc
            first_deflate_chunk = False
            decomp_total += len(piece)
            if decomp_total > max_decompressed_bytes:
                raise ValueError(
                    f"Decompressed response exceeded {max_decompressed_bytes} bytes"
                )
            if piece:
                chunks.append(piece)
            buf = decomp.unconsumed_tail

    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        raw_total += len(chunk)
        if raw_total > max_raw_bytes:
            raise ValueError(f"Response raw size exceeded {max_raw_bytes} bytes")
        if decomp is None:
            decomp_total += len(chunk)
            if decomp_total > max_decompressed_bytes:
                raise ValueError(
                    f"Response size exceeded {max_decompressed_bytes} bytes"
                )
            chunks.append(chunk)
            continue

        if first_deflate_chunk:
            deflate_pending += chunk
            if len(deflate_pending) < 2:
                continue
            buf = deflate_pending
            deflate_pending = b""
            _feed_decomp(buf)
        else:
            _feed_decomp(chunk)

    if decomp is not None and deflate_pending:
        _feed_decomp(deflate_pending)
        deflate_pending = b""

    if decomp is not None:
        remaining = max_decompressed_bytes - decomp_total + 1
        try:
            tail = decomp.flush(remaining)
        except zlib.error as exc:
            raise ValueError("Invalid compressed response stream tail") from exc
        decomp_total += len(tail)
        if decomp_total > max_decompressed_bytes or decomp.unconsumed_tail:
            raise ValueError(
                f"Decompressed response exceeded {max_decompressed_bytes} bytes"
            )
        if tail:
            chunks.append(tail)

    return b"".join(chunks)


def pinned_https_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
    max_redirects: int = 3,
    max_raw_bytes: int = 524288,
    max_decompressed_bytes: int = 1048576,
    allowed_content_types: Collection[str] | None = None,
    pre_hop_validator: Callable[[urllib.parse.ParseResult], None] | None = None,
    allowed_auth_origins: Collection[str] | None = None,
) -> tuple[int, dict[str, str], bytes, str]:
    """Perform a DNS-pinned, no-proxy, redirect-validated, decompression-capped HTTPS request."""
    verb = str(method or "GET").strip().upper()
    if not _HTTP_METHOD_RE.fullmatch(verb):
        raise ValueError(f"Invalid HTTP method: {method!r}")
    if max_redirects < 0:
        raise ValueError("max_redirects must be >= 0")

    allowed_origins = _normalize_allowed_origins(allowed_auth_origins)
    allowed_mimes = (
        {str(ct).split(";", 1)[0].strip().lower() for ct in allowed_content_types}
        if allowed_content_types is not None
        else None
    )

    current_url = url
    current_verb = verb
    current_body = body
    initial_origin: tuple[str, str, int] | None = None
    dropped_body_on_redirect = False

    for hop in range(max_redirects + 1):
        parsed = safe_https_parsed(current_url, allow_non_443=False)
        if pre_hop_validator is not None:
            pre_hop_validator(parsed)

        host = parsed.hostname or ""
        port = parsed.port or 443
        origin = _origin_key(parsed)
        if initial_origin is None:
            initial_origin = origin

        if allowed_origins is not None:
            strip_auth = origin not in allowed_origins
        else:
            strip_auth = hop > 0 and origin != initial_origin

        pinned_ips = resolve_and_validate_public_ips(host, port)
        host_header = host if port == 443 else f"{host}:{port}"
        hop_headers = _prepare_hop_headers(
            headers,
            host_header=host_header,
            strip_auth=strip_auth,
            drop_content_headers=dropped_body_on_redirect,
        )
        request_target = parsed.path or "/"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"

        conn = PinnedHTTPSConnection(
            host,
            port=port,
            pinned_ips=pinned_ips,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
        try:
            conn.request(current_verb, request_target, body=current_body, headers=hop_headers)
            resp = conn.getresponse()
            status = int(resp.status)
            resp_headers = {str(k).lower(): str(v) for k, v in resp.getheaders()}

            if status in _REDIRECT_STATUSES:
                location = resp.getheader("Location")
                if location:
                    if hop >= max_redirects:
                        raise ValueError(f"Exceeded max_redirects ({max_redirects})")
                    current_url = urllib.parse.urljoin(current_url, location.strip())
                    if status == 303 or (status in (301, 302) and current_verb not in ("GET", "HEAD")):
                        current_verb = "GET"
                        current_body = None
                        dropped_body_on_redirect = True
                    continue

            if allowed_mimes is not None and 200 <= status < 300 and status != 204 and current_verb != "HEAD":
                raw_ct = resp.getheader("Content-Type") or ""
                mime = raw_ct.split(";", 1)[0].strip().lower()
                if mime not in allowed_mimes:
                    raise ValueError(f"Disallowed Content-Type: {mime or 'missing'}")

            if current_verb == "HEAD" or status == 204:
                payload = b""
            elif status < 200 or status >= 300:
                try:
                    payload = _read_bounded_response(
                        resp,
                        max_raw_bytes=max_raw_bytes,
                        max_decompressed_bytes=max_decompressed_bytes,
                    )
                except ValueError:
                    payload = b""
            else:
                payload = _read_bounded_response(
                    resp,
                    max_raw_bytes=max_raw_bytes,
                    max_decompressed_bytes=max_decompressed_bytes,
                )
            return status, resp_headers, payload, current_url
        finally:
            conn.close()

    raise ValueError(f"Exceeded max_redirects ({max_redirects})")


def safe_public_https_url(
    url: str,
    *,
    blocked_hosts: set[str] | None = None,
) -> str | None:
    """Validate an external citation URL for safe Dashboard href rendering, or return None."""
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = safe_https_parsed(raw, allow_non_443=False)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()

    def _norm_denied(raw_host: str) -> str:
        h = str(raw_host or "").strip().lower().lstrip(".").rstrip(".")
        if not h:
            return ""
        try:
            return h.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return h

    denied: set[str] = set()
    for h in blocked_hosts or set():
        nh = _norm_denied(h)
        if nh:
            denied.add(nh)
    dash_public = os.environ.get("AZOM_DASHBOARD_PUBLIC_URL", "").strip()
    if dash_public:
        try:
            dash_url = dash_public if "://" in dash_public else f"https://{dash_public}"
            dash_host = (urllib.parse.urlparse(dash_url).hostname or "").strip().lower().rstrip(".")
            nh = _norm_denied(dash_host)
            if nh:
                denied.add(nh)
        except ValueError:
            pass
    if any(host == d or host.endswith(f".{d}") for d in denied):
        return None
    return urllib.parse.urlunsplit(
        ("https", host, parsed.path, parsed.query, parsed.fragment)
    )
