"""Pure, I/O-free CSS security normalization and file-change risk policy for ``wp_ops``."""

from __future__ import annotations

import re
from typing import Any

_HIGH_RISK = re.compile(
    r"checkout|cart|payment|gateway|kassan|varukorg|betalning",
    re.I,
)
_HIGH_RISK_PATH_PARTS = frozenset({"checkout", "cart", "gateway", "payment"})
_CSS_COMMENT_RE = re.compile(r"/\*[\s\S]*?(?:\*/|$)")
_CSS_HEX_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\r\n\f]?")
_CSS_CHAR_ESCAPE_RE = re.compile(r"\\([^\r\n\f0-9a-fA-F])")
_CSS_HIGH_RISK = re.compile(
    r"checkout|payment|gateway|kassan|betalning|<\?|<script\b|</|"
    r"javascript:|vbscript:|expression\s*\(|url\s*\(|image-set\s*\(|"
    r"@import\b|behavior\s*:|-moz-binding\s*:",
    re.I,
)


def normalize_css_for_check(text: str) -> str:
    """Strip CSS comments and decode CSS backslash/hex escapes before security regex checks."""
    cleaned = _CSS_COMMENT_RE.sub("", str(text or ""))

    def _decode_hex(match: re.Match[str]) -> str:
        try:
            cp = int(match.group(1), 16)
        except ValueError:
            return ""
        if cp == 0 or cp > 0x10FFFF:
            return "\ufffd"
        return chr(cp)

    cleaned = _CSS_HEX_ESCAPE_RE.sub(_decode_hex, cleaned)
    cleaned = _CSS_CHAR_ESCAPE_RE.sub(lambda m: m.group(1), cleaned)
    return cleaned


def is_safe_pure_css_file(relative: str, text: str) -> bool:
    """True for pure ``.css`` files outside checkout/payment paths with no script/PHP/exfil tokens."""
    clean = str(relative or "").strip().replace("\\", "/")
    if not clean.lower().endswith(".css"):
        return False
    parts = [part.lower() for part in clean.split("/") if part]
    if any(part in _HIGH_RISK_PATH_PARTS for part in parts):
        return False
    raw_text = text or ""
    normalized = normalize_css_for_check(raw_text)
    if (
        _CSS_HIGH_RISK.search(clean)
        or _CSS_HIGH_RISK.search(raw_text)
        or _CSS_HIGH_RISK.search(normalized)
    ):
        return False
    return True


def risk_for(relative: str, text: str) -> str:
    """Classify a file edit as ``'high'`` or ``'low'`` risk."""
    clean = str(relative or "").strip().replace("\\", "/")
    parts = [part.lower() for part in clean.split("/") if part]
    if any(part in _HIGH_RISK_PATH_PARTS for part in parts):
        return "high"
    if clean.lower().endswith(".css"):
        return "low" if is_safe_pure_css_file(clean, text) else "high"
    if _HIGH_RISK.search(clean) or _HIGH_RISK.search(text or ""):
        return "high"
    return "low"


def production_deploy_block() -> dict[str, Any]:
    """Production writes stay closed. A mock manifest is not a backup."""
    return {
        "executed": False,
        "error_class": "BackupNotVerified",
        "backup_offsite": False,
        "bytes_copied": False,
        "encryption": "unconfigured",
        "restore": "RestoreNotInMvp",
        "live_calls": False,
    }


__all__ = [
    "is_safe_pure_css_file",
    "normalize_css_for_check",
    "production_deploy_block",
    "risk_for",
]
