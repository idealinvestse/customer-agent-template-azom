"""Load the local WordPress site registry without storing secret values."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ecom_ops.wp_ops.models import Site, parse_site


def config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def load_sites(
    path: Path | None = None,
    *,
    data_dir: Path | None = None,
    apply_overlay: bool = True,
) -> tuple[Site, ...]:
    if path is not None:
        file_path = Path(path)
    else:
        config_root = config_dir()
        raw_mock = os.environ.get("AZOM_USE_MOCK")
        mock_mode = raw_mock is None or raw_mock.strip().lower() not in {"0", "false", "no", "off"}
        if mock_mode:
            mock_registry = config_root / "wp_sites.mock.yaml"
            file_path = mock_registry if mock_registry.is_file() else config_root / "wp_sites.yaml"
        else:
            data_root = Path(data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data"))
            runtime_registry = data_root / "wp_sites.yaml"
            file_path = runtime_registry if runtime_registry.is_file() else config_root / "wp_sites.yaml"
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
    if not apply_overlay:
        return sites
    from ecom_ops.wp_ops.ssh_settings import apply_ssh_overlay

    return apply_ssh_overlay(sites, data_dir)


def site_by_id(sites: tuple[Site, ...], site_id: str) -> Site | None:
    for site in sites:
        if site.id == site_id:
            return site
    return None


def resolve_site_id_from_text(
    text: str,
    *,
    path: Path | None = None,
    data_dir: Path | None = None,
) -> str:
    """Resolve a single unambiguous registered site id from message text."""
    raw = (text or "").strip()
    if not raw:
        return ""
    sites = load_sites(path=path, data_dir=data_dir, apply_overlay=False)
    matches: list[Site] = []
    for site in sites:
        id_hit = bool(
            site.id
            and re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(site.id)}(?![A-Za-z0-9_-])",
                raw,
                re.I,
            )
        )
        host = (urlparse(site.canonical_url).hostname or "").strip()
        host_hit = bool(
            host
            and re.search(
                rf"(?<![A-Za-z0-9.-]){re.escape(host)}(?![A-Za-z0-9.-])",
                raw,
                re.I,
            )
        )
        if id_hit or host_hit:
            matches.append(site)
    if len(matches) == 1:
        return matches[0].id
    return ""


def as_public(sites: tuple[Site, ...], present: dict[str, bool]) -> list[dict[str, Any]]:
    return [site.public_dict(credential_present=present.get(site.id, False)) for site in sites]
