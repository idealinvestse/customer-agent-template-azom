"""Load ``config/wp_sites.yaml``. The file holds references, never secret values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.wp_ops.models import Site, parse_site


def config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def load_sites(path: Path | None = None) -> tuple[Site, ...]:
    file_path = path or (config_dir() / "wp_sites.yaml")
    if not file_path.is_file():
        return ()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("wp_sites.yaml must be a mapping")
    rows = loaded.get("sites")
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise ValueError("wp_sites.yaml sites must be a list")
    sites = tuple(parse_site(row) for row in rows)
    seen: set[str] = set()
    refs: set[str] = set()
    for site in sites:
        if site.id in seen:
            raise ValueError(f"duplicate site id: {site.id}")
        if site.secret_ref in refs:
            raise ValueError(f"duplicate secret_ref: {site.secret_ref}")
        seen.add(site.id)
        refs.add(site.secret_ref)
    return sites


def site_by_id(sites: tuple[Site, ...], site_id: str) -> Site | None:
    for site in sites:
        if site.id == site_id:
            return site
    return None


def as_public(sites: tuple[Site, ...], present: dict[str, bool]) -> list[dict[str, Any]]:
    return [site.public_dict(credential_present=present.get(site.id, False)) for site in sites]
