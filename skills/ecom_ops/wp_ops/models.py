"""Site records and check results for WordPress operations.

Capability values are only ``available``, ``unavailable``, or ``unknown``.
A missing capability is unknown. Unknown is not treated as available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

_SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SECRET_REF = re.compile(r"^WP_SECRET_[A-Z0-9_]{1,40}$")
_ERROR_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_FORBIDDEN_REFS = frozenset({"WP_USERNAME", "WP_APP_PASSWORD", "WORDPRESS_USERNAME"})
_CAPABILITIES = (
    "hosting_api",
    "wp_cli",
    "staging",
    "backup",
    "restore",
    "uptime",
    "security_scanner",
)
_CAP_VALUES = frozenset({"available", "unavailable", "unknown"})
_ENVIRONMENTS = frozenset({"production", "staging"})
_QUERY_SECRET = ("password", "token", "secret", "key")
_FILES_ROOT = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_./-]{0,180}$")
_SSH_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def clean_error_class(value: str | None) -> str | None:
    """Keep a short class token. Anything else becomes ``CheckFailed``."""
    if value is None:
        return None
    text = str(value).strip()
    if _ERROR_CLASS.fullmatch(text):
        return text
    return "CheckFailed"


def tone_for(result: str) -> str:
    """Only an explicit ``ok`` is a healthy tone. Fail and unknown are not."""
    if result == "ok":
        return "ok"
    return "attention"


def _probe_url(value: str) -> str:
    """Treat a bare host and the same host with a trailing slash as one target."""
    parsed = urlsplit(value)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, "", ""))


def https_url(value: str, *, field: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field} must be an https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} must not include credentials")
    for key in parse_qs(parsed.query):
        lowered = key.lower()
        if any(marker in lowered for marker in _QUERY_SECRET):
            raise ValueError(f"{field} must not carry secrets in the query")
    return raw


@dataclass(frozen=True)
class Site:
    id: str
    display_name: str
    canonical_url: str
    environment: str
    business_owner: str
    hosting_provider: str
    wordpress_version: str
    php_version: str
    woocommerce: bool
    critical_urls: tuple[str, ...]
    maintenance_window: str
    secret_ref: str
    capabilities: tuple[tuple[str, str], ...]
    alert_confirmations: int
    check_interval_seconds: int
    timeout_seconds: float
    dedupe_window_seconds: int
    retention_days: int
    alert_owner: str
    files_root: str
    ssh_host: str

    def capability_map(self) -> dict[str, str]:
        return dict(self.capabilities)

    def probe_urls(self) -> tuple[str, ...]:
        seen: list[str] = []
        for url in (self.canonical_url, *self.critical_urls):
            key = _probe_url(url)
            if key not in seen:
                seen.append(key)
        return tuple(seen)

    def public_dict(self, *, credential_present: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "canonical_url": self.canonical_url,
            "environment": self.environment,
            "business_owner": self.business_owner,
            "hosting_provider": self.hosting_provider,
            "wordpress_version": self.wordpress_version,
            "php_version": self.php_version,
            "version_source": "registry",
            "woocommerce": self.woocommerce,
            "critical_urls": list(self.critical_urls),
            "maintenance_window": self.maintenance_window,
            "secret_ref": self.secret_ref,
            "credential_present": credential_present,
            "capabilities": self.capability_map(),
            "alert_confirmations": self.alert_confirmations,
            "check_interval_seconds": self.check_interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "dedupe_window_seconds": self.dedupe_window_seconds,
            "retention_days": self.retention_days,
            "alert_owner": self.alert_owner,
            "files_root": self.files_root,
            "ssh_host": self.ssh_host,
            "pending_updates": None,
        }


def parse_site(raw: Any) -> Site:
    if not isinstance(raw, dict):
        raise ValueError("site entry must be a mapping")
    site_id = str(raw.get("id") or "").strip()
    if not _SITE_ID.fullmatch(site_id):
        raise ValueError(f"invalid site id: {site_id!r}")
    environment = str(raw.get("environment") or "").strip()
    if environment not in _ENVIRONMENTS:
        raise ValueError(f"{site_id}: environment must be production or staging")
    secret_ref = str(raw.get("secret_ref") or "").strip()
    if secret_ref in _FORBIDDEN_REFS or not _SECRET_REF.fullmatch(secret_ref):
        raise ValueError(f"{site_id}: secret_ref must be a WP_SECRET_* environment name")
    urls_raw = raw.get("critical_urls")
    if not isinstance(urls_raw, list) or not urls_raw or len(urls_raw) > 10:
        raise ValueError(f"{site_id}: critical_urls must be 1 to 10 https URLs")
    critical = tuple(https_url(str(url), field=f"{site_id} critical_urls") for url in urls_raw)
    canonical = https_url(str(raw.get("canonical_url") or ""), field=f"{site_id} canonical_url")
    caps_raw = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
    caps: list[tuple[str, str]] = []
    for name in _CAPABILITIES:
        value = str(caps_raw.get(name) or "unknown").strip()
        if value not in _CAP_VALUES:
            raise ValueError(f"{site_id}: capability {name} must be available, unavailable, or unknown")
        caps.append((name, value))
    confirmations = int(raw.get("alert_confirmations") or 2)
    if confirmations < 2 or confirmations > 10:
        raise ValueError(f"{site_id}: alert_confirmations must be 2 to 10")
    interval = int(raw.get("check_interval_seconds") or 300)
    if interval < 60 or interval > 86400:
        raise ValueError(f"{site_id}: check_interval_seconds must be 60 to 86400")
    timeout = float(raw.get("timeout_seconds") or 10)
    if timeout <= 0 or timeout > 30:
        raise ValueError(f"{site_id}: timeout_seconds must be greater than 0 and at most 30")
    window = int(raw.get("dedupe_window_seconds") or 30)
    if window < 5 or window > 300:
        raise ValueError(f"{site_id}: dedupe_window_seconds must be 5 to 300")
    if raw.get("retention_days") is None:
        retention = 14
    else:
        retention = int(raw.get("retention_days"))
    if retention < 1 or retention > 90:
        raise ValueError(f"{site_id}: retention_days must be 1 to 90")
    woo = raw.get("woocommerce")
    if not isinstance(woo, bool):
        raise ValueError(f"{site_id}: woocommerce must be true or false")
    return Site(
        id=site_id,
        display_name=_short_text(raw.get("display_name"), field=f"{site_id} display_name", limit=80),
        canonical_url=canonical,
        environment=environment,
        business_owner=_short_text(raw.get("business_owner"), field=f"{site_id} business_owner", limit=80),
        hosting_provider=_short_text(
            raw.get("hosting_provider"), field=f"{site_id} hosting_provider", limit=80
        ),
        wordpress_version=_short_text(
            raw.get("wordpress_version") or "",
            field=f"{site_id} wordpress_version",
            limit=40,
            allow_empty=True,
        ),
        php_version=_short_text(
            raw.get("php_version") or "",
            field=f"{site_id} php_version",
            limit=40,
            allow_empty=True,
        ),
        woocommerce=woo,
        critical_urls=critical,
        maintenance_window=_short_text(
            raw.get("maintenance_window"), field=f"{site_id} maintenance_window", limit=120
        ),
        secret_ref=secret_ref,
        capabilities=tuple(caps),
        alert_confirmations=confirmations,
        check_interval_seconds=interval,
        timeout_seconds=timeout,
        dedupe_window_seconds=window,
        retention_days=retention,
        alert_owner=_short_text(raw.get("alert_owner") or "unassigned", field=f"{site_id} alert_owner", limit=80),
        files_root=_files_root(raw.get("files_root"), site_id=site_id),
        ssh_host=_ssh_host(raw.get("ssh_host"), site_id=site_id),
    )


def _files_root(value: Any, *, site_id: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not _FILES_ROOT.fullmatch(text) or ".." in text.split("/"):
        raise ValueError(f"{site_id}: files_root must be an absolute path without '..'")
    return text


def _ssh_host(value: Any, *, site_id: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    if not _SSH_HOST.fullmatch(text):
        raise ValueError(f"{site_id}: ssh_host must be a hostname")
    return text


def _short_text(value: Any, *, field: str, limit: int, allow_empty: bool = False) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if any(ch in text for ch in "\r\n"):
        raise ValueError(f"{field} must be a single line")
    if not text and not allow_empty:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    return text
