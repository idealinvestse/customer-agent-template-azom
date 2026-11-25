"""Fail-closed WordPress.org plugin/theme metadata lookup with offline/mock defaults.

Live HTTP calls to ``api.wordpress.org`` require BOTH ``AZOM_WPORG_INTEL=1``
AND ``AZOM_USE_MOCK!=1`` (and ``allow_live`` not False). By default and in mock
mode, no live network request is ever performed.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
from collections.abc import Callable
from typing import Any

from ecom_ops.integrations.wp_org_client import WpOrgClientError, fetch_wporg_json
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.code_index import _scrub_signature

_VALID_KINDS = frozenset({"theme", "plugin"})
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
_SEMVER_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:[-+]([A-Za-z0-9_.-]+))?\s*$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_MOCK_INTEL: dict[tuple[str, str], dict[str, Any]] = {
    ("plugin", "woocommerce"): {
        "latest_version": "9.4.3",
        "requires_php": "7.4",
        "requires_wp": "6.5",
        "tested_wp": "6.7",
        "active_installs": 7_000_000,
    },
    ("plugin", "wordpress-seo"): {
        "latest_version": "23.9",
        "requires_php": "7.4",
        "requires_wp": "6.5",
        "tested_wp": "6.7",
        "active_installs": 5_000_000,
    },
    ("plugin", "contact-form-7"): {
        "latest_version": "6.0.1",
        "requires_php": "7.4",
        "requires_wp": "6.5",
        "tested_wp": "6.7",
        "active_installs": 10_000_000,
    },
    ("plugin", "elementor"): {
        "latest_version": "3.25.10",
        "requires_php": "7.4",
        "requires_wp": "6.3",
        "tested_wp": "6.7",
        "active_installs": 10_000_000,
    },
    ("plugin", "azom-helper"): {
        "latest_version": "1.2.0",
        "requires_php": "8.1",
        "requires_wp": "6.5",
        "tested_wp": "6.7",
        "active_installs": 1,
    },
    ("theme", "storefront"): {
        "latest_version": "4.6.0",
        "requires_php": "7.4",
        "requires_wp": "6.0",
        "tested_wp": "6.7",
        "active_installs": 4_000_000,
    },
    ("theme", "hello-elementor"): {
        "latest_version": "3.1.1",
        "requires_php": "7.4",
        "requires_wp": "6.0",
        "tested_wp": "6.7",
        "active_installs": 1_000_000,
    },
    ("theme", "twentytwentyfive"): {
        "latest_version": "1.0",
        "requires_php": "7.2",
        "requires_wp": "6.7",
        "tested_wp": "6.7",
        "active_installs": 1_000_000,
    },
    ("theme", "twentytwentyfour"): {
        "latest_version": "1.3",
        "requires_php": "7.0",
        "requires_wp": "6.4",
        "tested_wp": "6.7",
        "active_installs": 1_000_000,
    },
    ("theme", "azom-child"): {
        "latest_version": "1.0.0",
        "requires_php": "8.1",
        "requires_wp": "6.5",
        "tested_wp": "6.7",
        "active_installs": 1,
    },
}


def _parse_version_key(version: str | None) -> tuple[tuple[int, ...], int, str] | None:
    if not isinstance(version, str) or not version.strip():
        return None
    match = _SEMVER_RE.fullmatch(version.strip())
    if not match:
        return None
    nums = tuple(int(part) for part in match.group(1).split("."))
    pre = (match.group(2) or "").lower()
    stability = 0 if pre else 1
    return nums, stability, pre


def newer_available(installed_version: str | None, latest_version: str | None) -> bool | None:
    """Return True if ``latest_version`` is strictly newer than ``installed_version``."""
    cur = _parse_version_key(installed_version)
    latest = _parse_version_key(latest_version)
    if cur is None or latest is None:
        return None
    width = max(len(cur[0]), len(latest[0]))
    cur_nums = cur[0] + (0,) * (width - len(cur[0]))
    latest_nums = latest[0] + (0,) * (width - len(latest[0]))
    return (latest_nums, latest[1], latest[2]) > (cur_nums, cur[1], cur[2])


def _clean_str(value: object, *, max_len: int = 80) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = _CONTROL_CHARS_RE.sub(" ", str(value)).strip()
    if not text:
        return None
    return _scrub_signature(text, max_len=max_len)


def _clean_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return max(0, int(value.strip()))
    return None


def _wporg_url(kind: str, slug: str) -> str:
    quoted = urllib.parse.quote(slug, safe="")
    if kind == "plugin":
        return (
            "https://api.wordpress.org/plugins/info/1.2/"
            f"?action=plugin_information&request[slug]={quoted}"
        )
    return (
        "https://api.wordpress.org/themes/info/1.2/"
        f"?action=theme_information&request[slug]={quoted}"
    )


def _result_payload(
    *,
    kind: str,
    slug: str,
    installed_version: str | None,
    source: str,
    intel_enabled: bool,
    latest_version: str | None = None,
    requires_php: str | None = None,
    requires_wp: str | None = None,
    tested_wp: str | None = None,
    active_installs: int | None = None,
    security_advisories: list[dict[str, Any]] | None = None,
    note: str = "",
    error_class: str | None = None,
) -> dict[str, Any]:
    clean_installed = _clean_str(installed_version, max_len=40)
    clean_latest = _clean_str(latest_version, max_len=40)
    payload: dict[str, Any] = {
        "kind": kind,
        "slug": slug,
        "source": source,
        "intel_enabled": intel_enabled,
        "installed_version": clean_installed,
        "latest_version": clean_latest,
        "newer_available": newer_available(clean_installed, clean_latest),
        "requires_php": _clean_str(requires_php, max_len=32),
        "requires_wp": _clean_str(requires_wp, max_len=32),
        "tested_wp": _clean_str(tested_wp, max_len=32),
        "active_installs": active_installs,
        "security_advisories": list(security_advisories or []),
        "note": _scrub_signature(note, max_len=200),
    }
    if error_class:
        payload["error_class"] = error_class
    return payload


def fetch_component_intel(
    kind: str,
    slug: str,
    installed_version: str | None = None,
    *,
    allow_live: bool | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch WordPress.org plugin/theme metadata with strict offline/mock gating."""
    clean_kind = str(kind or "").strip().lower()
    if clean_kind not in _VALID_KINDS:
        raise ValueError(f"Unsupported component kind: {kind!r}")
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid component slug: {slug!r}")

    is_mock = env_flag("AZOM_USE_MOCK")
    live_env_enabled = os.environ.get("AZOM_WPORG_INTEL", "").strip() == "1" and not is_mock

    if allow_live is False or (fetcher is None and not live_env_enabled):
        if is_mock:
            mock_row = _MOCK_INTEL.get((clean_kind, slug))
            if mock_row is None:
                return _result_payload(
                    kind=clean_kind,
                    slug=slug,
                    installed_version=installed_version,
                    source="mock",
                    intel_enabled=False,
                    note=f"Mock mode: no fixture entry for {clean_kind} '{slug}'.",
                )
            return _result_payload(
                kind=clean_kind,
                slug=slug,
                installed_version=installed_version,
                source="mock",
                intel_enabled=False,
                latest_version=mock_row["latest_version"],
                requires_php=mock_row["requires_php"],
                requires_wp=mock_row["requires_wp"],
                tested_wp=mock_row["tested_wp"],
                active_installs=mock_row["active_installs"],
                note=f"Deterministic mock intel for {clean_kind} '{slug}'.",
            )
        return _result_payload(
            kind=clean_kind,
            slug=slug,
            installed_version=installed_version,
            source="disabled",
            intel_enabled=False,
            note="WordPress.org live intel disabled (requires AZOM_WPORG_INTEL=1 and AZOM_USE_MOCK=0).",
        )

    url = _wporg_url(clean_kind, slug)
    active_fetcher = fetcher if fetcher is not None else fetch_wporg_json
    try:
        raw = active_fetcher(url)
    except (
        WpOrgClientError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _result_payload(
            kind=clean_kind,
            slug=slug,
            installed_version=installed_version,
            source="wporg",
            intel_enabled=True,
            error_class="WpOrgUnavailable",
            note=f"WordPress.org lookup failed ({type(exc).__name__}).",
        )

    if not isinstance(raw, dict) or raw.get("error"):
        return _result_payload(
            kind=clean_kind,
            slug=slug,
            installed_version=installed_version,
            source="wporg",
            intel_enabled=True,
            error_class="WpOrgNotFound",
            note=f"WordPress.org returned no record for {clean_kind} '{slug}'.",
        )

    raw_advisories = raw.get("security_advisories")
    advisories: list[dict[str, Any]] = []
    if isinstance(raw_advisories, list):
        for entry in raw_advisories[:10]:
            if isinstance(entry, dict):
                advisories.append(
                    {
                        k: _clean_str(v, max_len=120)
                        for k, v in entry.items()
                        if isinstance(k, str) and not any(s in k.lower() for s in ("secret", "token", "key"))
                    }
                )

    return _result_payload(
        kind=clean_kind,
        slug=slug,
        installed_version=installed_version,
        source="wporg",
        intel_enabled=True,
        latest_version=_clean_str(raw.get("version"), max_len=40),
        requires_php=_clean_str(raw.get("requires_php"), max_len=32),
        requires_wp=_clean_str(raw.get("requires"), max_len=32),
        tested_wp=_clean_str(raw.get("tested"), max_len=32),
        active_installs=_clean_int(raw.get("active_installs")),
        security_advisories=advisories,
        note=f"Fetched WordPress.org metadata for {clean_kind} '{slug}'.",
    )
