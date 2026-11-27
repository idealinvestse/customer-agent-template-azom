"""Slim ``ReviewPlans`` orchestrator and canonical public read-model API.

Delegates persistence to ``review.store``, checks to ``review.check_executor``,
previews to ``review.preview_coordinator``, and HITL deploys to ``review.deploy``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.file_paths import PathRejected, TransportError, resolve_file
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review import (
    check_executor,
    deploy,
    preview_coordinator,
)
from ecom_ops.wp_ops.review.risk import text_is_review_approval, tree_risk
from ecom_ops.wp_ops.review.runner import SandboxGate
from ecom_ops.wp_ops.review.scaffolds import (
    child_skeleton,
    mu_plugin_skeleton,
    plugin_skeleton,
    scaffold_work,
)
from ecom_ops.wp_ops.review.store import (
    _FORK_BLOCKED_STATUSES,
    _MAX_RESEARCH_IDS,
    _NON_EDITABLE,
    _PREVIEW_TTL,
    _TERMINAL,
    _TTL,
    PlanStore,
    _clean_variant_group,
    _fail,
    _generation,
    _ok,
    _parse_time,
    _prepared,
    _resolve_parent_theme,
    _unified,
    normalize_research_ids,
    public_plan,
    same_target,
)
from ecom_ops.wp_ops.review.workspace_io import (
    PLAN_ID,
    RESEARCH_ID,
    SLUG,
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
    copy_research_dir,
    iter_files,
    new_plan_id,
    place_work_file,
    restore_work_from_base,
    site_plugin_slug,
    tree_hash,
    workspace_root,
    write_diff,
    write_manifest,
    write_research_snapshot,
    write_tree,
)
from ecom_ops.wp_ops.review.workspace_ops import (
    apply_work_changes,
    bulk_replace_work,
    create_work_file,
    list_work_changes,
    list_work_files,
    patch_work_file,
    read_work_file,
    theme_json_work,
    work_diff,
)


class ReviewPlans:
    """Orchestrates isolated ``wr-*`` WordPress review workspaces and HITL gates."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        registry_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        transport_factory: Callable[[Site], object] | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.registry_path = registry_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transport_factory = transport_factory
        self.gate = SandboxGate()
        self.store = PlanStore(data_dir=self.data_dir, clock=self.clock, gate=self.gate)
        self.path = self.store.path

    def decide_text(self, text: str, *, actor: str) -> WpOpsResult:
        del actor
        approved = text_is_review_approval(text)
        return _fail("TextNotApproval", "Fritext ändrar inte granskningen.", changed=approved)

    def _new_plan_record(
        self,
        *,
        plan_id: str,
        site: Site,
        kind: str,
        slug: str,
        work_slug: str,
        risk: str,
        status: str,
        theme_relationship: str,
        root: Path,
        summary: str,
        variant_group: str,
        error_class: str = "",
        forked_from: str = "",
        preview_path: str = "",
        focus_selector: str = "",
        page_draft: dict[str, Any] | None = None,
        research_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        record: dict[str, Any] = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": kind,
            "slug": slug,
            "work_slug": work_slug,
            "risk": risk,
            "status": status,
            "theme_relationship": theme_relationship,
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
            "error_class": error_class,
            "superseded_by": "",
            "review_path": f"/wp/{site.id}/reviews/{plan_id}",
            "summary": summary,
            "variant_group": variant_group,
            "forked_from": forked_from,
            "preview_path": preview_path,
            "focus_selector": focus_selector,
            "research_ids": normalize_research_ids(research_ids),
        }
        if page_draft is not None:
            record["page_draft"] = dict(page_draft)
        return record

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
        if missing is not None or site is None:
            return missing if missing is not None else _fail("UnknownSite", "Okänd webbplats.")
        if kind not in {"theme", "plugin", "mu_plugin"}:
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
        plan = self._new_plan_record(
            plan_id=plan_id,
            site=site,
            kind=kind,
            slug=built["slug"],
            work_slug=built["slug"],
            risk=tree_risk(built["slug"], built["files"], summary),
            status=built["status"],
            theme_relationship=built["theme_relationship"],
            root=root,
            summary=summary,
            variant_group=clean_vg,
            error_class=built["error_class"],
        )
        write_diff(root, "")
        write_manifest(root, plan)
        self.store.append_and_supersede(
            plan,
            variant_group=clean_vg,
            save_fn=self._save,
            file_lock_fn=self._store_file_lock,
        )
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
            research_ids=plan["research_ids"],
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
        try:
            copy_research_dir(src_root, root)
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)

        slug = str(source.get("slug") or "")
        source_research_ids = normalize_research_ids(source.get("research_ids"))
        source_page_draft = source.get("page_draft")
        if not isinstance(source_page_draft, dict):
            source_page_draft = preview_coordinator._load_page_draft(root / "work")
        plan = self._new_plan_record(
            plan_id=plan_id,
            site=site,
            kind=str(source.get("kind") or "theme"),
            slug=slug,
            work_slug=str(source.get("work_slug") or slug),
            risk=tree_risk(slug, work_files, clean_summary),
            status="validated",
            theme_relationship=str(source.get("theme_relationship") or "unknown"),
            root=root,
            summary=clean_summary,
            variant_group=clean_vg,
            forked_from=source_plan_id,
            preview_path=str(source.get("preview_path") or ""),
            focus_selector=str(source.get("focus_selector") or ""),
            page_draft=source_page_draft if isinstance(source_page_draft, dict) else None,
            research_ids=source_research_ids,
        )
        write_diff(root, _unified(root))
        write_manifest(root, plan)
        self.store.append_and_supersede(
            plan,
            variant_group=clean_vg,
            save_fn=self._save,
            file_lock_fn=self._store_file_lock,
        )
        changed = list_work_changes(root)
        self._audit(
            actor,
            plan,
            "wp_review_fork",
            True,
            forked_from=source_plan_id,
            variant_group=clean_vg,
            changed_files=len(changed),
            research_count=len(plan["research_ids"]),
        )
        return _ok(
            "Ny arbetsyta klonades från tidigare förslag. Sajten skrevs inte.",
            plan_id=plan_id,
            site_id=site.id,
            forked_from=source_plan_id,
            variant_group=clean_vg,
            preview_path=plan["preview_path"],
            focus_selector=plan["focus_selector"],
            research_ids=plan["research_ids"],
            status=plan["status"],
            risk=plan["risk"],
            theme_relationship=plan["theme_relationship"],
            changed_files=changed,
            review_path=plan["review_path"],
            login_required=True,
            environment=site.environment,
        )

    def place(self, site_id: str, plan_id: str, relative: str, data: bytes, *, actor: str) -> WpOpsResult:
        def _do(_plan: dict[str, Any], root: Path):
            place_work_file(root, relative, data)
            return {"audit": {"path": relative, "bytes": len(data)}, "public": {}}, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_place",
            message="Arbetsytan uppdaterades.",
            mutate=_do,
        )

    def _refresh_locked(self, plan: dict[str, Any], *, actor: str) -> dict[str, Any]:
        del actor
        site_id = str(plan.get("site_id") or "")
        plan_id = str(plan.get("plan_id") or "")
        root = workspace_root(self.data_dir, site_id, plan_id)
        plan["work_sha256"] = tree_hash(root / "work")
        plan["base_sha256"] = tree_hash(root / "base")
        plan["risk"] = tree_risk(
            str(plan.get("slug") or ""),
            iter_files(root / "work"),
            str(plan.get("summary") or ""),
        )
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
            return None, _fail(
                str(plan.get("error_class") or "PlanNotEditable"),
                "Planen kan inte redigeras.",
            )
        return plan, None

    def _mutate_workspace(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        action: str,
        message: str,
        mutate: Callable[[dict[str, Any], Path], tuple[dict[str, Any] | None, WpOpsResult | None]],
        extra_perms: tuple[Permission, ...] = (),
        use_str_error: bool = False,
        should_refresh: Callable[[dict[str, Any]], bool] | None = None,
        audit_on_noop: bool = True,
    ) -> WpOpsResult:
        """Shared transactional wrapper for editable workspace mutations."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, *extra_perms, action=action)
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            try:
                root = workspace_root(self.data_dir, site_id, plan_id)
                outcome, err = mutate(plan, root)
            except ReviewError as exc:
                return _fail(exc.error_class, str(exc) if use_str_error else exc.error_class)
            if err is not None:
                return err
            assert outcome is not None
            did_refresh = should_refresh(outcome) if should_refresh is not None else True
            if did_refresh:
                plan = self._refresh_locked(plan, actor=actor)
        if did_refresh or audit_on_noop:
            self._audit(actor, plan, action, True, **outcome.get("audit", {}))
        return _ok(
            message,
            plan_id=plan_id,
            site_id=site_id,
            work_sha256=plan["work_sha256"],
            base_sha256=plan["base_sha256"],
            risk=plan["risk"],
            status=plan["status"],
            review_path=plan["review_path"],
            login_required=True,
            **outcome.get("public", {}),
        )

    def refresh(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_refresh")
        if denied is not None:
            return denied
        with self._locks(site_id, plan_id):
            plan, blocked = self._load_editable_locked(site_id, plan_id)
            if blocked is not None:
                return blocked
            assert plan is not None
            plan = self._refresh_locked(plan, actor=actor)
        self._audit(actor, plan, "wp_review_refresh", True)
        return _ok(
            "Arbetsytan uppdaterades.",
            plan_id=plan_id,
            site_id=site_id,
            status=plan["status"],
            risk=plan["risk"],
        )

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

        def _do(_plan: dict[str, Any], root: Path):
            meta = patch_work_file(root, relative, old_text, new_text, base_sha256=base_sha256)
            return {
                "audit": {"path": meta["path"], "bytes": meta["byte_length"]},
                "public": {
                    "path": meta["path"],
                    "before_sha256": meta["before_sha256"],
                    "after_sha256": meta["after_sha256"],
                },
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_patch",
            message="Arbetsytan patchades.",
            mutate=_do,
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

        def _do(_plan: dict[str, Any], root: Path):
            meta = create_work_file(root, relative, content)
            return {
                "audit": {"path": meta["path"], "bytes": meta["byte_length"]},
                "public": {"path": meta["path"], "after_sha256": meta["after_sha256"]},
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_create_file",
            message="Fil skapad i arbetsytan.",
            mutate=_do,
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

        def _do(plan: dict[str, Any], root: Path):
            if plan.get("kind") != "theme":
                return None, _fail("InvalidTarget", "Endast teman har föräldratema.")
            resolved_parent = _resolve_parent_theme(root, str(plan.get("slug") or ""), parent_slug)
            if not resolved_parent:
                return None, _fail("ParentThemeUnknown", "Föräldratemat är okänt.")
            site, missing = self._site(site_id)
            if missing is not None or site is None:
                return None, (missing if missing is not None else _fail("UnknownSite", "Okänd webbplats."))
            try:
                abs_path = resolve_file(site.files_root, "theme", resolved_parent, relative)
                raw_bytes = self._transport(site).read_bytes(abs_path)  # type: ignore[attr-defined]
            except PathRejected:
                return None, _fail("PathRejected", "Sökvägen avvisades.")
            except TransportError as exc:
                return None, _fail(exc.error_class, exc.error_class)
            if len(raw_bytes) > 65536:
                return None, _fail("FileTooLarge", "Filen är för stor.")
            if b"\x00" in raw_bytes:
                return None, _fail("NotText", "Filen är inte giltig UTF-8-text.")
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return None, _fail("NotText", "Filen är inte giltig UTF-8-text.")
            write_tree(root / "origin", [(relative, text.encode("utf-8"))])
            meta = create_work_file(root, relative, text)
            return {
                "audit": {
                    "parent_slug": resolved_parent,
                    "path": meta["path"],
                    "bytes": meta["byte_length"],
                },
                "public": {
                    "parent_slug": resolved_parent,
                    "path": meta["path"],
                    "after_sha256": meta["after_sha256"],
                },
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_copy_parent",
            message="Fil kopierad från föräldratemat.",
            mutate=_do,
            extra_perms=(Permission.WP_OPS_ADMIN,),
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

        def _do(_plan: dict[str, Any], root: Path):
            res = apply_work_changes(
                root,
                changes,
                allow_create=allow_create,
                allow_delete=allow_delete,
            )
            return {
                "audit": {"count": res["count"]},
                "public": {"applied": res["applied"], "count": res["count"]},
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_apply_changes",
            message="Ändringarna sparades i arbetsytan.",
            mutate=_do,
            use_str_error=True,
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

        def _do(_plan: dict[str, Any], root: Path):
            res = bulk_replace_work(
                root,
                find,
                replace,
                mode=mode,
                extensions=extensions,
                path_prefix=path_prefix,
                dry_run=dry_run,
            )
            return {
                "audit": {
                    "files_changed": res["files_changed"],
                    "total_matches": res["total_matches"],
                },
                "public": res,
            }, None

        msg = (
            "Förhandsgranskning av massersättning."
            if dry_run
            else "Massersättning genomförd i arbetsytan."
        )
        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_bulk_replace",
            message=msg,
            mutate=_do,
            use_str_error=True,
            should_refresh=lambda out: (not dry_run and out["public"]["files_changed"] > 0),
            audit_on_noop=False,
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

        def _do(plan: dict[str, Any], root: Path):
            slug = component_slug or str(plan.get("work_slug") or plan.get("slug") or "") or None
            res = scaffold_work(root, template, component_slug=slug)
            return {
                "audit": {
                    "template": template,
                    "created": res["created"],
                    "updated": res["updated"],
                    "skipped": res["skipped"],
                },
                "public": res,
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_scaffold",
            message="Mall skapad i arbetsytan.",
            mutate=_do,
            use_str_error=True,
            should_refresh=lambda out: bool(out["public"]["created"] or out["public"]["updated"]),
            audit_on_noop=True,
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

        def _do(plan: dict[str, Any], root: Path):
            if plan.get("kind") != "theme":
                return None, _fail("InvalidTarget", "theme.json gäller endast teman.")
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
            res = theme_json_work(root, operations, seed_json=seed_json)
            return {
                "audit": {"changed_paths": res["changed_paths"], "version": res["version"]},
                "public": res,
            }, None

        return self._mutate_workspace(
            site_id,
            plan_id,
            actor=actor,
            action="wp_review_theme_json",
            message="theme.json uppdaterades i arbetsytan.",
            mutate=_do,
            use_str_error=True,
        )

    def workspace_status(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        """Return workspace status, changed file list, and check summary."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_status")
        if denied is not None:
            return denied
        plan = self.store.get_or_expire(site_id, plan_id, replace_fn=self._replace)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return _fail(
                "Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id, status="expired"
            )
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
            research_ids=pub["research_ids"],
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

    # --- Checks, previews, artifacts, and deploy delegations ---

    def run_checks(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return check_executor.execute_checks(self, site_id, plan_id, actor=actor)

    def _run_sandbox_catalog(
        self,
        plan: dict[str, Any],
        root: Path,
        kind: str,
        files: list[tuple[str, str]],
        risk: str,
        artifact_dir: Path,
    ) -> list[dict[str, Any]]:
        return check_executor.run_sandbox_catalog(self, plan, root, kind, files, risk, artifact_dir)

    def _reusable_live_baseline_dir(
        self,
        site_id: str,
        plan: dict[str, Any],
        *,
        clean_path: str,
        full_page: bool,
    ) -> Path | None:
        return preview_coordinator.reusable_live_baseline_dir(
            self, site_id, plan, clean_path=clean_path, full_page=full_page
        )

    def build_overlay_html(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        path: str = "/",
        use_cache: bool = True,
    ) -> tuple[dict[str, Any] | None, WpOpsResult | None]:
        return preview_coordinator.build_overlay_html(
            self, site_id, plan_id, actor=actor, path=path, use_cache=use_cache
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
        return preview_coordinator.live_preview(
            self,
            site_id,
            plan_id,
            actor=actor,
            path=path,
            focus_section=focus_section,
            focus_selector=focus_selector,
            focus_text=focus_text,
            full_page=full_page,
            reuse_live_baseline=reuse_live_baseline,
        )

    def build_variant_comparison(
        self,
        site_id: str,
        plan_ids: list[str] | tuple[str, ...],
    ) -> Path | None:
        return preview_coordinator.build_variant_comparison(self.data_dir, site_id, plan_ids)

    def artifact_file(
        self, site_id: str, plan_id: str, name: str, *, actor: str
    ) -> tuple[Path | None, WpOpsResult | None]:
        return preview_coordinator.artifact_file(self, site_id, plan_id, name, actor=actor)

    def list_artifacts(self, site_id: str, plan_id: str, *, actor: str) -> list[str]:
        if not self._holds(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT):
            return []
        if self._row(site_id, plan_id) is None:
            return []
        try:
            art_dir = workspace_root(self.data_dir, site_id, plan_id) / "artifacts"
        except ReviewError:
            return []
        return preview_coordinator.list_artifact_pngs(art_dir)

    def safe_preview_html_path(self, site_id: str, plan_id: str) -> Path | None:
        """Publicly resolve ``artifacts/preview.html`` strictly inside the plan workspace."""
        return preview_coordinator.resolve_safe_preview_html_path(self.data_dir, site_id, plan_id)

    def preview_html_file(
        self, site_id: str, plan_id: str, *, actor: str
    ) -> tuple[Path | None, WpOpsResult | None]:
        """Resolve ``artifacts/preview.html`` safely for ``WP_OPS_READ`` actors."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_preview_html")
        if denied is not None:
            return None, denied
        if not PLAN_ID.fullmatch(str(plan_id or "")):
            return None, _fail("UnknownPlan", "Okänd plan.")
        plan = self.store.get_or_expire(site_id, plan_id, replace_fn=self._replace)
        if plan is None:
            return None, _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return None, _fail("Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id)
        candidate = self.safe_preview_html_path(site_id, plan_id)
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
        return self.safe_preview_html_path(site_id, plan_id) is not None

    def show(self, site_id: str, plan_id: str, *, actor: str, include_diff: bool = False) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_show")
        if denied is not None:
            return denied
        include_diff = include_diff and self._holds(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)
        plan = self.store.get_or_expire(site_id, plan_id, replace_fn=self._replace)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return _fail(
                "Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id, status="expired"
            )
        view = self._view(plan, include_diff=include_diff)
        return _ok("Plan.", plan=view, diff=view.get("diff", "") if include_diff else "")

    def list_site(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_list")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return missing if missing is not None else _fail("UnknownSite", "Okänd webbplats.")
        rows = [
            self._view(plan, include_diff=False)
            for plan in self._load()
            if plan.get("site_id") == site.id
        ]
        return _ok("Lista.", site_id=site.id, plans=rows)

    # --- Canonical public read-model API (RV-01, consumed by PR 6.2 & PR 6.3) ---

    def verify_current_evidence(self, plan: dict[str, Any]) -> WpOpsResult | None:
        """Public check that ``plan`` has fresh, publishable evidence matching its current tree."""
        return check_executor.verify_current_evidence(self.data_dir, plan)

    def _require_current_evidence(self, plan: dict[str, Any]) -> WpOpsResult | None:
        return self.verify_current_evidence(plan)

    def staging_readiness(
        self,
        plan: dict[str, Any],
        *,
        changed_count: int | None = None,
    ) -> tuple[bool, str]:
        """Return ``(ready, swedish_reason)`` indicating whether ``plan`` may be staged."""
        status = str(plan.get("status") or "")
        if status == "staged":
            return False, "redan i staging"
        if status in _TERMINAL:
            return False, status
        if plan.get("superseded_by"):
            return False, "ersatt av nyare plan"
        if plan.get("risk") == "high":
            return False, "hög risk blockerad"
        if plan.get("environment") != "staging":
            return False, "endast staging kan stageas"
        if status != "tests_passed":
            return False, f"kontroller ej godkända ({status})"
        evidence = self._require_current_evidence(plan)
        if evidence is not None:
            reason = str((evidence.data or {}).get("error_class") or "EvidenceMissing")
            return False, f"bevis saknas ({reason})"
        if changed_count is not None and changed_count == 0:
            return False, "inga ändringar"
        return True, "redo för staging"

    def _describe_row(
        self,
        plan: dict[str, Any],
        *,
        include_diff: bool,
        include_artifacts: bool,
        include_changed_files: bool,
        check_preview_html: bool,
    ) -> dict[str, Any]:
        site_id = str(plan.get("site_id") or "")
        plan_id = str(plan.get("plan_id") or "")
        view = self._view(plan, include_diff=include_diff)
        preview_meta = preview_coordinator.read_preview_meta(self.data_dir, site_id, plan_id)
        page_draft_meta = preview_coordinator.read_page_draft_meta(self.data_dir, site_id, plan_id)
        is_standalone = bool(page_draft_meta) or preview_meta.get("preview_kind") == "standalone_page"
        changed_files: list[dict[str, str]] | None = None
        if include_changed_files:
            try:
                changed_files = list_work_changes(workspace_root(self.data_dir, site_id, plan_id))
            except ReviewError:
                changed_files = []
        ready, ready_reason = self.staging_readiness(
            plan,
            changed_count=len(changed_files) if changed_files is not None else None,
        )
        entry: dict[str, Any] = {
            **view,
            "summary": str(plan.get("summary") or "")[:200],
            "preview_meta": preview_meta,
            "page_draft_meta": page_draft_meta,
            "is_standalone": is_standalone,
            "staging_ready": ready,
            "staging_reason": ready_reason,
        }
        if check_preview_html:
            entry["has_preview_html"] = self.safe_preview_html_path(site_id, plan_id) is not None
        if include_artifacts:
            try:
                art_dir = workspace_root(self.data_dir, site_id, plan_id) / "artifacts"
                entry["artifacts"] = preview_coordinator.list_artifact_pngs(art_dir)
            except ReviewError:
                entry["artifacts"] = []
        if changed_files is not None:
            entry["changed_files"] = changed_files
        return entry

    def describe_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        include_diff: bool = False,
    ) -> WpOpsResult:
        """Canonical single-plan read model for dashboard and bot surfaces."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_show")
        if denied is not None:
            return denied
        can_admin = self._holds(actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)
        plan = self.store.get_or_expire(site_id, plan_id, replace_fn=self._replace)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "expired":
            return _fail(
                "Expired", "Planen har gått ut.", plan_id=plan_id, site_id=site_id, status="expired"
            )
        detail = self._describe_row(
            plan,
            include_diff=include_diff and can_admin,
            include_artifacts=can_admin,
            include_changed_files=True,
            check_preview_html=True,
        )
        return _ok("Plan.", plan=detail, diff=detail.get("diff", "") if (include_diff and can_admin) else "")

    def describe_site_plans(
        self,
        site_id: str,
        *,
        actor: str,
        check_preview_html: bool = False,
    ) -> WpOpsResult:
        """Canonical site-level plan read model (single JSON load, N+1-safe by default)."""
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_review_list")
        if denied is not None:
            return denied
        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return missing if missing is not None else _fail("UnknownSite", "Okänd webbplats.")
        rows = [
            self._describe_row(
                plan,
                include_diff=False,
                include_artifacts=False,
                include_changed_files=False,
                check_preview_html=check_preview_html,
            )
            for plan in self._load()
            if plan.get("site_id") == site.id
        ]
        return _ok("Lista.", site_id=site.id, plans=rows)

    def attach_research(
        self,
        site_id: str,
        plan_id: str,
        research_id: str,
        *,
        actor: str,
        summary_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach a scrubbed ``res-*`` research brief snapshot to a ``wr-*`` plan without altering ``work/``."""

        def _err(error_class: str, message: str, **extra: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "error_class": error_class,
                "message": message,
                "site_id": site_id,
                "plan_id": plan_id,
                "executed": False,
                "live_calls": False,
                **extra,
            }

        if not (
            self._holds(actor, Permission.WP_OPS_OPERATE)
            or self._holds(actor, Permission.WP_OPS_ADMIN)
        ):
            self._audit_bare(
                actor,
                "wp_review_attach_research",
                False,
                site_id=site_id,
                plan_id=plan_id,
                error_class="AccessDenied",
            )
            return _err("AccessDenied", "Saknar behörighet.")

        site, missing = self._site(site_id)
        if missing is not None or site is None:
            return _err("UnknownSite", "Okänd webbplats.")
        clean_pid = str(plan_id or "").strip()
        if PLAN_ID.fullmatch(clean_pid) is None:
            return _err("UnknownPlan", "Okänd plan.")
        clean_rid = str(research_id or "").strip()
        if RESEARCH_ID.fullmatch(clean_rid) is None:
            return _err("InvalidToolArguments", "Ogiltigt research-id.")
        if not isinstance(summary_snapshot, dict):
            return _err("InvalidToolArguments", "Ogiltig research-sammanfattning.")

        with self._locks(site.id, clean_pid):
            plan = self._row(site.id, clean_pid)
            if plan is None:
                return _err("UnknownPlan", "Okänd plan.")
            blocked = self._fresh(plan, allow_superseded_read=False)
            if blocked is not None:
                return _err(
                    str((blocked.data or {}).get("error_class") or "StalePlan"),
                    blocked.message,
                )
            if str(plan.get("status") or "") in _NON_EDITABLE:
                return _err("PlanNotEditable", "Planen kan inte uppdateras.")
            try:
                root = workspace_root(self.data_dir, site.id, clean_pid)
            except ReviewError as exc:
                return _err(exc.error_class, exc.error_class)

            existing_ids = normalize_research_ids(plan.get("research_ids"))
            if clean_rid not in existing_ids:
                if len(existing_ids) >= _MAX_RESEARCH_IDS:
                    return _err(
                        "ResearchLimitReached",
                        f"Max {_MAX_RESEARCH_IDS} research-rapporter kan kopplas till en plan.",
                    )
                updated_ids = [*existing_ids, clean_rid]
            else:
                updated_ids = existing_ids

            try:
                write_research_snapshot(root, clean_rid, summary_snapshot)
            except ReviewError as exc:
                return _err(exc.error_class, exc.error_class)

            plan["research_ids"] = updated_ids
            self._replace(plan)
            write_manifest(root, plan)
            work_sha256 = str(plan.get("work_sha256") or "")

        self._audit(
            actor,
            plan,
            "wp_review_attach_research",
            True,
            research_id=clean_rid,
            research_count=len(updated_ids),
        )
        return {
            "ok": True,
            "site_id": site.id,
            "plan_id": clean_pid,
            "research_id": clean_rid,
            "research_ids": updated_ids,
            "work_sha256": work_sha256,
            "executed": False,
            "live_calls": False,
        }

    def comment(self, site_id: str, plan_id: str, text: str, *, actor: str) -> WpOpsResult:
        denied = self._need(
            actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_comment"
        )
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
        return deploy.approve_staging(
            self,
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha256,
            work_sha256=work_sha256,
        )

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
        return deploy.push_page_draft(
            self,
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha256,
            work_sha256=work_sha256,
            client=client,
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
        return deploy.request_production(
            self,
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha256,
            work_sha256=work_sha256,
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
                return _fail(
                    "PreviewBlocked",
                    "Förhandsvisningen startades inte.",
                    plan_id=plan_id,
                    status=plan.get("status"),
                )
            evidence = self._require_current_evidence(plan)
            if evidence is not None:
                return evidence
            site, missing = self._site(site_id)
            if missing is not None or site is None:
                return missing if missing is not None else _fail("UnknownSite", "Okänd webbplats.")
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
            running = record.get("state") == "running" and record.get("engine") not in {
                None,
                "unavailable",
            }
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
        denied = self._need(
            actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_rollback"
        )
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
        return _ok(
            "Arbetsytan återställdes från basen.",
            plan_id=plan_id,
            site_id=site_id,
            status="rolled_back",
            restore="not_called",
        )

    def extend(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(
            actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_review_extend"
        )
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

    # --- Component preparation & internal hooks (preserved for Section 5 monkeypatches) ---

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
        if kind == "mu_plugin":
            return self._prepare_mu_plugin(site, slug)
        return self._prepare_theme(site, slug, verified_theme)

    def _prepare_mu_plugin(self, site: Site, slug: str) -> dict[str, Any]:
        from ecom_ops.wp_ops.plugin_policy import is_bridge_plugin
        from ecom_ops.wp_ops.review.component_reader import read_site_component

        default_slug = f"azom-mu-{site.id}"
        chosen = slug or default_slug
        if not SLUG.fullmatch(chosen):
            raise ReviewError("SlugRejected")
        if chosen == "woocommerce":
            return _prepared([], "rejected", "unknown", "WooCoreRefused", chosen)
        if is_bridge_plugin(chosen):
            return _prepared([], "rejected", "unknown", "BridgeSelfProtectionRefused", chosen)
        files = read_site_component(self._transport(site), site.files_root, "mu_plugin", chosen)
        if files:
            return _prepared(files, "validated", "mu_plugin", "", chosen)
        if chosen.startswith("azom-"):
            return _prepared(mu_plugin_skeleton(chosen), "validated", "mu_plugin", "", chosen)
        return _prepared([], "fork_not_approved", "unknown", "ThirdPartyPluginRefused", chosen)

    def _prepare_plugin(
        self, site: Site, slug: str, verified: VerifiedPlugin | None
    ) -> dict[str, Any]:
        from ecom_ops.wp_ops.plugin_policy import is_bridge_plugin
        from ecom_ops.wp_ops.review.component_reader import read_site_component

        reserved = site_plugin_slug(site.id)
        chosen = slug or reserved
        if not SLUG.fullmatch(chosen):
            raise ReviewError("SlugRejected")
        if chosen == "woocommerce":
            return _prepared([], "rejected", "unknown", "WooCoreRefused", chosen)
        if is_bridge_plugin(chosen):
            return _prepared([], "rejected", "unknown", "BridgeSelfProtectionRefused", chosen)
        if verified is not None:
            if not SLUG.fullmatch(verified.slug) or verified.slug != chosen:
                return _prepared([], "rejected", "unknown", "PluginNotVerified", chosen)
            files = read_site_component(self._transport(site), site.files_root, "plugin", chosen)
            if not files:
                return _prepared([], "draft", "existing_plugin", "ComponentMissing", chosen)
            return _prepared(files, "validated", "existing_plugin", "", chosen)
        if chosen == reserved:
            return _prepared(plugin_skeleton(reserved), "validated", "site_plugin", "", reserved)
        if chosen.startswith("azom-"):
            files = read_site_component(self._transport(site), site.files_root, "plugin", chosen)
            if files:
                return _prepared(files, "validated", "site_plugin", "", chosen)
            return _prepared(plugin_skeleton(chosen), "validated", "site_plugin", "", chosen)
        return _prepared([], "fork_not_approved", "unknown", "ThirdPartyPluginRefused", chosen)

    def _prepare_theme(self, site: Site, slug: str, verified: VerifiedTheme | None) -> dict[str, Any]:
        from ecom_ops.wp_ops.review.component_reader import read_site_component

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
        return self.store.now()

    def _past(self, plan: dict[str, Any]) -> bool:
        return self.store.is_past(plan)

    def _locks(self, site_id: str, plan_id: str):
        return self.store.locks(site_id, plan_id)

    @contextlib.contextmanager
    def _store_file_lock(self) -> Iterator[None]:
        with self.store.file_lock():
            yield

    def _load(self) -> list[dict[str, Any]]:
        return self.store.load()

    def _save(self, plans: list[dict[str, Any]]) -> None:
        self.store.save(plans)

    def _replace(self, plan: dict[str, Any]) -> None:
        self.store.replace(plan, save_fn=self._save, file_lock_fn=self._store_file_lock)

    def _row(self, site_id: str, plan_id: str) -> dict[str, Any] | None:
        return self.store.find_row(site_id, plan_id, replace_fn=self._replace)

    def _fresh(self, plan: dict[str, Any], *, allow_superseded_read: bool = False) -> WpOpsResult | None:
        return self.store.fresh_guard(
            plan, allow_superseded_read=allow_superseded_read, replace_fn=self._replace
        )

    def _gate(self, plan: dict[str, Any], base_sha256: str, work_sha256: str) -> WpOpsResult | None:
        return self.store.gate_guard(plan, base_sha256, work_sha256, replace_fn=self._replace)

    def _expire_if_due(self, plan: dict[str, Any]) -> None:
        if plan.get("status") != "expired" and self._past(plan):
            self._mark_expired(plan)

    def _mark_expired(self, plan: dict[str, Any]) -> None:
        self.store.mark_expired(plan, replace_fn=self._replace)

    def _view(self, plan: dict[str, Any], *, include_diff: bool) -> dict[str, Any]:
        return self.store.view(plan, include_diff=include_diff)

    def _public(self, plan: dict[str, Any]) -> dict[str, Any]:
        return public_plan(plan)

    def _same_target(self, older: dict[str, Any], plan: dict[str, Any]) -> bool:
        return same_target(older, plan)

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


__all__ = [
    "ReviewPlans",
]
