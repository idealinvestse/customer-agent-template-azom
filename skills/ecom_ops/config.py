"""Load and validate agent configuration from config/ + environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.security import SecurityError, validate_site


def _repo_root() -> Path:
    # skills/ecom_ops/config.py -> repo root
    return Path(__file__).resolve().parents[2]


def _config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return _repo_root() / "config"


def load_yaml(name: str) -> dict[str, Any]:
    path = _config_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SecurityError(f"Config {name} must be a mapping")
    return data


def load_json(name: str) -> dict[str, Any]:
    path = _config_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SecurityError(f"Config {name} must be a mapping")
    return data


@dataclass(frozen=True)
class SiteConfig:
    customer: str
    domains: list[str]
    budget_cap_llm: float


@dataclass(frozen=True)
class RbacConfig:
    roles: dict[str, str]
    escalation_critical: str
    escalation_code_edit: str


@dataclass(frozen=True)
class LimitsConfig:
    openrouter_cap: float
    jonatan_role: str
    openrouter_warn_ratio: float = 0.8


@dataclass(frozen=True)
class AppConfig:
    customer: SiteConfig
    rbac: RbacConfig
    limits: LimitsConfig
    integrations: dict[str, Any] = field(default_factory=dict)
    customer_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def default_site(self) -> str:
        return validate_site(self.customer.customer)


def load_app_config() -> AppConfig:
    sites = load_yaml("sites.yaml")
    rbac_raw = load_yaml("rbac.yaml")
    limits_raw = load_yaml("limits.yaml")
    integrations = load_yaml("integrations.yaml")
    try:
        customer_meta = load_json("customer.json")
    except FileNotFoundError:
        customer_meta = {}

    customer = SiteConfig(
        customer=validate_site(str(sites.get("customer", "azom"))),
        domains=[str(d) for d in sites.get("domains", [])],
        budget_cap_llm=float(sites.get("budget_cap_llm", 80)),
    )
    escalation = rbac_raw.get("escalation") or {}
    rbac = RbacConfig(
        roles={str(k): str(v) for k, v in (rbac_raw.get("roles") or {}).items()},
        escalation_critical=str(escalation.get("critical", "oscar")),
        escalation_code_edit=str(escalation.get("code_edit", "oscar")),
    )
    warn_ratio = float(limits_raw.get("openrouter_warn_ratio", 0.8))
    if warn_ratio <= 0 or warn_ratio > 1:
        warn_ratio = 0.8
    limits = LimitsConfig(
        openrouter_cap=float(limits_raw.get("openrouter_cap", 100)),
        jonatan_role=str(limits_raw.get("jonatan_role", "read_only")),
        openrouter_warn_ratio=warn_ratio,
    )
    return AppConfig(
        customer=customer,
        rbac=rbac,
        limits=limits,
        integrations=integrations,
        customer_meta=customer_meta,
    )


def woo_base_url_for_domain(domain: str) -> str:
    """Resolve WooCommerce base URL from env or convention."""
    key = f"WOO_BASE_URL_{domain.upper()}"
    env_url = os.environ.get(key) or os.environ.get("WOO_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    # Convention for multi-site pilot; override via env in production
    return f"https://azom.{domain}"
