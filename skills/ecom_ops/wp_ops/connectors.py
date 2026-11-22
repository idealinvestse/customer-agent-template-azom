"""Typed probes for WordPress operations.

Phase 1 ships in-process mocks only. There is no HTTP client, shell, or
WP-CLI adapter in this package. A later phase can add a typed adapter
behind these methods without giving the model a command string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ecom_ops.wp_ops.models import Site


@dataclass(frozen=True)
class ProbeOutcome:
    result: str
    error_class: str | None = None
    tls_days: int | None = None
    dns_ok: bool | None = None


class UptimeConnector(Protocol):
    def probe(self, url: str, *, timeout: float) -> ProbeOutcome:
        """Check one URL without WordPress credentials."""


class RestConnector(Protocol):
    def probe(self, site: Site, *, credential_present: bool, timeout: float) -> ProbeOutcome:
        """Auth reachability only. Do not return customer content."""


class MockUptime:
    """Synthetic uptime for ``*.example.test``. Other hosts stay unknown."""

    def probe(self, url: str, *, timeout: float) -> ProbeOutcome:
        if timeout <= 0 or timeout > 30:
            return ProbeOutcome(result="unknown", error_class="TimeoutConfig")
        host = url.split("://", 1)[-1].split("/", 1)[0].lower()
        if host.startswith("down.") or ".down." in host:
            return ProbeOutcome(result="fail", error_class="ConnectError", dns_ok=True)
        if host.endswith(".example.test"):
            return ProbeOutcome(result="ok", tls_days=40, dns_ok=True)
        return ProbeOutcome(result="unknown", error_class="UptimeProviderUnconfigured")


class MockRest:
    """Auth probe from the credential-present flag. No post or page body."""

    def probe(self, site: Site, *, credential_present: bool, timeout: float) -> ProbeOutcome:
        del site
        if timeout <= 0 or timeout > 30:
            return ProbeOutcome(result="unknown", error_class="TimeoutConfig")
        if not credential_present:
            return ProbeOutcome(result="fail", error_class="CredentialMissing")
        return ProbeOutcome(result="ok")


class ScriptedUptime:
    """Test double that repeats one outcome and records each call."""

    def __init__(self, outcome: ProbeOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, float]] = []

    def probe(self, url: str, *, timeout: float) -> ProbeOutcome:
        self.calls.append((url, timeout))
        if timeout <= 0 or timeout > 30:
            return ProbeOutcome(result="unknown", error_class="TimeoutConfig")
        return self.outcome


class ScriptedRest:
    def __init__(self, outcome: ProbeOutcome | None = None, exc: Exception | None = None) -> None:
        self.outcome = outcome or ProbeOutcome(result="ok")
        self.exc = exc
        self.saw_secret_value = False

    def probe(self, site: Site, *, credential_present: bool, timeout: float) -> ProbeOutcome:
        del site, timeout
        if self.exc is not None:
            raise self.exc
        if not credential_present:
            return ProbeOutcome(result="fail", error_class="CredentialMissing")
        return self.outcome
