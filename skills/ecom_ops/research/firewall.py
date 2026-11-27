"""Fail-closed outbound PII, secret, internal-ID, and domain firewall.

Leaf module: no ``integrations.*``, ``urllib.request``, ``http.client``,
``requests``, or ``subprocess`` imports.
"""

from __future__ import annotations

import calendar
import ipaddress
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from ecom_ops.research.config import (
    effective_allowlist_domains,
    load_research_config,
)
from ecom_ops.research.models import SearchPolicyError
from ecom_ops.security import SECRET_ENV_KEYS
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.pii import contains_real_pii, normalize_pii_text

MAX_QUERY_CHARS = 250
MAX_URL_CHARS = 2048

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_DK_CPR_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})-(\d{4})(?!\d)")
_INTL_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+|00)[1-9]\d{0,2}[\s.\-]?(?:\(\d{1,4}\)[\s.\-]?)?(?:\d[\s.\-]?){6,12}\d(?!\d)"
)

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_SEARCH_TOKEN_RE = re.compile(r"\b(?:BSA[A-Za-z0-9_-]{12,}|tvly-[A-Za-z0-9_-]{8,})\b")
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_URI_USERINFO_RE = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//[^/\s@]*:[^/\s@]*@")

_INTERNAL_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:wr|fe|pr|cl|res)-[0-9a-f]{6,16}\b", re.I)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
_CONTEXT_NUM_ID_RE = re.compile(
    r"(?i)(?:\b(?:order|faktura|kolli|tracking|case|kund|ärende)(?:[_\s#-]*id|[_\s#-]*nr|[_\s#-]*nummer)?[\s:#=-]*\d{4,}\b"
    r"|\b\d{4,}[\s_-]*(?:order|faktura|kolli|tracking|case|kund|ärende)\b)"
)

_INTERNAL_HOST_EXACT = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "fd00:ec2::254"})
_INTERNAL_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".home.arpa",
    ".invalid",
    ".example",
    ".test",
)
_INTERNAL_HOST_IN_TEXT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:"
    r"localhost|127\.0\.0\.1|0\.0\.0\.0|169\.254\.169\.254|"
    r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|internal|localhost|lan|home\.arpa|invalid|example|test)"
    r")(?![A-Za-z0-9.-])"
)
_INTERNAL_PATH_TOKENS = (
    "/var/lib/azom",
    "/opt/azom-agent",
    "/var/log/azom",
    "wp-admin",
    "wp-login.php",
    "xmlrpc.php",
)

_IPV4_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)$")
_VALID_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_VALID_TLD_LABEL_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")
_ADDR_POSTAL_CITY_4_RE = re.compile(r"\s+\d{4}\s+[A-Za-zÅÄÖØÆåäöøæ].*$")
_ADDR_POSTAL_CITY_5_RE = re.compile(r"\s+\d{3}\s?\d{2}\s+[A-Za-zÅÄÖØÆåäöøæ].*$")

_TRACKING_QUERY_PARAMS = frozenset({"fbclid", "gclid"})

# Single-token collision allowlist for Nordic/Azom domain words (only applies to
# single customer-name tokens >= 4 chars, NEVER full names/emails/phones/addresses/order IDs/secrets).
_SINGLE_TOKEN_COLLISION_ALLOWLIST = frozenset(
    {
        "nord",
        "post",
        "bring",
        "auto",
        "play",
        "sverige",
        "norge",
        "danmark",
        "azom",
    }
)

_SEARCH_SECRET_ENV_KEYS = (
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "SEARXNG_API_KEY",
    "PARALLEL_API_KEY",
)


def _as_tuple(val: Any) -> tuple[str, ...]:
    if val is None:
        return ()
    if isinstance(val, str):
        s = val.strip()
        return (s,) if s else ()
    if isinstance(val, Iterable):
        out: list[str] = []
        for item in val:
            s = str(item or "").strip()
            if s:
                out.append(s)
        return tuple(out)
    return ()


@dataclass(frozen=True)
class QueryTaintContext:
    """Case/order/session PII & secret taint context blocked from outbound queries."""

    customer_names: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    postal_codes: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    tracking_numbers: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for attr in (
            "customer_names",
            "emails",
            "phones",
            "addresses",
            "postal_codes",
            "order_ids",
            "tracking_numbers",
            "case_ids",
            "secrets",
        ):
            object.__setattr__(self, attr, _as_tuple(getattr(self, attr)))


def _active_env_secrets(taint: QueryTaintContext | None) -> list[str]:
    secrets_list: list[str] = []
    for key in SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            secrets_list.append(val)
    for key in _SEARCH_SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            secrets_list.append(val)
    for env_key, env_val in os.environ.items():
        if env_key.startswith("WP_SECRET_"):
            v = (env_val or "").strip()
            if len(v) >= 8:
                secrets_list.append(v)
    if taint is not None:
        for sec in taint.secrets:
            if len(sec) >= 8:
                secrets_list.append(sec)
    return secrets_list


def _has_valid_dk_cpr(text: str) -> bool:
    for match in _DK_CPR_RE.finditer(text):
        day = int(match.group(1))
        month = int(match.group(2))
        year_2 = int(match.group(3))
        if not (1 <= month <= 12):
            continue
        # Validate against 2000+year_2 (allows Feb 29 on century boundaries safely)
        max_day = calendar.monthrange(2000 + year_2, month)[1]
        if month == 2 and day == 29:
            return True
        if 1 <= day <= max_day:
            return True
    return False


def _is_ip_or_numeric_host(host: str) -> bool:
    clean = host.strip("[]")
    if not clean:
        return True
    try:
        ipaddress.ip_address(clean)
        return True
    except ValueError:
        pass
    parts = clean.split(".")
    if all(_IPV4_PART_RE.match(p) for p in parts):
        return True
    return False


def _normalize_domain(domain: str) -> str:
    d = str(domain or "").strip().lower().lstrip(".").rstrip(".")
    if not d:
        return ""
    try:
        return d.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return d


def _host_matches_domain(host: str, domain: str) -> bool:
    d = _normalize_domain(domain)
    if not d:
        return False
    return host == d or host.endswith(f".{d}")


def _dashboard_public_host() -> str:
    raw = (os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        return _normalize_domain(parsed.hostname or "")
    except ValueError:
        return ""


def _normalize_for_scan(raw: str, *, is_url: bool = False) -> tuple[str, ...]:
    decoded = normalize_pii_text(raw)
    stripped_raw = _BIDI_RE.sub("", raw)
    stripped_dec = _BIDI_RE.sub("", decoded)
    raw_cands: list[str] = [raw, stripped_raw, decoded, stripped_dec]
    if is_url:
        plus_raw = unquote_plus(raw)
        plus_dec = normalize_pii_text(plus_raw)
        raw_cands.extend([plus_raw, plus_dec, _BIDI_RE.sub("", plus_dec)])
    out = []
    for cand in raw_cands:
        if cand and cand not in out:
            out.append(cand)
    return tuple(out)


def _word_boundary_match(token: str, candidates: tuple[str, ...]) -> bool:
    if not token:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)", re.IGNORECASE)
    return any(pattern.search(cand) for cand in candidates)


def _taint_id_match(token: str, candidates: tuple[str, ...]) -> bool:
    t = str(token or "").strip()
    if not t:
        return False
    if t.isdigit():
        pattern = re.compile(rf"(?<!\d){re.escape(t)}(?!\d)")
        return any(pattern.search(cand) for cand in candidates)
    if len(t) >= 5:
        low = t.lower()
        return any(low in cand.lower() for cand in candidates)
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", re.IGNORECASE)
    return any(pattern.search(cand) for cand in candidates)


def _check_common_policy(
    candidates: tuple[str, ...],
    *,
    taint: QueryTaintContext | None,
    is_url: bool = False,
) -> None:
    active_secrets = _active_env_secrets(taint)

    for cand in candidates:
        # 1. Secrets & credentials (raw substring match retained; never allowlisted)
        for sec in active_secrets:
            if sec in cand:
                raise SearchPolicyError("OutboundQueryBlocked: secret_detected")
        if _JWT_RE.search(cand) or _SEARCH_TOKEN_RE.search(cand) or _AUTH_SCHEME_RE.search(cand):
            raise SearchPolicyError("OutboundQueryBlocked: secret_detected")
        if _URI_USERINFO_RE.search(cand):
            raise SearchPolicyError("OutboundQueryBlocked: secret_detected")
        if redact_secret_text(cand, extra_secrets=active_secrets) != cand:
            raise SearchPolicyError("OutboundQueryBlocked: secret_detected")

        # 2. PII (emails, Nordic phones, SE personnummer, NO fodselsnummer, DK CPR, intl phones)
        if contains_real_pii(cand):
            raise SearchPolicyError("OutboundQueryBlocked: pii_detected")
        if _has_valid_dk_cpr(cand):
            raise SearchPolicyError("OutboundQueryBlocked: pii_dk_cpr")
        if _INTL_PHONE_RE.search(cand):
            raise SearchPolicyError("OutboundQueryBlocked: pii_phone")

        # 3. Internal IDs
        if _INTERNAL_ID_RE.search(cand) or _UUID_RE.search(cand) or _CONTEXT_NUM_ID_RE.search(cand):
            raise SearchPolicyError("OutboundQueryBlocked: internal_id")

        # 4. Internal hosts and paths
        if _INTERNAL_HOST_IN_TEXT_RE.search(cand):
            raise SearchPolicyError("OutboundQueryBlocked: internal_host")
        lower_cand = cand.lower()
        dash_host = _dashboard_public_host()
        if dash_host and _word_boundary_match(dash_host, (lower_cand,)):
            raise SearchPolicyError("OutboundQueryBlocked: internal_dashboard_host")
        for token in _INTERNAL_PATH_TOKENS:
            if token in lower_cand:
                raise SearchPolicyError("OutboundQueryBlocked: internal_path")

    # 5. Context-bound taint checks (#17.2)
    if taint is not None:
        # Always blocked (no allowlist exemptions)
        for full_name in taint.customer_names:
            norm_name = " ".join(full_name.split())
            if norm_name and _word_boundary_match(norm_name, candidates):
                raise SearchPolicyError("OutboundQueryBlocked: taint_customer_name")
            for tok in re.findall(r"\w+", norm_name):
                if len(tok) >= 4 and tok.lower() not in _SINGLE_TOKEN_COLLISION_ALLOWLIST:
                    if _word_boundary_match(tok, candidates):
                        raise SearchPolicyError("OutboundQueryBlocked: taint_customer_token")

        for email in taint.emails:
            e = email.strip()
            if not e:
                continue
            if any(e.lower() in c.lower() for c in candidates):
                raise SearchPolicyError("OutboundQueryBlocked: taint_email")
            if "@" in e:
                local = e.split("@", 1)[0].strip()
                if len(local) >= 4 and _word_boundary_match(local, candidates):
                    raise SearchPolicyError("OutboundQueryBlocked: taint_email_local")

        digit_candidates = tuple("".join(ch for ch in c if ch.isdigit()) for c in candidates)
        for phone in taint.phones:
            digits = "".join(ch for ch in phone if ch.isdigit())
            if len(digits) >= 7:
                variants: set[str] = {digits}
                if digits.startswith("0") and len(digits) >= 8:
                    variants.add(digits[1:])
                e164_base = ""
                if digits.startswith("00") and len(digits) > 4:
                    e164_base = digits[2:]
                elif phone.strip().startswith("+") or len(digits) >= 9:
                    e164_base = digits
                if e164_base:
                    if len(e164_base) >= 7:
                        variants.add(e164_base)
                    for cc in ("46", "47", "45"):
                        if e164_base.startswith(cc) and len(e164_base) - len(cc) >= 7:
                            nat = e164_base[len(cc) :]
                            variants.add(nat)
                            variants.add(f"0{nat}")
                if any(var in dc for var in variants for dc in digit_candidates):
                    raise SearchPolicyError("OutboundQueryBlocked: taint_phone")

        for addr in taint.addresses:
            norm_addr = " ".join(addr.split())
            street = norm_addr.split(",", 1)[0].strip()
            street_no5 = _ADDR_POSTAL_CITY_5_RE.sub("", street).strip()
            street_no4 = _ADDR_POSTAL_CITY_4_RE.sub("", street_no5).strip()
            for addr_cand in {norm_addr, street, street_no5, street_no4}:
                if len(addr_cand) >= 4 and _word_boundary_match(addr_cand, candidates):
                    raise SearchPolicyError("OutboundQueryBlocked: taint_address")

        for postal in taint.postal_codes:
            p = " ".join(postal.split())
            p_compact = p.replace(" ", "")
            if len(p_compact) >= 4 and (
                _word_boundary_match(p, candidates) or _word_boundary_match(p_compact, candidates)
            ):
                raise SearchPolicyError("OutboundQueryBlocked: taint_postal")

        for oid in taint.order_ids:
            if oid and _taint_id_match(oid, candidates):
                raise SearchPolicyError("OutboundQueryBlocked: taint_order_id")

        for trk in taint.tracking_numbers:
            if trk and _taint_id_match(trk, candidates):
                raise SearchPolicyError("OutboundQueryBlocked: taint_tracking")

        for cid in taint.case_ids:
            if cid and _word_boundary_match(cid, candidates):
                raise SearchPolicyError("OutboundQueryBlocked: taint_case_id")

    _ = is_url


class OutboundQueryFirewall:
    """Validates outbound search queries and fetch URLs fail-closed before egress."""

    def check_text(
        self,
        text: str,
        *,
        max_chars: int = 500,
        taint: QueryTaintContext | None = None,
    ) -> str:
        """Validate ``text`` up to ``max_chars`` on the full normalized string without chunk splits."""
        if not isinstance(text, str):
            raise SearchPolicyError("OutboundQueryBlocked: invalid_query_type")
        if "\n" in text or "\r" in text or _CTRL_RE.search(text):
            raise SearchPolicyError("OutboundQueryBlocked: control_chars_or_multiline")
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            raise SearchPolicyError("OutboundQueryBlocked: empty_query")
        if max_chars > 0 and len(cleaned) > max_chars:
            raise SearchPolicyError("OutboundQueryBlocked: query_too_long")

        candidates = _normalize_for_scan(cleaned, is_url=False)
        _check_common_policy(candidates, taint=taint, is_url=False)
        return cleaned

    def check_query(self, query: str, *, taint: QueryTaintContext | None = None) -> str:
        """Validate ``query`` and return normalized single-line text or raise ``SearchPolicyError``."""
        return self.check_text(query, max_chars=MAX_QUERY_CHARS, taint=taint)

    def check_url(
        self,
        url: str,
        *,
        taint: QueryTaintContext | None = None,
        denylist_domains: Sequence[str] | None = None,
        allowlist_domains: Sequence[str] | None = None,
    ) -> str:
        """Validate public HTTPS ``url``, strip tracking params, and enforce domain policy."""
        if not isinstance(url, str):
            raise SearchPolicyError("OutboundQueryBlocked: invalid_url_type")
        if _CTRL_RE.search(url) or any(ch.isspace() for ch in url.strip()):
            raise SearchPolicyError("OutboundQueryBlocked: invalid_url_whitespace")
        if "\\" in url:
            raise SearchPolicyError("OutboundQueryBlocked: backslash_in_url")
        raw = url.strip()
        if not raw:
            raise SearchPolicyError("OutboundQueryBlocked: empty_url")
        if len(raw) > MAX_URL_CHARS:
            raise SearchPolicyError("OutboundQueryBlocked: url_too_long")

        try:
            parsed = urlsplit(raw)
        except ValueError:
            raise SearchPolicyError("OutboundQueryBlocked: malformed_url")

        if parsed.scheme.lower() != "https":
            raise SearchPolicyError("OutboundQueryBlocked: non_https_blocked")
        netloc = parsed.netloc or ""
        if parsed.username is not None or parsed.password is not None or "@" in netloc:
            raise SearchPolicyError("OutboundQueryBlocked: userinfo_blocked")
        if "[" in netloc or "]" in netloc:
            raise SearchPolicyError("OutboundQueryBlocked: ip_literal_blocked")
        try:
            port = parsed.port
        except ValueError:
            raise SearchPolicyError("OutboundQueryBlocked: invalid_port")
        if port not in (None, 443):
            raise SearchPolicyError("OutboundQueryBlocked: port_blocked")

        raw_host = (parsed.hostname or "").strip().lower()
        host_no_dot = raw_host.rstrip(".")
        if not host_no_dot or raw_host.startswith(".") or ".." in host_no_dot:
            raise SearchPolicyError("OutboundQueryBlocked: missing_host")
        try:
            host = host_no_dot.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            raise SearchPolicyError("OutboundQueryBlocked: invalid_host")
        if not host or len(host) > 253:
            raise SearchPolicyError("OutboundQueryBlocked: invalid_host")
        if host in _INTERNAL_HOST_EXACT or any(host.endswith(sfx) for sfx in _INTERNAL_HOST_SUFFIXES):
            raise SearchPolicyError("OutboundQueryBlocked: internal_host")
        if _is_ip_or_numeric_host(host):
            raise SearchPolicyError("OutboundQueryBlocked: ip_literal_blocked")
        labels = host.split(".")
        if (
            len(labels) < 2
            or not all(_VALID_HOST_LABEL_RE.fullmatch(lbl) for lbl in labels)
            or _VALID_TLD_LABEL_RE.fullmatch(labels[-1]) is None
        ):
            raise SearchPolicyError("OutboundQueryBlocked: invalid_host")

        dash_host = _dashboard_public_host()
        if dash_host and _host_matches_domain(host, dash_host):
            raise SearchPolicyError("OutboundQueryBlocked: internal_dashboard_host")

        # Strip tracking parameters (utm_*, fbclid, gclid)
        filtered_params = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_QUERY_PARAMS
        ]
        clean_query = urlencode(filtered_params, doseq=True)
        cleaned_url = urlunsplit(("https", host, parsed.path or "/", clean_query, ""))
        if len(cleaned_url) > MAX_URL_CHARS:
            raise SearchPolicyError("OutboundQueryBlocked: url_too_long")

        # Check both raw and cleaned URL against PII, secrets, taint, internal IDs/paths
        candidates = _normalize_for_scan(raw, is_url=True) + _normalize_for_scan(cleaned_url, is_url=True)
        _check_common_policy(candidates, taint=taint, is_url=True)

        # Canonical security precedence: denylist > allowlist
        cfg = load_research_config()
        effective_deny: list[str] = [
            nd for d in cfg.egress.denylist_domains if (nd := _normalize_domain(d))
        ]
        if denylist_domains:
            effective_deny.extend(
                nd for d in denylist_domains if (nd := _normalize_domain(str(d)))
            )
        if dash_host:
            effective_deny.append(dash_host)
        for denied in effective_deny:
            if _host_matches_domain(host, denied):
                raise SearchPolicyError("OutboundQueryBlocked: domain_denylisted")

        effective_allow: tuple[str, ...]
        if allowlist_domains is not None:
            effective_allow = tuple(
                na for d in allowlist_domains if (na := _normalize_domain(str(d)))
            )
        else:
            effective_allow = tuple(
                na for d in effective_allowlist_domains(cfg) if (na := _normalize_domain(d))
            )

        if effective_allow and not any(_host_matches_domain(host, allowed) for allowed in effective_allow):
            raise SearchPolicyError("OutboundQueryBlocked: domain_not_allowlisted")

        return cleaned_url


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_URL_CHARS",
    "OutboundQueryFirewall",
    "QueryTaintContext",
]
