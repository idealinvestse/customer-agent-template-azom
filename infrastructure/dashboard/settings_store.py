"""Load/save dashboard settings (YAML non-secrets) and env overlays."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

# Keys Jonatan may never write via settings form
SECRET_KEYS = frozenset(
    {
        "WOO_CONSUMER_KEY",
        "WOO_CONSUMER_SECRET",
        "WP_APP_PASSWORD",
        "SSH_PRIVATE_KEY",
        "SSH_PASSWORD",
        "OPENROUTER_API_KEY",
        "MAILCOW_API_KEY",
        "SMTP_PASSWORD",
        "MAIL_PASSWORD",
        "MAIL_OAUTH_ACCESS_TOKEN",
        "MAIL_OAUTH_REFRESH_TOKEN",
        "MAIL_OAUTH_CLIENT_SECRET",
        "GRAPH_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
    }
)

# Secrets Oscar may set via secrets.env overlay
EDITABLE_SECRET_KEYS = (
    "WOO_BASE_URL",
    "WOO_CONSUMER_KEY",
    "WOO_CONSUMER_SECRET",
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
    "MAIL_FROM",
    "MAIL_PROVIDER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_PASSWORD",
    "IMAP_HOST",
    "IMAP_PORT",
    "MAIL_OAUTH_CLIENT_ID",
    "MAIL_OAUTH_CLIENT_SECRET",
    "MAIL_OAUTH_REDIRECT_URI",
    "GRAPH_TENANT_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_CLIENT_SECRET",
    "GRAPH_USER",
    "SSH_HOST",
    "SSH_USER",
    "SSH_PORT",
    "SSH_PASSWORD",
    "OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_OSCAR_PASSWORD",
)

MAIL_PROVIDERS = (
    "gmail",
    "outlook",
    "exchange_graph",
    "generic_imap",
    "generic_pop3",
)


def _config_dir() -> Path:
    return Path(os.environ.get("AZOM_CONFIG_DIR", "config"))


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def apply_env_overlays() -> None:
    """Load runtime.env + secrets.env into os.environ (secrets win)."""
    for name in ("runtime.env", "secrets.env"):
        path = _data_dir() / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ[key] = val


def _load_yaml(name: str) -> dict[str, Any]:
    path = _config_dir() / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _save_yaml(name: str, data: dict[str, Any]) -> None:
    path = _config_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _write_env_file(path: Path, updates: dict[str, str], *, allow_keys: frozenset[str] | None = None) -> None:
    existing: dict[str, str] = {}
    order: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, _, v = raw.partition("=")
            k = k.strip()
            if k not in existing:
                order.append(k)
            existing[k] = v.strip()
    for k, v in updates.items():
        if allow_keys is not None and k not in allow_keys:
            continue
        if k not in existing:
            order.append(k)
        existing[k] = v
        os.environ[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={existing[k]}" for k in order if k in existing]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_settings_view() -> dict[str, Any]:
    apply_env_overlays()
    sites = _load_yaml("sites.yaml")
    limits = _load_yaml("limits.yaml")
    integrations = _load_yaml("integrations.yaml")
    email = integrations.get("email") if isinstance(integrations.get("email"), dict) else {}
    domains = sites.get("domains") or []
    if isinstance(domains, list):
        domains_str = ", ".join(str(d) for d in domains)
    else:
        domains_str = str(domains)
    mock = os.environ.get("AZOM_USE_MOCK", "1").lower() in {"1", "true", "yes"}
    return {
        "customer": str(sites.get("customer", "azom")),
        "domains": domains_str,
        "budget_cap_llm": float(sites.get("budget_cap_llm", 80)),
        "openrouter_cap": float(limits.get("openrouter_cap", 100)),
        "jonatan_role": str(limits.get("jonatan_role", "read_only")),
        "mail_provider": os.environ.get("MAIL_PROVIDER")
        or str(email.get("default_provider", "generic_imap")),
        "email_enabled": bool(email.get("enabled", True)),
        "email_smtp": bool(email.get("smtp", True)),
        "email_imap": bool(email.get("imap", True)),
        "email_pop3": bool(email.get("pop3", True)),
        "mailcow": bool(integrations.get("mailcow", True)),
        "order_api": bool(integrations.get("order_api", True)),
        "selenium": bool(integrations.get("selenium", True)),
        "woocommerce_api": bool(integrations.get("woocommerce_api", True)),
        "wordpress_api": bool(integrations.get("wordpress_api", True)),
        "smart_handling": bool(integrations.get("smart_handling", True)),
        "full_agent_tools": bool(integrations.get("full_agent_tools", True)),
        "mock_mode": mock,
        "mail_providers": list(MAIL_PROVIDERS),
    }


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def save_settings(form: dict[str, Any]) -> dict[str, Any]:
    """Save allowlisted non-secret settings. Raises ValueError on bad input."""
    for key in form:
        if key.upper() in SECRET_KEYS or key in SECRET_KEYS:
            raise ValueError(f"Secret key not editable here: {key}")

    sites = _load_yaml("sites.yaml")
    limits = _load_yaml("limits.yaml")
    integrations = _load_yaml("integrations.yaml")
    email = dict(integrations.get("email") or {}) if isinstance(integrations.get("email"), dict) else {}

    if "customer" in form:
        customer = str(form["customer"]).strip().lower()
        if not customer or len(customer) > 64:
            raise ValueError("Invalid customer")
        sites["customer"] = customer

    if "domains" in form:
        raw = str(form["domains"])
        domains = [d.strip() for d in raw.replace(";", ",").split(",") if d.strip()]
        if not domains:
            raise ValueError("At least one domain required")
        # Quote "no" for YAML boolean safety
        sites["domains"] = domains

    if "budget_cap_llm" in form:
        sites["budget_cap_llm"] = float(form["budget_cap_llm"])

    if "openrouter_cap" in form:
        limits["openrouter_cap"] = float(form["openrouter_cap"])

    if "mail_provider" in form:
        prov = str(form["mail_provider"]).strip().lower()
        if prov not in MAIL_PROVIDERS:
            raise ValueError(f"Invalid mail_provider: {prov}")
        email["default_provider"] = prov
        _write_env_file(
            _data_dir() / "runtime.env",
            {"MAIL_PROVIDER": prov},
            allow_keys=frozenset({"MAIL_PROVIDER", "AZOM_USE_MOCK"}),
        )

    for flag in ("email_enabled", "email_smtp", "email_imap", "email_pop3"):
        if flag in form:
            short = flag.replace("email_", "") if flag != "email_enabled" else "enabled"
            email[short] = _as_bool(form[flag])

    integrations["email"] = email

    for flag in (
        "mailcow",
        "order_api",
        "selenium",
        "woocommerce_api",
        "wordpress_api",
        "smart_handling",
        "full_agent_tools",
    ):
        if flag in form:
            integrations[flag] = _as_bool(form[flag])

    if "mock_mode" in form:
        mock_val = "1" if _as_bool(form["mock_mode"]) else "0"
        _write_env_file(
            _data_dir() / "runtime.env",
            {"AZOM_USE_MOCK": mock_val},
            allow_keys=frozenset({"MAIL_PROVIDER", "AZOM_USE_MOCK"}),
        )
        os.environ["AZOM_USE_MOCK"] = mock_val

    _save_yaml("sites.yaml", sites)
    _save_yaml("limits.yaml", limits)
    _save_yaml("integrations.yaml", integrations)

    try:
        from ecom_ops.rbac import clear_rbac_cache

        clear_rbac_cache()
    except Exception:
        pass

    return load_settings_view()


def save_secrets(updates: dict[str, str]) -> list[str]:
    """Oscar-only: write secrets.env. Returns list of keys saved."""
    cleaned: dict[str, str] = {}
    for k, v in updates.items():
        key = str(k).strip()
        if key not in EDITABLE_SECRET_KEYS:
            continue
        val = str(v).strip()
        if val == "":
            continue  # empty = leave unchanged
        cleaned[key] = val
    if not cleaned:
        return []
    _write_env_file(
        _data_dir() / "secrets.env",
        cleaned,
        allow_keys=frozenset(EDITABLE_SECRET_KEYS),
    )
    apply_env_overlays()
    return list(cleaned.keys())


def secrets_status() -> list[dict[str, Any]]:
    apply_env_overlays()
    groups = [
        ("WooCommerce", ["WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET"]),
        ("Mail", ["MAIL_PROVIDER", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM"]),
        ("Mail OAuth", ["MAIL_OAUTH_CLIENT_ID", "MAIL_OAUTH_CLIENT_SECRET", "MAIL_OAUTH_REDIRECT_URI"]),
        ("Graph", ["GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER"]),
        ("SSH", ["SSH_HOST", "SSH_USER", "SSH_PORT", "SSH_PASSWORD"]),
        ("LLM", ["OPENROUTER_API_KEY"]),
        ("Telegram", ["TELEGRAM_BOT_TOKEN"]),
        ("Dashboard", ["DASHBOARD_PASSWORD", "DASHBOARD_OSCAR_PASSWORD"]),
    ]
    out: list[dict[str, Any]] = []
    for group, keys in groups:
        for key in keys:
            out.append(
                {
                    "group": group,
                    "name": key,
                    "present": bool(os.environ.get(key, "").strip()),
                    "editable": key in EDITABLE_SECRET_KEYS,
                }
            )
    return out


def resolve_escalation(ticket_id: str) -> bool:
    """Mark escalation ticket resolved by rewriting JSONL. Returns True if found."""
    path = _data_dir() / "escalations.jsonl"
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    import json

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if str(obj.get("id")) == str(ticket_id):
            obj["status"] = "resolved"
            found = True
        out.append(json.dumps(obj, ensure_ascii=False))
    if found:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return found
