"""Review-plan state. This module does not import a process runner.

Plan ids are ``wr-`` plus 12 hex digits. File bodies stay in the workspace.
Production deploy stays closed. ``restore()`` is never called.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.file_changes import production_deploy_block, risk_for
from ecom_ops.wp_ops.files import LocalTree, TransportError, resolve_file
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review_checks import run_catalog, suite_status
from ecom_ops.wp_ops.review_runner import SandboxGate
from ecom_ops.wp_ops.review_workspace import (
    PLAN_ID,
    SLUG,
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
    child_skeleton,
    delete_work,
    iter_files,
    new_plan_id,
    place_work_file,
    plugin_skeleton,
    read_diff,
    read_site_component,
    restore_work_from_base,
    site_plugin_slug,
    tree_hash,
    workspace_root,
    write_diff,
    write_manifest,
    write_tree,
)
from ecom_ops.wp_ops.service import WpOpsResult

_TTL = timedelta(hours=12)
_PREVIEW_TTL = timedelta(hours=2)
_STORE = threading.RLock()
_PLAN_LOCKS: dict[str, threading.Lock] = {}
_PLAN_GUARD = threading.Lock()
_ORDER_RISK = re.compile(
    r"(?i)(?<![a-z])(?:shipping|stock|inventory|orders?|frakt|lager)(?![a-z])"
)
_ORDER_PARTS = frozenset({"shipping", "stock", "inventory", "order", "orders", "frakt", "lager"})
_PUBLIC = (
    "plan_id",
    "site_id",
    "environment",
    "kind",
    "slug",
    "work_slug",
    "risk",
    "status",
    "theme_relationship",
    "base_sha256",
    "work_sha256",
    "created_at",
    "expires_at",
    "extended",
    "checks",
    "sandbox",
    "comments",
    "label",
    "error_class",
    "superseded_by",
    "review_path",
    "summary",
)
_TERMINAL = frozenset({"rejected", "denied", "expired", "fork_not_approved", "not_resumable"})
_STAGE_OK = frozenset({"tests_passed", "review_requested", "approved_for_staging"})
_UNREACHABLE = frozenset({"approved_for_production", "deployed", "verified"})


def review_risk(relative: str, text: str) -> str:
    """High-risk classifier for the review chain. ``risk_for`` stays unchanged."""
    if risk_for(relative, text) == "high":
        return "high"
    parts = [part.lower() for part in str(relative or "").replace("\\", "/").split("/")]
    if any(part in _ORDER_PARTS for part in parts):
        return "high"
    if _ORDER_RISK.search(relative or "") or _ORDER_RISK.search(text or ""):
        return "high"
    return "low"


def text_is_review_approval(text: str) -> bool:
    """Free text and ``action:yes`` never approve a review plan."""
    del text
    return False


def _fail(error_class: str, message: str, **extra: Any) -> WpOpsResult:
    data: dict[str, Any] = {"error_class": error_class, "executed": False, "live_calls": False}
    data.update(extra)
    return WpOpsResult(False, message, data)


def _ok(message: str, **extra: Any) -> WpOpsResult:
    extra.setdefault("executed", False)
    extra.setdefault("live_calls", False)
    return WpOpsResult(True, message, extra)


def _plan_lock(plan_id: str) -> threading.Lock:
    key = f"wp-review-{plan_id}"
    with _PLAN_GUARD:
        lock = _PLAN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PLAN_LOCKS[key] = lock
        return lock


def _site_lock(site_id: str) -> threading.Lock:
    from ecom_ops.wp_ops.service import _site_lock as lock_for

    return lock_for(site_id)


class ReviewPlans:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        registry_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        transport_factory: Callable[[Site], object] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data"))
        self.registry_path = registry_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transport_factory = transport_factory
        self.path = self.data_dir / "wp_review_plans.json"
        self.gate = SandboxGate()

    def decide_text(self, text: str, *, actor: str) -> WpOpsResult:
        del actor
        approved = text_is_review_approval(text)
        return _fail("TextNotApproval", "Fritext ändrar inte granskningen.", changed=approved)

    def open(
        self,
        site_id: str,
        kind: str,
        slug: str = "",
        *,
        actor: str,
        summary: str = "",
        verified_theme: VerifiedTheme | None = None,
        verified_plugin: VerifiedPlugin | None = None,
    ) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_open")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None:
            return missing
        if kind not in {"theme", "plugin"}:
            return _fail("InvalidTarget", "Ogiltig komponent.")
        summary = str(summary or "")[:200]
        try:
            built = self._prepare(site, kind, slug, verified_theme, verified_plugin)
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)
        plan_id = new_plan_id()
        root = workspace_root(self.data_dir, site.id, plan_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "base").mkdir()
        (root / "work").mkdir()
        if built["files"]:
            write_tree(root / "base", built["files"])
            write_tree(root / "work", built["files"])
        now = self._now()
        risk = review_risk(built["slug"], _risk_text(built["files"], summary))
        plan = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": kind,
            "slug": built["slug"],
            "work_slug": built["slug"],
            "risk": risk,
            "status": built["status"],
            "theme_relationship": built["theme_relationship"],
            "base_sha256": tree_hash(root / "base"),
            "work_sha256": tree_hash(root / "work"),
            "created_at": now.isoformat(),
            "expires_at": (now + _TTL).isoformat(),
            "extended": False,
            "checks": [],
            "sandbox": self.gate.stop(
                {"plan_id": plan_id, "engine": "unavailable", "state": "unavailable"}
            ),
            "comments": [],
            "label": "mock",
            "error_class": built["error_class"],
            "superseded_by": "",
            "review_path": f"/wp/{site.id}/reviews/{plan_id}",
            "summary": summary,
        }
        write_diff(root, "")
        write_manifest(root, plan)
        with _STORE:
            plans = self._load()
            for older in plans:
                if self._same_target(older, plan) and older.get("status") not in _TERMINAL:
                    older["superseded_by"] = plan_id
            plans.append(self._public(plan))
            self._save(plans)
        self._audit(actor, plan, "wp_review_open", built["status"] not in {"rejected", "fork_not_approved"})
        if built["status"] in {"rejected", "fork_not_approved"}:
            return _fail(
                built["error_class"] or built["status"],
                built["error_class"] or built["status"],
                plan_id=plan_id,
                site_id=site.id,
                status=built["status"],
                review_path=plan["review_path"],
                login_required=True,
            )
        return _ok(
            "Arbetsytan är skapad. Sajten skrevs inte.",
            plan_id=plan_id,
            site_id=site.id,
            status=plan["status"],
            risk=plan["risk"],
            theme_relationship=plan["theme_relationship"],
            review_path=plan["review_path"],
            login_required=True,
            environment=site.environment,
        )

    def place(self, site_id: str, plan_id: str, relative: str, data: bytes, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_place")
        if denied is not None:
            return denied
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
            place_work_file(root, relative, data)
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)
        return self.refresh(site_id, plan_id, actor=actor)

    def refresh(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_refresh")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._fresh(plan)
            if blocked is not None:
                return blocked
            root = workspace_root(self.data_dir, site_id, plan_id)
            plan["work_sha256"] = tree_hash(root / "work")
            plan["base_sha256"] = tree_hash(root / "base")
            plan["risk"] = review_risk(str(plan.get("slug") or ""), _disk_text(root / "work"))
            if plan.get("status") in {
                "tests_passed",
                "tests_failed",
                "checks_incomplete",
                "review_requested",
                "approved_for_staging",
            }:
                plan["status"] = "validated"
                plan["checks"] = []
            write_diff(root, _unified(root))
            write_manifest(root, plan)
            self._replace(plan)
        self._audit(actor, plan, "wp_review_refresh", True)
        return _ok("Arbetsytan uppdaterades.", plan_id=plan_id, site_id=site_id, status=plan["status"], risk=plan["risk"])

    def run_checks(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_checks")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._fresh(plan)
            if blocked is not None:
                return blocked
            if plan.get("status") in {"rejected", "fork_not_approved", "draft"}:
                return _fail(str(plan.get("error_class") or "NotValidated"), "Planen är inte validerad.")
            root = workspace_root(self.data_dir, site_id, plan_id)
            files = _text_files(root / "work")
            kind = str(plan.get("kind") or "")
            risk = str(plan.get("risk") or "low")
            results = run_catalog(kind, files, risk, artifact_dir=root / "artifacts")
            status = suite_status(kind, files, risk, results)
            plan["checks"] = results
            plan["status"] = status
            self._replace(plan)
        for row in results:
            self._audit(
                actor,
                plan,
                "wp_review_check",
                row.get("status") != "failed",
                job=row.get("job"),
                outcome=row.get("status"),
            )
        return _ok("Kontrollerna är bokförda.", plan_id=plan_id, site_id=site_id, status=status, checks=results)

    def show(self, site_id: str, plan_id: str, *, actor: str, include_diff: bool = False) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_show")
        if denied is not None:
            return denied
        include_diff = include_diff and self._holds(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        self._expire_if_due(plan)
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return _fail("Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id, status="expired")
        view = self._view(plan, include_diff=include_diff)
        return _ok("Plan.", plan=view, diff=view.get("diff", "") if include_diff else "")

    def list_site(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_list")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None:
            return missing
        rows = []
        for plan in self._load():
            if plan.get("site_id") != site.id:
                continue
            rows.append(self._view(plan, include_diff=False))
        return _ok("Lista.", site_id=site.id, plans=rows)

    def comment(self, site_id: str, plan_id: str, text: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_comment")
        if denied is not None:
            return denied
        cleaned = str(text or "").strip()
        if not cleaned:
            return _fail("InvalidComment", "Tom synpunkt.")
        cleaned = cleaned[:500]
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._fresh(plan)
            if blocked is not None:
                return blocked
            status = str(plan.get("status") or "")
            comments = list(plan.get("comments") or [])
            comments.append({"actor": actor, "at": self._now().isoformat(), "text": cleaned})
            plan["comments"] = comments
            plan["status"] = status
            self._replace(plan)
        self._audit(actor, plan, "wp_review_comment", True, chars=len(cleaned))
        return _ok("Synpunkten är sparad.", plan_id=plan_id, site_id=site_id, status=status)

    def approve_staging(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        base_sha256: str,
        work_sha256: str,
    ) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_staging")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._gate(plan, base_sha256, work_sha256)
            if blocked is not None:
                return blocked
            if plan.get("risk") == "high":
                return _fail("HighRiskBlocked", "Hög risk skrivs inte.", plan_id=plan_id, site_id=site_id)
            site, missing = self._site(site_id)
            if missing is not None:
                return missing
            assert site is not None
            sites = self._sites()
            if site.environment != "staging":
                if any(item.environment == "staging" for item in sites):
                    return _fail("ProductionWriteDisabled", "Produktion skrivs inte.", plan_id=plan_id)
                return _fail("StagingUnconfigured", "Ingen staging-sajt är registrerad.", plan_id=plan_id)
            transport = self._transport(site)
            if not isinstance(transport, LocalTree):
                return _fail("DeployTransportUnavailable", "Transporten tar inte emot skrivning.", plan_id=plan_id)
            if plan.get("status") not in _STAGE_OK:
                return _fail("TestsNotPassed", "Kontrollerna är inte godkända.", plan_id=plan_id, status=plan.get("status"))
            root = workspace_root(self.data_dir, site_id, plan_id)
            changed, problem = _changed(root)
            if problem:
                return _fail(problem, problem, plan_id=plan_id)
            if not changed:
                return _fail("NoChange", "Arbetsytan matchar basen.", plan_id=plan_id)
            targets: list[tuple[str, bytes]] = []
            notes: list[dict[str, Any]] = []
            for relative, data in changed:
                try:
                    absolute = resolve_file(site.files_root, str(plan.get("kind")), str(plan.get("slug")), relative)
                    transport.read_bytes(absolute)
                except (TransportError, ValueError):
                    return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)
                targets.append((absolute, data))
                notes.append({"path": relative, "bytes": len(data), "sha256": _sha(data)})
            for absolute, data in targets:
                transport.write_bytes(absolute, data)
            plan["status"] = "staged"
            plan["label"] = "mock"
            self._replace(plan)
        self._audit(actor, plan, "wp_review_staging", True, files=notes, label="mock")
        return _ok(
            "Mockträdet uppdaterades.",
            plan_id=plan_id,
            site_id=site_id,
            status="staged",
            executed=True,
            label="mock",
            bytes_copied=False,
            backup_offsite=False,
        )

    def request_production(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        base_sha256: str,
        work_sha256: str,
    ) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_production")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._gate(plan, base_sha256, work_sha256)
            if blocked is not None:
                return blocked
            if plan.get("risk") == "high":
                return _fail("HighRiskBlocked", "Hög risk går inte till produktion.", plan_id=plan_id)
            plan["status"] = "production_review"
            for name in _UNREACHABLE:
                plan.pop(name, None)
            self._replace(plan)
        block = production_deploy_block()
        self._audit(actor, plan, "wp_review_production", False, error_class="BackupNotVerified")
        return WpOpsResult(
            False,
            "BackupNotVerified",
            {**block, "status": "production_review", "plan_id": plan_id, "site_id": site_id},
        )

    def preview(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_preview")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            blocked = self._fresh(plan)
            if blocked is not None:
                return blocked
            if plan.get("status") != "tests_passed":
                return _fail("PreviewBlocked", "Förhandsvisningen startades inte.", plan_id=plan_id, status=plan.get("status"))
            site, missing = self._site(site_id)
            if missing is not None:
                return missing
            assert site is not None
            try:
                record = self.gate.start(site, plan_id, high_risk=plan.get("risk") == "high")
            except ReviewError as exc:
                plan["sandbox"] = self.gate.stop(dict(plan.get("sandbox") or {}))
                self._replace(plan)
                return _fail(exc.error_class, exc.error_class, plan_id=plan_id)
            record["preview_expires_at"] = (self._now() + _PREVIEW_TTL).isoformat()
            record["payments_enabled"] = False
            record["checkout_enabled"] = False
            plan["sandbox"] = record
            self._replace(plan)
        self._audit(actor, plan, "wp_review_preview", True, outcome=record.get("state"))
        return _ok("Förhandsvisningen är bokförd.", plan_id=plan_id, site_id=site_id, sandbox=record, status=plan["status"])

    def rollback(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_rollback")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            root = workspace_root(self.data_dir, site_id, plan_id)
            try:
                restore_work_from_base(root)
            except ReviewError as exc:
                return _fail(exc.error_class, exc.error_class)
            plan["work_sha256"] = tree_hash(root / "work")
            plan["sandbox"] = self.gate.stop(dict(plan.get("sandbox") or {}))
            plan["status"] = "rolled_back"
            write_diff(root, "")
            self._replace(plan)
        self._audit(actor, plan, "wp_review_rollback", True, restore="not_called")
        return _ok("Arbetsytan återställdes från basen.", plan_id=plan_id, site_id=site_id, status="rolled_back", restore="not_called")

    def extend(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_extend")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            if plan.get("status") == "expired" or self._past(plan):
                self._mark_expired(plan)
                return _fail("Expired", "Planen har gått ut.")
            if plan.get("extended"):
                return _fail("ExtendUsed", "Planen är redan förlängd en gång.")
            current = _parse_time(str(plan.get("expires_at") or ""))
            if current is None:
                return _fail("Expired", "Planen har gått ut.")
            plan["expires_at"] = (current + _TTL).isoformat()
            plan["extended"] = True
            self._replace(plan)
        self._audit(actor, plan, "wp_review_extend", True)
        return _ok("Planen förlängdes tolv timmar.", plan_id=plan_id, expires_at=plan["expires_at"])

    def expire_due(self, *, actor: str = "agent") -> int:
        count = 0
        for plan in self._load():
            if plan.get("status") == "expired" or not self._past(plan):
                continue
            self._mark_expired(plan)
            self._audit(actor, plan, "wp_review_expire", True)
            count += 1
        return count

    def note_process_restart(self, *, actor: str = "agent") -> int:
        count = 0
        for plan in self._load():
            sandbox = dict(plan.get("sandbox") or {})
            if sandbox.get("state") not in {"created", "started", "running"}:
                continue
            sandbox["state"] = "stopped"
            sandbox["container"] = "stopped"
            sandbox["port"] = None
            plan["sandbox"] = sandbox
            plan["status"] = "sandbox_expired"
            self._replace(plan)
            self._audit(actor, plan, "wp_review_restart", True, outcome="stopped")
            count += 1
        return count

    def _prepare(
        self,
        site: Site,
        kind: str,
        slug: str,
        verified_theme: VerifiedTheme | None,
        verified_plugin: VerifiedPlugin | None,
    ) -> dict[str, Any]:
        if kind == "plugin":
            return self._prepare_plugin(site, slug, verified_plugin)
        return self._prepare_theme(site, slug, verified_theme)

    def _prepare_plugin(
        self, site: Site, slug: str, verified: VerifiedPlugin | None
    ) -> dict[str, Any]:
        reserved = site_plugin_slug(site.id)
        chosen = slug or reserved
        if not SLUG.fullmatch(chosen):
            raise ReviewError("SlugRejected")
        if chosen == "woocommerce":
            return _prepared([], "rejected", "unknown", "WooCoreRefused", chosen)
        if verified is not None:
            if not SLUG.fullmatch(verified.slug) or verified.slug != chosen:
                return _prepared([], "rejected", "unknown", "PluginNotVerified", chosen)
            files = read_site_component(self._transport(site), site.files_root, "plugin", chosen)
            if not files:
                return _prepared([], "draft", "existing_plugin", "ComponentMissing", chosen)
            return _prepared(files, "validated", "existing_plugin", "", chosen)
        if chosen != reserved:
            return _prepared([], "fork_not_approved", "unknown", "ThirdPartyPluginRefused", chosen)
        return _prepared(plugin_skeleton(reserved), "validated", "site_plugin", "", reserved)

    def _prepare_theme(self, site: Site, slug: str, verified: VerifiedTheme | None) -> dict[str, Any]:
        if verified is None:
            return _prepared([], "draft", "unknown", "", slug or "")
        if not SLUG.fullmatch(verified.stylesheet) or not SLUG.fullmatch(verified.template):
            raise ReviewError("SlugRejected")
        if verified.template == verified.stylesheet:
            child = f"{verified.stylesheet}-child"
            if not SLUG.fullmatch(child):
                raise ReviewError("SlugRejected")
            return _prepared(child_skeleton(verified.stylesheet), "validated", "no_child", "", child)
        if slug == verified.template:
            return _prepared([], "rejected", "child", "ParentThemeBlocked", slug)
        if slug != verified.stylesheet:
            return _prepared([], "draft", "unknown", "ThemeRelationshipUnknown", slug)
        files = read_site_component(self._transport(site), site.files_root, "theme", slug)
        if not files:
            return _prepared([], "draft", "child", "ComponentMissing", slug)
        return _prepared(files, "validated", "child", "", slug)

    def _holds(self, actor: str, *perms: Permission) -> bool:
        try:
            resolved = resolve_actor(actor)
        except AccessDenied:
            return False
        return all(resolved.has(perm) for perm in perms)

    def _need(self, actor: str, *perms: Permission, action: str) -> WpOpsResult | None:
        try:
            resolved = resolve_actor(actor)
            for perm in perms:
                require_permission(resolved, perm)
        except AccessDenied:
            self._audit_bare(actor, action, False, error_class="AccessDenied")
            return _fail("AccessDenied", "Saknar behörighet.")
        return None

    def _site(self, site_id: str) -> tuple[Site | None, WpOpsResult | None]:
        site = site_by_id(self._sites(), site_id)
        if site is None:
            return None, _fail("UnknownSite", "Okänd webbplats.", site_id=site_id)
        return site, None

    def _sites(self) -> tuple[Site, ...]:
        return load_sites(self.registry_path, data_dir=self.data_dir)

    def _transport(self, site: Site) -> object:
        if self.transport_factory is not None:
            return self.transport_factory(site)
        from ecom_ops.integrations.wp_files_ssh import build_file_transport

        return build_file_transport(site, self.data_dir)

    def _now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    def _past(self, plan: dict[str, Any]) -> bool:
        expires = _parse_time(str(plan.get("expires_at") or ""))
        if expires is None:
            return True
        return self._now() >= expires

    def _locks(self, site_id: str, plan_id: str):
        return _LockBoth(_site_lock(site_id), _plan_lock(plan_id))

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return []
        plans = raw.get("plans") if isinstance(raw, dict) else None
        if not isinstance(plans, list):
            return []
        return [row for row in plans if isinstance(row, dict)]

    def _save(self, plans: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"plans": [self._public(row) for row in plans]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _replace(self, plan: dict[str, Any]) -> None:
        plans = self._load()
        replaced = False
        for index, row in enumerate(plans):
            if row.get("plan_id") == plan.get("plan_id"):
                plans[index] = self._public(plan)
                replaced = True
                break
        if not replaced:
            plans.append(self._public(plan))
        self._save(plans)

    def _row(self, site_id: str, plan_id: str) -> dict[str, Any] | None:
        if not PLAN_ID.fullmatch(plan_id):
            return None
        for plan in self._load():
            if plan.get("plan_id") == plan_id and plan.get("site_id") == site_id:
                status = str(plan.get("status") or "")
                if status not in {"expired", "not_resumable"}:
                    try:
                        root = workspace_root(self.data_dir, site_id, plan_id)
                    except ReviewError:
                        root = None
                    if root is None or not root.is_dir():
                        plan["status"] = "not_resumable"
                        plan["sandbox"] = {
                            **dict(plan.get("sandbox") or {}),
                            "engine": "unavailable",
                            "state": "destroyed",
                            "container": "unavailable",
                            "availability": "unavailable",
                        }
                        self._replace(plan)
                return plan
        return None

    def _fresh(self, plan: dict[str, Any]) -> WpOpsResult | None:
        if plan.get("status") == "not_resumable":
            return _fail(
                "WorkspaceNotResumable",
                "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                plan_id=plan.get("plan_id"),
                status="not_resumable",
            )
        if plan.get("superseded_by"):
            return _fail("StalePlan", "En nyare diff har ett annat plan-id.", plan_id=plan.get("plan_id"))
        if plan.get("status") == "expired" or self._past(plan):
            self._mark_expired(plan)
            return _fail("Expired", "Planen har gått ut.", plan_id=plan.get("plan_id"), status="expired")
        return None

    def _gate(self, plan: dict[str, Any], base_sha256: str, work_sha256: str) -> WpOpsResult | None:
        blocked = self._fresh(plan)
        if blocked is not None:
            return blocked
        if base_sha256 != plan.get("base_sha256") or work_sha256 != plan.get("work_sha256"):
            return _fail("DigestMismatch", "Hasharna matchar inte planen.", plan_id=plan.get("plan_id"))
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)
        if tree_hash(root / "base") != plan.get("base_sha256") or tree_hash(root / "work") != plan.get("work_sha256"):
            return _fail("DigestMismatch", "Hasharna matchar inte arbetsytan.", plan_id=plan.get("plan_id"))
        return None

    def _expire_if_due(self, plan: dict[str, Any]) -> None:
        if plan.get("status") != "expired" and self._past(plan):
            self._mark_expired(plan)

    def _mark_expired(self, plan: dict[str, Any]) -> None:
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
            delete_work(root)
        except ReviewError:
            root = None
        plan["status"] = "expired"
        plan["sandbox"] = self.gate.destroy(dict(plan.get("sandbox") or {}))
        self._replace(plan)
        if root is not None:
            write_manifest(root, plan)

    def _view(self, plan: dict[str, Any], *, include_diff: bool) -> dict[str, Any]:
        view = self._public(plan)
        if not include_diff:
            view.pop("comments", None)
            view.pop("summary", None)
            return view
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
            view["diff"] = read_diff(root)
        except ReviewError:
            view["diff"] = ""
        return view

    def _public(self, plan: dict[str, Any]) -> dict[str, Any]:
        clean = {key: plan.get(key) for key in _PUBLIC}
        checks = []
        for row in plan.get("checks") or []:
            if not isinstance(row, dict):
                continue
            item = {
                "job": row.get("job"),
                "status": row.get("status"),
                "summary": row.get("summary"),
                "command": row.get("command"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "exit_code": row.get("exit_code"),
                "artifact_path": row.get("artifact_path"),
                "executed": row.get("executed") is True,
                "simulated": row.get("simulated") is True,
            }
            if row.get("availability"):
                item["availability"] = str(row.get("availability"))[:40]
            if row.get("reason"):
                item["reason"] = str(row.get("reason"))[:80]
            if "declaration_is_not_proof" in row:
                item["declaration_is_not_proof"] = True
            if isinstance(row.get("findings"), list):
                item["findings"] = [str(part) for part in row["findings"]]
            checks.append(item)
        clean["checks"] = checks
        sandbox = dict(plan.get("sandbox") or {})
        clean["sandbox"] = {
            "engine": sandbox.get("engine", "unavailable"),
            "database": sandbox.get("database", "not_created"),
            "network": sandbox.get("network", "not_run"),
            "env_names": [str(name) for name in sandbox.get("env_names") or []],
            "state": sandbox.get("state", "stopped"),
            "port": None,
            "volume": sandbox.get("volume"),
            "container": sandbox.get("container", "stopped"),
            "payments_enabled": False,
            "checkout_enabled": False,
            "admin_password_stored": False,
            "plan_id": plan.get("plan_id"),
            "preview_expires_at": sandbox.get("preview_expires_at"),
        }
        comments = []
        for row in plan.get("comments") or []:
            if isinstance(row, dict) and isinstance(row.get("text"), str):
                comments.append(
                    {"actor": str(row.get("actor") or ""), "at": str(row.get("at") or ""), "text": row["text"][:500]}
                )
        clean["comments"] = comments
        return clean

    def _same_target(self, older: dict[str, Any], plan: dict[str, Any]) -> bool:
        return (
            older.get("site_id") == plan.get("site_id")
            and older.get("kind") == plan.get("kind")
            and older.get("slug") == plan.get("slug")
            and older.get("plan_id") != plan.get("plan_id")
        )

    def _audit(self, actor: str, plan: dict[str, Any], action: str, success: bool, **extra: Any) -> None:
        details = {
            "site_id": plan.get("site_id"),
            "plan_id": plan.get("plan_id"),
            "status": plan.get("status"),
            "base_sha256": plan.get("base_sha256"),
            "work_sha256": plan.get("work_sha256"),
            "live_calls": False,
        }
        details.update(extra)
        self._audit_bare(actor, action, success, **details)

    def _audit_bare(self, actor: str, action: str, success: bool, **details: Any) -> None:
        hidden = {"diff", "text", "source", "new_text", "content", "body", "summary"}
        safe = {key: value for key, value in details.items() if key not in hidden}
        log_action(
            actor=actor,
            action=action,
            target="wp_sites",
            target_id=str(safe.get("site_id") or safe.get("plan_id") or "") or None,
            success=success,
            details=safe,
        )


def _prepared(
    files: list[tuple[str, bytes]],
    status: str,
    relationship: str,
    error_class: str,
    slug: str,
) -> dict[str, Any]:
    return {
        "files": files,
        "status": status,
        "theme_relationship": relationship,
        "error_class": error_class,
        "slug": slug,
    }


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _risk_text(files: list[tuple[str, bytes]], summary: str) -> str:
    parts = [summary]
    for _relative, data in files:
        parts.append(data.decode("utf-8", errors="replace"))
    return "\n".join(parts)


def _disk_text(folder: Path) -> str:
    return "\n".join(data.decode("utf-8", errors="replace") for _rel, data in iter_files(folder))


def _text_files(folder: Path) -> list[tuple[str, str]]:
    return [(rel, data.decode("utf-8", errors="replace")) for rel, data in iter_files(folder)]


def _changed(root: Path) -> tuple[list[tuple[str, bytes]], str]:
    base = root / "base"
    work = root / "work"
    names: set[str] = set()
    for folder in (base, work):
        for relative, _data in iter_files(folder):
            names.add(relative)
    changed: list[tuple[str, bytes]] = []
    for relative in sorted(names):
        before = base / relative
        after = work / relative
        if not after.is_file():
            return [], "DeleteRefused"
        new = after.read_bytes()
        old = before.read_bytes() if before.is_file() else b""
        if new != old:
            changed.append((relative, new))
    return changed, ""


def _unified(root: Path) -> str:
    chunks: list[str] = []
    base = root / "base"
    work = root / "work"
    names = {rel for rel, _data in iter_files(base)} | {rel for rel, _data in iter_files(work)}
    for relative in sorted(names):
        before = (base / relative).read_text(encoding="utf-8", errors="replace") if (base / relative).is_file() else ""
        after = (work / relative).read_text(encoding="utf-8", errors="replace") if (work / relative).is_file() else ""
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=relative,
                tofile=relative,
                n=2,
            )
        )
    return "".join(chunks)[:65536]


class _LockBoth:
    def __init__(self, first: threading.Lock, second: threading.Lock) -> None:
        self.first = first
        self.second = second

    def __enter__(self) -> _LockBoth:
        self.first.acquire()
        self.second.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.second.release()
        self.first.release()
