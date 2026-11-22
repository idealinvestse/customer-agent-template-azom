"""Typed edits of one existing theme or plugin file.

The editor never accepts a shell command. It computes one path inside the
site's files root and asks a transport to list, read, or replace that file.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.models import Site, files_root_narrow
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.service import WpOpsResult
from ecom_ops.wp_ops.store import WpOpsStore

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_PLAN_ID = re.compile(r"^fe-[a-f0-9]{12}$")
_EXTENSIONS = frozenset({".php", ".css", ".js", ".json", ".txt", ".html"})
_MAX_BYTES = 65536
_KINDS = frozenset({"theme", "plugin"})


class FileTransport(Protocol):
    def list_dir(self, absolute_dir: str) -> list[str]: ...

    def read_bytes(self, absolute_file: str) -> bytes: ...

    def write_bytes(self, absolute_file: str, data: bytes) -> None: ...


class PathRejected(ValueError):
    """The path is outside the theme or plugin directory."""


class TransportError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


def transport_message(error_class: str) -> str:
    if error_class == "SshIdentityRejected":
        return "The SSH identity path was rejected."
    if error_class == "SshHostKeyMissing":
        return "A public host key is required before SSH."
    return "File transport failed."


def components_parent(root: str, kind: str) -> str:
    if kind not in _KINDS:
        raise PathRejected("kind")
    if not files_root_narrow(root):
        raise PathRejected("root")
    folder = "themes" if kind == "theme" else "plugins"
    return f"{root}/wp-content/{folder}"


def component_dir(root: str, kind: str, slug: str) -> str:
    if not _SLUG.fullmatch(slug):
        raise PathRejected("component")
    return components_parent(root, kind) + "/" + slug


def resolve_file(root: str, kind: str, slug: str, relative: str) -> str:
    """Return one absolute path inside the component, or reject it."""
    base = component_dir(root, kind, slug)
    text = str(relative or "").strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part != ""]
    if not parts or text.startswith("/") or any(part in {".", ".."} for part in parts):
        raise PathRejected("relative")
    if any(_SEGMENT.fullmatch(part) is None for part in parts):
        raise PathRejected("segment")
    leaf = parts[-1]
    if leaf.endswith(".azom-bak"):
        raise PathRejected("backup")
    suffix = Path(leaf).suffix.lower()
    if suffix not in _EXTENSIONS:
        raise PathRejected("extension")
    full = base + "/" + "/".join(parts)
    prefix = base + "/"
    if not full.startswith(prefix) or ".." in full.split("/"):
        raise PathRejected("escape")
    return full


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalTree:
    """Map the site's remote root onto a directory in AZOM_DATA_DIR."""

    def __init__(self, remote_root: str, local_root: Path) -> None:
        self.remote_root = remote_root.rstrip("/")
        self.local_root = local_root

    def ensure_demo(self) -> None:
        theme = self._local(self.remote_root + "/wp-content/themes/twentytwentyfive")
        plugin = self._local(self.remote_root + "/wp-content/plugins/hello")
        theme.mkdir(parents=True, exist_ok=True)
        plugin.mkdir(parents=True, exist_ok=True)
        style = theme / "style.css"
        hello = plugin / "hello.php"
        if not style.exists():
            style.write_text("/* mock theme */\n", encoding="utf-8")
        if not hello.exists():
            hello.write_text("<?php\n// mock plugin\n", encoding="utf-8")

    def list_dir(self, absolute_dir: str) -> list[str]:
        path = self._local(absolute_dir)
        if not path.is_dir():
            return []
        return sorted(item.name for item in path.iterdir() if item.name not in {".", ".."})

    def read_bytes(self, absolute_file: str) -> bytes:
        path = self._local(absolute_file)
        if not path.is_file():
            raise TransportError("FileMissing")
        return path.read_bytes()

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        path = self._local(absolute_file)
        if not path.is_file():
            raise TransportError("FileMissing")
        backup = path.with_name(path.name + ".azom-bak")
        backup.write_bytes(path.read_bytes())
        path.write_bytes(data)

    def _local(self, remote: str) -> Path:
        prefix = self.remote_root + "/"
        if remote != self.remote_root and not remote.startswith(prefix):
            raise PathRejected("root")
        relative = "" if remote == self.remote_root else remote[len(prefix) :]
        if ".." in relative.split("/"):
            raise PathRejected("root")
        local = (self.local_root / relative).resolve()
        root = self.local_root.resolve()
        if local != root and root not in local.parents:
            raise PathRejected("root")
        return local


class FileEditor:
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
    ) -> None:
        self.registry_path = registry_path
        self.store = store
        self.clock = clock
        self.factory = factory
        self.audit = audit
        self.need = need
        self.lock = lock

    def list_components(self, site_id: str, kind: str, *, actor: str) -> WpOpsResult:
        site, denied = self._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            parent = components_parent(site.files_root, kind)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        try:
            names = self._transport(site).list_dir(parent)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class), site=site, ssh=self._ssh(site))
        slugs = [name for name in names if _SLUG.fullmatch(name)]
        return WpOpsResult(
            ok=True,
            message="components",
            data={"site_id": site.id, "kind": kind, "slugs": slugs, "ssh": self._ssh(site), "live_calls": False},
        )

    def list_files(self, site_id: str, kind: str, slug: str, *, actor: str) -> WpOpsResult:
        site, denied = self._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            directory = component_dir(site.files_root, kind, slug)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        try:
            names = self._transport(site).list_dir(directory)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class), site=site, ssh=self._ssh(site))
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
                "ssh": self._ssh(site),
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
    ) -> WpOpsResult:
        site, denied = self._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            path = resolve_file(site.files_root, kind, slug, relative)
            body = self._read_text(site, path)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class), site=site, ssh=self._ssh(site))
        encoded = body.encode()
        self._audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_read",
            path=path,
            byte_length=len(encoded),
            digest=_digest(encoded),
        )
        return WpOpsResult(
            ok=True,
            message="file",
            data={
                "site_id": site.id,
                "path": path,
                "content": self._scrub(site, body),
                "byte_length": len(body.encode()),
                "digest": _digest(body.encode()),
                "ssh": self._ssh(site),
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
        site, denied = self._site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        locked = self.lock(site.id)
        if not locked.acquire(blocking=False):
            return self._fail("SiteBusy", "Site check already running", executed=False)
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
        site, denied = self._site(site_id, actor, write=True)
        if denied is not None:
            return denied
        if not _PLAN_ID.fullmatch(str(plan_id or "")):
            return self._fail("UnknownPlan", "Unknown edit plan.")
        assert isinstance(site, Site)
        locked = self.lock(site.id)
        if not locked.acquire(blocking=False):
            return self._fail("SiteBusy", "Site check already running", executed=False)
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
            current = self._read_text(site, path)
            proposed = self._validate_text(new_text)
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class), site=site, ssh=self._ssh(site))
        before = _digest(current.encode())
        after = _digest(proposed.encode())
        if before == after:
            return self._fail("NoChange", "The file is unchanged.")
        plan_id = "fe-" + uuid.uuid4().hex[:12]
        now = self.clock().isoformat()
        prefix = component_dir(site.files_root, kind, slug) + "/"
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
            "ssh": self._ssh(site),
        }
        self.store.update(lambda state: state.setdefault("file_edits", []).append(plan))
        self._audit_file(
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
        public = dict(plan)
        return WpOpsResult(
            ok=True,
            message="edit proposed",
            data={"plan": public, "executed": False, "ssh": self._ssh(site), "live_calls": False},
        )

    def _apply_locked(self, site: Site, plan_id: str, new_text: str, *, actor: str) -> WpOpsResult:
        state = self.store.load()
        plan = next(
            (row for row in state.get("file_edits", []) if isinstance(row, dict) and row.get("id") == plan_id),
            None,
        )
        if plan is None or plan.get("site_id") != site.id:
            return self._fail("UnknownPlan", "Unknown edit plan.")
        if plan.get("status") != "pending":
            return self._fail("PlanNotPending", "Edit plan is not pending.")
        try:
            proposed = self._validate_text(new_text)
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class))
        if _digest(proposed.encode()) != plan.get("after_digest"):
            return self._fail("DigestMismatch", "The text does not match the approved plan.")
        stored = str(plan.get("path") or "")
        try:
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except PathRejected:
            return self._fail("PathRejected", "Kind or path is not editable.")
        if resolved != stored:
            return self._fail("PathRejected", "Kind or path is not editable.")
        try:
            current = self._read_text(site, resolved)
        except TransportError as exc:
            return self._fail(exc.error_class, transport_message(exc.error_class), site=site, ssh=self._ssh(site))
        if _digest(current.encode()) != plan.get("before_digest"):
            return self._fail("StaleFile", "The file changed after the plan was created.")
        try:
            transport = self._transport(site)
            transport.write_bytes(resolved, proposed.encode())
            written = self._read_text(site, resolved)
        except (PathRejected, TransportError) as exc:
            error = exc.error_class if isinstance(exc, TransportError) else "PathRejected"
            return self._fail(error, transport_message(error), site=site, executed=False)
        if _digest(written.encode()) != plan.get("after_digest"):
            return self._fail("DigestMismatch", "The written file does not match the plan.", executed=False)
        applied_at = self.clock().isoformat()

        def mutate(current_state: dict[str, Any]) -> None:
            for row in current_state.get("file_edits", []):
                if isinstance(row, dict) and row.get("id") == plan_id:
                    row["status"] = "applied"
                    row["applied_at"] = applied_at
                    row["applied_by"] = actor

        self.store.update(mutate)
        self._audit_file(
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
                "ssh": self._ssh(site),
                "backup": resolved + ".azom-bak",
                "live_calls": False,
            },
        )

    def _site(self, site_id: str, actor: str, *, write: bool) -> tuple[Site | None, WpOpsResult | None]:
        try:
            sites = load_sites(self.registry_path, data_dir=self.store.path.parent)
        except ValueError as exc:
            return None, WpOpsResult(ok=False, message=str(exc), data={"error_class": "ConfigInvalid", "live_calls": False})
        site = site_by_id(sites, site_id)
        if site is None:
            return None, self._fail("UnknownSite", "Unknown site")
        permission = Permission.WP_OPS_READ
        if write and site.environment == "production":
            permission = Permission.WP_OPS_ADMIN
        elif write:
            permission = Permission.WP_OPS_OPERATE
        denied = self.need(actor, permission, site=site, action="wp_ops_file_edit")
        if denied is not None:
            return None, denied
        if write and site.environment == "production":
            denied = self.need(actor, Permission.CODE_EDIT, site=site, action="wp_ops_file_edit")
            if denied is not None:
                return None, denied
        if not site.files_root:
            return None, self._fail("FileRootUnconfigured", "The site has no files root.")
        if not files_root_narrow(site.files_root):
            return None, self._fail("PathRejected", "The files root is too broad.")
        return site, None

    def _transport(self, site: Site) -> FileTransport:
        transport = self.factory(site)
        if isinstance(transport, LocalTree):
            transport.ensure_demo()
        return transport

    def _ssh(self, site: Site) -> bool:
        return site.ssh_host != "" and not isinstance(self.factory(site), LocalTree)

    def _read_text(self, site: Site, path: str) -> str:
        data = self._transport(site).read_bytes(path)
        if len(data) > _MAX_BYTES:
            raise TransportError("FileTooLarge")
        if b"\x00" in data:
            raise TransportError("NotText")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("NotText") from exc

    def _validate_text(self, text: str) -> str:
        if not isinstance(text, str):
            raise TransportError("NotText")
        if "\x00" in text:
            raise TransportError("NotText")
        encoded = text.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise TransportError("FileTooLarge")
        return text

    def _scrub(self, site: Site, text: str) -> str:
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        cleaned = scrub_secrets(text, secrets)
        return cleaned if isinstance(cleaned, str) else text

    def _fail(
        self,
        error_class: str,
        message: str,
        *,
        site: Site | None = None,
        executed: bool = False,
        ssh: bool = False,
    ) -> WpOpsResult:
        del site
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": error_class, "executed": executed, "ssh": ssh, "live_calls": False},
        )

    def _audit_file(
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
                "ssh": self._ssh(site),
                "live_calls": False,
            },
        )
