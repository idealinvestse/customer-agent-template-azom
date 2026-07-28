"""Pure helpers for outbound mail thread headers."""

from __future__ import annotations


def assemble_outbound_thread_headers(
    *,
    parent: str | None,
    references_header: str | None = None,
    in_reply_to: str | None = None,
) -> tuple[str | None, str | None]:
    """Build In-Reply-To / References from parent message fields."""
    refs: list[str] = []
    if references_header:
        refs.extend(references_header.split())
    if in_reply_to:
        refs.append(in_reply_to)
    if parent:
        refs.append(parent)
    seen: set[str] = set()
    unique: list[str] = []
    for part in refs:
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            unique.append(p)
    return parent, (" ".join(unique) if unique else None)
