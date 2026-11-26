"""Honest sandbox availability gate for a review plan.

This module does not import a process runner or container client. By default no
runtime is wired, so ``SandboxGate.start`` stays fail-closed. An explicit,
opt-in local runtime starter may be supplied only when ``AZOM_WP_SANDBOX=1``,
and ``state`` becomes ``running`` only after a real loopback process is verified
alive without published ports, Docker sockets, broad host mounts, or host
secrets.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.review_workspace import ReviewError

FORBIDDEN_ENV_NAMES = frozenset(
    {
        "WP_APP_PASSWORD",
        "WOO_CONSUMER_KEY",
        "WOO_CONSUMER_SECRET",
        "WOO_WEBHOOK_SECRET",
        "MAIL_PASSWORD",
        "SMTP_PASSWORD",
        "SMTP_USER",
        "GMAIL_PASSWORD",
        "STRIPE_SECRET_KEY",
        "PAYPAL_CLIENT_SECRET",
        "PAYMENT_SECRET",
    }
)
_ALLOWED_LOCAL_ENGINES = frozenset({"local_php_wp"})
_REQUIRED_SAFE_FALSE = (
    "published_port",
    "docker_socket",
    "broad_host_mount",
    "payments_enabled",
    "checkout_enabled",
    "admin_password_stored",
)


def sandbox_enabled() -> bool:
    return env_flag("AZOM_WP_SANDBOX")


def synthetic_env() -> dict[str, str]:
    """Return a new mapping. This is not a copy of the process environment."""
    return {"AZOM_REVIEW_SYNTHETIC": "1"}


def versions_unknown(site: object) -> bool:
    def missing(value: object) -> bool:
        text = str(value or "").strip().lower()
        return text in {"", "unknown", "unconfigured"}

    if missing(getattr(site, "php_version", "")) or missing(getattr(site, "wordpress_version", "")):
        return True
    if getattr(site, "woocommerce", False) and missing(getattr(site, "woo_version", "")):
        return True
    return False


def public_sandbox_record(
    plan_id: str,
    *,
    engine: str = "unavailable",
    database: str = "not_created",
    network: str = "not_run",
    state: str = "unavailable",
    container: str = "unavailable",
    env_names: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a canonical fail-closed sandbox record with safe boolean defaults."""
    record: dict[str, Any] = {
        "engine": engine,
        "database": database,
        "network": network,
        "env_names": sorted(synthetic_env()) if env_names is None else list(env_names),
        "state": state,
        "port": None,
        "volume": None,
        "container": container,
        "payments_enabled": False,
        "checkout_enabled": False,
        "admin_password_stored": False,
        "published_port": False,
        "docker_socket": False,
        "broad_host_mount": False,
        "plan_id": plan_id,
    }
    record.update(extra)
    return record


def unavailable_destroyed_record(plan_id: str) -> dict[str, Any]:
    """Build the canonical destroyed/unavailable sandbox record for expired or cleaned plans."""
    return public_sandbox_record(plan_id, state="destroyed", availability="unavailable")


def empty_record(plan_id: str) -> dict:
    env = synthetic_env()
    if FORBIDDEN_ENV_NAMES.intersection(env):
        raise ReviewError("SandboxSecretRefused")
    return public_sandbox_record(plan_id, env_names=sorted(env))


def _validate_live_record(plan_id: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewError("SandboxRuntimeUnavailable")
    raw_env = raw.get("env_names")
    if not isinstance(raw_env, (list, tuple, set)):
        raise ReviewError("SandboxSecretRefused")
    env_names = sorted({str(name) for name in raw_env if str(name)})
    if not env_names or FORBIDDEN_ENV_NAMES.intersection(env_names):
        raise ReviewError("SandboxSecretRefused")
    if any(
        name.upper().startswith(
            ("WP_SECRET_", "WOO_", "MAIL_", "SMTP_", "OPENROUTER_", "TELEGRAM_", "MESSENGER_")
        )
        for name in env_names
    ):
        raise ReviewError("SandboxSecretRefused")
    engine = str(raw.get("engine") or "")
    if engine not in _ALLOWED_LOCAL_ENGINES:
        raise ReviewError("SandboxRuntimeUnavailable")
    if raw.get("state") != "running" or raw.get("process_alive") is not True:
        raise ReviewError("SandboxRuntimeUnavailable")
    if raw.get("bind_host") != "127.0.0.1":
        raise ReviewError("SandboxRuntimeUnavailable")
    pid = raw.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ReviewError("SandboxRuntimeUnavailable")
    for flag in _REQUIRED_SAFE_FALSE:
        if raw.get(flag) is not False:
            raise ReviewError("SandboxRuntimeUnavailable")
    return public_sandbox_record(
        plan_id,
        engine=engine,
        database=str(raw.get("database") or "synthetic_sqlite"),
        network="loopback_only",
        env_names=env_names,
        state="running",
        container="local_process",
        pid=pid,
    )


class SandboxGate:
    """Records create, stop, resume, and destroy. Never publishes a port."""

    def __init__(
        self,
        *,
        runtime_starter: Callable[..., dict[str, Any]] | None = None,
        runtime_stopper: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._runtime_starter = runtime_starter
        self._runtime_stopper = runtime_stopper

    def start(self, site: object, plan_id: str, *, high_risk: bool) -> dict:
        if sandbox_enabled():
            if versions_unknown(site):
                raise ReviewError("VersionsUnknown")
            if self._runtime_starter is None:
                raise ReviewError("SandboxRuntimeUnavailable")
            raw = self._runtime_starter(site, plan_id, high_risk=high_risk)
            return _validate_live_record(plan_id, raw)
        record = empty_record(plan_id)
        # Flag off does not start a container. A later restart only stops
        # rows that a real runtime had already marked created or running.
        record["availability"] = "unavailable"
        del high_risk
        return record

    def stop(self, record: dict) -> dict:
        stopped = dict(record)
        if stopped.get("engine") == "unavailable" or stopped.get("state") in {"unavailable", "not_run"}:
            stopped["engine"] = "unavailable"
            stopped["state"] = "unavailable"
            stopped["container"] = "unavailable"
            stopped["port"] = None
            return stopped
        if self._runtime_stopper is not None and stopped.get("state") == "running":
            self._runtime_stopper(stopped)
        stopped["state"] = "stopped"
        stopped["container"] = "stopped"
        stopped["port"] = None
        return stopped

    def resume(self, site: object, record: dict, *, expired: bool, high_risk: bool) -> dict:
        if expired:
            raise ReviewError("Expired")
        return self.start(site, str(record.get("plan_id") or ""), high_risk=high_risk)

    def destroy(self, record: dict) -> dict:
        destroyed = self.stop(record)
        if destroyed.get("engine") == "unavailable":
            return destroyed
        destroyed["state"] = "destroyed"
        destroyed["volume"] = None
        return destroyed


__all__ = [
    "FORBIDDEN_ENV_NAMES",
    "SandboxGate",
    "empty_record",
    "public_sandbox_record",
    "sandbox_enabled",
    "synthetic_env",
    "unavailable_destroyed_record",
    "versions_unknown",
]
