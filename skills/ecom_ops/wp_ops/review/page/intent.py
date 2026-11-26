"""Detect a homepage source-review request. No IO and no permissions."""

from __future__ import annotations

import re

_PAGE_REVIEW_RE = re.compile(
    r"(?:"
    r"granska\s+startsid(?:an|ans)(?:\s+k[äa]llkod)?"
    r"|granska\s+(?:sidan|sida)\s+[a-z0-9][a-z0-9-]{0,60}\s+k[äa]llkod"
    r"|startsidans\s+k[äa]llkod"
    r"|homepage\s+source"
    r"|template\s+hierarchy"
    r"|mallhierarki"
    r")",
    re.I,
)
_PAGE_TARGET_RE = re.compile(
    r"\bgranska\s+(?:sidan|sida)\s+(?P<target>[a-z0-9][a-z0-9-]{0,60})(?:\s+k[äa]llkod)?\b",
    re.I,
)
_HOMEPAGE_WORDS = frozenset({"startsidan", "startsida", "hem", "home", "källkod", "kallkod"})


def is_page_review(text: str) -> bool:
    """True for a homepage source review. A bare page or order mention is not."""
    return bool(_PAGE_REVIEW_RE.search(text or ""))


def extract_page_review_target(text: str) -> str | None:
    """Return a targeted page slug/id when the request names a specific page."""
    if not is_page_review(text):
        return None
    match = _PAGE_TARGET_RE.search(text or "")
    if match is None:
        return None
    target = match.group("target").lower()
    if target in _HOMEPAGE_WORDS:
        return None
    return target


__all__ = [
    "extract_page_review_target",
    "is_page_review",
]
