"""Typed WordPress file editor, change-plan payload store, and staging workflow.

The model may propose a diff. Payloads live outside chat, audit, and job results.
Live SFTP stays read-only. Production deploy stays closed until a real backup and
restore exist.
"""

from __future__ import annotations

import difflib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.credentials import bind_credential, redact_secret_text, scrub_secrets
from ecom_ops.wp_ops.file_paths import (
    MAX_FILE_BYTES,
    PLAN_ID_RE,
    PLAN_TTL,
    SKIP_DIRS,
    SLUG_RE,
    PathRejected,
    TransportError,
    component_dir,
    components_parent,
    plan_expired,
    resolve_dir,
    resolve_file,
    sensitive_relative,
    sha256_digest,
    transport_message,
)
from ecom_ops.wp_ops.file_policy import production_deploy_block, risk_for
from ecom_ops.wp_ops.file_transport import (
    FileTransport,
    LocalTree,
    transport_is_remote,
    transport_list_entries,
)
from ecom_ops.wp_ops.models import Site, WpOpsResult, files_root_narrow
from ecom_ops.wp_ops.policy import WpOpsPolicy
from ecom_ops.wp_ops.registry import site_by_id
from ecom_ops.wp_ops.search import ComponentSearcher
from ecom_ops.wp_ops.store import WpOpsStore, atomic_write_text

_MAX_SUMMARY = 200
_MAX_DIFF_BYTES = 65536
_TREE_CAP = 256


def _redact_secret_lines(text: str) -> str:
    return redact_secret_text(text)


def _unified(before: str, after: str, path: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=2,
        )
    )
    if len(diff.encode("utf-8")) > _MAX_DIFF_BYTES:
        return ""
    return diff


class FilePayloadStore:
    """Stores pending file-diff payloads outside chat logs, audit logs, and state JSON."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def path_for(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise TransportError("UnknownPlan")
        root = (self.data_dir / "wp_file_payloads").resolve()
        path = (root / plan_id).resolve()
        if path.parent != root:
            raise TransportError("UnknownPlan")
        return path

    def write(self, plan_id: str, new_text: str, diff: str) -> None:
        atomic_write_text(
            self.path_for(plan_id),
            json.dumps({"new_text": new_text, "diff": diff}, ensure_ascii=False),
            mode=0o600,
        )

    def read(self, plan_id: str) -> dict[str, str]:
        path = self.path_for(plan_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise TransportError("PayloadMissing") from exc
        if not isinstance(raw, dict):
            raise TransportError("PayloadMissing")
        new_text = raw.get("new_text")
        diff = raw.get("diff")
        if not isinstance(new_text, str) or not isinstance(diff, str):
            raise TransportError("PayloadMissing")
        return {"new_text": new_text, "diff": diff}

    def delete(self, plan_id: str) -> None:
        try:
            self.path_for(plan_id).unlink(missing_ok=True)
        except TransportError:
            return


def payload_path(data_dir: Path, plan_id: str) -> Path:
    return FilePayloadStore(data_dir).path_for(plan_id)


def write_payload(data_dir: Path, plan_id: str, new_text: str, diff: str) -> None:
    FilePayloadStore(data_dir).write(plan_id, new_text, diff)


def read_payload(data_dir: Path, plan_id: str) -> dict[str, str]:
    return FilePayloadStore(data_dir).read(plan_id)


def delete_payload(data_dir: Path, plan_id: str) -> None:
    FilePayloadStore(data_dir).delete(plan_id)


class FileEditor:
    """Typed editor for one existing theme or plugin file."""

    def __init__(
        self,
        *,
        registry_path: Path | None,
        store: WpOpsStore,
        clock: Callable[[], Any],
        factory: Callable[[Site], FileTransport],
        audit: Callable[..., None],
        need: Callable[..., WpOpsResult | None],
        lock: Callable[[str], Any],
        policy: WpOpsPolicy | None = None,
    ) -> None:
        self.registry_path = registry_path
        self.store = store
        self.clock = clock
        self.factory = factory
        self.audit = audit
        self.need = need
        self.lock = lock
        self.policy = policy or WpOpsPolicy(
            registry_path=registry_path,
            data_dir=store.path.parent,
            lock_fn=lock,
        )

    def list_components(self, site_id: str, kind: str, *, actor: str) -> WpOpsResult:
        site, denied = self.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            parent = components_parent(site.files_root, kind)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        try:
            names = self.transport_for(site).list_dir(parent)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=self.uses_ssh(site)
            )
        slugs = [name for name in names if SLUG_RE.fullmatch(name)]
        return WpOpsResult(
            ok=True,
            message="components",
            data={
                "site_id": site.id,
                "kind": kind,
                "slugs": slugs,
                "ssh": self.uses_ssh(site),
                "live_calls": False,
            },
        )

    def list_files(self, site_id: str, kind: str, slug: str, *, actor: str) -> WpOpsResult:
        site, denied = self.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            directory = component_dir(site.files_root, kind, slug)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        try:
            names = self.transport_for(site).list_dir(directory)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=self.uses_ssh(site)
            )
        files = []
        for name in names:
            try:
                resolve_file(site.files_root, kind, slug, name)
            except PathRejected:
                continue
            files.append(name)
        return WpOpsResult(
            ok=True,
            message="files",
            data={
                "site_id": site.id,
                "kind": kind,
                "slug": slug,
                "files": files,
                "ssh": self.uses_ssh(site),
                "live_calls": False,
            },
        )

    def read_file(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        *,
        actor: str,
        admin_only: bool = False,
    ) -> WpOpsResult:
        site, denied = self.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if admin_only:
            denied = self.need(
                actor,
                Permission.WP_OPS_ADMIN,
                site=site,
                action="wp_ops_file_read",
            )
            if denied is not None:
                return denied
        try:
            path = resolve_file(site.files_root, kind, slug, relative)
            body = self.read_text(site, path)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=self.uses_ssh(site)
            )
        encoded = body.encode()
        self.audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_read",
            path=path,
            byte_length=len(encoded),
            digest=sha256_digest(encoded),
        )
        return WpOpsResult(
            ok=True,
            message="file",
            data={
                "site_id": site.id,
                "path": path,
                "content": _redact_secret_lines(self.scrub_text(site, body)),
                "byte_length": len(encoded),
                "digest": sha256_digest(encoded),
                "ssh": self.uses_ssh(site),
                "live_calls": False,
            },
        )

    def propose(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        site, denied = self.resolve_site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        locked = self.lock(site.id)
        if not locked.acquire(blocking=False):
            return self.fail_result("SiteBusy", "Site check already running", executed=False)
        try:
            return self._propose_locked(site, kind, slug, relative, new_text, actor=actor)
        finally:
            locked.release()

    def apply(
        self,
        site_id: str,
        plan_id: str,
        new_text: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        site, denied = self.resolve_site(site_id, actor, write=True)
        if denied is not None:
            return denied
        if not PLAN_ID_RE.fullmatch(str(plan_id or "")):
            return self.fail_result("UnknownPlan", "Unknown edit plan.")
        assert isinstance(site, Site)
        locked = self.lock(site.id)
        if not locked.acquire(blocking=False):
            return self.fail_result("SiteBusy", "Site check already running", executed=False)
        try:
            return self._apply_locked(site, plan_id, new_text, actor=actor)
        finally:
            locked.release()

    def _propose_locked(
        self,
        site: Site,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        try:
            path = resolve_file(site.files_root, kind, slug, relative)
            current = self.read_text(site, path)
            proposed = self.validate_text(new_text)
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=self.uses_ssh(site)
            )
        before = sha256_digest(current.encode())
        after = sha256_digest(proposed.encode())
        if before == after:
            return self.fail_result("NoChange", "The file is unchanged.")
        plan_id = "fe-" + uuid.uuid4().hex[:12]
        now = self.clock().isoformat()
        prefix = component_dir(site.files_root, kind, slug) + "/"
        uses_ssh = self.uses_ssh(site)
        plan = {
            "id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": kind,
            "slug": slug,
            "relative_path": path[len(prefix) :],
            "path": path,
            "before_digest": before,
            "after_digest": after,
            "byte_length": len(proposed.encode()),
            "created_at": now,
            "created_by": actor,
            "status": "pending",
            "ssh": uses_ssh,
        }
        self.store.update(lambda state: state.setdefault("file_edits", []).append(plan))
        self.audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_propose",
            path=path,
            byte_length=plan["byte_length"],
            plan_id=plan_id,
            digest=after,
        )
        return WpOpsResult(
            ok=True,
            message="edit proposed",
            data={"plan": dict(plan), "executed": False, "ssh": uses_ssh, "live_calls": False},
        )

    def _apply_locked(self, site: Site, plan_id: str, new_text: str, *, actor: str) -> WpOpsResult:
        state = self.store.load()
        plan = next(
            (row for row in state.get("file_edits", []) if isinstance(row, dict) and row.get("id") == plan_id),
            None,
        )
        if plan is None or plan.get("site_id") != site.id:
            return self.fail_result("UnknownPlan", "Unknown edit plan.")
        if plan.get("status") != "pending":
            return self.fail_result("PlanNotPending", "Edit plan is not pending.")
        current_time = self.clock()
        if plan_expired(plan, current_time if isinstance(current_time, datetime) else datetime.now(UTC)):
            return self.fail_result("PlanExpired", "The edit plan has expired.")
        try:
            proposed = self.validate_text(new_text)
        except TransportError as exc:
            return self.fail_result(exc.error_class, transport_message(exc.error_class))
        if sha256_digest(proposed.encode()) != plan.get("after_digest"):
            return self.fail_result("DigestMismatch", "The text does not match the approved plan.")
        stored = str(plan.get("path") or "")
        try:
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except PathRejected:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        if resolved != stored:
            return self.fail_result("PathRejected", "Kind or path is not editable.")
        try:
            current = self.read_text(site, resolved)
        except TransportError as exc:
            return self.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=self.uses_ssh(site)
            )
        if sha256_digest(current.encode()) != plan.get("before_digest"):
            return self.fail_result("StaleFile", "The file changed after the plan was created.")
        try:
            transport = self.transport_for(site)
            transport.write_bytes(resolved, proposed.encode())
            written = self.read_text(site, resolved)
        except (PathRejected, TransportError) as exc:
            error = exc.error_class if isinstance(exc, TransportError) else "PathRejected"
            return self.fail_result(
                error,
                transport_message(error),
                executed=False,
                ssh=self.uses_ssh(site),
            )
        if sha256_digest(written.encode()) != plan.get("after_digest"):
            return self.fail_result(
                "DigestMismatch", "The written file does not match the plan.", executed=False
            )
        applied_at = self.clock().isoformat()

        def mutate(current_state: dict[str, Any]) -> None:
            for row in current_state.get("file_edits", []):
                if isinstance(row, dict) and row.get("id") == plan_id:
                    row["status"] = "applied"
                    row["applied_at"] = applied_at
                    row["applied_by"] = actor

        self.store.update(mutate)
        self.audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_apply",
            path=resolved,
            byte_length=len(written.encode()),
            plan_id=plan_id,
            digest=str(plan.get("after_digest") or ""),
        )
        return WpOpsResult(
            ok=True,
            message="edit applied",
            data={
                "plan_id": plan_id,
                "path": resolved,
                "executed": True,
                "ssh": self.uses_ssh(site),
                "backup": resolved + ".azom-bak",
                "live_calls": False,
            },
        )

    def resolve_site(
        self,
        site_id: str,
        actor: str,
        *,
        write: bool,
    ) -> tuple[Site | None, WpOpsResult | None]:
        sites, config_err = self.policy.load_all_sites()
        if config_err is not None:
            return None, config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return None, self.fail_result("UnknownSite", "Unknown site")
        permission = self.policy.write_permission_for(site) if write else Permission.WP_OPS_READ
        denied = self.need(actor, permission, site=site, action="wp_ops_file_edit")
        if denied is not None:
            return None, denied
        if write and site.environment == "production":
            denied = self.need(actor, Permission.CODE_EDIT, site=site, action="wp_ops_file_edit")
            if denied is not None:
                return None, denied
        if not site.files_root:
            return None, self.fail_result("FileRootUnconfigured", "The site has no files root.")
        if not files_root_narrow(site.files_root):
            return None, self.fail_result("PathRejected", "The files root is too broad.")
        return site, None

    def transport_for(self, site: Site) -> FileTransport:
        transport = self.factory(site)
        if isinstance(transport, LocalTree):
            transport.ensure_demo()
        return transport

    def uses_ssh(self, site: Site) -> bool:
        return site.ssh_host != "" and transport_is_remote(self.factory(site))

    def read_text(self, site: Site, path: str) -> str:
        data = self.transport_for(site).read_bytes(path)
        if len(data) > MAX_FILE_BYTES:
            raise TransportError("FileTooLarge")
        if b"\x00" in data:
            raise TransportError("NotText")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("NotText") from exc

    def validate_text(self, text: str) -> str:
        if not isinstance(text, str):
            raise TransportError("NotText")
        if "\x00" in text:
            raise TransportError("NotText")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise TransportError("FileTooLarge")
        return text

    def scrub_text(self, site: Site, text: str) -> str:
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        cleaned = scrub_secrets(text, secrets)
        return cleaned if isinstance(cleaned, str) else text

    def fail_result(
        self,
        error_class: str,
        message: str,
        *,
        executed: bool = False,
        ssh: bool = False,
    ) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": error_class, "executed": executed, "ssh": ssh, "live_calls": False},
        )

    def audit_file(
        self,
        actor: str,
        site: Site,
        *,
        success: bool,
        error_class: str | None,
        operation: str,
        path: str,
        byte_length: int,
        plan_id: str | None = None,
        digest: str | None = None,
    ) -> None:
        self.audit(
            actor,
            action="wp_ops_file_edit",
            target_id=site.id,
            success=success,
            details={
                "environment": site.environment,
                "operation": operation,
                "path": path,
                "byte_length": byte_length,
                "plan_id": plan_id,
                "digest": digest,
                "error_class": error_class,
                "ssh": self.uses_ssh(site),
                "live_calls": False,
            },
        )

    # Compatibility aliases for callers outside wp_ops (e.g. agent_tools, workspace_io).
    _site = resolve_site
    _transport = transport_for
    _ssh = uses_ssh
    _read_text = read_text
    _validate_text = validate_text
    _scrub = scrub_text
    _fail = fail_result
    _audit_file = audit_file


class FileChangeOps:
    """Site-bound change-plan operations built on top of ``FileEditor``."""

    def __init__(self, editor: FileEditor) -> None:
        self.editor = editor
        self.payloads = FilePayloadStore(editor.store.path.parent)
        self.searcher = ComponentSearcher(editor)

    def _now(self) -> datetime:
        current = self.editor.clock()
        if not isinstance(current, datetime):
            return datetime.now(UTC)
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    def list_tree(
        self, site_id: str, kind: str, slug: str, relative: str, *, actor: str
    ) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if sensitive_relative(relative):
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        transport = editor.transport_for(site)
        try:
            directory = resolve_dir(site.files_root, kind, slug, relative)
            raw_entries = transport_list_entries(transport, directory)
        except PathRejected:
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor.fail_result(
                exc.error_class, transport_message(exc.error_class), ssh=editor.uses_ssh(site)
            )
        entries: list[dict[str, str]] = []
        truncated = False
        for name, is_dir in raw_entries:
            if name in {".", ".."} or sensitive_relative(name) or name.lower() in SKIP_DIRS:
                continue
            if len(entries) >= _TREE_CAP:
                truncated = True
                break
            entries.append({"name": name, "kind": "directory" if is_dir else "file"})
        uses_ssh = editor.uses_ssh(site)
        return WpOpsResult(
            ok=True,
            message="tree",
            data={
                "ok": True,
                "status": "listed",
                "site_id": site.id,
                "environment": site.environment,
                "kind": kind,
                "slug": slug,
                "directory": relative or "",
                "entries": entries,
                "truncated": truncated,
                "ssh": uses_ssh,
                "live_calls": uses_ssh,
            },
        )

    def search(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        query: str,
        *,
        actor: str,
        extensions: list[str] | None = None,
        case_insensitive: bool = True,
        mode: str = "literal",
        context_lines: int = 0,
    ) -> WpOpsResult:
        return self.searcher.search(
            site_id,
            kind,
            slug,
            relative,
            query,
            actor=actor,
            extensions=extensions,
            case_insensitive=case_insensitive,
            mode=mode,
            context_lines=context_lines,
        )

    def propose(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        create: bool,
        parent_slug: str | None,
    ) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if site.environment == "production":
            code = editor.need(actor, Permission.CODE_EDIT, site=site, action="wp_ops_file_edit")
            if code is not None:
                return code
        if kind == "plugin" and slug == "woocommerce":
            return editor.fail_result("WooCoreRefused", "WooCommerce core files cannot be edited.")
        if sensitive_relative(relative):
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        if kind == "theme" and parent_slug and slug == parent_slug:
            return editor.fail_result("ParentThemeRefused", "Edit the child theme, not the parent.")
        note = " ".join(str(summary or "").split())[:_MAX_SUMMARY]
        locked = editor.lock(site.id)
        if not locked.acquire(blocking=False):
            return editor.fail_result("SiteBusy", "Site check already running", executed=False)
        try:
            return self._propose_locked(
                site,
                kind,
                slug,
                relative,
                new_text,
                base_sha256,
                note,
                actor=actor,
                create=create,
                parent_slug=parent_slug,
            )
        finally:
            locked.release()

    def _propose_locked(
        self,
        site: Site,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        create: bool,
        parent_slug: str | None,
    ) -> WpOpsResult:
        editor = self.editor
        try:
            path = resolve_file(site.files_root, kind, slug, relative)
            proposed = editor.validate_text(new_text)
        except PathRejected:
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor.fail_result(exc.error_class, transport_message(exc.error_class))
        if create and not self._parent_ready(site, path):
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        try:
            current = editor.read_text(site, path)
        except TransportError as exc:
            if create and exc.error_class == "FileMissing":
                current = ""
            else:
                return editor.fail_result(
                    exc.error_class, transport_message(exc.error_class), ssh=editor.uses_ssh(site)
                )
        else:
            if create:
                return editor.fail_result("AlreadyExists", "The file already exists.")
        before = sha256_digest(current.encode())
        if str(base_sha256 or "") != before:
            return editor.fail_result("DigestMismatch", "The base hash does not match the file.")
        after = sha256_digest(proposed.encode())
        if before == after:
            return editor.fail_result("NoChange", "The file is unchanged.")
        risk = risk_for(relative, proposed)
        plan_id = "fe-" + uuid.uuid4().hex[:12]
        now = self._now()
        diff = _unified(current, proposed, path)
        try:
            self.payloads.write(plan_id, proposed, diff)
        except (OSError, TransportError):
            return editor.fail_result("PayloadMissing", "The review copy could not be stored.")
        theme_relationship = "unknown" if parent_slug is None else "known"
        uses_ssh = editor.uses_ssh(site)
        plan = {
            "id": plan_id,
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": kind,
            "slug": slug,
            "relative_path": relative.replace("\\", "/").strip("/"),
            "path": path,
            "before_digest": before,
            "after_digest": after,
            "byte_length": len(proposed.encode()),
            "created_at": now.isoformat(),
            "expires_at": (now + PLAN_TTL).isoformat(),
            "created_by": actor,
            "status": "pending_review",
            "risk": risk,
            "summary": summary,
            "created": create,
            "theme_relationship": theme_relationship,
            "payload_present": True,
            "ssh": uses_ssh,
        }
        editor.store.update(lambda state: state.setdefault("file_edits", []).append(plan))
        editor.audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_diff_propose",
            path=path,
            byte_length=plan["byte_length"],
            plan_id=plan_id,
            digest=after,
        )
        public = {key: value for key, value in plan.items() if key != "path"}
        return WpOpsResult(
            ok=True,
            message="diff proposed",
            data={
                "ok": True,
                "status": "pending_review",
                "plan": public,
                "executed": False,
                "isolated_execution": "not_available",
                "ssh": uses_ssh,
                "live_calls": False,
            },
        )

    def _load_plan(self, plan_id: str, site_id: str | None) -> dict[str, Any] | None:
        if not PLAN_ID_RE.fullmatch(plan_id):
            return None
        state = self.editor.store.load()
        for row in state.get("file_edits", []):
            if not isinstance(row, dict) or row.get("id") != plan_id:
                continue
            if site_id is not None and row.get("site_id") != site_id:
                continue
            return row
        return None

    def _set_status(self, plan_id: str, status: str, *, actor: str) -> None:
        now = self._now().isoformat()

        def mutate(state: dict[str, Any]) -> None:
            for row in state.get("file_edits", []):
                if isinstance(row, dict) and row.get("id") == plan_id:
                    row["status"] = status
                    row["updated_at"] = now
                    row["updated_by"] = actor

        self.editor.store.update(mutate)

    def validate(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None or plan.get("payload_present") is not True:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        if plan_expired(plan, self._now()):
            self.payloads.delete(plan_id)
            return editor.fail_result("PlanExpired", "The edit plan has expired.")
        try:
            payload = self.payloads.read(plan_id)
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except (PathRejected, TransportError):
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        if sha256_digest(payload["new_text"].encode()) != plan.get("after_digest"):
            return editor.fail_result("DigestMismatch", "The stored diff does not match the plan.")
        if plan.get("created") is True:
            try:
                editor.read_text(site, resolved)
            except TransportError as exc:
                if exc.error_class != "FileMissing":
                    return editor.fail_result(exc.error_class, transport_message(exc.error_class))
            else:
                return editor.fail_result("AlreadyExists", "The file already exists.")
        else:
            try:
                current = editor.read_text(site, resolved)
            except TransportError as exc:
                return editor.fail_result(exc.error_class, transport_message(exc.error_class))
            if sha256_digest(current.encode()) != plan.get("before_digest"):
                return editor.fail_result("StaleFile", "The file changed after the plan was created.")
        advance = plan.get("risk") != "high"
        if advance and plan.get("status") == "pending_review":
            self._set_status(plan_id, "validated", actor=actor)
        return WpOpsResult(
            ok=True,
            message="validated",
            data={
                "ok": True,
                "status": "validated" if advance else "pending_review",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "risk": plan.get("risk"),
                "advance": advance,
                "isolated_execution": "not_available",
                "executed": False,
                "live_calls": False,
            },
        )

    def _has_staging(self) -> bool:
        sites, config_err = self.editor.policy.load_all_sites()
        if config_err is not None:
            return False
        return any(site.environment == "staging" for site in sites)

    def _parent_ready(self, site: Site, path: str) -> bool:
        transport = self.editor.transport_for(site)
        parent_check = getattr(transport, "parent_dir_exists", None)
        if callable(parent_check):
            return bool(parent_check(path))
        return True

    def _prepare_stage(self, plan_id: str, *, actor: str) -> tuple[Any, ...] | WpOpsResult:
        """Check every staging guard. The tuple is ready to write; a result is a refusal."""
        editor = self.editor
        plan = self._load_plan(plan_id, None)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        site_id = str(plan.get("site_id") or "")
        site, denied = editor.resolve_site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if site.environment != "staging":
            if not self._has_staging():
                return editor.fail_result("StagingUnconfigured", "No staging site is registered.")
            return editor.fail_result("ProductionWriteDisabled", "Production file writes are disabled.")
        if plan.get("risk") == "high":
            return editor.fail_result(
                "HighRiskBlocked",
                "High-risk changes stop until an isolated check exists.",
            )
        if plan.get("status") not in {"pending_review", "validated"}:
            return editor.fail_result("PlanNotPending", "Edit plan is not pending.")
        if plan_expired(plan, self._now()):
            self.payloads.delete(plan_id)
            return editor.fail_result("PlanExpired", "The edit plan has expired.")
        transport = editor.transport_for(site)
        if transport_is_remote(transport):
            return editor.fail_result(
                "DeployTransportUnavailable",
                "Live SFTP cannot write. A separate deploy identity is required.",
                ssh=True,
            )
        try:
            payload = self.payloads.read(plan_id)
            proposed = editor.validate_text(payload["new_text"])
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except PathRejected:
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor.fail_result(exc.error_class, transport_message(exc.error_class))
        if sha256_digest(proposed.encode()) != plan.get("after_digest"):
            return editor.fail_result("DigestMismatch", "The stored diff does not match the plan.")
        if plan.get("created") is True:
            if not self._parent_ready(site, resolved):
                return editor.fail_result("PathRejected", "Kind or path is not editable.")
            try:
                editor.read_text(site, resolved)
            except TransportError as exc:
                if exc.error_class != "FileMissing":
                    return editor.fail_result(exc.error_class, transport_message(exc.error_class))
            else:
                return editor.fail_result("AlreadyExists", "The file already exists.")
        else:
            try:
                current = editor.read_text(site, resolved)
            except TransportError as exc:
                return editor.fail_result(exc.error_class, transport_message(exc.error_class))
            if sha256_digest(current.encode()) != plan.get("before_digest"):
                return editor.fail_result("StaleFile", "The file changed after the plan was created.")
        return site, plan, proposed, resolved, transport

    def preview_stage(self, plan_id: str, *, actor: str) -> WpOpsResult:
        """Tell the chat a server button is required. This method does not write."""
        prepared = self._prepare_stage(plan_id, actor=actor)
        if isinstance(prepared, WpOpsResult):
            return prepared
        site, plan, _proposed, _resolved, _transport = prepared
        return WpOpsResult(
            ok=True,
            message="approval required",
            data={
                "ok": True,
                "status": "approval_required",
                "executed": False,
                "approval_required": True,
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "risk": plan.get("risk"),
                "live_calls": False,
            },
        )

    def stage(self, plan_id: str, *, actor: str) -> WpOpsResult:
        """Write a staging plan on the local mock tree. Live transport is refused."""
        editor = self.editor
        prepared = self._prepare_stage(plan_id, actor=actor)
        if isinstance(prepared, WpOpsResult):
            return prepared
        site, plan, proposed, resolved, transport = prepared
        try:
            if plan.get("created") is True:
                transport.create_bytes(resolved, proposed.encode())
            else:
                transport.write_bytes(resolved, proposed.encode())
        except PathRejected:
            return editor.fail_result("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor.fail_result(exc.error_class, transport_message(exc.error_class))
        written = editor.read_text(site, resolved)
        if sha256_digest(written.encode()) != plan.get("after_digest"):
            return editor.fail_result("DigestMismatch", "The written file does not match the plan.")
        self._set_status(plan_id, "staged", actor=actor)
        self.payloads.delete(plan_id)
        editor.audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_stage",
            path=resolved,
            byte_length=len(written.encode()),
            plan_id=plan_id,
            digest=str(plan.get("after_digest") or ""),
        )
        return WpOpsResult(
            ok=True,
            message="staged",
            data={
                "ok": True,
                "status": "staged",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "executed": True,
                "ssh": False,
                "live_calls": False,
            },
        )

    def deny(self, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        plan = self._load_plan(plan_id, None)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        _site, denied = editor.resolve_site(str(plan.get("site_id") or ""), actor, write=True)
        if denied is not None:
            return denied
        if plan.get("status") not in {"pending_review", "validated"}:
            return editor.fail_result("PlanNotPending", "Edit plan is not pending.")
        self.payloads.delete(plan_id)
        self._set_status(plan_id, "denied", actor=actor)
        return WpOpsResult(
            ok=True,
            message="denied",
            data={
                "ok": True,
                "status": "denied",
                "plan_id": plan_id,
                "executed": False,
                "live_calls": False,
            },
        )

    def request_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        blocked = production_deploy_block()
        return WpOpsResult(
            ok=False,
            message="Production deploy is not available.",
            data={
                "ok": False,
                "status": "production_blocked",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                **blocked,
            },
        )

    def deploy_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self.request_production(site_id, plan_id, actor=actor)

    def verify(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        matched = False
        if plan.get("status") in {"staged", "deployed", "verified"}:
            try:
                resolved = resolve_file(
                    site.files_root,
                    str(plan.get("kind") or ""),
                    str(plan.get("slug") or ""),
                    str(plan.get("relative_path") or ""),
                )
                current = editor.read_text(site, resolved)
                matched = sha256_digest(current.encode()) == plan.get("after_digest")
            except (PathRejected, TransportError):
                matched = False
        return WpOpsResult(
            ok=True,
            message="verify",
            data={
                "ok": True,
                "status": "verified" if matched else plan.get("status") or "unknown",
                "plan_id": plan_id,
                "site_id": site.id,
                "hash_matches": matched,
                "page_checks": "not_run",
                "executed": False,
                "live_calls": False,
            },
        )

    def rollback(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        denied = editor.need(actor, Permission.WP_OPS_RESTORE, action="wp_ops_restore")
        if denied is not None:
            return denied
        plan = self._load_plan(plan_id, site_id)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        return editor.fail_result("RestoreNotInMvp", "Restore is not available in this phase.")

    def review(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor.fail_result("UnknownPlan", "Unknown edit plan.")
        diff = ""
        if plan.get("payload_present") is True and not plan_expired(plan, self._now()):
            try:
                diff = self.payloads.read(plan_id)["diff"]
            except TransportError:
                diff = ""
        public = {
            key: plan.get(key)
            for key in (
                "plan_id",
                "site_id",
                "environment",
                "kind",
                "slug",
                "relative_path",
                "before_digest",
                "after_digest",
                "risk",
                "summary",
                "status",
                "expires_at",
                "created",
            )
        }
        return WpOpsResult(
            ok=True,
            message="review",
            data={"ok": True, "plan": public, "diff": diff, "executed": False, "live_calls": False},
        )


__all__ = [
    "FileChangeOps",
    "FileEditor",
    "FilePayloadStore",
    "_redact_secret_lines",
    "delete_payload",
    "payload_path",
    "read_payload",
    "write_payload",
]
