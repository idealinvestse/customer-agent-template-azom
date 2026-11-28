"""Load/save dashboard settings (YAML non-secrets) and env overlays."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
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
        "XAI_API_KEY",
        "MAILCOW_API_KEY",
        "SMTP_PASSWORD",
        "MAIL_PASSWORD",
        "MAIL_OAUTH_ACCESS_TOKEN",
        "MAIL_OAUTH_REFRESH_TOKEN",
        "MAIL_OAUTH_CLIENT_SECRET",
        "GRAPH_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "MESSENGER_PAGE_ACCESS_TOKEN",
        "MESSENGER_APP_SECRET",
        "MESSENGER_VERIFY_TOKEN",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GA4_MEASUREMENT_API_SECRET",
        "GSC_OAUTH_CLIENT_SECRET",
        "GSC_OAUTH_ACCESS_TOKEN",
        "GSC_OAUTH_REFRESH_TOKEN",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
        "DASHBOARD_SECRET_KEY",
        "PARALLEL_API_KEY",
        "EXA_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SEARXNG_API_KEY",
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
    "XAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "MESSENGER_PAGE_ACCESS_TOKEN",
    "MESSENGER_APP_SECRET",
    "MESSENGER_VERIFY_TOKEN",
    "AZOM_DASHBOARD_PUBLIC_URL",
    "WP_USERNAME",
    "WP_APP_PASSWORD",
    "WOO_WEBHOOK_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "AZOM_GA4_PROPERTY_IDS",
    "AZOM_GADS_CUSTOMER_IDS",
    "GA4_MEASUREMENT_ID",
    "GA4_MEASUREMENT_API_SECRET",
    "GSC_OAUTH_CLIENT_ID",
    "GSC_OAUTH_CLIENT_SECRET",
    "GSC_OAUTH_REDIRECT_URI",
    "AZOM_GSC_PROPERTY_URL_NO",
    "AZOM_GSC_PROPERTY_URL_SE",
    "AZOM_GSC_PROPERTY_URL_DK",
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "SEARXNG_API_KEY",
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
    """Load allowlisted overlay keys. Delegates to ecom_ops.runtime_env."""
    from ecom_ops.runtime_env import apply_overlays

    apply_overlays(data_dir=_data_dir())


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


def _reject_control_chars(label: str, value: str) -> None:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"Ogiltiga kontrolltecken i {label}")


def _atomic_write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_env_file(path: Path, updates: dict[str, str], *, allow_keys: frozenset[str] | None = None) -> None:
    from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS

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
        _reject_control_chars("nyckel", str(k))
        _reject_control_chars(str(k), str(v))
        if allow_keys is not None and k not in allow_keys:
            continue
        if k in PROTECTED_OVERLAY_KEYS:
            continue
        if k not in existing:
            order.append(k)
        existing[k] = v
        os.environ[k] = v
    lines = [f"{k}={existing[k]}" for k in order if k in existing]
    _atomic_write_0600(path, "\n".join(lines) + ("\n" if lines else ""))


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
    from ecom_ops.runtime_env import env_flag

    mock = env_flag("AZOM_USE_MOCK", default=True)
    cap = float(limits.get("openrouter_cap", 100))
    return {
        "customer": str(sites.get("customer", "azom")),
        "domains": domains_str,
        "budget_cap_llm": cap,
        "openrouter_cap": cap,
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
        "live_locked": not mock,
        "mail_providers": list(MAIL_PROVIDERS),
    }


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _parse_positive_cap(raw: Any, label: str) -> float:
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} måste vara ett positivt tal")
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"{label} måste vara ett ändligt tal > 0")
    return val


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
        _parse_positive_cap(form["budget_cap_llm"], "LLM budget cap")
    if "openrouter_cap" in form:
        cap = _parse_positive_cap(form["openrouter_cap"], "OpenRouter cap")
        limits["openrouter_cap"] = cap
        sites["budget_cap_llm"] = cap
    elif "budget_cap_llm" in form:
        cap = _parse_positive_cap(form["budget_cap_llm"], "LLM budget cap")
        limits["openrouter_cap"] = cap
        sites["budget_cap_llm"] = cap

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

    mock_mode_ignored = False
    if "mock_mode" in form:
        from ecom_ops.runtime_env import env_flag

        # Live process: never flip mock (webhook fail-closed). Overlay never
        # stores AZOM_USE_MOCK — process/systemd env is the only source.
        if env_flag("AZOM_USE_MOCK", default=True):
            os.environ["AZOM_USE_MOCK"] = "1" if _as_bool(form["mock_mode"]) else "0"
        elif _as_bool(form["mock_mode"]):
            mock_mode_ignored = True

    _save_yaml("sites.yaml", sites)
    _save_yaml("limits.yaml", limits)
    _save_yaml("integrations.yaml", integrations)

    try:
        from ecom_ops.rbac import clear_rbac_cache

        clear_rbac_cache()
    except Exception:
        pass

    view = load_settings_view()
    view["mock_mode_ignored"] = mock_mode_ignored
    return view


MODEL_RUNTIME_KEYS = frozenset(
    {
        "OPENAI_MODEL",
        "XAI_MODEL",
        "OPENROUTER_MODEL",
        "AZOM_XAI_GROK_BUILD",
    }
)


def _valid_model_id(value: str) -> bool:
    if not value or len(value) > 128 or any(ch.isspace() for ch in value):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/@+-~")
    return all(ch in allowed for ch in value)


def save_model_runtime(form: dict[str, Any], *, grok_build_posted: bool = False) -> list[str]:
    """Write non-secret model ids to runtime.env. Empty fields stay unchanged."""
    updates: dict[str, str] = {}
    for key in ("OPENAI_MODEL", "XAI_MODEL", "OPENROUTER_MODEL"):
        if key not in form:
            continue
        raw = str(form.get(key) or "").strip()
        if raw == "":
            continue
        if not _valid_model_id(raw):
            raise ValueError(f"Ogiltigt modell-id för {key}")
        updates[key] = raw
    if grok_build_posted:
        updates["AZOM_XAI_GROK_BUILD"] = "1" if _as_bool(form.get("AZOM_XAI_GROK_BUILD")) else "0"
    if not updates:
        return []
    _write_env_file(
        _data_dir() / "runtime.env",
        updates,
        allow_keys=MODEL_RUNTIME_KEYS,
    )
    apply_env_overlays()
    return list(updates)


def secrets_file_has_key(key: str) -> bool:
    """True when secrets.env names the key. The value is not returned."""
    path = _data_dir() / "secrets.env"
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        name, _, _value = raw.partition("=")
        if name.strip() == key:
            return True
    return False


def save_secrets(updates: dict[str, str]) -> list[str]:
    """Oscar-only: write secrets.env. Returns list of keys saved."""
    cleaned: dict[str, str] = {}
    for k, v in updates.items():
        raw_k = str(k)
        raw_v = str(v)
        _reject_control_chars("nyckel", raw_k)
        key = raw_k.strip()
        if key not in EDITABLE_SECRET_KEYS:
            continue
        _reject_control_chars(key, raw_v)
        val = raw_v.strip()
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


_SECRET_FIELD_META: dict[str, dict[str, Any]] = {
    "WOO_BASE_URL": {"secret": False, "input_type": "url", "label": "WooCommerce bas-URL", "hint": "t.ex. https://azom.se"},
    "WOO_CONSUMER_KEY": {"secret": True, "input_type": "password", "label": "WooCommerce Consumer Key", "hint": "REST API ck_..."},
    "WOO_CONSUMER_SECRET": {"secret": True, "input_type": "password", "label": "WooCommerce Consumer Secret", "hint": "REST API cs_..."},
    "WOO_WEBHOOK_SECRET": {"secret": True, "input_type": "password", "label": "WooCommerce Webhook Secret", "hint": "HMAC-hemlighet för /webhooks/woo"},
    "WP_USERNAME": {"secret": False, "input_type": "text", "label": "WordPress användarnamn", "hint": "REST-användare"},
    "WP_APP_PASSWORD": {"secret": True, "input_type": "password", "label": "WordPress Application Password", "hint": "App-lösenord för WP REST"},
    "MAIL_PROVIDER": {"secret": False, "input_type": "select", "label": "Mailleverantör", "hint": "Aktiv provider för poll/utskick", "options": list(MAIL_PROVIDERS)},
    "MAIL_USERNAME": {"secret": False, "input_type": "text", "label": "Mail-användarnamn", "hint": "Konto/inloggning för mail"},
    "MAIL_PASSWORD": {"secret": True, "input_type": "password", "label": "Mail-lösenord", "hint": "Lösenord / app-lösenord"},
    "MAIL_FROM": {"secret": False, "input_type": "email", "label": "Avsändaradress (From)", "hint": "t.ex. support@azom.se"},
    "SMTP_HOST": {"secret": False, "input_type": "text", "label": "SMTP-värd", "hint": "t.ex. smtp.example.com"},
    "SMTP_PORT": {"secret": False, "input_type": "number", "label": "SMTP-port", "hint": "t.ex. 587 eller 465"},
    "SMTP_PASSWORD": {"secret": True, "input_type": "password", "label": "SMTP-lösenord", "hint": "Om annat än MAIL_PASSWORD"},
    "IMAP_HOST": {"secret": False, "input_type": "text", "label": "IMAP-värd", "hint": "t.ex. imap.example.com"},
    "IMAP_PORT": {"secret": False, "input_type": "number", "label": "IMAP-port", "hint": "vanligen 993"},
    "MAIL_OAUTH_CLIENT_ID": {"secret": False, "input_type": "text", "label": "Gmail OAuth Client ID", "hint": "Google Cloud OAuth-klient"},
    "MAIL_OAUTH_CLIENT_SECRET": {"secret": True, "input_type": "password", "label": "Gmail OAuth Client Secret", "hint": "Hemlig klientnyckel"},
    "MAIL_OAUTH_REDIRECT_URI": {"secret": False, "input_type": "url", "label": "Gmail OAuth Redirect URI", "hint": "https://.../oauth/gmail/callback"},
    "GRAPH_TENANT_ID": {"secret": False, "input_type": "text", "label": "Microsoft Graph Tenant ID", "hint": "Azure AD tenant UUID"},
    "GRAPH_CLIENT_ID": {"secret": False, "input_type": "text", "label": "Microsoft Graph Client ID", "hint": "App registration ID"},
    "GRAPH_CLIENT_SECRET": {"secret": True, "input_type": "password", "label": "Microsoft Graph Client Secret", "hint": "App secret"},
    "GRAPH_USER": {"secret": False, "input_type": "email", "label": "Microsoft Graph-brevlåda", "hint": "Delad/användarens e-post"},
    "SSH_HOST": {"secret": False, "input_type": "text", "label": "SSH-värd", "hint": "Värdnamn eller IP för read-only SSH"},
    "SSH_USER": {"secret": False, "input_type": "text", "label": "SSH-användare", "hint": "Dedikerad icke-root reader"},
    "SSH_PORT": {"secret": False, "input_type": "number", "label": "SSH-port", "hint": "Standard 22"},
    "SSH_PASSWORD": {"secret": True, "input_type": "password", "label": "SSH-lösenord", "hint": "Endast om nyckel inte används"},
    "OPENROUTER_API_KEY": {"secret": True, "input_type": "password", "label": "OpenRouter API-nyckel", "hint": "Fallback LLM-route"},
    "XAI_API_KEY": {"secret": True, "input_type": "password", "label": "xAI API-nyckel", "hint": "Direkt Grok API (valfri)"},
    "TELEGRAM_BOT_TOKEN": {"secret": True, "input_type": "password", "label": "Telegram Bot Token", "hint": "Från @BotFather"},
    "TELEGRAM_ALLOWED_CHAT_IDS": {"secret": False, "input_type": "text", "label": "Tillåtna Telegram-chattar", "hint": "Styrs endast i processmiljön"},
    "MESSENGER_PAGE_ACCESS_TOKEN": {"secret": True, "input_type": "password", "label": "Messenger Page Access Token", "hint": "Meta Page-token"},
    "MESSENGER_APP_SECRET": {"secret": True, "input_type": "password", "label": "Messenger App Secret", "hint": "HMAC-verifiering för webhook"},
    "MESSENGER_VERIFY_TOKEN": {"secret": True, "input_type": "password", "label": "Messenger Verify Token", "hint": "Webhook verify-token"},
    "MESSENGER_ALLOWED_PSIDS": {"secret": False, "input_type": "text", "label": "Tillåtna Messenger-PSID", "hint": "Styrs endast i processmiljön"},
    "MESSENGER_ACTOR_MAP": {"secret": False, "input_type": "text", "label": "Messenger Actor Map", "hint": "Styrs endast i processmiljön"},
    "AZOM_DASHBOARD_PUBLIC_URL": {"secret": False, "input_type": "url", "label": "Publik Dashboard-URL", "hint": "https://... för granskningslänkar"},
    "GOOGLE_OAUTH_CLIENT_ID": {"secret": False, "input_type": "text", "label": "Google Marketing Client ID", "hint": "OAuth-klient för Ads/GA4"},
    "GOOGLE_OAUTH_CLIENT_SECRET": {"secret": True, "input_type": "password", "label": "Google Marketing Client Secret", "hint": "Hemlig OAuth-nyckel"},
    "GOOGLE_OAUTH_REDIRECT_URI": {"secret": False, "input_type": "url", "label": "Google Marketing Redirect URI", "hint": "https://.../oauth/google/callback"},
    "GOOGLE_ADS_DEVELOPER_TOKEN": {"secret": True, "input_type": "password", "label": "Google Ads Developer Token", "hint": "Krävs för live Ads-läsningar"},
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": {"secret": False, "input_type": "text", "label": "Google Ads Login Customer ID", "hint": "MCC-konto (siffror)"},
    "AZOM_GA4_PROPERTY_IDS": {"secret": False, "input_type": "text", "label": "GA4 Property IDs", "hint": "Kommaseparerad lista"},
    "AZOM_GADS_CUSTOMER_IDS": {"secret": False, "input_type": "text", "label": "Google Ads Customer IDs", "hint": "Kommaseparerad lista"},
    "GA4_MEASUREMENT_ID": {"secret": False, "input_type": "text", "label": "GA4 Measurement ID", "hint": "G-XXXXXXXXXX"},
    "GA4_MEASUREMENT_API_SECRET": {"secret": True, "input_type": "password", "label": "GA4 Measurement API Secret", "hint": "Measurement Protocol-hemlighet"},
    "GSC_OAUTH_CLIENT_ID": {"secret": False, "input_type": "text", "label": "GSC OAuth Client ID", "hint": "Isolerad Google Cloud OAuth-klient för Search Console"},
    "GSC_OAUTH_CLIENT_SECRET": {"secret": True, "input_type": "password", "label": "GSC OAuth Client Secret", "hint": "Hemlig OAuth-nyckel för Search Console"},
    "GSC_OAUTH_REDIRECT_URI": {"secret": False, "input_type": "url", "label": "GSC OAuth Redirect URI", "hint": "https://.../oauth/gsc/callback"},
    "AZOM_GSC_PROPERTY_URL_NO": {"secret": False, "input_type": "text", "label": "GSC Property (NO)", "hint": "t.ex. sc-domain:azom.no eller https://azom.no/"},
    "AZOM_GSC_PROPERTY_URL_SE": {"secret": False, "input_type": "text", "label": "GSC Property (SE)", "hint": "t.ex. sc-domain:azom.se eller https://azom.se/"},
    "AZOM_GSC_PROPERTY_URL_DK": {"secret": False, "input_type": "text", "label": "GSC Property (DK)", "hint": "t.ex. sc-domain:azom.dk eller https://azom.dk/"},
    "PARALLEL_API_KEY": {"secret": True, "input_type": "password", "label": "Parallel.ai API-nyckel", "hint": "Webbsök & extrahering (x-api-key)"},
    "EXA_API_KEY": {"secret": True, "input_type": "password", "label": "Exa API-nyckel", "hint": "Semantisk webbsökning (x-api-key)"},
    "BRAVE_SEARCH_API_KEY": {"secret": True, "input_type": "password", "label": "Brave Search API-nyckel", "hint": "Snabbsökning (BSA...)"},
    "TAVILY_API_KEY": {"secret": True, "input_type": "password", "label": "Tavily API-nyckel", "hint": "Sök & innehåll (tvly-...)"},
    "SEARXNG_API_KEY": {"secret": True, "input_type": "password", "label": "SearXNG API-nyckel", "hint": "Valfri Bearer-token för SearXNG"},
    "DASHBOARD_PASSWORD": {"secret": True, "input_type": "password", "label": "Dashboard-lösenord (Jonatan)", "hint": "Basic Auth-lösenord"},
    "DASHBOARD_OSCAR_PASSWORD": {"secret": True, "input_type": "password", "label": "Dashboard-lösenord (Oscar)", "hint": "Admin Basic Auth-lösenord"},
}

_SECRET_GROUPS: list[tuple[str, list[str]]] = [
    ("WooCommerce", ["WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET", "WOO_WEBHOOK_SECRET"]),
    ("WordPress", ["WP_USERNAME", "WP_APP_PASSWORD"]),
    ("Mail", ["MAIL_PROVIDER", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_PASSWORD", "IMAP_HOST", "IMAP_PORT"]),
    ("Mail OAuth", ["MAIL_OAUTH_CLIENT_ID", "MAIL_OAUTH_CLIENT_SECRET", "MAIL_OAUTH_REDIRECT_URI"]),
    ("Graph", ["GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER"]),
    ("SSH", ["SSH_HOST", "SSH_USER", "SSH_PORT", "SSH_PASSWORD"]),
    ("LLM", ["OPENROUTER_API_KEY", "XAI_API_KEY"]),
    ("Telegram", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS"]),
    ("Messenger", ["MESSENGER_PAGE_ACCESS_TOKEN", "MESSENGER_APP_SECRET", "MESSENGER_VERIFY_TOKEN", "MESSENGER_ALLOWED_PSIDS", "MESSENGER_ACTOR_MAP", "AZOM_DASHBOARD_PUBLIC_URL"]),
    ("Google Marketing", ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI", "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "AZOM_GA4_PROPERTY_IDS", "AZOM_GADS_CUSTOMER_IDS", "GA4_MEASUREMENT_ID", "GA4_MEASUREMENT_API_SECRET"]),
    ("Google Search Console", ["GSC_OAUTH_CLIENT_ID", "GSC_OAUTH_CLIENT_SECRET", "GSC_OAUTH_REDIRECT_URI", "AZOM_GSC_PROPERTY_URL_NO", "AZOM_GSC_PROPERTY_URL_SE", "AZOM_GSC_PROPERTY_URL_DK"]),
    ("Web Search & Research", ["PARALLEL_API_KEY", "EXA_API_KEY", "BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "SEARXNG_API_KEY"]),
    ("Dashboard", ["DASHBOARD_PASSWORD", "DASHBOARD_OSCAR_PASSWORD"]),
]


def _secret_item(group: str, key: str) -> dict[str, Any]:
    meta = _SECRET_FIELD_META.get(key, {})
    is_secret = bool(meta.get("secret", True))
    return {
        "group": group,
        "name": key,
        "present": bool(os.environ.get(key, "").strip()),
        "editable": key in EDITABLE_SECRET_KEYS,
        "secret": is_secret,
        "input_type": str(meta.get("input_type") or ("password" if is_secret else "text")),
        "label": str(meta.get("label") or key),
        "hint": str(meta.get("hint") or ""),
        "options": list(meta.get("options") or []),
    }


def secrets_status() -> list[dict[str, Any]]:
    apply_env_overlays()
    out: list[dict[str, Any]] = []
    for group, keys in _SECRET_GROUPS:
        for key in keys:
            out.append(_secret_item(group, key))
    return out


def editable_secret_groups() -> list[dict[str, Any]]:
    """Return all EDITABLE_SECRET_KEYS grouped with rich field metadata."""
    apply_env_overlays()
    groups: list[dict[str, Any]] = []
    for group, keys in _SECRET_GROUPS:
        items = [_secret_item(group, key) for key in keys if key in EDITABLE_SECRET_KEYS]
        if items:
            groups.append({"group": group, "items": items})
    return groups


def resolve_escalation(ticket_id: str, *, resolved_by: str | None = None) -> bool:
    """Mark escalation ticket resolved atomically. Returns True if found."""
    path = _data_dir() / "escalations.jsonl"
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    now = datetime.now(UTC).isoformat()

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
            obj["resolved_at"] = now
            if resolved_by:
                obj["resolved_by"] = resolved_by
            found = True
        out.append(json.dumps(obj, ensure_ascii=False))
    if found:
        _atomic_write_0600(path, "\n".join(out) + ("\n" if out else ""))
    return found


ALLOW_RESEARCH_RUNTIME_KEYS = frozenset(
    {
        "AZOM_SEARCH_ROUTE_ORDER",
        "AZOM_RESEARCH_DEFAULT_COUNTRY",
        "AZOM_RESEARCH_DEFAULT_LANG",
    }
)
_VALID_RESEARCH_ROUTES = frozenset({"parallel", "exa", "brave", "tavily", "searxng"})
_RESEARCH_PROVIDER_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("parallel", "PARALLEL_API_KEY"),
    ("exa", "EXA_API_KEY"),
    ("brave", "BRAVE_SEARCH_API_KEY"),
    ("tavily", "TAVILY_API_KEY"),
    ("searxng", "SEARXNG_BASE_URL"),
)


def _safe_int_setting(raw: Any, default: int) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float_setting(raw: Any, default: float) -> float:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if math.isfinite(val) else default


def _parse_int_range(raw: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{label} måste vara ett heltal ({minimum}–{maximum})")
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise ValueError(f"{label} måste vara ett heltal ({minimum}–{maximum})")
    try:
        val = int(text)
    except ValueError:
        raise ValueError(f"{label} måste vara ett heltal ({minimum}–{maximum})") from None
    if val < minimum or val > maximum:
        raise ValueError(f"{label} måste vara mellan {minimum} och {maximum}")
    return val


def _parse_float_range(raw: Any, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{label} måste vara ett tal ({minimum}–{maximum})")
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise ValueError(f"{label} måste vara ett tal ({minimum}–{maximum})")
    try:
        val = float(text)
    except ValueError:
        raise ValueError(f"{label} måste vara ett tal ({minimum}–{maximum})") from None
    if not math.isfinite(val) or val < minimum or val > maximum:
        raise ValueError(f"{label} måste vara mellan {minimum} och {maximum}")
    return val


def load_research_settings_view() -> dict[str, Any]:
    """Load non-secret research configuration, provider readiness, and read-only server gates."""
    from ecom_ops.research.config import (
        is_mock_mode,
        kill_switch_active,
        research_enabled,
        web_fetch_enabled,
        web_search_enabled,
    )

    apply_env_overlays()
    raw = _load_yaml("research.yaml")
    budgets = raw.get("budgets") if isinstance(raw.get("budgets"), dict) else {}
    cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}

    route_order = str(os.environ.get("AZOM_SEARCH_ROUTE_ORDER", "") or "").strip()
    default_country = str(os.environ.get("AZOM_RESEARCH_DEFAULT_COUNTRY", "SE") or "SE").strip().upper()
    if not default_country:
        default_country = "SE"
    default_lang = str(os.environ.get("AZOM_RESEARCH_DEFAULT_LANG", "sv") or "sv").strip().lower()
    if not default_lang:
        default_lang = "sv"

    provider_keys_present = {
        rid: bool(os.environ.get(env_key, "").strip())
        for rid, env_key in _RESEARCH_PROVIDER_ENV_KEYS
    }

    return {
        "route_order": route_order,
        "default_country": default_country,
        "default_lang": default_lang,
        "max_queries_per_day": _safe_int_setting(budgets.get("max_queries_per_day"), 200),
        "max_fetches_per_day": _safe_int_setting(budgets.get("max_fetches_per_day"), 300),
        "monthly_external_usd_cap": _safe_float_setting(
            budgets.get("monthly_external_usd_cap"), 25.0
        ),
        "cache_enabled": _as_bool(cache.get("enabled"), default=True),
        "provider_keys_present": provider_keys_present,
        "live_gates": {
            "mock_mode": is_mock_mode(),
            "web_search_enabled": web_search_enabled(),
            "web_fetch_enabled": web_fetch_enabled(),
            "research_enabled": research_enabled(),
            "kill_search": kill_switch_active("search"),
            "kill_research": kill_switch_active("research"),
        },
    }


def save_research_settings(form_data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and save allowlisted non-secret research runtime overlays and YAML budgets/cache."""
    from ecom_ops.research.config import clear_research_config_cache
    from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS

    for raw_k, raw_v in form_data.items():
        _reject_control_chars("nyckel", str(raw_k))
        if raw_v is not None:
            _reject_control_chars(str(raw_k), str(raw_v))

    env_updates: dict[str, str] = {}
    cleared_route_order = False

    route_key = (
        "AZOM_SEARCH_ROUTE_ORDER"
        if "AZOM_SEARCH_ROUTE_ORDER" in form_data
        else ("route_order" if "route_order" in form_data else None)
    )
    if route_key is not None:
        raw_order = str(form_data.get(route_key) or "").strip()
        if raw_order == "":
            env_updates["AZOM_SEARCH_ROUTE_ORDER"] = ""
            cleared_route_order = True
        else:
            parts = [p.strip().lower() for p in raw_order.split(",") if p.strip()]
            invalid = [p for p in parts if p not in _VALID_RESEARCH_ROUTES]
            if invalid:
                raise ValueError(f"Ogiltig sökmotor i motorordning: {', '.join(invalid)}")
            deduped = list(dict.fromkeys(parts))
            if deduped:
                env_updates["AZOM_SEARCH_ROUTE_ORDER"] = ",".join(deduped)
            else:
                env_updates["AZOM_SEARCH_ROUTE_ORDER"] = ""
                cleared_route_order = True

    country_key = (
        "AZOM_RESEARCH_DEFAULT_COUNTRY"
        if "AZOM_RESEARCH_DEFAULT_COUNTRY" in form_data
        else ("default_country" if "default_country" in form_data else None)
    )
    if country_key is not None:
        country = str(form_data.get(country_key) or "").strip().upper()
        if len(country) != 2 or not (country.isascii() and country.isalpha()):
            raise ValueError("Ogiltig landskod (ange två bokstäver, t.ex. SE, NO, DK, GB eller US)")
        env_updates["AZOM_RESEARCH_DEFAULT_COUNTRY"] = country

    lang_key = (
        "AZOM_RESEARCH_DEFAULT_LANG"
        if "AZOM_RESEARCH_DEFAULT_LANG" in form_data
        else ("default_lang" if "default_lang" in form_data else None)
    )
    if lang_key is not None:
        lang = str(form_data.get(lang_key) or "").strip().lower()
        if len(lang) != 2 or not (lang.isascii() and lang.isalpha()):
            raise ValueError("Ogiltig språkkod (ange två bokstäver, t.ex. sv, nb, da eller en)")
        env_updates["AZOM_RESEARCH_DEFAULT_LANG"] = lang

    research_yaml = _load_yaml("research.yaml")
    budgets = dict(research_yaml.get("budgets") or {}) if isinstance(research_yaml.get("budgets"), dict) else {}
    cache = dict(research_yaml.get("cache") or {}) if isinstance(research_yaml.get("cache"), dict) else {}
    yaml_dirty = False

    if "max_queries_per_day" in form_data:
        budgets["max_queries_per_day"] = _parse_int_range(
            form_data.get("max_queries_per_day"),
            "max_queries_per_day",
            minimum=1,
            maximum=5000,
        )
        yaml_dirty = True

    if "max_fetches_per_day" in form_data:
        budgets["max_fetches_per_day"] = _parse_int_range(
            form_data.get("max_fetches_per_day"),
            "max_fetches_per_day",
            minimum=0,
            maximum=5000,
        )
        yaml_dirty = True

    if "monthly_external_usd_cap" in form_data:
        budgets["monthly_external_usd_cap"] = _parse_float_range(
            form_data.get("monthly_external_usd_cap"),
            "monthly_external_usd_cap",
            minimum=0.0,
            maximum=500.0,
        )
        yaml_dirty = True

    if "cache_enabled" in form_data:
        cache["enabled"] = _as_bool(form_data.get("cache_enabled"), default=True)
        yaml_dirty = True

    if yaml_dirty:
        research_yaml["budgets"] = budgets
        research_yaml["cache"] = cache
        _save_yaml("research.yaml", research_yaml)

    allow_keys = frozenset(k for k in ALLOW_RESEARCH_RUNTIME_KEYS if k not in PROTECTED_OVERLAY_KEYS)
    if env_updates:
        _write_env_file(
            _data_dir() / "runtime.env",
            env_updates,
            allow_keys=allow_keys,
        )

    clear_research_config_cache()
    view = load_research_settings_view()
    if cleared_route_order:
        os.environ.pop("AZOM_SEARCH_ROUTE_ORDER", None)
        view["route_order"] = ""
    return view
