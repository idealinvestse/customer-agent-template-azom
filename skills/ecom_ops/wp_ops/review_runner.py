"""Sandbox gate for a review plan.

Flag off records a fake lifecycle. Flag on still refuses on this machine.
This module does not import a process runner or a container client.
"""

from __future__ import annotations

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


def empty_record(plan_id: str) -> dict:
    env = synthetic_env()
    if FORBIDDEN_ENV_NAMES.intersection(env):
        raise ReviewError("SandboxSecretRefused")
    return {
        "engine": "fake",
        "database": "synthetic",
        "network": "none",
        "env_names": sorted(env),
        "state": "stopped",
        "port": None,
        "volume": None,
        "container": "stopped",
        "payments_enabled": False,
        "checkout_enabled": False,
        "admin_password_stored": False,
        "plan_id": plan_id,
    }


class SandboxGate:
    """Records create, stop, resume, and destroy. It never binds a port."""

    def start(self, site: object, plan_id: str, *, high_risk: bool) -> dict:
        if sandbox_enabled():
            if versions_unknown(site):
                raise ReviewError("VersionsUnknown")
            raise ReviewError("SandboxRuntimeUnavailable")
        record = empty_record(plan_id)
        # Flag off does not start a container. A later restart only stops
        # rows that a real runtime had already marked created or running.
        record["engine"] = "unavailable"
        record["network"] = "not_run"
        record["state"] = "not_run"
        record["volume"] = None
        record["container"] = "unavailable"
        record["availability"] = "unavailable"
        record["payments_enabled"] = False
        record["checkout_enabled"] = False
        del high_risk
        return record

    def stop(self, record: dict) -> dict:
        stopped = dict(record)
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
        destroyed["state"] = "destroyed"
        destroyed["volume"] = None
        return destroyed
