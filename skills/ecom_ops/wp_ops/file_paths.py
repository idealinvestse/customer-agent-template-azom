"""Pure path validation, error types, and plan constants for WordPress file ops.

This module has no I/O and never imports transports or external clients.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.models import files_root_narrow

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
PLAN_ID_RE = re.compile(r"^fe-[a-f0-9]{12}$")

ALLOWED_EXTENSIONS = frozenset(
    {".php", ".css", ".scss", ".js", ".json", ".txt", ".md", ".html", ".pot", ".po"}
)
MAX_FILE_BYTES = 65536
COMPONENT_KINDS = frozenset({"theme", "plugin"})
PLAN_TTL = timedelta(hours=12)
SKIP_DIRS = frozenset({"vendor", "node_modules", "cache", "uploads", ".git"})
SENSITIVE_RE = re.compile(
    r"(?i)(^\.env(?:$|\.)|^wp-config[^/]*\.php$|"
    r"(?:^|[-_.])(?:secrets?|credentials?|tokens?|passwords?|auth|private[-_.]?key|api[-_.]?key)(?:[-_.]|$)|"
    r"\.(?:bak|backup|sql|sqlite3?|dump|swp|orig)$)"
)

FILE_TOOL_NAMES = frozenset(
    {
        "wp_component_context",
        "wp_list_components",
        "wp_theme_overview",
        "wp_plugin_overview",
        "wp_list_component_tree",
        "wp_search_component",
        "wp_read_theme_plugin_file",
        "wp_propose_file_diff",
        "wp_propose_new_file",
        "wp_validate_plan",
        "wp_apply_staging",
        "wp_request_production",
        "wp_deploy_production",
        "wp_verify_change",
        "wp_rollback_file",
        "wp_open_review_workspace",
        "wp_workspace_patch_file",
        "wp_workspace_create_file",
        "wp_workspace_copy_from_parent",
        "wp_workspace_status",
        "wp_workspace_run_checks",
    }
)

TRANSPORT_MESSAGES: dict[str, str] = {
    "SshIdentityRejected": "The SSH identity path was rejected.",
    "SshIdentityMissing": "A per-site SSH key is required before SFTP.",
    "SshIdentityPermissions": "The per-site SSH key permissions are too broad.",
    "SshHostKeyMissing": "A public host key is required before SSH.",
    "SshHostKeyMismatch": "The server host key did not match the pinned key.",
    "SshAuthenticationFailed": "The dedicated SSH key was not accepted.",
    "SshDedicatedUserRequired": "Live file access requires a dedicated *-wp-reader account.",
    "SshWriteDisabled": "Live SFTP is read-only; file writes are disabled.",
    "SshDirectoryTooLarge": "The remote directory exceeds the safe listing limit.",
    "FileTooLarge": "The remote file exceeds the safe read limit.",
    "FileMissing": "The requested file was not found.",
}

# Compatibility aliases for callers importing private names from wp_ops.files.
_SLUG = SLUG_RE
_SEGMENT = SEGMENT_RE
_PLAN_ID = PLAN_ID_RE
_EXTENSIONS = ALLOWED_EXTENSIONS
_MAX_BYTES = MAX_FILE_BYTES
_KINDS = COMPONENT_KINDS
_PLAN_TTL = PLAN_TTL
_SKIP_DIRS = SKIP_DIRS
_SENSITIVE = SENSITIVE_RE


class PathRejected(ValueError):
    """The path is outside the theme or plugin directory."""


class TransportError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


def transport_message(error_class: str) -> str:
    return TRANSPORT_MESSAGES.get(error_class, "File transport failed.")


def sha256_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_digest = sha256_digest


def sensitive_relative(relative: str) -> bool:
    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part]
    return any(part.lower() in SKIP_DIRS or SENSITIVE_RE.search(part) for part in parts)


def components_parent(root: str, kind: str) -> str:
    if kind not in COMPONENT_KINDS:
        raise PathRejected("kind")
    if not files_root_narrow(root):
        raise PathRejected("root")
    folder = "themes" if kind == "theme" else "plugins"
    return f"{root}/wp-content/{folder}"


def component_dir(root: str, kind: str, slug: str) -> str:
    if not SLUG_RE.fullmatch(slug):
        raise PathRejected("component")
    return components_parent(root, kind) + "/" + slug


def resolve_file(root: str, kind: str, slug: str, relative: str) -> str:
    """Return one absolute path inside the component, or reject it."""
    base = component_dir(root, kind, slug)
    text = str(relative or "").strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part != ""]
    if not parts or text.startswith("/") or any(part in {".", ".."} for part in parts):
        raise PathRejected("relative")
    if any(SEGMENT_RE.fullmatch(part) is None for part in parts):
        raise PathRejected("segment")
    if sensitive_relative("/".join(parts)):
        raise PathRejected("sensitive")
    leaf = parts[-1]
    if leaf.endswith(".azom-bak"):
        raise PathRejected("backup")
    suffix = Path(leaf).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise PathRejected("extension")
    full = base + "/" + "/".join(parts)
    prefix = base + "/"
    if not full.startswith(prefix) or ".." in full.split("/"):
        raise PathRejected("escape")
    return full


def resolve_dir(root: str, kind: str, slug: str, relative: str = "") -> str:
    """Return one absolute directory path inside the component, or reject it."""
    base = component_dir(root, kind, slug)
    text = str(relative or "").strip().replace("\\", "/")
    if not text:
        return base
    parts = [part for part in text.split("/") if part]
    if text.startswith("/") or any(part in {".", ".."} for part in parts):
        raise PathRejected("relative")
    if any(SEGMENT_RE.fullmatch(part) is None or sensitive_relative(part) for part in parts):
        raise PathRejected("segment")
    return base + "/" + "/".join(parts)


def plan_expired(plan: dict[str, Any], now: datetime) -> bool:
    """Legacy rows without expires_at expire 12 hours after created_at."""
    stamp = plan.get("expires_at") or plan.get("created_at")
    if not isinstance(stamp, str) or not stamp:
        return True
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if not isinstance(now, datetime):
        now = datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if plan.get("expires_at"):
        return now >= parsed
    return now - parsed > PLAN_TTL


__all__ = [
    "ALLOWED_EXTENSIONS",
    "COMPONENT_KINDS",
    "FILE_TOOL_NAMES",
    "MAX_FILE_BYTES",
    "PLAN_ID_RE",
    "PLAN_TTL",
    "PathRejected",
    "SEGMENT_RE",
    "SENSITIVE_RE",
    "SKIP_DIRS",
    "SLUG_RE",
    "TRANSPORT_MESSAGES",
    "TransportError",
    "_EXTENSIONS",
    "_KINDS",
    "_MAX_BYTES",
    "_PLAN_ID",
    "_PLAN_TTL",
    "_SEGMENT",
    "_SENSITIVE",
    "_SKIP_DIRS",
    "_SLUG",
    "_digest",
    "component_dir",
    "components_parent",
    "plan_expired",
    "resolve_dir",
    "resolve_file",
    "sensitive_relative",
    "sha256_digest",
    "transport_message",
]
