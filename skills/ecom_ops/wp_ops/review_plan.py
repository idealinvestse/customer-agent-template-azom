"""Review-plan state. This module does not import a process runner.

Plan ids are ``wr-`` plus 12 hex digits. File bodies stay in the workspace.
Production deploy stays closed. ``restore()`` is never called.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import inspect
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.file_changes import (
    _normalize_css_for_check,
    is_safe_pure_css_file,
    production_deploy_block,
    risk_for,
)
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
    "variant_group",
    "forked_from",
    "preview_path",
    "focus_selector",
    "page_draft",
    "draft_page",
)
_VARIANT_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_TERMINAL = frozenset({"rejected", "denied", "expired", "fork_not_approved", "not_resumable"})
_NON_EDITABLE = _TERMINAL | frozenset(
    {"staged", "rolled_back", "production_review", "approved_for_production", "deployed", "verified"}
)
_STAGE_OK = frozenset({"tests_passed", "review_requested", "approved_for_staging"})
_UNREACHABLE = frozenset({"approved_for_production", "deployed", "verified"})
_ARTIFACT_PNG = re.compile(r"^(?:base|work|live|preview|change|focus|compare)-(?:desktop|mobile)\.png$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_BASELINE_REUSE_TTL_S = 300.0


def _clean_variant_group(raw: str | None) -> str:
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if _VARIANT_GROUP_RE.fullmatch(val) is None:
        raise ReviewError("InvalidToolArguments")
    return val


_SANDBOX_ONLY_FILES = frozenset({"page_draft.json", "product_edits.json"})
_FORK_BLOCKED_STATUSES = frozenset(
    {
        "draft",
        "denied",
        "rejected",
        "fork_not_approved",
        "rolled_back",
        "expired",
        "not_resumable",
    }
)
_PAGE_DRAFT_UNSAFE_RE = re.compile(
    r"<\?|<script\b|<iframe\b|<svg\b|<object\b|<embed\b|</style|"
    r"javascript:|vbscript:|expression\s*\(|url\s*\(|image-set\s*\(|"
    r"@import\b|behavior\s*:|-moz-binding\s*:|\bon[a-z0-9_-]+\s*=",
    re.I,
)


def review_risk(relative: str, text: str) -> str:
    """High-risk classifier for the review chain. ``risk_for`` stays unchanged."""
    clean = str(relative or "").strip().replace("\\", "/")
    if clean == "page_draft.json":
        raw_text = text or ""
        norm_text = _normalize_css_for_check(raw_text)
        return (
            "high"
            if (_PAGE_DRAFT_UNSAFE_RE.search(raw_text) or _PAGE_DRAFT_UNSAFE_RE.search(norm_text))
            else "low"
        )
    if risk_for(relative, text) == "high":
        return "high"
    parts = [part.lower() for part in clean.split("/")]
    if any(part in _ORDER_PARTS for part in parts):
        return "high"
    if is_safe_pure_css_file(relative, text):
        return "low"
    if _ORDER_RISK.search(relative or "") or _ORDER_RISK.search(text or ""):
        return "high"
    return "low"


def _tree_risk(slug: str, files: list[tuple[str, bytes]], summary: str = "") -> str:
    if review_risk(slug, summary) == "high":
        return "high"
    for rel, data in files:
        if review_risk(rel, data.decode("utf-8", errors="replace")) == "high":
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
        variant_group: str = "",
        base_plan_id: str = "",
    ) -> WpOpsResult:
        if base_plan_id and str(base_plan_id).strip():
            return self.fork(
                site_id,
                str(base_plan_id).strip(),
                actor=actor,
                summary=summary,
                variant_group=variant_group,
            )
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_open")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None:
            return missing
        if kind not in {"theme", "plugin"}:
            return _fail("InvalidTarget", "Ogiltig komponent.")
        try:
            clean_vg = _clean_variant_group(variant_group)
        except ReviewError as exc:
            return _fail(exc.error_class, "Ogiltig variant_group.")
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
        risk = _tree_risk(built["slug"], built["files"], summary)
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
            "variant_group": clean_vg,
            "forked_from": "",
            "preview_path": "",
            "focus_selector": "",
        }
        write_diff(root, "")
        write_manifest(root, plan)
        with _STORE:
            plans = self._load()
            for older in plans:
                if not self._same_target(older, plan) or older.get("status") in _TERMINAL:
                    continue
                if clean_vg and str(older.get("variant_group") or "") == clean_vg:
                    continue
                older["superseded_by"] = plan_id
            plans.append(self._public(plan))
            self._save(plans)
        self._audit(actor, plan, "wp_review_open", True, variant_group=clean_vg)
        return _ok(
            "Arbetsytan är skapad. Sajten skrevs inte.",
            plan_id=plan_id,
            site_id=site.id,
            status=plan["status"],
            risk=plan["risk"],
            theme_relationship=plan["theme_relationship"],
            variant_group=clean_vg,
            forked_from="",
            review_path=plan["review_path"],
            login_required=True,
            environment=site.environment,
        )

    def fork(
        self,
        site_id: str,
        source_plan_id: str,
        *,
        actor: str,
        summary: str = "",
        variant_group: str = "",
    ) -> WpOpsResult:
        """Clone an existing workspace's ``base/``, ``work/``, and ``origin/`` into a new ``wr-*``."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_fork")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return missing if missing is not None else _fail("UnknownSite", "Okänd webbplats.")
        try:
            clean_vg_req = _clean_variant_group(variant_group)
        except ReviewError as exc:
            return _fail(exc.error_class, "Ogiltig variant_group.")

        source = self._row(site.id, source_plan_id)
        if source is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(source, allow_superseded_read=False)
        if blocked is not None:
            return blocked
        source_status = str(source.get("status") or "")
        if source_status in _FORK_BLOCKED_STATUSES or not source_status:
            return _fail(
                "ForkRefused",
                "Endast en aktiv, validerad plan kan förgrenas.",
                plan_id=source_plan_id,
                status=source_status,
            )
        try:
            src_root = workspace_root(self.data_dir, site.id, source_plan_id)
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)
        if not (src_root / "base").is_dir() or not (src_root / "work").is_dir():
            return _fail("WorkspaceNotResumable", "Källarbetsytan saknas.")

        base_files = iter_files(src_root / "base")
        work_files = iter_files(src_root / "work")
        origin_files = iter_files(src_root / "origin") if (src_root / "origin").is_dir() else []

        clean_vg = clean_vg_req or str(source.get("variant_group") or "")
        clean_summary = str(summary or source.get("summary") or "")[:200]
        plan_id = new_plan_id()
        root = workspace_root(self.data_dir, site.id, plan_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "base").mkdir()
        (root / "work").mkdir()
        if base_files:
            write_tree(root / "base", base_files)
        if work_files:
            write_tree(root / "work", work_files)
        if origin_files:
            write_tree(root / "origin", origin_files)

        now = self._now()
        slug = str(source.get("slug") or "")
        risk = _tree_risk(slug, work_files, clean_summary)
        source_page_draft = source.get("page_draft")
        if not isinstance(source_page_draft, dict):
            source_page_draft = _load_page_draft(root / "work")
        plan = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": str(source.get("kind") or "theme"),
            "slug": slug,
            "work_slug": str(source.get("work_slug") or slug),
            "risk": risk,
            "status": "validated",
            "theme_relationship": str(source.get("theme_relationship") or "unknown"),
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
            "error_class": "",
            "superseded_by": "",
            "review_path": f"/wp/{site.id}/reviews/{plan_id}",
            "summary": clean_summary,
            "variant_group": clean_vg,
            "forked_from": source_plan_id,
            "preview_path": str(source.get("preview_path") or ""),
            "focus_selector": str(source.get("focus_selector") or ""),
            "page_draft": dict(source_page_draft) if isinstance(source_page_draft, dict) else None,
        }
        write_diff(root, _unified(root))
        write_manifest(root, plan)
        with _STORE, self._store_file_lock():
            plans = self._load()
            for older in plans:
                if not self._same_target(older, plan) or older.get("status") in _TERMINAL:
                    continue
                if clean_vg and str(older.get("variant_group") or "") == clean_vg:
                    continue
                older["superseded_by"] = plan_id
            plans.append(self._public(plan))
            self._save(plans)
        changed = list_work_changes(root)
        self._audit(
            actor,
            plan,
            "wp_review_fork",
            True,
            forked_from=source_plan_id,
            variant_group=clean_vg,
            changed_files=len(changed),
        )
        return _ok(
            "Ny arbetsyta klonades från tidigare förslag. Sajten skrevs inte.",
            plan_id=plan_id,
            site_id=site.id,
            forked_from=source_plan_id,
            variant_group=clean_vg,
            preview_path=plan["preview_path"],
            focus_selector=plan["focus_selector"],
            status=plan["status"],
            risk=plan["risk"],
            theme_relationship=plan["theme_relationship"],
            changed_files=changed,
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
        plan["risk"] = _tree_risk(str(plan.get("slug") or ""), iter_files(root / "work"))
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
                write_tree(root / "origin", [(relative, text.encode("utf-8"))])
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
        blocked = self._fresh(plan, allow_superseded_read=True)
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
        blocked = self._fresh(plan, allow_superseded_read=True)
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
        blocked = self._fresh(plan, allow_superseded_read=True)
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
        browser_row = next((r for r in results if isinstance(r, dict) and r.get("job") == "browser_smoke"), None)
        rep_sandbox = (
            isinstance(browser_row, dict)
            and browser_row.get("status") in {"passed", "failed"}
            and browser_row.get("reason") != "sandbox_not_representative"
        )
        sandbox_shots = (
            [n for n in _list_artifact_pngs(artifact_dir) if n.startswith(("base-", "work-"))]
            if rep_sandbox
            else []
        )
        preview_data: dict[str, Any] | None = None
        preview_error: str | None = None
        if kind == "theme":
            from ecom_ops.integrations.wp_live_preview import live_preview_enabled

            if live_preview_enabled():
                try:
                    pv = self.live_preview(site_id, plan_id, actor=actor, path="/")
                    if pv.ok:
                        preview_data = dict(pv.data or {})
                    else:
                        preview_error = str((pv.data or {}).get("error_class") or "LivePreviewFailed")
                except ReviewError as exc:
                    preview_error = exc.error_class
                except Exception:
                    preview_error = "LivePreviewFailed"

        screenshots: list[str] = []
        if preview_data:
            for name in preview_data.get("screenshots") or []:
                if isinstance(name, str) and name not in screenshots:
                    screenshots.append(name)
        for name in sandbox_shots:
            if name not in screenshots:
                screenshots.append(name)
        screenshots = screenshots[:4]
        out_extra: dict[str, Any] = {
            "plan_id": plan_id,
            "site_id": site_id,
            "status": status,
            "checks": stamped,
            "check_run": snapshot,
            "screenshots": screenshots,
            "screenshot_paths": [str(artifact_dir / name) for name in screenshots],
            "sandbox_representative": rep_sandbox,
        }
        if preview_data is not None:
            out_extra["preview"] = preview_data
            out_extra["preview_status"] = "ok"
            if not rep_sandbox:
                out_extra["preview_note"] = (
                    "Live-overlay-förhandsvisning lyckades med riktiga skärmdumpar från livesajten "
                    "(lokal PHP-sandlåda är inte representativ för temat)."
                )
            else:
                out_extra["preview_note"] = "Live-overlay-förhandsvisning lyckades."
        elif preview_error is not None:
            out_extra["preview_error"] = preview_error
            out_extra["preview_status"] = "failed"
            out_extra["preview_note"] = f"Live-overlay-förhandsvisning misslyckades ({preview_error})."
        else:
            out_extra["preview_status"] = "skipped"
            out_extra["preview_note"] = ""
        msg = (
            "Kontrollerna är bokförda. Live-overlay-förhandsvisning skapades med riktiga skärmdumpar "
            "från livesajten (lokal PHP-sandlåda är inte representativ för temat)."
            if preview_data is not None and not rep_sandbox
            else "Kontrollerna är bokförda."
        )
        return _ok(msg, **out_extra)

    def _reusable_live_baseline_dir(
        self,
        site_id: str,
        plan: dict[str, Any],
        *,
        clean_path: str,
        full_page: bool,
    ) -> Path | None:
        """Find a recent artifact dir (same plan or same variant_group) with matching live-*.png."""
        now_ts = self._now().timestamp()
        candidates: list[str] = [str(plan.get("plan_id") or "")]
        vg = str(plan.get("variant_group") or "").strip()
        if vg:
            for row in self._load():
                if (
                    row.get("site_id") == site_id
                    and str(row.get("variant_group") or "") == vg
                    and row.get("plan_id") not in candidates
                ):
                    candidates.append(str(row.get("plan_id") or ""))
        for cand_id in candidates:
            if not PLAN_ID.fullmatch(cand_id):
                continue
            try:
                art = workspace_root(self.data_dir, site_id, cand_id) / "artifacts"
            except ReviewError:
                continue
            pv_json = art / "preview.json"
            ld = art / "live-desktop.png"
            lm = art / "live-mobile.png"
            if (
                pv_json.is_symlink()
                or not pv_json.is_file()
                or ld.is_symlink()
                or not ld.is_file()
                or lm.is_symlink()
                or not lm.is_file()
            ):
                continue
            try:
                age = now_ts - ld.stat().st_mtime
                if age < -5.0 or age > _BASELINE_REUSE_TTL_S:
                    continue
                meta = json.loads(pv_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if (
                isinstance(meta, dict)
                and str(meta.get("path") or "") == clean_path
                and bool(meta.get("full_page")) is bool(full_page)
            ):
                return art
        return None

    def build_overlay_html(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        path: str = "/",
        use_cache: bool = True,
    ) -> tuple[dict[str, Any] | None, WpOpsResult | None]:
        """Build in-memory overlay HTML for an editable or superseded workspace without writing prod."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_live_preview")
        if denied is not None:
            return None, denied
        from urllib.parse import urlparse

        from ecom_ops.integrations.wp_live_preview import (
            _STATIC_ASSET_HOSTS,
            _allowed_site_hosts,
            fetch_live_html,
            live_preview_enabled,
            validate_preview_path,
        )
        from ecom_ops.wp_ops.preview_overlay import apply_overlay, compose_standalone_page

        if not live_preview_enabled():
            return None, _fail("LivePreviewDisabled", "Live-förhandsvisning är inte aktiverad.")
        plan = self._row(site_id, plan_id)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        blocked = self._fresh(plan, allow_superseded_read=True)
        if blocked is not None:
            return None, blocked
        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return None, (missing if missing is not None else _fail("UnknownSite", "Okänd webbplats."))
        try:
            root = workspace_root(self.data_dir, site_id, plan_id)
            work_dir = root / "work"
            base_dir = root / "base"
            product_edits = _load_product_edits(work_dir)
            page_draft = _load_page_draft(work_dir)
            raw_path_clean = "" if path is None else str(path).strip()

            use_standalone = False
            if page_draft is not None:
                draft_slug = str(page_draft.get("slug") or "").strip("/")
                draft_shell = str(page_draft.get("shell_path") or "/").strip() or "/"
                draft_candidates = {"", "/", draft_shell, f"/{draft_slug}", f"/{draft_slug}/"}
                if raw_path_clean in draft_candidates:
                    use_standalone = True

            if use_standalone and page_draft is not None:
                shell_path = validate_preview_path(str(page_draft.get("shell_path") or "/"))
                fetch_kwargs: dict[str, Any] = {}
                if use_cache:
                    try:
                        sig = inspect.signature(fetch_live_html)
                        if "use_cache" in sig.parameters:
                            fetch_kwargs["use_cache"] = True
                    except (TypeError, ValueError):
                        pass
                fetched = fetch_live_html(site, shell_path, **fetch_kwargs)
                extra_css = _collect_extra_css_files(work_dir, base_dir, include_style_css=True)
                site_host = (urlparse(str(site.canonical_url or "")).hostname or "").lower()
                allowed_hosts = _allowed_site_hosts(site_host) | _STATIC_ASSET_HOSTS
                overlay = compose_standalone_page(
                    str(fetched.get("html") or ""),
                    title=str(page_draft.get("title") or "Ny sida"),
                    body_html=str(page_draft.get("body_html") or ""),
                    page_css=str(page_draft.get("page_css") or ""),
                    extra_css_files=extra_css,
                    allowed_hosts=allowed_hosts,
                    synthetic_banner="Förhandsvisning av ny sida i sandlåda med syntetisk exempeldata (inga ändringar på live)",
                )
                return (
                    {
                        "site": site,
                        "plan": plan,
                        "root": root,
                        "clean_path": shell_path,
                        "fetched": fetched,
                        "overlay": overlay,
                        "mode": "standalone_page",
                        "page_draft": page_draft,
                        "shell_path": shell_path,
                    },
                    None,
                )

            effective_path = path
            if effective_path is None or str(effective_path).strip() in {"", "/"}:
                if (
                    product_edits
                    and isinstance(product_edits.get("product_path"), str)
                    and str(product_edits.get("product_path")).strip()
                ):
                    effective_path = str(product_edits["product_path"]).strip()
                elif isinstance(plan.get("preview_path"), str) and str(plan.get("preview_path")).strip():
                    effective_path = str(plan["preview_path"]).strip()
            clean_path = validate_preview_path(effective_path)
            fetch_kwargs = {}
            if use_cache:
                try:
                    sig = inspect.signature(fetch_live_html)
                    if "use_cache" in sig.parameters:
                        fetch_kwargs["use_cache"] = True
                except (TypeError, ValueError):
                    pass
            fetched = fetch_live_html(site, clean_path, **fetch_kwargs)
        except ValueError:
            return None, _fail("HtmlTooLarge", "Sidan överskrider storleksgränsen.")
        except ReviewError as exc:
            return None, _fail(exc.error_class, exc.error_class)

        base_dir = root / "base"
        origin_dir = root / "origin"
        parent_slug: str | None = None
        parent_checked = False
        transport: Any = None
        changes: list[tuple[str, str, str]] = []
        for rel, after_bytes in iter_files(work_dir):
            base_file = base_dir / rel
            origin_file = origin_dir / rel
            if base_file.is_file() and not base_file.is_symlink():
                before_bytes = base_file.read_bytes()
            elif origin_file.is_file() and not origin_file.is_symlink():
                before_bytes = origin_file.read_bytes()
            elif plan.get("kind") == "theme" and site.files_root:
                if not parent_checked:
                    parent_checked = True
                    parent_slug = _resolve_parent_theme(root, str(plan.get("slug") or ""), None)
                    if parent_slug:
                        transport = self._transport(site)
                if not parent_slug or transport is None:
                    continue
                try:
                    abs_path = resolve_file(site.files_root, "theme", parent_slug, rel)
                    raw_parent = transport.read_bytes(abs_path)
                except (PathRejected, TransportError, OSError):
                    continue
                if len(raw_parent) > 65536 or b"\x00" in raw_parent:
                    continue
                before_bytes = raw_parent
            else:
                continue
            if before_bytes == after_bytes:
                continue
            changes.append(
                (
                    rel,
                    before_bytes.decode("utf-8", errors="replace"),
                    after_bytes.decode("utf-8", errors="replace"),
                )
            )

        child_css: str | None = None
        css_work = work_dir / "style.css"
        css_base = base_dir / "style.css"
        if css_work.is_file() and not css_work.is_symlink():
            css_text = css_work.read_text(encoding="utf-8", errors="replace")
            base_css_text = (
                css_base.read_text(encoding="utf-8", errors="replace")
                if css_base.is_file() and not css_base.is_symlink()
                else ""
            )
            if plan.get("theme_relationship") in {"no_child", "child"} and (
                css_text != base_css_text or len(base_css_text) < 200
            ):
                child_css = css_text

        extra_css_files = _collect_extra_css_files(work_dir, base_dir, include_style_css=False)
        try:
            overlay = apply_overlay(
                str(fetched.get("html") or ""),
                changes,
                child_css=child_css,
                extra_css_files=extra_css_files,
                product_edits=product_edits,
            )
        except ValueError:
            return None, _fail("HtmlTooLarge", "Sidan överskrider storleksgränsen.")

        return (
            {
                "site": site,
                "plan": plan,
                "root": root,
                "clean_path": clean_path,
                "fetched": fetched,
                "overlay": overlay,
                "mode": "live_overlay",
                "page_draft": None,
            },
            None,
        )

    def live_preview(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        path: str = "/",
        focus_section: str | None = None,
        focus_selector: str | None = None,
        focus_text: str | None = None,
        full_page: bool = False,
        reuse_live_baseline: bool = True,
    ) -> WpOpsResult:
        """Render a read-only live HTML overlay preview for a workspace without writing prod."""
        from ecom_ops.bot.dashboard_links import sandbox_preview_url
        from ecom_ops.integrations.wp_live_preview import (
            build_interactive_sandbox_html,
            capture_preview,
            validate_focus_options,
        )
        from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets

        try:
            clean_section, clean_selector, clean_text, clean_full_page = validate_focus_options(
                focus_section=focus_section,
                focus_selector=focus_selector,
                focus_text=focus_text,
                full_page=full_page,
            )
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)

        built, err = self.build_overlay_html(
            site_id,
            plan_id,
            actor=actor,
            path=path,
            use_cache=bool(reuse_live_baseline),
        )
        if err is not None or built is None:
            return err if err is not None else _fail("LivePreviewFailed", "Förhandsvisningen misslyckades.")

        site = built["site"]
        plan = built["plan"]
        root = built["root"]
        clean_path = built["clean_path"]
        fetched = built["fetched"]
        overlay = built["overlay"]
        is_standalone = built.get("mode") == "standalone_page"
        page_draft = built.get("page_draft") if is_standalone and isinstance(built.get("page_draft"), dict) else None
        draft_summary: dict[str, Any] | None = None
        if page_draft is not None:
            draft_summary = {
                "slug": str(page_draft.get("slug") or ""),
                "title": str(page_draft.get("title") or ""),
                "page_type": str(page_draft.get("page_type") or "custom"),
                "shell_path": str(page_draft.get("shell_path") or "/"),
                "template_file": str(page_draft.get("template_file") or ""),
                "synthetic_data": True,
            }
            if not clean_selector and isinstance(page_draft.get("focus_selector"), str):
                raw_ds = page_draft["focus_selector"].strip()
                if raw_ds:
                    try:
                        _, clean_selector, _, _ = validate_focus_options(focus_selector=raw_ds)
                    except ReviewError:
                        pass

        artifact_dir = root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        reused_baseline = False
        baseline_dir: Path | None = None
        if reuse_live_baseline and not is_standalone:
            baseline_dir = self._reusable_live_baseline_dir(
                site_id,
                plan,
                clean_path=clean_path,
                full_page=clean_full_page,
            )
        saved_live_bytes: dict[str, bytes] = {}
        if baseline_dir is not None:
            try:
                ld_bytes = (baseline_dir / "live-desktop.png").read_bytes()
                lm_bytes = (baseline_dir / "live-mobile.png").read_bytes()
                if ld_bytes[:8] == _PNG_MAGIC and lm_bytes[:8] == _PNG_MAGIC:
                    saved_live_bytes["live-desktop.png"] = ld_bytes
                    saved_live_bytes["live-mobile.png"] = lm_bytes
                    reused_baseline = True
            except OSError:
                saved_live_bytes = {}
                reused_baseline = False

        for stale in list(artifact_dir.iterdir()):
            if stale.name.startswith(("live-", "preview-", "change-", "focus-", "compare-")) and stale.suffix.lower() == ".png":
                try:
                    if stale.is_file() or stale.is_symlink():
                        stale.unlink()
                except OSError:
                    pass

        if reused_baseline:
            for name, raw_png in saved_live_bytes.items():
                (artifact_dir / name).write_bytes(raw_png)

        revisions_arg: list[tuple[str, str]] = (
            [("preview", overlay.html)]
            if (reused_baseline or is_standalone)
            else [
                ("live", str(fetched.get("html") or "")),
                ("preview", overlay.html),
            ]
        )
        capture_extra: dict[str, Any] = {}
        try:
            cap_sig = inspect.signature(capture_preview)
            if is_standalone:
                if "preview_kind_override" in cap_sig.parameters:
                    capture_extra["preview_kind_override"] = "standalone_page"
                if "extra_preview_meta" in cap_sig.parameters and draft_summary is not None:
                    capture_extra["extra_preview_meta"] = {
                        "synthetic_data": True,
                        "page_draft": draft_summary,
                    }
            elif reused_baseline and "preview_kind_override" in cap_sig.parameters:
                capture_extra["preview_kind_override"] = "live_overlay"
        except (TypeError, ValueError):
            pass
        try:
            rendered = capture_preview(
                site,
                path=clean_path,
                artifact_dir=artifact_dir,
                revisions=revisions_arg,
                highlight_token=overlay.highlight_token,
                focus_section=clean_section,
                focus_selector=clean_selector,
                focus_text=clean_text,
                full_page=clean_full_page,
                target_url=str(fetched.get("final_url") or "") or None,
                **capture_extra,
            )
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)

        if is_standalone and draft_summary is not None:
            pv_json_path = artifact_dir / "preview.json"
            try:
                existing_meta = (
                    json.loads(pv_json_path.read_text(encoding="utf-8"))
                    if pv_json_path.is_file() and not pv_json_path.is_symlink()
                    else {}
                )
                if not isinstance(existing_meta, dict):
                    existing_meta = {}
                existing_meta["preview_kind"] = "standalone_page"
                existing_meta["synthetic_data"] = True
                existing_meta["page_draft"] = draft_summary
                pv_json_path.write_text(json.dumps(existing_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError, UnicodeError):
                pass

        if not is_standalone:
            live_shot = artifact_dir / "live-desktop.png"
            after_shot: Path | None = None
            for cand_name in ("preview-desktop.png", "focus-desktop.png", "change-desktop.png"):
                cand_path = artifact_dir / cand_name
                if cand_path.is_file() and not cand_path.is_symlink():
                    after_shot = cand_path
                    break
            if live_shot.is_file() and not live_shot.is_symlink() and after_shot is not None:
                try:
                    from ecom_ops.integrations.wp_live_preview import build_comparison_composite

                    build_comparison_composite(
                        [("Före (Live)", live_shot), (f"Efter ({plan_id})", after_shot)],
                        artifact_dir / "compare-desktop.png",
                    )
                except Exception:
                    pass

        preferred_order = (
            (
                "compare-desktop.png",
                "preview-desktop.png",
                "preview-mobile.png",
                "focus-desktop.png",
                "focus-mobile.png",
                "change-desktop.png",
                "change-mobile.png",
            )
            if is_standalone
            else (
                "compare-desktop.png",
                "change-desktop.png",
                "change-mobile.png",
                "focus-desktop.png",
                "focus-mobile.png",
                "live-desktop.png",
                "preview-desktop.png",
                "live-mobile.png",
                "preview-mobile.png",
            )
        )
        available = set(_list_artifact_pngs(artifact_dir))
        ordered_shots = [name for name in preferred_order if name in available]
        counts = {
            kind_name: sum(1 for h in overlay.hunks if h.kind == kind_name)
            for kind_name in ("applied", "unmatched", "php_skipped", "insert_unanchored")
        }
        secrets_list: list[str] = []
        try:
            bound_cred = bind_credential(site)
            if bound_cred.present and bound_cred.value:
                secrets_list.append(bound_cred.value)
        except Exception:
            pass

        display_path = f"/{draft_summary['slug'].strip('/')}/" if (is_standalone and draft_summary and draft_summary.get("slug")) else clean_path
        sandbox_html_kwargs: dict[str, Any] = {
            "site_canonical_url": str(site.canonical_url or ""),
            "site_id": site_id,
            "plan_id": plan_id,
            "path": display_path,
            "highlight_token": overlay.highlight_token,
            "focus_text": clean_text,
            "focus_selector": clean_selector,
        }
        if is_standalone:
            try:
                sb_sig = inspect.signature(build_interactive_sandbox_html)
                if "standalone_page" in sb_sig.parameters:
                    sandbox_html_kwargs["standalone_page"] = True
                if "synthetic_data" in sb_sig.parameters:
                    sandbox_html_kwargs["synthetic_data"] = True
            except (TypeError, ValueError):
                pass
        interactive_html = build_interactive_sandbox_html(overlay.html, **sandbox_html_kwargs)
        if secrets_list:
            scrubbed_html = scrub_secrets(interactive_html, secrets_list)
            if isinstance(scrubbed_html, str):
                interactive_html = scrubbed_html
        preview_html_path = artifact_dir / "preview.html"
        if preview_html_path.is_symlink():
            preview_html_path.unlink()
        preview_html_path.write_text(interactive_html, encoding="utf-8")

        # Persist preview_path, focus_selector, and page_draft summary on the plan record
        meta_changed = False
        if display_path and str(plan.get("preview_path") or "") != display_path:
            plan["preview_path"] = display_path
            meta_changed = True
        if clean_selector and str(plan.get("focus_selector") or "") != clean_selector:
            plan["focus_selector"] = clean_selector
            meta_changed = True
        if is_standalone and draft_summary is not None and plan.get("page_draft") != draft_summary:
            plan["page_draft"] = draft_summary
            meta_changed = True
        if meta_changed:
            with _STORE:
                self._replace(plan)
            write_manifest(root, plan)

        preview_route_path = f"/wp/{site_id}/reviews/{plan_id}/preview"
        preview_public_url = sandbox_preview_url(site_id, plan_id)

        hunk_excerpts = [
            scrub_secrets(
                {
                    "path": h.path,
                    "kind": h.kind,
                    "old_excerpt": h.old_excerpt[:160],
                    "new_excerpt": h.new_excerpt[:160],
                },
                secrets_list,
            )
            for h in overlay.hunks[:5]
        ]
        safe_token = scrub_secrets(overlay.highlight_token, secrets_list)
        safe_blocked = scrub_secrets(rendered.get("blocked_counts") or {}, secrets_list)
        safe_focus_target = scrub_secrets(rendered.get("focus_target"), secrets_list)
        raw_vh = rendered.get("visual_health")
        safe_visual_health = scrub_secrets(raw_vh, secrets_list) if isinstance(raw_vh, dict) else None
        focus_matched = bool(rendered.get("focus_matched"))
        self._audit(
            actor,
            plan,
            "wp_review_live_preview",
            True,
            live_calls=True,
            writes=False,
            path=display_path,
            applied=counts["applied"],
            unmatched=counts["unmatched"],
            php_skipped=counts["php_skipped"],
            css_bytes=overlay.injected_css_bytes,
            screenshots=len(ordered_shots),
            focus_matched=focus_matched,
            full_page=clean_full_page,
            preview_html_ready=True,
            reused_baseline=reused_baseline,
            standalone_page=is_standalone,
        )
        return _ok(
            "Förhandsvisning av ny sida skapad i sandlådan." if is_standalone else "Live-förhandsvisning skapad.",
            plan_id=plan_id,
            site_id=site_id,
            risk=str(plan.get("risk") or "low"),
            path=display_path,
            preview_path=preview_route_path,
            preview_url=preview_public_url,
            preview_html_ready=True,
            reused_live_baseline=reused_baseline,
            screenshots=ordered_shots,
            screenshot_paths=[str(artifact_dir / name) for name in ordered_shots],
            focus_matched=focus_matched,
            focus_target=safe_focus_target,
            full_page=clean_full_page,
            visual_health=safe_visual_health,
            synthetic_data=is_standalone,
            page_draft=draft_summary,
            overlay={
                "counts": counts,
                "injected_css_bytes": overlay.injected_css_bytes,
                "highlight_token": safe_token,
                "hunks": hunk_excerpts,
            },
            blocked_counts=safe_blocked,
            preview_kind="standalone_page" if is_standalone else "live_overlay",
            live_calls=True,
            note=(
                "Sandlådeförhandsvisning av ny sida inbäddad i sajtens riktiga skal (header/footer/CSS) med syntetisk exempeldata — inga ändringar på staging eller live."
                if is_standalone
                else "Förhandsvisning = live-HTML från sajten (GET, utan cookies) + statiska mallfragment/CSS från arbetsytan. PHP-logik körs inte."
            ),
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
                        parent_theme_slug = resolved_parent
                        transport = self._transport(site)
                        if isinstance(transport, LocalTree):
                            themes_dir = (transport.local_root / "wp-content" / "themes").resolve()
                            candidate = (themes_dir / resolved_parent).resolve()
                            if (
                                candidate.parent == themes_dir
                                and not candidate.is_symlink()
                                and candidate.is_dir()
                            ):
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
                if parent_theme_slug is not None:
                    kwargs["parent_theme_slug"] = parent_theme_slug
                    if parent_theme_dir is not None:
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

    def preview_html_file(
        self, site_id: str, plan_id: str, *, actor: str
    ) -> tuple[Path | None, WpOpsResult | None]:
        """Resolve ``artifacts/preview.html`` safely for ``WP_OPS_READ`` actors."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_preview_html")
        if denied is not None:
            return None, denied
        if not PLAN_ID.fullmatch(str(plan_id or "")):
            return None, _fail("UnknownPlan", "Okänd plan.")
        plan = self._row(site_id, plan_id)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        self._expire_if_due(plan)
        plan = self._row(site_id, plan_id)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return None, _fail("Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id)
        candidate = _safe_preview_html_path(self.data_dir, site_id, plan_id)
        if candidate is None:
            return None, _fail("PreviewNotFound", "Ingen sandbox-förhandsvisning har skapats ännu.")
        return candidate, None

    def has_preview_html(self, site_id: str, plan_id: str, *, actor: str) -> bool:
        """Check whether ``artifacts/preview.html`` exists for this plan without writing audit logs."""
        if not self._holds(actor, Permission.WP_OPS_READ):
            return False
        if not PLAN_ID.fullmatch(str(plan_id or "")):
            return False
        if self._row(site_id, plan_id) is None:
            return False
        return _safe_preview_html_path(self.data_dir, site_id, plan_id) is not None

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
            all_diff_ops = _diff_ops(root)
            if not all_diff_ops:
                return _fail("NoChange", "Arbetsytan matchar basen.", plan_id=plan_id)
            diff_ops = [entry for entry in all_diff_ops if entry[1] not in _SANDBOX_ONLY_FILES]
            if not diff_ops:
                return _fail(
                    "SandboxBlueprintOnly",
                    "Planen innehåller endast sandbox-utkast (page_draft.json / product_edits.json) och inga deployerbara tema-/plugin-filer.",
                    plan_id=plan_id,
                )
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

    def build_variant_comparison(
        self,
        site_id: str,
        plan_ids: list[str] | tuple[str, ...],
    ) -> Path | None:
        """Build a side-by-side ``compare-desktop.png`` across 2–4 previewed variants."""
        from ecom_ops.integrations.wp_live_preview import build_comparison_composite

        unique_ids: list[str] = []
        for pid in plan_ids:
            clean_pid = str(pid or "").strip()
            if clean_pid and PLAN_ID.fullmatch(clean_pid) and clean_pid not in unique_ids:
                unique_ids.append(clean_pid)
        if len(unique_ids) < 2:
            return None

        panels: list[tuple[str, Path]] = []
        latest_artifact_dir: Path | None = None
        for idx, pid in enumerate(unique_ids[:4], start=1):
            try:
                root = workspace_root(self.data_dir, site_id, pid)
            except ReviewError:
                continue
            art_dir = root / "artifacts"
            if not art_dir.is_dir():
                continue
            chosen: Path | None = None
            for cand in ("focus-desktop.png", "change-desktop.png", "preview-desktop.png", "work-desktop.png"):
                p = art_dir / cand
                if p.is_file() and not p.is_symlink():
                    chosen = p
                    break
            if chosen is not None:
                panels.append((f"Förslag {idx} ({pid})", chosen))
                latest_artifact_dir = art_dir
        if len(panels) < 2 or latest_artifact_dir is None:
            return None
        return build_comparison_composite(panels, latest_artifact_dir / "compare-desktop.png")

    def push_page_draft(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        base_sha256: str,
        work_sha256: str,
        client: Any = None,
    ) -> dict[str, Any]:
        """Push ``work/page_draft.json`` to WordPress strictly as ``status='draft'`` (Oscar-only HITL)."""
        from urllib.parse import urlparse

        from ecom_ops.integrations import wp_client_from_env
        from ecom_ops.integrations.wp_live_preview import _STATIC_ASSET_HOSTS, _allowed_site_hosts
        from ecom_ops.wp_ops.agent_tools import WpAgentTools
        from ecom_ops.wp_ops.file_changes import rest_reads_allowed
        from ecom_ops.wp_ops.preview_overlay import sanitize_page_draft_html

        def _err(error_class: str, message: str, **extra: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "error_class": error_class,
                "message": message,
                "plan_id": plan_id,
                "site_id": site_id,
                "executed": False,
                "live_calls": False,
                **extra,
            }

        denied = self._need(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_push_draft")
        if denied is not None:
            return _err(
                str((denied.data or {}).get("error_class") or "AccessDenied"),
                denied.message or "Endast Oscar (full_admin) får skicka WordPress-utkast.",
            )

        if not isinstance(base_sha256, str) or not base_sha256.strip() or not isinstance(work_sha256, str) or not work_sha256.strip():
            return _err("DigestMismatch", "Saknar låsta revision-hashar (base_sha256/work_sha256).")

        with self._locks(site_id, plan_id):
            plan = self._row(site_id, plan_id)
            if plan is None:
                return _err("UnknownPlan", "Okänd plan.")
            blocked = self._gate(plan, base_sha256, work_sha256)
            if blocked is not None:
                return _err(
                    str((blocked.data or {}).get("error_class") or "StalePlan"),
                    blocked.message,
                )
            site, missing = self._site(site_id)
            if missing is not None or site is None:
                return _err("UnknownSite", "Okänd webbplats.")
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
            except ReviewError as exc:
                return _err(exc.error_class, exc.error_class)

            draft = _load_page_draft(root / "work")
            if draft is None:
                return _err("DraftMissing", "Arbetsytan saknar work/page_draft.json.")

            title = " ".join(str(draft.get("title") or "").split()).strip()[:200]
            slug = str(draft.get("slug") or "").strip().strip("/").lower()
            raw_body = str(draft.get("body_html") or "")
            page_css = str(draft.get("page_css") or "")
            synthetic_ctx = draft.get("synthetic_context") if isinstance(draft.get("synthetic_context"), dict) else {}

            if not title or not raw_body.strip():
                return _err("InvalidDraft", "page_draft.json saknar titel eller innehåll.")
            if page_css.strip() and not is_safe_pure_css_file("page-draft.css", page_css):
                return _err("UnsafeCssRefused", "page_css innehåller otillåten CSS.")

            parsed_site = urlparse(str(site.canonical_url or ""))
            site_host = (parsed_site.hostname or "").strip().lower().rstrip(".")
            allowed_hosts = (_allowed_site_hosts(site_host) | _STATIC_ASSET_HOSTS) if site_host else _STATIC_ASSET_HOSTS
            sanitized_body = sanitize_page_draft_html(raw_body, allowed_hosts=allowed_hosts)
            if not sanitized_body.strip():
                return _err("InvalidDraft", "Sanerat sidutkast är tomt.")

            if any(
                WpAgentTools._contains_real_pii(field)
                for field in (title, raw_body, sanitized_body, page_css, synthetic_ctx)
            ):
                self._audit(actor, plan, "wp_review_push_draft", False, error_class="RealPiiRefused")
                return _err(
                    "RealPiiRefused",
                    "Sidutkastet innehåller riktig kunddata/PII och nekas innan publicering till WordPress.",
                )

            safe_css = re.sub(r"</style", r"<\/style", page_css.strip(), flags=re.IGNORECASE)
            style_block = f"<style>\n{safe_css}\n</style>\n" if safe_css else ""
            gutenberg_html = f"<!-- wp:html -->\n{style_block}{sanitized_body}\n<!-- /wp:html -->"

            use_mock = not rest_reads_allowed()
            live_calls = False
            if client is not None:
                wp_client = client
            else:
                try:
                    wp_client = wp_client_from_env(
                        base_url=str(site.canonical_url or "https://mock.local"),
                        use_mock=use_mock,
                    )
                    live_calls = not use_mock
                except Exception as exc:
                    self._audit(actor, plan, "wp_review_push_draft", False, error_class="WpClientUnavailable")
                    return _err("WpClientUnavailable", f"WordPress REST-klient kunde inte initieras: {type(exc).__name__}")

            try:
                created = wp_client.create_page(
                    title=title,
                    content=gutenberg_html,
                    slug=slug,
                    status="draft",
                )
            except ValueError as exc:
                self._audit(actor, plan, "wp_review_push_draft", False, error_class="DraftPushRejected")
                return _err("DraftPushRejected", str(exc))
            except Exception as exc:
                self._audit(actor, plan, "wp_review_push_draft", False, error_class="DraftPushFailed")
                return _err("DraftPushFailed", f"Kunde inte skapa utkast i WordPress ({type(exc).__name__}).")

            draft_page = {
                "id": int(created.get("id") or 0),
                "slug": str(created.get("slug") or slug),
                "title": str(created.get("title") or title),
                "status": "draft",
                "link": str(created.get("link") or ""),
                "edit_url": str(created.get("edit_url") or ""),
                "pushed_at": self._now().isoformat(),
                "pushed_by": actor,
            }
            plan["draft_page"] = draft_page
            self._replace(plan)
            write_manifest(root, plan)

        self._audit(
            actor,
            plan,
            "wp_review_push_draft",
            True,
            draft_page_id=draft_page["id"],
            slug=draft_page["slug"],
            draft_status="draft",
            live_calls=live_calls,
        )
        return {
            "ok": True,
            "plan_id": plan_id,
            "site_id": site_id,
            "status": "draft_pushed",
            "message": "Sidutkast skapat i WordPress (status='draft').",
            "draft_page": draft_page,
            "executed": True,
            "live_calls": live_calls,
        }

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

    @contextlib.contextmanager
    def _store_file_lock(self) -> Iterator[None]:
        """Advisory cross-process lock around ``wp_review_plans.json`` mutations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".json.lock")
        fh = lock_path.open("a+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.02)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass

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
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
        )
        payload = json.dumps({"plans": [self._public(row) for row in plans]}, ensure_ascii=False, indent=2)
        try:
            temporary.write_text(payload, encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(self.path)
                    return
                except PermissionError:
                    if attempt >= 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _replace(self, plan: dict[str, Any]) -> None:
        with _STORE, self._store_file_lock():
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

    def _fresh(self, plan: dict[str, Any], *, allow_superseded_read: bool = False) -> WpOpsResult | None:
        if plan.get("status") == "not_resumable":
            return _fail(
                "WorkspaceNotResumable",
                "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                plan_id=plan.get("plan_id"),
                status="not_resumable",
            )
        if plan.get("superseded_by") and not allow_superseded_read:
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


def _load_product_edits(work_dir: Path) -> dict[str, Any] | None:
    """Safely read ``work/product_edits.json`` if present (refusing symlinks and oversized files)."""
    raw_file = work_dir / "product_edits.json"
    if raw_file.is_symlink() or not raw_file.is_file():
        return None
    try:
        resolved_dir = work_dir.resolve()
        resolved_file = raw_file.resolve()
        if resolved_file.parent != resolved_dir or resolved_file.is_symlink():
            return None
        raw = resolved_file.read_bytes()
        if len(raw) > 65536 or b"\x00" in raw:
            return None
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_page_draft(work_dir: Path) -> dict[str, Any] | None:
    """Safely read ``work/page_draft.json`` if present (refusing symlinks and oversized files)."""
    raw_file = work_dir / "page_draft.json"
    if raw_file.is_symlink() or not raw_file.is_file():
        return None
    try:
        resolved_dir = work_dir.resolve()
        resolved_file = raw_file.resolve()
        if resolved_file.parent != resolved_dir or resolved_file.is_symlink():
            return None
        raw = resolved_file.read_bytes()
        if len(raw) > 65536 or b"\x00" in raw:
            return None
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _collect_extra_css_files(
    work_dir: Path,
    base_dir: Path,
    *,
    include_style_css: bool = False,
) -> list[tuple[str, str]]:
    """Collect modified or newly created ``.css`` files in ``work/`` (bounded to 10 files / 128KB)."""
    out: list[tuple[str, str]] = []
    total_bytes = 0
    for rel, after_bytes in iter_files(work_dir):
        if not rel.lower().endswith(".css"):
            continue
        if not include_style_css and rel == "style.css":
            continue
        base_file = base_dir / rel
        if base_file.is_file() and not base_file.is_symlink():
            try:
                if base_file.read_bytes() == after_bytes:
                    continue
            except OSError:
                pass
        if len(after_bytes) > 65536 or b"\x00" in after_bytes:
            continue
        if total_bytes + len(after_bytes) > 131072 or len(out) >= 10:
            break
        total_bytes += len(after_bytes)
        out.append((rel, after_bytes.decode("utf-8", errors="replace")))
    return out


def _safe_preview_html_path(data_dir: Path, site_id: str, plan_id: str) -> Path | None:
    """Resolve ``artifacts/preview.html`` strictly inside the plan workspace (refusing symlinks)."""
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return None
    if root.is_symlink() or not root.is_dir():
        return None
    raw_art = root / "artifacts"
    if raw_art.is_symlink() or not raw_art.is_dir():
        return None
    art_dir = raw_art.resolve()
    if art_dir.parent != root.resolve():
        return None
    raw_file = art_dir / "preview.html"
    if raw_file.is_symlink():
        return None
    candidate = raw_file.resolve()
    if candidate.parent != art_dir or candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        if candidate.stat().st_size > 5 * 1024 * 1024:
            return None
    except OSError:
        return None
    return candidate


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
