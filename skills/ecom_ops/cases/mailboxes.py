"""Load configurable functional mailboxes from config/mailboxes.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.config import _config_dir


@dataclass(frozen=True)
class MailboxConfig:
    id: str
    label: str
    address: str
    site: str = "azom"
    market: str | None = None
    language: str = "sv"
    enabled: bool = True
    provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "address": self.address,
            "site": self.site,
            "market": self.market,
            "language": self.language,
            "enabled": self.enabled,
            "provider": self.provider,
        }


def load_mailboxes(path: Path | None = None) -> list[MailboxConfig]:
    cfg_path = path or (_config_dir() / "mailboxes.yaml")
    if not cfg_path.is_file():
        return []
    with cfg_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    items = raw.get("mailboxes") or []
    out: list[MailboxConfig] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        out.append(
            MailboxConfig(
                id=mid,
                label=str(item.get("label") or mid),
                address=str(item.get("address") or ""),
                site=str(item.get("site") or "azom"),
                market=str(item["market"]) if item.get("market") else None,
                language=str(item.get("language") or "sv"),
                enabled=bool(item.get("enabled", True)),
                provider=str(item["provider"]) if item.get("provider") else None,
            )
        )
    return out


def enabled_mailboxes(path: Path | None = None) -> list[MailboxConfig]:
    return [m for m in load_mailboxes(path) if m.enabled]
