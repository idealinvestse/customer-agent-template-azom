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
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.file_changes import production_deploy_block, risk_for
from ecom_ops.wp_ops.files import LocalTree, PathRejected, TransportError, resolve_file
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review_checks import (
    PROFILE,
    PROFILE_VERSION,
    bind_artifacts,
    public_policy,
    requirements,
    run_catalog,
    run_is_publishable,
    stamp_run,
    suite_status,
)
from ecom_ops.wp_ops.review_runner import SandboxGate, sandbox_enabled
from ecom_ops.wp_ops.review_workspace import (
    PLAN_ID,
    SLUG,
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
    apply_work_changes,
    bulk_replace_work,
    child_skeleton,
    create_work_file,
    delete_work,
    iter_files,
    list_work_changes,
    list_work_files,
    new_plan_id,
    patch_work_file,
    place_work_file,
    plugin_skeleton,
    read_diff,
    read_site_component,
    read_work_file,
    restore_work_from_base,
    scaffold_work,
    site_plugin_slug,
    theme_json_work,
    tree_hash,
    work_diff,
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
_TEMPLATE_HEADER = re.compile(r"^\s*Template:\s*([a-z0-9][a-z0-9_-]{0,60})\s*$", re.IGNORECASE | re.MULTILINE)
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
    "check_run",
    "check_generation",
    "sandbox",
    "comments",
    "label",
    "error_class",
    "superseded_by",
    "review_path",
    "summary",
)
_TERMINAL = frozenset({"rejected", "denied", "expired", "fork_not_approved", "not_resumable"})
_NON_EDITABLE = _TERMINAL | frozenset(
    {"staged", "rolled_back", "production_review", "approved_for_production", "deployed", "verified"}
)
_STAGE_OK = frozenset({"tests_passed", "review_requested", "approved_for_staging"})
_UNREACHABLE = frozenset({"approved_for_production", "deployed", "verified"})
_ARTIFACT_PNG = re.compile(r"^(?:base|work)-(?:desktop|mobile)\.png$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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
        if built["status"] in {"rejected", "fork_not_approved"}:
            self._audit_bare(
                actor,
                "wp_review_open",
                False,
                site_id=site.id,
                kind=kind,
                slug=built["slug"],
                status=built["status"],
                error_class=built["error_class"],
            )
            return _fail(
                built["error_class"] or built["status"],
                built["error_class"] or built["status"],
                site_id=site.id,
                status=built["status"],
            )
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
            "check_run": {},
            "check_generation": 0,
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
        self._audit(actor, plan, "wp_review_open", True)
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

    def _refresh_locked(self, plan: dict[str, Any], *, actor: str) -> dict[str, Any]:
        del actor
        site_id = str(plan.get("site_id") or "")
        plan_id = str(plan.get("plan_id") or "")
        root = workspace_root(self.data_dir, site_id, plan_id)
        plan["work_sha256"] = tree_hash(root / "work")
        plan["base_sha256"] = tree_hash(root / "base")
        plan["risk"] = review_risk(str(plan.get("slug") or ""), _disk_text(root / "work"))
        plan["check_generation"] = _generation(plan) + 1
        plan["checks"] = []
        plan["check_run"] = {}
        if plan.get("status") in {
            "tests_passed",
            "tests_failed",
            "checks_incomplete",
            "checks_running",
            "review_requested",
            "approved_for_staging",
        }:
            plan["status"] = "validated"
        write_diff(root, _unified(root))
        write_manifest(root, plan)
        self._replace(plan)
        return plan

    def _load_editable_locked(
        self, site_id: str, plan_id: str
    ) -> tuple[dict[str, Any] | None, WpOpsResult | None]:
        plan = self._row(site_id, plan_id)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(plan)
        if blocked is not None:
            return None, blocked
        if plan.get("status") in _NON_EDITABLE:
            return None, _fail(str(plan.get("error_class") or "PlanNotEditable"), "Planen kan inte redigeras.")
        return plan, None

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
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(actor, plan, "wp_review_refresh", True)
        return _ok("Arbetsytan uppdaterades.", plan_id=plan_id, site_id=site_id, status=plan["status"], risk=plan["risk"])

    def patch_file(
        self,
        site_id: str,
        plan_id: str,
        relative: str,
        old_text: str,
        new_text: str,
        *,
        actor: str,
        base_sha256: str | None = None,
    ) -> WpOpsResult:
        """Patch one unique string occurrence in an editable workspace file."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_patch")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                meta = patch_work_file(root, relative, old_text, new_text, base_sha256=base_sha256)
            except ReviewError as exc:
                return _fail(exc.error_class, exc.error_class)
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(actor, plan, "wp_review_patch", True, path=meta["path"], bytes=meta["byte_length"])
        return _ok(
            "Arbetsytan patchades.",
            plan_id=plan_id,
            site_id=site_id,
            path=meta["path"],
            before_sha256=meta["before_sha256"],
            after_sha256=meta["after_sha256"],
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
        )

    def create_file(
        self,
        site_id: str,
        plan_id: str,
        relative: str,
        content: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        """Create a new text file in an editable workspace."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_create_file")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                meta = create_work_file(root, relative, content)
            except ReviewError as exc:
                return _fail(exc.error_class, exc.error_class)
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(actor, plan, "wp_review_create_file", True, path=meta["path"], bytes=meta["byte_length"])
        return _ok(
            "Fil skapad i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            path=meta["path"],
            after_sha256=meta["after_sha256"],
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
        )

    def copy_from_parent(
        self,
        site_id: str,
        plan_id: str,
        relative: str,
        *,
        actor: str,
        parent_slug: str | None = None,
    ) -> WpOpsResult:
        """Copy one text file read-only from a parent theme into the child workspace."""
        denied = self._need(
            actor,
            Permission.WP_OPS_OPERATE,
            Permission.WP_OPS_ADMIN,
            action="wp_review_copy_parent",
        )
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            if plan.get("kind") != "theme":
                return _fail("InvalidTarget", "Endast teman har föräldratema.")
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
            except ReviewError as exc:
                return _fail(exc.error_class, exc.error_class)
            resolved_parent = _resolve_parent_theme(root, str(plan.get("slug") or ""), parent_slug)
            if not resolved_parent:
                return _fail("ParentThemeUnknown", "Föräldratemat är okänt.")
            site, missing = self._site(site_id)
            if missing is not None:
                return missing
            assert site is not None
            try:
                abs_path = resolve_file(site.files_root, "theme", resolved_parent, relative)
                raw_bytes = self._transport(site).read_bytes(abs_path)
            except PathRejected:
                return _fail("PathRejected", "Sökvägen avvisades.")
            except TransportError as exc:
                return _fail(exc.error_class, exc.error_class)
            if len(raw_bytes) > 65536:
                return _fail("FileTooLarge", "Filen är för stor.")
            if b"\x00" in raw_bytes:
                return _fail("NotText", "Filen är inte giltig UTF-8-text.")
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return _fail("NotText", "Filen är inte giltig UTF-8-text.")
            try:
                meta = create_work_file(root, relative, text)
            except ReviewError as exc:
                return _fail(exc.error_class, exc.error_class)
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(
            actor,
            plan,
            "wp_review_copy_parent",
            True,
            parent_slug=resolved_parent,
            path=meta["path"],
            bytes=meta["byte_length"],
        )
        return _ok(
            "Fil kopierad från föräldratemat.",
            plan_id=plan_id,
            site_id=site_id,
            parent_slug=resolved_parent,
            path=meta["path"],
            after_sha256=meta["after_sha256"],
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
        )

    def workspace_status(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        """Return workspace status, changed file list, and check summary."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_status")
        if denied is not None:
            return denied
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        self._expire_if_due(plan)
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return _fail("Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id, status="expired")
        if plan.get("status") == "not_resumable":
            return _fail(
                "WorkspaceNotResumable",
                "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                plan_id=plan_id,
                site_id=site_id,
                status="not_resumable",
            )
        try:
            changed = list_work_changes(workspace_root(self.data_dir, site_id, plan_id))
        except ReviewError:
            changed = []
        pub = self._public(plan)
        return _ok(
            "Arbetsytestatus.",
            plan_id=plan_id,
            site_id=site_id,
            kind=plan.get("kind"),
            slug=plan.get("slug"),
            status=plan.get("status"),
            risk=plan.get("risk"),
            theme_relationship=plan.get("theme_relationship"),
            base_sha256=plan.get("base_sha256"),
            work_sha256=plan.get("work_sha256"),
            changed_files=changed,
            check_run=pub["check_run"],
            checks=[
                {
                    "job": c.get("job"),
                    "status": c.get("status"),
                    "reason": c.get("reason", ""),
                    "executed": c.get("executed") is True,
                    "simulated": c.get("simulated") is True,
                }
                for c in pub["checks"]
            ],
            review_path=plan.get("review_path"),
            login_required=True,
        )

    def get(self, plan_id: str, *, site_id: str | None = None) -> dict[str, Any] | None:
        """Look up a single plan by id (optionally scoped to site_id)."""
        if not PLAN_ID.fullmatch(str(plan_id or "")):
            return None
        for row in self._load():
            if row.get("plan_id") != plan_id:
                continue
            row_site = str(row.get("site_id") or "")
            if site_id is not None and row_site != site_id:
                continue
            return self._row(row_site, plan_id)
        return None

    def list_plans(
        self,
        site_id: str | None = None,
        *,
        actor: str,
        status: str | None = None,
        include_terminal: bool = False,
    ) -> WpOpsResult:
        """List review workspaces, optionally filtered by site_id or status."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_list")
        if denied is not None:
            return denied
        if site_id is not None and site_id != "":
            site, missing = self._site(site_id)
            if missing is not None:
                return missing
            assert site is not None
            target_site = site.id
        else:
            target_site = None

        rows: list[dict[str, Any]] = []
        for plan in self._load():
            if target_site is not None and plan.get("site_id") != target_site:
                continue
            row_status = str(plan.get("status") or "")
            if status is not None and status != "":
                if row_status != status:
                    continue
            elif not include_terminal and (row_status in _TERMINAL or self._past(plan)):
                continue
            rows.append(self._view(plan, include_diff=False))
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return _ok("Arbetsytor.", site_id=target_site, plans=rows)

    def workspace_list_files(
        self, site_id: str, plan_id: str, rel_dir: str = "", *, actor: str
    ) -> WpOpsResult:
        """List files inside a review workspace's ``work/`` tree."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_list_files")
        if denied is not None:
            return denied
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(plan)
        if blocked is not None:
            return blocked
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
            listed = list_work_files(root, rel_dir)
        except ReviewError as exc:
            return _fail(exc.error_class, str(exc))
        return _ok(
            "Filer i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            review_path=plan.get("review_path"),
            login_required=True,
            **listed,
        )

    def workspace_read_file(
        self,
        site_id: str,
        plan_id: str,
        rel_path: str,
        *,
        actor: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> WpOpsResult:
        """Read one text file from a review workspace's ``work/`` tree."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_read_file")
        if denied is not None:
            return denied
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(plan)
        if blocked is not None:
            return blocked
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
            loaded = read_work_file(root, rel_path, start_line=start_line, end_line=end_line)
        except ReviewError as exc:
            return _fail(exc.error_class, str(exc))
        self._audit(
            actor,
            plan,
            "wp_review_read_file",
            True,
            path=loaded["path"],
            bytes=loaded["byte_length"],
        )
        return _ok(
            "Fil läst från arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            review_path=plan.get("review_path"),
            login_required=True,
            **loaded,
        )

    def workspace_diff(
        self,
        site_id: str,
        plan_id: str,
        rel_path: str | None = None,
        *,
        actor: str,
    ) -> WpOpsResult:
        """Compute bounded unified diff between ``base/`` and ``work/``."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_diff")
        if denied is not None:
            return denied
        plan = self._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(plan)
        if blocked is not None:
            return blocked
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
            diff_data = work_diff(root, rel_path)
        except ReviewError as exc:
            return _fail(exc.error_class, str(exc))
        return _ok(
            "Diff i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            review_path=plan.get("review_path"),
            login_required=True,
            **diff_data,
        )

    def workspace_apply_changes(
        self,
        site_id: str,
        plan_id: str,
        changes: list[dict[str, Any]],
        *,
        actor: str,
        allow_create: bool = True,
        allow_delete: bool = True,
    ) -> WpOpsResult:
        """Atomically apply multi-file edit/create/delete/revert operations in ``work/``."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_apply_changes")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                res = apply_work_changes(
                    root,
                    changes,
                    allow_create=allow_create,
                    allow_delete=allow_delete,
                )
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc))
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(actor, plan, "wp_review_apply_changes", True, count=res["count"])
        return _ok(
            "Ändringarna sparades i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            applied=res["applied"],
            count=res["count"],
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
        )

    def workspace_bulk_replace(
        self,
        site_id: str,
        plan_id: str,
        find: str,
        replace: str,
        *,
        actor: str,
        mode: str = "literal",
        extensions: list[str] | None = None,
        path_prefix: str | None = None,
        dry_run: bool = False,
    ) -> WpOpsResult:
        """Preview or apply literal/regex search-and-replace across ``work/`` files."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_bulk_replace")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                res = bulk_replace_work(
                    root,
                    find,
                    replace,
                    mode=mode,
                    extensions=extensions,
                    path_prefix=path_prefix,
                    dry_run=dry_run,
                )
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc))
            if not dry_run and res["files_changed"] > 0:
                plan = self._refresh_locked(plan, actor=actor)
                self._audit(
                    actor,
                    plan,
                    "wp_review_bulk_replace",
                    True,
                    files_changed=res["files_changed"],
                    total_matches=res["total_matches"],
                )
        msg = "Förhandsgranskning av massersättning." if dry_run else "Massersättning genomförd i arbetsytan."
        return _ok(
            msg,
            plan_id=plan_id,
            site_id=site_id,
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
            **res,
        )

    def workspace_scaffold(
        self,
        site_id: str,
        plan_id: str,
        template: str,
        *,
        actor: str,
        component_slug: str | None = None,
    ) -> WpOpsResult:
        """Scaffold child theme, site plugin, or WooCommerce HPOS compatibility files."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_scaffold")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            slug = component_slug or str(plan.get("work_slug") or plan.get("slug") or "") or None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                res = scaffold_work(root, template, component_slug=slug)
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc))
            if res["created"] or res["updated"]:
                plan = self._refresh_locked(plan, actor=actor)
        self._audit(
            actor,
            plan,
            "wp_review_scaffold",
            True,
            template=template,
            created=res["created"],
            updated=res["updated"],
            skipped=res["skipped"],
        )
        return _ok(
            "Mall skapad i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
            **res,
        )

    def workspace_theme_json(
        self,
        site_id: str,
        plan_id: str,
        operations: list[dict[str, Any]],
        *,
        actor: str,
    ) -> WpOpsResult:
        """Apply validated JSON pointer operations to ``work/theme.json``."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_theme_json")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            if plan.get("kind") != "theme":
                return _fail("InvalidTarget", "theme.json gäller endast teman.")
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc))

            seed_json: str | None = None
            if (
                not (root / "work" / "theme.json").exists()
                and not (root / "base" / "theme.json").exists()
                and plan.get("theme_relationship") == "child"
            ):
                parent_slug = _resolve_parent_theme(root, str(plan.get("slug") or ""), None)
                if parent_slug:
                    site, missing = self._site(site_id)
                    if missing is None and site is not None:
                        try:
                            abs_parent = resolve_file(site.files_root, "theme", parent_slug, "theme.json")
                            raw = self._transport(site).read_bytes(abs_parent)  # type: ignore[attr-defined]
                            if isinstance(raw, bytes) and len(raw) <= 65536 and b"\x00" not in raw:
                                seed_json = raw.decode("utf-8")
                        except (PathRejected, TransportError, UnicodeDecodeError, AttributeError):
                            seed_json = None

            try:
                res = theme_json_work(root, operations, seed_json=seed_json)
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc))
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(
            actor,
            plan,
            "wp_review_theme_json",
            True,
            changed_paths=res["changed_paths"],
            version=res["version"],
        )
        return _ok(
            "theme.json uppdaterades i arbetsytan.",
            plan_id=plan_id,
            site_id=site_id,
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
            **res,
        )

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
            policy = requirements(kind, files, risk)
            generation = _generation(plan) + 1
            run_id = "cr-" + secrets.token_hex(6)
            snapshot = {
                "run_id": run_id,
                "site_id": site_id,
                "plan_id": plan_id,
                "base_sha256": tree_hash(root / "base"),
                "work_sha256": tree_hash(root / "work"),
                "policy": policy,
                "profile": PROFILE,
                "profile_version": PROFILE_VERSION,
                "started_at": self._now().isoformat(),
                "finished_at": "",
                "published": False,
            }
            plan["check_generation"] = generation
            plan["status"] = "checks_running"
            plan["check_run"] = snapshot
            self._replace(plan)
            artifact_dir = root / "artifacts"
        try:
            if sandbox_enabled():
                results = self._run_sandbox_catalog(plan, root, kind, files, risk, artifact_dir)
            else:
                results = run_catalog(kind, files, risk, artifact_dir=artifact_dir)
            crashed = False
        except Exception:
            results = []
            crashed = True
        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _fail("UnknownPlan", "Okänd plan.")
            if plan.get("status") == "not_resumable":
                return _fail(
                    "WorkspaceNotResumable",
                    "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                    plan_id=plan_id,
                    status="not_resumable",
                )
            if plan.get("status") == "expired" or self._past(plan):
                if plan.get("status") != "expired":
                    self._mark_expired(plan)
                return _fail("Expired", "Planen har gått ut.", plan_id=plan_id, status="expired")
            if _generation(plan) != generation:
                return _fail(
                    "StaleRun",
                    "En nyare ändring finns. Den här körningen skrevs inte.",
                    plan_id=plan_id,
                    site_id=site_id,
                    status=plan.get("status"),
                )
            root = workspace_root(self.data_dir, site_id, plan_id)
            base_now = tree_hash(root / "base")
            work_now = tree_hash(root / "work")
            stamped = stamp_run(
                results,
                run_id=run_id,
                site_id=site_id,
                plan_id=plan_id,
                base_sha256=str(snapshot["base_sha256"]),
                work_sha256=str(snapshot["work_sha256"]),
                policy=policy,
                profile=PROFILE,
                profile_version=PROFILE_VERSION,
            )
            bind_artifacts(artifact_dir, stamped, snapshot)
            moved = base_now != snapshot["base_sha256"] or work_now != snapshot["work_sha256"]
            status = "checks_incomplete" if crashed or moved else suite_status(kind, files, risk, stamped)
            published = False
            if status == "tests_passed" and not moved and not crashed:
                candidate = {**snapshot, "published": True, "finished_at": self._now().isoformat()}
                if run_is_publishable(kind, files, risk, stamped, candidate):
                    published = True
                    snapshot = candidate
                else:
                    status = "checks_incomplete"
            if not published:
                snapshot = {**snapshot, "published": False, "finished_at": self._now().isoformat()}
            plan["checks"] = stamped
            plan["status"] = status
            plan["check_run"] = snapshot
            plan["base_sha256"] = base_now
            plan["work_sha256"] = work_now
            plan["error_class"] = "RevisionChanged" if moved else ""
            write_diff(root, _unified(root))
            write_manifest(root, plan)
            self._replace(plan)
        for row in results:
            self._audit(
                actor,
                plan,
                "wp_review_check",
                row.get("status") == "passed",
                job=row.get("job"),
                outcome=row.get("status"),
                run_id=run_id,
            )
        screenshots = _list_artifact_pngs(artifact_dir)
        return _ok(
            "Kontrollerna är bokförda.",
            plan_id=plan_id,
            site_id=site_id,
            status=status,
            checks=stamped,
            check_run=snapshot,
            screenshots=screenshots,
            screenshot_paths=[str(artifact_dir / name) for name in screenshots],
        )

    def _run_sandbox_catalog(
        self,
        plan: dict[str, Any],
        root: Path,
        kind: str,
        files: list[tuple[str, str]],
        risk: str,
        artifact_dir: Path,
    ) -> list[dict[str, Any]]:
        site_id = str(plan.get("site_id") or "")
        plan_id = str(plan.get("plan_id") or "")
        slug = str(plan.get("slug") or "")
        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return run_catalog(kind, files, risk, artifact_dir=artifact_dir)
        try:
            if kind == "theme":
                from ecom_ops.integrations.wp_browser_smoke import run_vertical_theme_render

                parent_theme_slug: str | None = None
                parent_theme_dir: Path | None = None
                if plan.get("theme_relationship") == "child" or (root / "base" / "style.css").is_file():
                    resolved_parent = _resolve_parent_theme(root, slug, None)
                    if resolved_parent:
                        transport = self._transport(site)
                        if isinstance(transport, LocalTree):
                            themes_dir = (transport.local_root / "wp-content" / "themes").resolve()
                            candidate = (themes_dir / resolved_parent).resolve()
                            if (
                                candidate.parent == themes_dir
                                and not candidate.is_symlink()
                                and candidate.is_dir()
                            ):
                                parent_theme_slug = resolved_parent
                                parent_theme_dir = candidate
                kwargs: dict[str, Any] = {
                    "site": site,
                    "site_id": site_id,
                    "plan_id": plan_id,
                    "slug": slug,
                    "base_dir": root / "base",
                    "work_dir": root / "work",
                    "artifact_dir": artifact_dir,
                    "risk": risk,
                }
                if parent_theme_slug is not None and parent_theme_dir is not None:
                    kwargs["parent_theme_slug"] = parent_theme_slug
                    kwargs["parent_theme_dir"] = parent_theme_dir
                outcome = run_vertical_theme_render(**kwargs)
                return list(outcome["checks"])
            if kind == "plugin":
                from ecom_ops.integrations.wp_browser_smoke import run_vertical_plugin_smoke

                outcome = run_vertical_plugin_smoke(
                    site=site,
                    site_id=site_id,
                    plan_id=plan_id,
                    slug=slug,
                    base_dir=root / "base",
                    work_dir=root / "work",
                    artifact_dir=artifact_dir,
                    risk=risk,
                )
                return list(outcome["checks"])
        except ReviewError:
            return run_catalog(kind, files, risk, artifact_dir=artifact_dir)
        return run_catalog(kind, files, risk, artifact_dir=artifact_dir)

    def artifact_file(
        self, site_id: str, plan_id: str, name: str, *, actor: str
    ) -> tuple[Path | None, WpOpsResult | None]:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_artifact")
        if denied is not None:
            return None, denied
        if not PLAN_ID.fullmatch(plan_id) or not _ARTIFACT_PNG.fullmatch(name):
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        plan = self._row(site_id, plan_id)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        try:
            artifact_dir = (workspace_root(self.data_dir, site_id, plan_id) / "artifacts").resolve()
        except ReviewError:
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        raw_candidate = artifact_dir / name
        if raw_candidate.is_symlink():
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        candidate = raw_candidate.resolve()
        if candidate.parent != artifact_dir or candidate.is_symlink() or not candidate.is_file():
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        try:
            with candidate.open("rb") as fh:
                magic = fh.read(len(_PNG_MAGIC))
        except OSError:
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        if magic != _PNG_MAGIC:
            return None, _fail("ArtifactNotFound", "Okänd artefakt.")
        return candidate, None

    def list_artifacts(self, site_id: str, plan_id: str, *, actor: str) -> list[str]:
        if not self._holds(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT):
            return []
        if self._row(site_id, plan_id) is None:
            return []
        try:
            artifact_dir = workspace_root(self.data_dir, site_id, plan_id) / "artifacts"
        except ReviewError:
            return []
        return _list_artifact_pngs(artifact_dir)

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
                return _fail(
                    "TestsNotPassed",
                    "Kontrollerna är inte godkända.",
                    plan_id=plan_id,
                    status=plan.get("status"),
                )
            if plan.get("status") != "tests_passed":
                return _fail(
                    "EvidenceMissing",
                    "Äldre stagingflagga öppnar inte grinden.",
                    plan_id=plan_id,
                    status=plan.get("status"),
                )
            evidence = self._require_current_evidence(plan)
            if evidence is not None:
                return evidence
            root = workspace_root(self.data_dir, site_id, plan_id)
            diff_ops = _diff_ops(root)
            if not diff_ops:
                return _fail("NoChange", "Arbetsytan matchar basen.", plan_id=plan_id)
            new_component = plan.get("theme_relationship") in {"no_child", "site_plugin"}
            actions: list[tuple[str, str, str, bytes | None]] = []
            notes: list[dict[str, Any]] = []
            for op, relative, data, in_base in diff_ops:
                try:
                    absolute = resolve_file(site.files_root, str(plan.get("kind")), str(plan.get("slug")), relative)
                except (PathRejected, ValueError):
                    return _fail("PathRejected", "Sökvägen avvisades.", plan_id=plan_id)
                if op == "delete":
                    try:
                        local_target = transport._local_checked(absolute)
                    except (PathRejected, ValueError):
                        return _fail("PathRejected", "Sökvägen avvisades.", plan_id=plan_id)
                    if local_target.is_dir():
                        return _fail("PathRejected", "Katalog kan inte raderas.", plan_id=plan_id)
                    actions.append(("delete", absolute, relative, None))
                    notes.append({"path": relative, "op": "deleted", "bytes": 0, "sha256": None})
                    continue

                assert data is not None
                exists_on_staging = True
                try:
                    transport.read_bytes(absolute)
                except TransportError as exc:
                    if exc.error_class == "FileMissing":
                        exists_on_staging = False
                    else:
                        return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)
                except ValueError:
                    return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)

                if exists_on_staging:
                    actions.append(("write", absolute, relative, data))
                    notes.append({"path": relative, "op": "updated", "bytes": len(data), "sha256": _sha(data)})
                elif in_base and not new_component:
                    return _fail("FileMissing", "Filen finns inte på mockträdet.", plan_id=plan_id)
                else:
                    actions.append(("create", absolute, relative, data))
                    notes.append({"path": relative, "op": "created", "bytes": len(data), "sha256": _sha(data)})

            for action, absolute, _relative, data in actions:
                if action == "write":
                    assert data is not None
                    transport.write_bytes(absolute, data)
                elif action == "create":
                    assert data is not None
                    transport.create_bytes(absolute, data, overwrite=False)
                elif action == "delete":
                    local_target = transport._local_checked(absolute)
                    if local_target.is_file():
                        backup = local_target.with_name(local_target.name + ".azom-bak")
                        backup.write_bytes(local_target.read_bytes())
                        local_target.unlink()
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
            evidence = self._require_current_evidence(plan)
            if evidence is not None:
                return evidence
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
            running = record.get("state") == "running" and record.get("engine") not in {None, "unavailable"}
        self._audit(actor, plan, "wp_review_preview", running, outcome=record.get("state"))
        if not running:
            return _fail(
                "PreviewBlocked",
                "Förhandsvisningen startades inte.",
                plan_id=plan_id,
                site_id=site_id,
                status=plan.get("status"),
                sandbox=record,
            )
        return _ok(
            "Förhandsvisningen är bokförd.",
            plan_id=plan_id,
            site_id=site_id,
            sandbox=record,
            status=plan["status"],
        )

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
                if status not in {"expired", "not_resumable", "rejected", "fork_not_approved"}:
                    try:
                        root = workspace_root(self.data_dir, site_id, plan_id)
                    except ReviewError:
                        root = None
                    missing_tree = (
                        root is None
                        or not root.is_dir()
                        or not (root / "base").is_dir()
                        or not (root / "work").is_dir()
                    )
                    if missing_tree:
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

    def _require_current_evidence(self, plan: dict[str, Any]) -> WpOpsResult | None:
        plan_id = str(plan.get("plan_id") or "")
        site_id = str(plan.get("site_id") or "")
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class, plan_id=plan_id)
        if not (root / "base").is_dir() or not (root / "work").is_dir():
            return _fail("WorkspaceNotResumable", "Arbetsytan saknas.", plan_id=plan_id, status="not_resumable")
        base_now = tree_hash(root / "base")
        work_now = tree_hash(root / "work")
        if base_now != plan.get("base_sha256") or work_now != plan.get("work_sha256"):
            return _fail("RevisionStale", "Revisionen har ändrats.", plan_id=plan_id, site_id=site_id)
        files = _text_files(root / "work")
        kind = str(plan.get("kind") or "")
        risk = str(plan.get("risk") or "low")
        run = plan.get("check_run") if isinstance(plan.get("check_run"), dict) else {}
        if not run_is_publishable(kind, files, risk, list(plan.get("checks") or []), run):
            return _fail("EvidenceMissing", "Körningsbeviset räcker inte.", plan_id=plan_id, site_id=site_id)
        if run.get("base_sha256") != base_now or run.get("work_sha256") != work_now:
            return _fail("RevisionStale", "Körningen gäller en äldre revision.", plan_id=plan_id, site_id=site_id)
        if run.get("site_id") != site_id or run.get("plan_id") != plan_id:
            return _fail("EvidenceMismatch", "Körningen gäller en annan plan.", plan_id=plan_id, site_id=site_id)
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
            if row.get("evidence_class"):
                item["evidence_class"] = str(row.get("evidence_class"))[:40]
            if "runtime_proof" in row:
                item["runtime_proof"] = row.get("runtime_proof") is True
            for key in ("run_id", "site_id", "plan_id", "base_sha256", "work_sha256", "profile", "profile_version"):
                if row.get(key):
                    item[key] = str(row.get(key))[:80]
            policy = row.get("policy")
            if policy in {"required", "optional", "not_applicable"}:
                item["policy"] = policy
            if isinstance(row.get("findings"), list):
                item["findings"] = [str(part) for part in row["findings"]]
            checks.append(item)
        clean["checks"] = checks
        raw_run = plan.get("check_run") if isinstance(plan.get("check_run"), dict) else {}
        clean["check_run"] = {
            "run_id": str(raw_run.get("run_id") or "")[:32],
            "site_id": str(raw_run.get("site_id") or "")[:80],
            "plan_id": str(raw_run.get("plan_id") or "")[:32],
            "base_sha256": str(raw_run.get("base_sha256") or "")[:64],
            "work_sha256": str(raw_run.get("work_sha256") or "")[:64],
            "policy": public_policy(raw_run.get("policy")),
            "profile": str(raw_run.get("profile") or "")[:40],
            "profile_version": str(raw_run.get("profile_version") or "")[:20],
            "started_at": str(raw_run.get("started_at") or "")[:40],
            "finished_at": str(raw_run.get("finished_at") or "")[:40],
            "published": raw_run.get("published") is True,
        }
        clean["check_generation"] = _generation(plan)
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


def _list_artifact_pngs(artifact_dir: Path) -> list[str]:
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        return []
    resolved_dir = artifact_dir.resolve()
    names: list[str] = []
    try:
        entries = list(artifact_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not _ARTIFACT_PNG.fullmatch(entry.name) or entry.is_symlink():
            continue
        resolved = entry.resolve()
        if resolved.parent != resolved_dir or resolved.is_symlink() or not resolved.is_file():
            continue
        try:
            with resolved.open("rb") as fh:
                if fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC:
                    names.append(entry.name)
        except OSError:
            continue
    return sorted(names)


def _resolve_parent_theme(root: Path, child_slug: str, parent_slug: str | None) -> str:
    if parent_slug and SLUG.fullmatch(parent_slug) and parent_slug != child_slug:
        return parent_slug
    style_path = root / "base" / "style.css"
    if style_path.is_symlink() or not style_path.is_file():
        return ""
    try:
        style_text = style_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _TEMPLATE_HEADER.search(style_text)
    if match is None:
        return ""
    candidate = match.group(1).lower()
    if SLUG.fullmatch(candidate) and candidate != child_slug:
        return candidate
    return ""


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


def _generation(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("check_generation") or 0)
    except (TypeError, ValueError):
        return 0


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


def _diff_ops(root: Path) -> list[tuple[str, str, bytes | None, bool]]:
    base_map = dict(iter_files(root / "base"))
    work_map = dict(iter_files(root / "work"))
    ops: list[tuple[str, str, bytes | None, bool]] = []
    for relative in sorted(set(base_map) | set(work_map)):
        old = base_map.get(relative)
        new = work_map.get(relative)
        if new == old:
            continue
        if new is None:
            ops.append(("delete", relative, None, True))
        else:
            ops.append(("write", relative, new, old is not None))
    return ops


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
