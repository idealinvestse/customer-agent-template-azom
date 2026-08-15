"""Single-tenant customer profile. Missing file → Azom defaults (fail-closed unchanged)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ecom_ops.config import _config_dir

DEFAULT_VIEWER = "jonatan"
DEFAULT_ADMIN = "oscar"
DEFAULT_CUSTOMER = "azom"


@dataclass(frozen=True)
class CustomerProfile:
    customer: str = DEFAULT_CUSTOMER
    brand: str = "Azom"
    markets: tuple[str, ...] = ("se", "no", "dk")
    languages: tuple[str, ...] = ("sv", "nb", "da")
    viewer_actor: str = DEFAULT_VIEWER
    admin_actor: str = DEFAULT_ADMIN
    operator_actor: str = "agent"

    def actor_usernames(self) -> tuple[str, str]:
        return (self.viewer_actor, self.admin_actor)


def _default_profile() -> CustomerProfile:
    return CustomerProfile()


def load_profile(path: Path | None = None) -> CustomerProfile:
    cfg = path or (_config_dir() / "profile.yaml")
    if not cfg.is_file():
        return _default_profile()
    try:
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return _default_profile()
    if not isinstance(raw, dict):
        return _default_profile()
    actors = raw.get("actors") if isinstance(raw.get("actors"), dict) else {}
    markets = raw.get("markets") or ["se", "no", "dk"]
    languages = raw.get("languages") or ["sv", "nb", "da"]
    return CustomerProfile(
        customer=str(raw.get("customer") or DEFAULT_CUSTOMER),
        brand=str(raw.get("brand") or "Azom"),
        markets=tuple(str(m) for m in markets),
        languages=tuple(str(x) for x in languages),
        viewer_actor=str(actors.get("viewer") or DEFAULT_VIEWER),
        admin_actor=str(actors.get("admin") or DEFAULT_ADMIN),
        operator_actor=str(actors.get("operator") or "agent"),
    )


def reload_profile() -> None:
    """Kept for tests; profile is loaded fresh each call."""
    return None
