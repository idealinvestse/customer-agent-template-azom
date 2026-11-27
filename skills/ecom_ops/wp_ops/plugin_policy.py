"""Pure-Python WordPress plugin update policy, canary batching, and auto-update drift engine.

Zero network I/O, zero filesystem I/O, zero external transport imports.
"""

from __future__ import annotations

import re
from typing import Any

from ecom_ops.wp_ops.file_paths import SLUG_RE
from ecom_ops.wp_ops.source_scan import parse_semver_key

BRIDGE_PLUGIN_SLUG = "azom-ops-bridge"

CRITICAL_PLUGIN_SLUGS: frozenset[str] = frozenset(
    {
        "woocommerce",
        "klarna-checkout-for-woocommerce",
        "klarna-payments-for-woocommerce",
        "woo-vipps",
        "woocommerce-gateway-stripe",
        "svea-checkout-for-woocommerce",
        BRIDGE_PLUGIN_SLUG,
    }
)

SELECTION_MODES: frozenset[str] = frozenset({"single", "selected", "all", "policy"})
UPDATE_POLICIES: frozenset[str] = frozenset(
    {"security_only", "patch_only", "minor_safe", "non_critical_only"}
)
AUTOUPDATE_PRESETS: frozenset[str] = frozenset(
    {"all_off", "non_critical_only", "allowlist_only"}
)

_RISK_ORDER: dict[str, int] = {
    "security": 0,
    "patch": 1,
    "minor": 2,
    "major": 3,
    "unknown": 4,
}
_NUMERIC_PREFIX_RE = re.compile(r"^\s*v?(\d+(?:\.\d+){0,3})", re.IGNORECASE)


def normalize_plugin_slug(slug_or_file: str) -> str:
    """Normalize a plugin slug or ``dir/file.php`` identifier to its lowercase slug."""
    raw = str(slug_or_file or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if "/" in raw:
        head = raw.split("/", 1)[0].strip()
    else:
        head = raw[:-4] if raw.lower().endswith(".php") else raw
    return head.strip().lower()


def is_bridge_plugin(slug_or_file: str) -> bool:
    """True when ``slug_or_file`` refers to the self-protected ``azom-ops-bridge`` plugin."""
    return normalize_plugin_slug(slug_or_file) == BRIDGE_PLUGIN_SLUG


def is_critical_plugin(slug_or_file: str) -> bool:
    """True when ``slug_or_file`` is in canonical ``CRITICAL_PLUGIN_SLUGS``."""
    return normalize_plugin_slug(slug_or_file) in CRITICAL_PLUGIN_SLUGS


def _parse_version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version or not isinstance(version, str):
        return None
    parsed = parse_semver_key(version)
    if parsed is not None:
        return tuple(parsed[0])
    match = _NUMERIC_PREFIX_RE.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def classify_update_risk(
    from_version: str | None,
    to_version: str | None,
    *,
    security_advisory: bool = False,
) -> str:
    """Classify a plugin version transition as ``security|major|minor|patch|unknown``."""
    if security_advisory:
        return "security"
    cur = _parse_version_tuple(from_version)
    nxt = _parse_version_tuple(to_version)
    if cur is None or nxt is None:
        return "unknown"
    width = max(3, len(cur), len(nxt))
    cur_pad = cur + (0,) * (width - len(cur))
    nxt_pad = nxt + (0,) * (width - len(nxt))
    if nxt_pad <= cur_pad:
        return "unknown"
    if nxt_pad[0] > cur_pad[0]:
        return "major"
    if nxt_pad[1] > cur_pad[1]:
        return "minor"
    return "patch"


def version_satisfies(actual: str | None, minimum: str | None) -> bool | None:
    """Return True if ``actual >= minimum``, False if lower, or None if unparseable."""
    if not minimum or not str(minimum).strip():
        return True
    cur = _parse_version_tuple(actual)
    req = _parse_version_tuple(minimum)
    if cur is None or req is None:
        return None
    width = max(len(cur), len(req))
    cur_pad = cur + (0,) * (width - len(cur))
    req_pad = req + (0,) * (width - len(req))
    return cur_pad >= req_pad


def _clean_slug_list(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        slug = normalize_plugin_slug(str(item or ""))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def select_plugin_updates(
    plugins: list[dict[str, Any]],
    *,
    selection_mode: str = "policy",
    slugs: list[str] | None = None,
    policy: str = "non_critical_only",
    exclude_slugs: list[str] | None = None,
    include_critical: bool = False,
    canary: bool = True,
    wp_version: str = "",
    php_version: str = "",
) -> dict[str, Any]:
    """Select eligible plugin updates and organize them into canary-safe batches."""
    mode = str(selection_mode or "policy").strip().lower()
    if mode not in SELECTION_MODES:
        raise ValueError(f"Unsupported selection_mode: {selection_mode!r}")
    pol = str(policy or "non_critical_only").strip().lower()
    if mode == "policy" and pol not in UPDATE_POLICIES:
        raise ValueError(f"Unsupported update policy: {policy!r}")

    requested_slugs = _clean_slug_list(slugs)
    if mode == "single" and len(requested_slugs) != 1:
        raise ValueError("selection_mode='single' requires exactly one plugin slug.")
    if mode == "selected" and not requested_slugs:
        raise ValueError("selection_mode='selected' requires at least one plugin slug.")
    requested_set = set(requested_slugs)
    excluded_set = set(_clean_slug_list(exclude_slugs))

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_slugs: set[str] = set()

    for row in plugins:
        if not isinstance(row, dict):
            continue
        plugin_file = str(row.get("plugin_file") or "").strip()
        slug = normalize_plugin_slug(str(row.get("slug") or plugin_file))
        if not slug:
            continue
        seen_slugs.add(slug)

        if mode in {"single", "selected"} and slug not in requested_set:
            continue

        if is_bridge_plugin(slug) or bool(row.get("is_bridge")):
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "bridge_self_protected",
                }
            )
            if slug in requested_set:
                warnings.append("azom-ops-bridge är självskyddad och uppdateras aldrig via automatik.")
            continue

        if slug in excluded_set:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "excluded_by_operator",
                }
            )
            continue

        update_available = bool(row.get("update_available"))
        new_version = str(row.get("new_version") or "").strip()
        from_version = str(row.get("version") or "").strip()
        if not update_available or not new_version:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "up_to_date",
                }
            )
            continue

        critical = bool(row.get("is_critical")) or is_critical_plugin(slug)
        advisory = bool(row.get("security_advisory")) or bool(row.get("security_advisories"))
        risk_class = classify_update_risk(
            from_version,
            new_version,
            security_advisory=advisory,
        )

        req_php = str(row.get("requires_php") or "").strip()
        if php_version and req_php and version_satisfies(php_version, req_php) is False:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "php_version_incompatible",
                    "requires_php": req_php,
                }
            )
            warnings.append(
                f"{slug} kräver PHP {req_php} (sajten kör PHP {php_version}); hoppades över."
            )
            continue

        req_wp = str(row.get("requires_wp") or "").strip()
        if wp_version and req_wp and version_satisfies(wp_version, req_wp) is False:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "wp_version_incompatible",
                    "requires_wp": req_wp,
                }
            )
            warnings.append(
                f"{slug} kräver WordPress {req_wp} (sajten kör WP {wp_version}); hoppades över."
            )
            continue

        if mode == "policy" and pol == "non_critical_only" and critical:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "policy_non_critical_only",
                    "risk_class": risk_class,
                }
            )
            continue

        if critical and not include_critical:
            skipped.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "reason": "critical_excluded_by_default",
                    "risk_class": risk_class,
                }
            )
            warnings.append(
                f"Kritiskt plugin '{slug}' exkluderades (kräver include_critical=True)."
            )
            continue

        if mode == "policy":
            if pol == "security_only" and risk_class != "security":
                skipped.append(
                    {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "reason": "policy_security_only",
                        "risk_class": risk_class,
                    }
                )
                continue
            if pol == "patch_only" and risk_class not in {"security", "patch"}:
                skipped.append(
                    {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "reason": "policy_patch_only",
                        "risk_class": risk_class,
                    }
                )
                continue
            if pol == "minor_safe" and risk_class not in {"security", "patch", "minor"}:
                skipped.append(
                    {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "reason": "policy_minor_safe",
                        "risk_class": risk_class,
                    }
                )
                continue

        if risk_class == "major":
            warnings.append(
                f"{slug} har en major-uppdatering ({from_version} -> {new_version}); kontrollera changelog noga."
            )
        if row.get("hpos_compatible") is False:
            warnings.append(
                f"{slug} deklarerar inte WooCommerce HPOS-kompatibilitet."
            )

        selected.append(
            {
                "slug": slug,
                "plugin_file": plugin_file,
                "name": str(row.get("name") or slug),
                "from_version": from_version,
                "to_version": new_version,
                "active": bool(row.get("active")),
                "is_critical": critical,
                "risk_class": risk_class,
                "requires_wp": req_wp,
                "requires_php": req_php,
                "hpos_compatible": row.get("hpos_compatible"),
            }
        )

    for missing in sorted(requested_set - seen_slugs):
        skipped.append({"slug": missing, "plugin_file": "", "reason": "not_installed"})
        warnings.append(f"Begärt plugin '{missing}' hittades inte i inventeringen.")

    non_critical = sorted(
        [item for item in selected if not item["is_critical"]],
        key=lambda item: (
            _RISK_ORDER.get(item["risk_class"], 9),
            1 if item["active"] else 0,
            item["slug"],
        ),
    )
    critical_items = sorted(
        [item for item in selected if item["is_critical"]],
        key=lambda item: (
            0 if item["slug"] == "woocommerce" else 1,
            _RISK_ORDER.get(item["risk_class"], 9),
            item["slug"],
        ),
    )
    ordered = [*non_critical, *critical_items]

    batches: list[dict[str, Any]] = []
    if canary:
        idx = 1
        if non_critical:
            batches.append(
                {
                    "batch_index": idx,
                    "stage": "canary",
                    "is_critical": False,
                    "plugins": [non_critical[0]],
                }
            )
            idx += 1
            if len(non_critical) > 1:
                batches.append(
                    {
                        "batch_index": idx,
                        "stage": "non_critical",
                        "is_critical": False,
                        "plugins": non_critical[1:],
                    }
                )
                idx += 1
        for crit in critical_items:
            batches.append(
                {
                    "batch_index": idx,
                    "stage": "critical",
                    "is_critical": True,
                    "plugins": [crit],
                }
            )
            idx += 1
    elif ordered:
        batches.append(
            {
                "batch_index": 1,
                "stage": "ordered",
                "is_critical": bool(critical_items),
                "plugins": ordered,
            }
        )

    return {
        "selected": ordered,
        "batches": batches,
        "skipped": skipped,
        "warnings": warnings,
        "has_critical": bool(critical_items),
    }


def default_autoupdate_policy(site_id: str) -> dict[str, Any]:
    """Return the default fail-closed auto-update policy for ``site_id``."""
    return {
        "site_id": str(site_id),
        "preset": "non_critical_only",
        "allow_slugs": [],
        "deny_slugs": sorted(CRITICAL_PLUGIN_SLUGS),
        "allow_critical": False,
        "updated_at": "",
        "updated_by": "",
    }


def validate_autoupdate_policy(site_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an auto-update policy, always protecting ``azom-ops-bridge``."""
    if not isinstance(raw, dict):
        raise ValueError("Policy must be a JSON object.")
    preset = str(raw.get("preset") or "non_critical_only").strip().lower()
    if preset not in AUTOUPDATE_PRESETS:
        raise ValueError(f"Unsupported auto-update preset: {preset!r}")

    allow_raw = raw.get("allow_slugs") or []
    deny_raw = raw.get("deny_slugs") or []
    if not isinstance(allow_raw, list) or not isinstance(deny_raw, list):
        raise ValueError("allow_slugs and deny_slugs must be lists.")

    allow_slugs: list[str] = []
    for item in allow_raw:
        slug = normalize_plugin_slug(str(item or ""))
        if not slug or not SLUG_RE.fullmatch(slug):
            raise ValueError(f"Invalid slug in allow_slugs: {item!r}")
        if is_bridge_plugin(slug):
            raise ValueError("azom-ops-bridge cannot be added to allow_slugs.")
        if slug not in allow_slugs:
            allow_slugs.append(slug)

    deny_slugs: list[str] = []
    for item in deny_raw:
        slug = normalize_plugin_slug(str(item or ""))
        if not slug or not SLUG_RE.fullmatch(slug):
            raise ValueError(f"Invalid slug in deny_slugs: {item!r}")
        if slug not in deny_slugs:
            deny_slugs.append(slug)

    if BRIDGE_PLUGIN_SLUG not in deny_slugs:
        deny_slugs.append(BRIDGE_PLUGIN_SLUG)

    allow_critical = bool(raw.get("allow_critical", False))
    if not allow_critical:
        for crit in sorted(CRITICAL_PLUGIN_SLUGS):
            if crit not in deny_slugs:
                deny_slugs.append(crit)
        allow_slugs = [s for s in allow_slugs if not is_critical_plugin(s)]

    deny_set = set(deny_slugs)
    allow_slugs = [s for s in allow_slugs if s not in deny_set]

    return {
        "site_id": str(site_id),
        "preset": preset,
        "allow_slugs": sorted(allow_slugs),
        "deny_slugs": sorted(deny_slugs),
        "allow_critical": allow_critical,
        "updated_at": str(raw.get("updated_at") or ""),
        "updated_by": str(raw.get("updated_by") or ""),
    }


def evaluate_autoupdate_drift(
    plugins: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    constants: dict[str, Any] | None = None,
    autoupdate_filters: list[Any] | None = None,
) -> dict[str, Any]:
    """Compare live ``auto_update_plugins`` state against declarative site policy."""
    preset = str(policy.get("preset") or "non_critical_only").strip().lower()
    allow_set = set(_clean_slug_list(policy.get("allow_slugs")))
    deny_set = set(_clean_slug_list(policy.get("deny_slugs"))) | {BRIDGE_PLUGIN_SLUG}
    allow_critical = bool(policy.get("allow_critical", False))

    drift_items: list[dict[str, Any]] = []
    desired_changes: list[dict[str, Any]] = []

    for row in plugins:
        if not isinstance(row, dict):
            continue
        plugin_file = str(row.get("plugin_file") or "").strip()
        slug = normalize_plugin_slug(str(row.get("slug") or plugin_file))
        if not slug or not plugin_file:
            continue
        actual = bool(row.get("auto_update"))
        critical = bool(row.get("is_critical")) or is_critical_plugin(slug)

        if is_bridge_plugin(slug):
            desired = False
            reason = "bridge_self_protected"
        elif slug in deny_set:
            desired = False
            reason = "deny_list"
        elif critical and not allow_critical:
            desired = False
            reason = "critical_protected"
        elif preset == "all_off":
            desired = False
            reason = "preset_all_off"
        elif preset == "allowlist_only":
            desired = slug in allow_set
            reason = "allowlist_match" if desired else "not_in_allowlist"
        else:
            desired = not critical or allow_critical
            reason = "preset_non_critical_only"

        if actual != desired:
            drift_items.append(
                {
                    "slug": slug,
                    "plugin_file": plugin_file,
                    "actual": actual,
                    "desired": desired,
                    "is_critical": critical,
                    "reason": reason,
                }
            )
            if not is_bridge_plugin(slug):
                desired_changes.append(
                    {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "enabled": desired,
                    }
                )

    const_map = dict(constants or {})
    filters_list = list(autoupdate_filters or [])
    override_warnings: list[str] = []
    if const_map.get("automatic_updater_disabled"):
        override_warnings.append(
            "AUTOMATIC_UPDATER_DISABLED är aktiv i wp-config.php — WordPress kör inte autouppdateringar."
        )
    if const_map.get("disallow_file_mods"):
        override_warnings.append(
            "DISALLOW_FILE_MODS är aktiv i wp-config.php — filändringar och plugin-uppdateringar blockeras av WP."
        )
    if filters_list:
        override_warnings.append(
            f"{len(filters_list)} PHP-filter är kopplade till 'auto_update_plugin' och kan överstyra inställningen."
        )

    return {
        "in_sync": len(drift_items) == 0,
        "drift_count": len(drift_items),
        "drift_items": drift_items,
        "desired_changes": desired_changes,
        "override_warnings": override_warnings,
        "constants": const_map,
        "autoupdate_filters": filters_list,
    }


__all__ = [
    "AUTOUPDATE_PRESETS",
    "BRIDGE_PLUGIN_SLUG",
    "CRITICAL_PLUGIN_SLUGS",
    "SELECTION_MODES",
    "UPDATE_POLICIES",
    "classify_update_risk",
    "default_autoupdate_policy",
    "evaluate_autoupdate_drift",
    "is_bridge_plugin",
    "is_critical_plugin",
    "normalize_plugin_slug",
    "select_plugin_updates",
    "validate_autoupdate_policy",
    "version_satisfies",
]
