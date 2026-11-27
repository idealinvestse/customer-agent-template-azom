"""ShopIntelService facade: RBAC, fail-closed live gates, collection, drift, storage, and schedules."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Actor, Permission, resolve_actor
from ecom_ops.shop_intel.analyzer import analyze_shop_signals, render_dossier_markdown
from ecom_ops.shop_intel.collectors import collect_shop_signals
from ecom_ops.shop_intel.config import (
    is_mock_mode,
    load_shop_intel_config,
    shop_intel_kill_active,
    shop_intel_live_enabled,
)
from ecom_ops.shop_intel.context_builder import build_tailored_context
from ecom_ops.shop_intel.models import (
    SHOP_INTEL_RUN_ID_RE,
    VALID_DOMAINS,
    VALID_DOMAINS_SET,
    VALID_MODES,
    VALID_SEVERITIES_SET,
    IntelRun,
    IntelSchedule,
    new_run_id,
)
from ecom_ops.shop_intel.store import ShopIntelStore, scrub_text
from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.registry import load_sites, site_by_id

_SERVICE_LOCK = threading.Lock()
_SERVICES: dict[str, ShopIntelService] = {}

_MODE_DOMAINS: dict[str, tuple[str, ...]] = {
    "full": VALID_DOMAINS,
    "quick": (
        "platform_rules",
        "catalog_pricing",
        "brand_voice_copy",
        "policies_legal",
        "plugins_ecosystem",
        "security_posture",
    ),
    "security": (
        "platform_rules",
        "theme_architecture",
        "policies_legal",
        "plugins_ecosystem",
        "security_posture",
    ),
}


def _require_read_actor(actor: str | Actor) -> Actor:
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    if actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.READ):
        return actor_obj
    raise AccessDenied(
        f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks WP_OPS_READ or READ"
    )


def _require_run_actor(actor: str | Actor) -> Actor:
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    has_read = actor_obj.has(Permission.WP_OPS_READ)
    has_run = (
        actor_obj.has(Permission.RESEARCH_RUN)
        or actor_obj.has(Permission.WP_OPS_OPERATE)
        or actor_obj.has(Permission.WP_OPS_ADMIN)
    )
    if has_read and has_run:
        return actor_obj
    raise AccessDenied(
        f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks Shop Intel run permission"
    )


def _require_schedule_actor(actor: str | Actor) -> Actor:
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    if actor_obj.has(Permission.WP_OPS_ADMIN) or actor_obj.has(Permission.RESEARCH_ADMIN):
        return actor_obj
    raise AccessDenied(
        f"Actor {actor_obj.name!r} (role={actor_obj.role}) lacks WP_OPS_ADMIN or RESEARCH_ADMIN"
    )


class ShopIntelService:
    """Read-only, zero-PII Shop Intelligence facade."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        woo_client_factory: Callable[..., Any] | None = None,
        wp_client_factory: Callable[..., Any] | None = None,
        bridge_client_factory: Callable[[Site], Any] | None = None,
        files_factory: Callable[[Site], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.store = ShopIntelStore(data_dir=self.data_dir)
        self._woo_client_factory = woo_client_factory
        self._wp_client_factory = wp_client_factory
        self._bridge_client_factory = bridge_client_factory
        self._files_factory = files_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def _resolve_site(self, site_id: str | None) -> Site:
        sid = str(site_id or "").strip().lower()
        sites = load_sites(data_dir=self.data_dir, apply_overlay=False)
        found = site_by_id(sites, sid) if sid else None
        if found is not None:
            return found
        if not sid and sites:
            return sites[0]
        if not is_mock_mode():
            if sid:
                raise ValueError(
                    f"Unknown site_id {site_id!r} in live mode; register the site in wp_sites.yaml."
                )
            raise ValueError("No WordPress sites configured in live mode.")
        clean_sid = sid or "mock-shop"
        url = f"https://{clean_sid}.example.test"
        return Site(
            id=clean_sid,
            display_name=clean_sid,
            canonical_url=url,
            environment="staging" if "staging" in clean_sid else "production",
            business_owner="customer",
            hosting_provider="unknown",
            wordpress_version="6.7",
            php_version="8.3",
            woocommerce=True,
            critical_urls=(f"{url}/",),
            maintenance_window="not agreed",
            secret_ref=f"WP_SECRET_{clean_sid.upper().replace('-', '_')}",
            capabilities=(
                ("hosting_api", "unknown"),
                ("wp_cli", "unknown"),
                ("staging", "unknown"),
                ("backup", "unknown"),
                ("restore", "unknown"),
                ("uptime", "unknown"),
                ("security_scanner", "unknown"),
            ),
            alert_confirmations=2,
            check_interval_seconds=300,
            timeout_seconds=10.0,
            dedupe_window_seconds=30,
            retention_days=30,
            alert_owner="oscar",
            files_root="/var/www/mock",
            ssh_host=f"{clean_sid}.example.test",
        )

    def run_learning(
        self,
        site_id: str = "mock-shop",
        *,
        actor: str = "agent",
        mode: str = "full",
        domains: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Run a deterministic, zero-PII Shop Intelligence learning pass."""
        actor_obj = _require_run_actor(actor)
        clean_mode = str(mode or "full").strip().lower()
        if clean_mode not in VALID_MODES:
            raise ValueError(f"Unsupported mode {mode!r}. Allowed: {sorted(VALID_MODES)}")

        cfg = load_shop_intel_config()
        if domains is not None:
            selected: list[str] = []
            for d in domains:
                dom = str(d or "").strip().lower()
                if dom not in VALID_DOMAINS_SET:
                    raise ValueError(f"Unsupported domain {d!r}. Allowed: {VALID_DOMAINS}")
                if dom not in selected:
                    selected.append(dom)
            if not selected:
                raise ValueError("At least one domain must be selected.")
        else:
            selected = [
                d
                for d in _MODE_DOMAINS[clean_mode]
                if cfg.domains.get(d, {}).get("enabled", True)
            ]
            if not selected:
                selected = list(_MODE_DOMAINS[clean_mode])

        if shop_intel_kill_active():
            log_action(
                actor=actor_obj.name,
                action="shop_intel_run",
                target=str(site_id or ""),
                success=False,
                details={"error_class": "ShopIntelKillSwitchActive", "mode": clean_mode},
            )
            return {
                "ok": False,
                "error_class": "ShopIntelKillSwitchActive",
                "message": "Shop Intelligence är stoppad av AZOM_SHOP_INTEL_KILL.",
                "live_calls": False,
            }

        mock = is_mock_mode()
        if not mock and not shop_intel_live_enabled():
            log_action(
                actor=actor_obj.name,
                action="shop_intel_run",
                target=str(site_id or ""),
                success=False,
                details={"error_class": "ShopIntelLiveDisabled", "mode": clean_mode},
            )
            return {
                "ok": False,
                "error_class": "ShopIntelLiveDisabled",
                "message": "Live Shop Intelligence kräver AZOM_SHOP_INTEL_LIVE_ENABLED=1 i processmiljön.",
                "live_calls": False,
            }

        site = self._resolve_site(site_id)
        started = time.monotonic()
        run_id = new_run_id()
        provenance = "mock" if mock else "live"

        prev_run = self.store.get_latest_run(site.id)
        signals = collect_shop_signals(
            site,
            actor=actor_obj,
            domains=selected,
            mode=clean_mode,
            cfg=cfg,
            data_dir=self.data_dir,
            woo_client_factory=self._woo_client_factory,
            wp_client_factory=self._wp_client_factory,
            bridge_client_factory=self._bridge_client_factory,
            files_factory=self._files_factory,
        )

        dossier, facts, findings, diffs = analyze_shop_signals(
            run_id=run_id,
            site_id=site.id,
            signals=signals,
            domains=selected,
            prev_run=prev_run,
        )

        posture = dossier.get("security_posture") or {}
        security_score = int(posture.get("security_score", 100))
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        warnings = list(signals.get("warnings") or [])
        status = "partial" if warnings else "completed"

        run = IntelRun(
            run_id=run_id,
            site_id=site.id,
            mode=clean_mode,
            provenance=provenance,
            actor=actor_obj.name,
            domains_collected=selected,
            status=status,
            security_score=security_score,
            findings_count=len(findings),
            diffs_count=len(diffs),
            cost_usd=0.0,
            duration_ms=duration_ms,
            sources=list(signals.get("sources") or []),
            warnings=warnings,
            dossier=dossier,
            created_at=self._clock().astimezone(UTC).isoformat(),
        )

        self.store.save_run(run, facts=facts, diffs=diffs, findings=findings)
        md_text = render_dossier_markdown(run, findings=findings, diffs=diffs)
        artifacts = self.store.write_dossier_artifacts(
            site.id,
            run_payload=run.to_dict(),
            markdown_text=md_text,
        )
        self.store.prune_old_runs(
            site.id,
            max_runs=cfg.max_runs_per_site,
            retention_days=cfg.retention_days,
        )

        # Refresh schedule timestamps if scheduled
        sched = self.store.get_schedule(site.id)
        if sched and sched.get("enabled"):
            now_dt = self._clock().astimezone(UTC)
            nxt_dt = now_dt + timedelta(hours=int(sched.get("interval_hours") or 24))
            self.store.save_schedule(
                IntelSchedule(
                    site_id=site.id,
                    enabled=True,
                    interval_hours=int(sched.get("interval_hours") or 24),
                    mode=str(sched.get("mode") or "full"),
                    last_run_at=now_dt.isoformat(),
                    next_run_at=nxt_dt.isoformat(),
                    updated_by=str(sched.get("updated_by") or actor_obj.name),
                    updated_at=now_dt.isoformat(),
                )
            )

        log_action(
            actor=actor_obj.name,
            action="shop_intel_run",
            target=site.id,
            target_id=run_id,
            success=True,
            details={
                "mode": clean_mode,
                "provenance": provenance,
                "security_score": security_score,
                "findings_count": len(findings),
                "diffs_count": len(diffs),
                "cost_usd": 0.0,
            },
        )

        return {
            "ok": True,
            "run": run.to_dict(),
            "dossier": dossier,
            "facts_count": len(facts),
            "diffs": [d.to_dict() for d in diffs],
            "findings": [f.to_dict() for f in findings],
            "security_score": security_score,
            "artifacts": artifacts,
            "live_calls": provenance == "live",
        }

    def get_status(self, site_id: str | None = None, *, actor: str = "jonatan") -> dict[str, Any]:
        _require_read_actor(actor)
        sites = load_sites(data_dir=self.data_dir, apply_overlay=False)
        gates = {
            "mock_mode": is_mock_mode(),
            "live_enabled": shop_intel_live_enabled(),
            "kill_active": shop_intel_kill_active(),
        }
        if site_id:
            clean_sid = str(site_id).strip().lower()
            latest = self.store.get_latest_run(clean_sid)
            sched = self.store.get_schedule(clean_sid)
            runs = self.store.list_runs(clean_sid, limit=10)
            return {
                "ok": True,
                "site_id": clean_sid,
                "gates": gates,
                "latest_run": latest,
                "schedule": sched,
                "recent_runs": runs,
            }

        site_rows: list[dict[str, Any]] = []
        for s in sites:
            latest = self.store.get_latest_run(s.id)
            sched = self.store.get_schedule(s.id)
            site_rows.append(
                {
                    "site_id": s.id,
                    "display_name": s.display_name,
                    "latest_run_id": latest.get("run_id") if latest else None,
                    "last_observed_at": latest.get("created_at") if latest else None,
                    "security_score": latest.get("security_score") if latest else None,
                    "schedule": sched,
                }
            )
        return {
            "ok": True,
            "gates": gates,
            "sites": site_rows,
            "recent_runs": self.store.list_runs(limit=15),
        }

    def get_run(self, run_id: str, *, actor: str = "jonatan") -> dict[str, Any]:
        _require_read_actor(actor)
        clean_id = str(run_id or "").strip()
        if not SHOP_INTEL_RUN_ID_RE.fullmatch(clean_id):
            raise ValueError(f"Invalid run_id: {run_id!r}")
        run = self.store.get_run(clean_id)
        if not run:
            return {"ok": False, "error_class": "NotFound", "message": f"Unknown run {clean_id}"}
        site_id = str(run["site_id"])
        return {
            "ok": True,
            "run": run,
            "facts": self.store.list_facts(run_id=clean_id),
            "findings": self.store.list_security_findings(site_id, run_id=clean_id),
            "diffs": self.store.list_diffs(site_id, run_id=clean_id),
        }

    def list_runs(
        self, site_id: str | None = None, *, limit: int = 20, actor: str = "jonatan"
    ) -> dict[str, Any]:
        _require_read_actor(actor)
        return {"ok": True, "runs": self.store.list_runs(site_id, limit=limit)}

    def query_knowledge(
        self,
        site_id: str,
        *,
        domain: str | None = None,
        query: str | None = None,
        purpose: str | None = None,
        max_chars: int | None = None,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        _require_read_actor(actor)
        clean_sid = str(site_id or "").strip().lower()
        clean_dom = str(domain).strip().lower() if domain else None
        if clean_dom and clean_dom not in VALID_DOMAINS_SET:
            raise ValueError(f"Unsupported domain {domain!r}. Allowed: {VALID_DOMAINS}")

        latest = self.store.get_latest_run(clean_sid)
        if not latest:
            return {
                "ok": False,
                "error_class": "NoSnapshot",
                "message": f"Ingen Shop Intel-körning hittades för {clean_sid}.",
            }
        run_id = str(latest["run_id"])
        facts = self.store.list_facts(run_id=run_id, site_id=clean_sid, domain=clean_dom)
        needle = scrub_text(query or "", max_chars=200).lower()
        if needle:
            facts = [
                f
                for f in facts
                if needle in str(f.get("key") or "").lower()
                or needle in str(f.get("domain") or "").lower()
                or needle in str(f.get("value") or "").lower()
            ]
        dossier = latest.get("dossier") if isinstance(latest.get("dossier"), dict) else {}
        if clean_dom:
            dossier = {clean_dom: dossier.get(clean_dom)} if clean_dom in dossier else {}

        ctx = (
            build_tailored_context(
                clean_sid,
                purpose=purpose,
                data_dir=self.data_dir,
                max_chars=max_chars,
            )
            if purpose
            else None
        )
        return {
            "ok": True,
            "site_id": clean_sid,
            "run_id": run_id,
            "provenance": latest.get("provenance"),
            "observed_at": latest.get("created_at"),
            "facts": facts,
            "dossier": dossier,
            "context": ctx,
        }

    def get_diffs(
        self, site_id: str, *, limit: int = 20, actor: str = "jonatan"
    ) -> dict[str, Any]:
        _require_read_actor(actor)
        clean_sid = str(site_id or "").strip().lower()
        return {
            "ok": True,
            "site_id": clean_sid,
            "diffs": self.store.list_diffs(clean_sid, limit=limit),
        }

    def get_security_report(
        self,
        site_id: str,
        *,
        severity: str | None = None,
        limit: int = 50,
        actor: str = "jonatan",
    ) -> dict[str, Any]:
        _require_read_actor(actor)
        clean_sid = str(site_id or "").strip().lower()
        clean_sev = str(severity).strip().lower() if severity else None
        if clean_sev and clean_sev not in VALID_SEVERITIES_SET:
            raise ValueError(f"Invalid severity: {severity!r}")
        latest = self.store.get_latest_run(clean_sid)
        if not latest:
            return {
                "ok": False,
                "error_class": "NoSnapshot",
                "message": f"Ingen Shop Intel-körning hittades för {clean_sid}.",
            }
        run_id = str(latest["run_id"])
        findings = self.store.list_security_findings(
            clean_sid, run_id=run_id, severity=clean_sev, limit=limit
        )
        return {
            "ok": True,
            "site_id": clean_sid,
            "run_id": run_id,
            "security_score": latest.get("security_score", 100),
            "posture": (latest.get("dossier") or {}).get("security_posture") or {},
            "findings": findings,
        }

    def export_dossier(
        self, site_id: str, *, fmt: str = "json", actor: str = "jonatan"
    ) -> dict[str, Any]:
        _require_read_actor(actor)
        clean_sid = str(site_id or "").strip().lower()
        clean_fmt = str(fmt or "json").strip().lower()
        if clean_fmt not in {"json", "md", "markdown"}:
            raise ValueError(f"Unsupported export format {fmt!r}")
        content = self.store.read_dossier_artifact(clean_sid, fmt=clean_fmt)
        if content is None:
            return {
                "ok": False,
                "error_class": "NoSnapshot",
                "message": f"Ingen exporterbar dossier hittades för {clean_sid}.",
            }
        ext = "md" if clean_fmt in {"md", "markdown"} else "json"
        path = self.store.artifact_dir(clean_sid) / f"latest.{ext}"
        return {
            "ok": True,
            "site_id": clean_sid,
            "format": ext,
            "path": str(path),
            "content": content,
        }

    def configure_schedule(
        self,
        site_id: str,
        *,
        enabled: bool = True,
        interval_hours: int = 24,
        mode: str = "full",
        actor: str = "oscar",
    ) -> dict[str, Any]:
        actor_obj = _require_schedule_actor(actor)
        clean_sid = str(site_id or "").strip().lower()
        if not clean_sid:
            raise ValueError("site_id is required")
        clean_mode = str(mode or "full").strip().lower()
        if clean_mode not in VALID_MODES:
            raise ValueError(f"Unsupported mode {mode!r}. Allowed: {sorted(VALID_MODES)}")
        hours = max(1, min(720, int(interval_hours)))
        now_dt = self._clock().astimezone(UTC)
        next_dt = now_dt + timedelta(hours=hours) if enabled else None
        existing = self.store.get_schedule(clean_sid)
        sched = IntelSchedule(
            site_id=clean_sid,
            enabled=bool(enabled),
            interval_hours=hours,
            mode=clean_mode,
            last_run_at=existing.get("last_run_at") if existing else None,
            next_run_at=next_dt.isoformat() if next_dt else None,
            updated_by=actor_obj.name,
            updated_at=now_dt.isoformat(),
        )
        self.store.save_schedule(sched)
        log_action(
            actor=actor_obj.name,
            action="shop_intel_schedule_configured",
            target=clean_sid,
            success=True,
            details={"enabled": bool(enabled), "interval_hours": hours, "mode": clean_mode},
        )
        return {"ok": True, "schedule": sched.to_dict()}

    def run_due_schedules(self, *, actor: str = "agent") -> dict[str, Any]:
        _require_run_actor(actor)
        now_iso = self._clock().astimezone(UTC).isoformat()
        executed: list[dict[str, Any]] = []
        for sched in self.store.list_schedules():
            if not sched.get("enabled"):
                continue
            nxt = sched.get("next_run_at")
            if nxt and str(nxt) > now_iso:
                continue
            res = self.run_learning(
                str(sched["site_id"]),
                actor=actor,
                mode=str(sched.get("mode") or "full"),
            )
            executed.append({"site_id": sched["site_id"], "ok": bool(res.get("ok")), "result": res})
        return {"ok": True, "executed_count": len(executed), "executed": executed}


def get_shop_intel_service(data_dir: Path | None = None) -> ShopIntelService:
    """Return a ``ShopIntelService`` keyed dynamically by resolved ``AZOM_DATA_DIR``."""
    resolved = str(data_dir_path(data_dir).resolve())
    with _SERVICE_LOCK:
        svc = _SERVICES.get(resolved)
        if svc is None:
            svc = ShopIntelService(data_dir=Path(resolved))
            _SERVICES[resolved] = svc
        return svc


def reset_shop_intel_service() -> None:
    """Clear cached ``ShopIntelService`` instances (intended for tests)."""
    with _SERVICE_LOCK:
        _SERVICES.clear()


__all__ = [
    "ShopIntelService",
    "get_shop_intel_service",
    "reset_shop_intel_service",
]
