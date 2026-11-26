"""WordPress site health probes, failure streaks, incident deduplication, and summaries."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_ops.wp_ops.backup import BackupService
from ecom_ops.wp_ops.connectors import (
    MockRest,
    MockUptime,
    ProbeOutcome,
    RestConnector,
    UptimeConnector,
)
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.models import Site, WpOpsResult, clean_error_class, ensure_utc, tone_for
from ecom_ops.wp_ops.policy import WpOpsPolicy
from ecom_ops.wp_ops.store import WpOpsStore

_SIGNAL_CHECKS = frozenset({"http_uptime", "tls", "dns", "wp_rest", "inventory"})


class HealthChecker:
    """Run mock/local site probes, update streaks & incidents, and build site summaries."""

    def __init__(
        self,
        *,
        store: WpOpsStore,
        policy: WpOpsPolicy,
        backups: BackupService,
        uptime: UptimeConnector | None = None,
        rest: RestConnector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.backups = backups
        self.uptime = uptime or MockUptime()
        self.rest = rest or MockRest()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return ensure_utc(self.clock())

    def safe_state(self) -> dict[str, Any] | WpOpsResult:
        try:
            return self.store.load()
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "live_calls": False},
            )

    def run_checks(self, site_id: str, *, actor: str, scheduled: bool = False) -> WpOpsResult:
        site, err = self.policy.resolve_site(
            site_id,
            actor=actor,
            access="write",
            action="wp_ops_check",
        )
        if err is not None:
            return err
        assert site is not None
        permission = self.policy.write_permission_for(site)
        with self.policy.try_site_lock(
            site,
            actor=actor,
            action="wp_ops_check",
            operation="health_check",
            permission=permission,
        ) as busy:
            if busy is not None:
                return busy
            return self._run_locked(site, actor=actor, scheduled=scheduled, permission=permission.value)

    def run_due(self, *, actor: str) -> WpOpsResult:
        sites, config_err = self.policy.load_all_sites()
        if config_err is not None:
            return config_err
        state = self.safe_state()
        if isinstance(state, WpOpsResult):
            return state
        now = self._now()
        ran: list[str] = []
        skipped: list[dict[str, str]] = []
        for site in sites:
            last_raw = state.get("last_run", {}).get(site.id)
            if last_raw:
                last = ensure_utc(datetime.fromisoformat(str(last_raw)))
                if now - last < timedelta(seconds=site.check_interval_seconds):
                    skipped.append({"site_id": site.id, "error_class": "NotDue"})
                    continue
            result = self.run_checks(site.id, actor=actor, scheduled=True)
            error_class = str((result.data or {}).get("error_class") or "")
            if result.ok:
                ran.append(site.id)
            else:
                skipped.append({"site_id": site.id, "error_class": error_class or "CheckFailed"})
        denied_all = bool(skipped) and not ran and all(
            row["error_class"] == "AccessDenied" for row in skipped
        )
        return WpOpsResult(
            ok=not denied_all,
            message="due checks",
            data={"ran": ran, "skipped": skipped, "live_calls": False},
        )

    def _run_locked(self, site: Site, *, actor: str, scheduled: bool, permission: str) -> WpOpsResult:
        now = self._now()
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        try:
            state = self.store.load()
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        if not scheduled and self._is_duplicate(state, site, now):
            summary = self.summary(site, state)
            return WpOpsResult(
                ok=True,
                message="duplicate check",
                data={
                    "site": summary,
                    "checks": self.checks_for(state, site.id),
                    "duplicate": True,
                    "executed": False,
                    "live_calls": False,
                    "partial": False,
                    "healthy": summary["healthy"],
                    "down": summary["down"],
                },
            )
        records, partial = self._collect(site, credential_present=bound.present, now=now)
        records = scrub_secrets(records, secrets)
        assert isinstance(records, list)

        def mutate(current: dict[str, Any]) -> None:
            self._apply(current, site, records, now=now, scheduled=scheduled)

        try:
            updated = self.store.update(mutate)
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        summary = self.summary(site, updated)
        audit_err = self.policy.audit(
            actor,
            action="wp_ops_check",
            target_id=site.id,
            success=True,
            details={
                "environment": site.environment,
                "permission": permission,
                "operation": "health_check",
                "plan_id": None,
                "error_class": None,
                "result": "fail" if summary["down"] or not summary["healthy"] else "ok",
                "partial": partial,
            },
        )
        if audit_err is not None:
            return audit_err
        return WpOpsResult(
            ok=True,
            message="checks finished",
            data={
                "site": summary,
                "checks": self.checks_for(updated, site.id),
                "duplicate": False,
                "executed": True,
                "live_calls": False,
                "partial": partial,
                "healthy": summary["healthy"],
                "down": summary["down"],
            },
        )

    def _collect(
        self, site: Site, *, credential_present: bool, now: datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        partial = False
        for url in site.probe_urls():
            outcome, failed, duration_ms = self._call_uptime(url, timeout=site.timeout_seconds)
            partial = partial or failed
            rows.append(
                self._row(
                    site, "http_uptime", url, outcome.result, outcome.error_class, now, duration_ms
                )
            )
            rows.extend(self._tls_dns(site, url, outcome, now, duration_ms))
        rest_outcome, rest_failed, rest_ms = self._call_rest(
            site, credential_present=credential_present
        )
        partial = partial or rest_failed
        rows.append(
            self._row(
                site,
                "wp_rest",
                site.probe_urls()[0],
                rest_outcome.result,
                rest_outcome.error_class,
                now,
                rest_ms,
            )
        )
        if site.wordpress_version and site.php_version:
            inventory = ProbeOutcome(result="ok")
        else:
            inventory = ProbeOutcome(result="unknown", error_class="InventoryIncomplete")
        rows.append(self._row(site, "inventory", site.id, inventory.result, inventory.error_class, now, 0))
        rows.append(self._row(site, "backup", site.id, "unknown", "NotImplemented", now, 0))
        rows.append(self._row(site, "checksum", site.id, "unknown", "NotImplemented", now, 0))
        return rows, partial

    def _call_uptime(self, url: str, *, timeout: float) -> tuple[ProbeOutcome, bool, int]:
        started = time.perf_counter()
        try:
            outcome = self.uptime.probe(url, timeout=timeout)
            return outcome, False, self._duration_ms(started)
        except Exception:
            return ProbeOutcome(result="fail", error_class="CheckFailed"), True, self._duration_ms(started)

    def _call_rest(self, site: Site, *, credential_present: bool) -> tuple[ProbeOutcome, bool, int]:
        started = time.perf_counter()
        try:
            outcome = self.rest.probe(
                site, credential_present=credential_present, timeout=site.timeout_seconds
            )
            return outcome, False, self._duration_ms(started)
        except Exception:
            return ProbeOutcome(result="fail", error_class="CheckFailed"), True, self._duration_ms(started)

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    def _tls_dns(
        self, site: Site, url: str, outcome: ProbeOutcome, now: datetime, duration_ms: int
    ) -> list[dict[str, Any]]:
        if outcome.tls_days is None:
            tls = self._row(site, "tls", url, "unknown", "TlsUnavailable", now, duration_ms)
        elif outcome.tls_days <= 14:
            tls = self._row(site, "tls", url, "fail", "CertificateExpiring", now, duration_ms)
        else:
            tls = self._row(site, "tls", url, "ok", None, now, duration_ms)
        if outcome.dns_ok is True:
            dns = self._row(site, "dns", url, "ok", None, now, duration_ms)
        elif outcome.dns_ok is False:
            dns = self._row(site, "dns", url, "fail", "DnsFailed", now, duration_ms)
        else:
            dns = self._row(site, "dns", url, "unknown", "DnsUnavailable", now, duration_ms)
        return [tls, dns]

    @staticmethod
    def _row(
        site: Site,
        check: str,
        target: str,
        result: str,
        error_class: str | None,
        now: datetime,
        duration_ms: int,
    ) -> dict[str, Any]:
        if result not in {"ok", "fail", "unknown"}:
            result = "unknown"
            error_class = "CheckFailed"
        clean = None if result == "ok" else clean_error_class(error_class or "CheckFailed")
        return {
            "site_id": site.id,
            "environment": site.environment,
            "check": check,
            "target": target,
            "result": result,
            "tone": tone_for(result),
            "green": result == "ok",
            "duration_ms": duration_ms,
            "timeout_seconds": site.timeout_seconds,
            "error_class": clean,
            "checked_at": now.isoformat(),
        }

    def _apply(
        self,
        state: dict[str, Any],
        site: Site,
        records: list[dict[str, Any]],
        *,
        now: datetime,
        scheduled: bool,
    ) -> None:
        checks: dict[str, Any] = state.setdefault("checks", {})
        streaks: dict[str, int] = state.setdefault("streaks", {})
        incidents: list[dict[str, Any]] = state.setdefault("incidents", [])
        for row in records:
            key = f"{site.id}|{row['check']}|{row['target']}"
            checks[key] = row
            if row["check"] not in _SIGNAL_CHECKS:
                continue
            if row["result"] == "unknown":
                continue
            if row["result"] == "ok":
                streaks[key] = 0
                self._recover(incidents, site.id, row["check"], row["target"], now)
                continue
            streaks[key] = int(streaks.get(key) or 0) + 1
            if streaks[key] >= site.alert_confirmations:
                self._open_or_update(incidents, site, row, streaks[key], now)
        state.setdefault("last_run", {})[site.id] = now.isoformat()
        if not scheduled:
            state.setdefault("on_demand", {})[site.id] = now.isoformat()

    @staticmethod
    def _open_or_update(
        incidents: list[dict[str, Any]],
        site: Site,
        row: dict[str, Any],
        streak: int,
        now: datetime,
    ) -> None:
        for incident in incidents:
            if (
                incident.get("state") == "open"
                and incident.get("site_id") == site.id
                and incident.get("check") == row["check"]
                and incident.get("target") == row["target"]
            ):
                incident["failure_count"] = streak
                incident["updated_at"] = now.isoformat()
                incident["error_class"] = row.get("error_class")
                return
        incidents.append(
            {
                "id": str(uuid.uuid4()),
                "site_id": site.id,
                "environment": site.environment,
                "check": row["check"],
                "target": row["target"],
                "state": "open",
                "opened_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "recovered_at": None,
                "failure_count": streak,
                "owner": site.alert_owner,
                "error_class": row.get("error_class"),
            }
        )

    @staticmethod
    def _recover(
        incidents: list[dict[str, Any]],
        site_id: str,
        check: str,
        target: str,
        now: datetime,
    ) -> None:
        for incident in incidents:
            if (
                incident.get("state") == "open"
                and incident.get("site_id") == site_id
                and incident.get("check") == check
                and incident.get("target") == target
            ):
                incident["state"] = "recovered"
                incident["recovered_at"] = now.isoformat()
                incident["updated_at"] = now.isoformat()

    def summary(self, site: Site, state: dict[str, Any]) -> dict[str, Any]:
        present = bind_credential(site).present
        body = site.public_dict(credential_present=present)
        checks = self.checks_for(state, site.id)
        catalog = self.backups.catalog_backup_view(site, state)
        backup = catalog or next((row for row in checks if row["check"] == "backup"), None)
        if backup is None:
            backup = {
                "check": "backup",
                "result": "unknown",
                "tone": "attention",
                "green": False,
                "error_class": "NotRun",
            }
        incidents = [row for row in self.incidents_for(state, site.id) if row.get("state") == "open"]
        last = state.get("last_run", {}).get(site.id)
        required = [row for row in checks if row["check"] in {"http_uptime", "wp_rest", "backup", "checksum"}]
        healthy = bool(required) and all(row.get("result") == "ok" for row in required)
        body.update(
            {
                "healthy": healthy,
                "down": any(row.get("check") == "http_uptime" for row in incidents),
                "open_incidents": len(incidents),
                "last_check": last,
                "backup": {
                    "result": backup["result"],
                    "tone": "attention" if backup["result"] != "ok" else backup["tone"],
                    "green": False,
                    "error_class": backup.get("error_class"),
                    "offsite": False,
                    "encryption": backup.get("encryption") or "unconfigured",
                    "provider": backup.get("provider") or "mock",
                },
                "live_calls": False,
            }
        )
        return body

    @staticmethod
    def checks_for(state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in state.get("checks", {}).values()
            if isinstance(row, dict) and row.get("site_id") == site_id
        ]

    @staticmethod
    def incidents_for(state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in state.get("incidents", [])
            if isinstance(row, dict) and row.get("site_id") == site_id
        ]

    @staticmethod
    def staging_targets(sites: tuple[Site, ...] | list[Site]) -> list[dict[str, str]]:
        return [
            {"id": site.id, "display_name": site.display_name}
            for site in sites
            if site.environment == "staging"
        ]

    @staticmethod
    def _is_duplicate(state: dict[str, Any], site: Site, now: datetime) -> bool:
        raw = state.get("on_demand", {}).get(site.id)
        if not raw:
            return False
        previous = ensure_utc(datetime.fromisoformat(str(raw)))
        return now - previous < timedelta(seconds=site.dedupe_window_seconds)
