"""Detect a homepage source-review request. No IO and no permissions."""

from __future__ import annotations

import re

_PAGE_REVIEW_RE = re.compile(
    r"(?:"
    r"granska\s+startsid(?:an|ans)(?:\s+k[äa]llkod)?"
    r"|startsidans\s+k[äa]llkod"
    r"|homepage\s+source"
    r"|template\s+hierarchy"
    r"|mallhierarki"
    r")",
    re.I,
)


def is_page_review(text: str) -> bool:
    """True for a homepage source review. A bare page or order mention is not."""
    return bool(_PAGE_REVIEW_RE.search(text or ""))
