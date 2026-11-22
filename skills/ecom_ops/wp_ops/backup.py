"""Local mock backup manifests.

The digest covers canonical metadata. It is not encryption. This provider
does not copy site files, write a database dump, or send bytes anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Protocol

from ecom_ops.wp_ops.models import Site

_BACKUP_ID = re.compile(r"^bk-[a-f0-9]{12}$")

_PARTS = ["files", "database"]
_DIGEST_FIELDS = (
    "id",
    "site_id",
    "environment",
    "provider",
    "offsite",
    "encryption",
    "destination_ref",
    "parts",
    "created_at",
    "retention_days",
    "woocommerce",
    "payload",
    "bytes_copied",
)


def valid_backup_id(value: str) -> bool:
    return bool(_BACKUP_ID.fullmatch(value))


class BackupProvider(Protocol):
    def verify(self, record: dict[str, Any]) -> bool:
        """Return true when the manifest digest still matches."""


def integrity_digest(record: dict[str, Any]) -> str:
    body = {key: record.get(key) for key in _DIGEST_FIELDS}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_manifest(site: Site, *, created_at: str) -> dict[str, Any]:
    backup_id = "bk-" + uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "id": backup_id,
        "site_id": site.id,
        "environment": site.environment,
        "provider": "mock",
        "offsite": False,
        "encryption": "unconfigured",
        "destination_ref": f"mock:{site.id}:{backup_id}",
        "parts": list(_PARTS),
        "created_at": created_at,
        "verified_at": None,
        "completion": "pending",
        "integrity": "pending",
        "digest": "",
        "retention_days": site.retention_days,
        "woocommerce": site.woocommerce,
        "payload": "metadata",
        "bytes_copied": False,
        "live_calls": False,
    }
    record["digest"] = integrity_digest(record)
    return record


class MockBackup:
    """Verify a local manifest. Never marks the set as off-site or encrypted."""

    def verify(self, record: dict[str, Any]) -> bool:
        if record.get("provider") != "mock" or record.get("offsite") is not False:
            return False
        if record.get("encryption") != "unconfigured" or record.get("payload") != "metadata":
            return False
        if record.get("bytes_copied") is not False or record.get("parts") != _PARTS:
            return False
        return record.get("digest") == integrity_digest(record)
