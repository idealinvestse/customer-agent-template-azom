"""Per-site credential references.

The value stays in the process environment under that site's own
``WP_SECRET_*`` name. Callers never receive another site's value, and the
shared content-client variables are not a fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ecom_ops.rbac import AccessDenied
from ecom_ops.wp_ops.models import Site


@dataclass
class BoundCredential:
    ref: str
    present: bool
    value: str = field(default="", repr=False, compare=False)

    def __str__(self) -> str:
        return f"BoundCredential(ref={self.ref}, present={self.present})"


def bind_credential(site: Site, ref: str | None = None) -> BoundCredential:
    """Bind ``site`` to its own secret ref. A different ref is denied first."""
    wanted = site.secret_ref if ref is None else ref
    if wanted != site.secret_ref:
        raise AccessDenied("secret ref does not belong to this site")
    raw = os.environ.get(site.secret_ref)
    present = bool(raw and str(raw).strip())
    return BoundCredential(ref=site.secret_ref, present=present, value=raw if present else "")


def scrub_secrets(payload: object, secrets: list[str]) -> object:
    """Replace bound secret values that leaked into a nested structure."""
    blocked = [item for item in secrets if item and len(item) >= 8]

    def _one(value: object) -> object:
        if isinstance(value, str):
            out = value
            for secret in blocked:
                if secret in out:
                    out = out.replace(secret, "***")
            return out
        if isinstance(value, dict):
            return {str(key): _one(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_one(item) for item in value]
        return value

    return _one(payload)
