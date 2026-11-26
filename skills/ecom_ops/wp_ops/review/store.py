"""Plan persistence, locking, expiry, and public projection for WordPress review plans.

All ``wp_review_plans.json`` mutations run under ``_STORE`` + ``advisory_file_lock``
in a fixed lock order (``site_lock -> plan_lock -> _STORE -> advisory_file_lock``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.locks import advisory_file_lock
from ecom_ops.wp_ops.locks import site_lock as _site_lock
from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.review.checks import public_policy
from ecom_ops.wp_ops.review.runner import SandboxGate
from ecom_ops.wp_ops.review.workspace_io import (
    PLAN_ID,
    SLUG,
    ReviewError,
    delete_work,
    iter_files,
    read_diff,
    tree_hash,
    unified_diff_text,
    workspace_root,
    write_manifest,
)
from ecom_ops.wp_ops.store import atomic_write_text

_TTL = timedelta(hours=12)
_PREVIEW_TTL = timedelta(hours=2)
_STORE = threading.RLock()
_PLAN_LOCKS: dict[str, threading.Lock] = {}
_PLAN_GUARD = threading.Lock()
_TEMPLATE_HEADER = re.compile(
    r"^\s*Template:\s*([a-z0-9][a-z0-9_-]{0,60})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
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


def _unified(root: Path) -> str:
    base_files = dict(iter_files(root / "base"))
    work_files = dict(iter_files(root / "work"))
    return unified_diff_text(base_files, work_files)


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


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Project one review plan into its canonical secret-free public dictionary."""
    clean = {key: plan.get(key) for key in _PUBLIC}
    checks = []
    for row in plan.get("checks") or []:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {
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
        for key in (
            "run_id",
            "site_id",
            "plan_id",
            "base_sha256",
            "work_sha256",
            "profile",
            "profile_version",
        ):
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
                {
                    "actor": str(row.get("actor") or ""),
                    "at": str(row.get("at") or ""),
                    "text": row["text"][:500],
                }
            )
    clean["comments"] = comments
    return clean


def same_target(older: dict[str, Any], plan: dict[str, Any]) -> bool:
    return (
        older.get("site_id") == plan.get("site_id")
        and older.get("kind") == plan.get("kind")
        and older.get("slug") == plan.get("slug")
        and older.get("plan_id") != plan.get("plan_id")
    )


class PlanStore:
    """Thread-safe and process-safe persistence for ``wp_review_plans.json``."""

    def __init__(
        self,
        *,
        data_dir: Path,
        clock: Callable[[], datetime],
        gate: SandboxGate,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "wp_review_plans.json"
        self.clock = clock
        self.gate = gate

    def now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    def is_past(self, plan: dict[str, Any]) -> bool:
        expires = _parse_time(str(plan.get("expires_at") or ""))
        if expires is None:
            return True
        return self.now() >= expires

    def locks(self, site_id: str, plan_id: str) -> _LockBoth:
        return _LockBoth(_site_lock(site_id), _plan_lock(plan_id))

    @contextlib.contextmanager
    def file_lock(self) -> Iterator[None]:
        """Advisory cross-process lock around ``wp_review_plans.json`` mutations."""
        with advisory_file_lock(self.path.with_suffix(".json.lock")):
            yield

    def load(self) -> list[dict[str, Any]]:
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

    def save(self, plans: list[dict[str, Any]]) -> None:
        payload = json.dumps(
            {"plans": [public_plan(row) for row in plans]},
            ensure_ascii=False,
            indent=2,
        )
        atomic_write_text(self.path, payload)

    def replace(
        self,
        plan: dict[str, Any],
        *,
        save_fn: Callable[[list[dict[str, Any]]], None] | None = None,
        file_lock_fn: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
    ) -> None:
        writer = save_fn or self.save
        locker = file_lock_fn or self.file_lock
        with _STORE, locker():
            plans = self.load()
            replaced = False
            for index, row in enumerate(plans):
                if row.get("plan_id") == plan.get("plan_id"):
                    plans[index] = public_plan(plan)
                    replaced = True
                    break
            if not replaced:
                plans.append(public_plan(plan))
            writer(plans)

    def append_and_supersede(
        self,
        plan: dict[str, Any],
        *,
        variant_group: str = "",
        save_fn: Callable[[list[dict[str, Any]]], None] | None = None,
        file_lock_fn: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
    ) -> None:
        """Atomically supersede prior non-terminal plans for the same target and append ``plan``."""
        writer = save_fn or self.save
        locker = file_lock_fn or self.file_lock
        plan_id = str(plan.get("plan_id") or "")
        with _STORE, locker():
            plans = self.load()
            for older in plans:
                if not same_target(older, plan) or older.get("status") in _TERMINAL:
                    continue
                if variant_group and str(older.get("variant_group") or "") == variant_group:
                    continue
                older["superseded_by"] = plan_id
            plans.append(public_plan(plan))
            writer(plans)

    def mark_expired(
        self,
        plan: dict[str, Any],
        *,
        replace_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        replacer = replace_fn or self.replace
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
            delete_work(root)
        except ReviewError:
            root = None
        plan["status"] = "expired"
        plan["sandbox"] = self.gate.destroy(dict(plan.get("sandbox") or {}))
        replacer(plan)
        if root is not None:
            write_manifest(root, plan)
        return plan

    def find_row(
        self,
        site_id: str,
        plan_id: str,
        *,
        replace_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        if not PLAN_ID.fullmatch(str(plan_id or "")):
            return None
        replacer = replace_fn or self.replace
        for plan in self.load():
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
                        replacer(plan)
                return plan
        return None

    def get_or_expire(
        self,
        site_id: str,
        plan_id: str,
        *,
        replace_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Load one plan once, expiring it in-place if past deadline (no double JSON read)."""
        plan = self.find_row(site_id, plan_id, replace_fn=replace_fn)
        if plan is None:
            return None
        if plan.get("status") != "expired" and self.is_past(plan):
            self.mark_expired(plan, replace_fn=replace_fn)
        return plan

    def fresh_guard(
        self,
        plan: dict[str, Any],
        *,
        allow_superseded_read: bool = False,
        replace_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> WpOpsResult | None:
        if plan.get("status") == "not_resumable":
            return _fail(
                "WorkspaceNotResumable",
                "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                plan_id=plan.get("plan_id"),
                status="not_resumable",
            )
        if plan.get("superseded_by") and not allow_superseded_read:
            return _fail("StalePlan", "En nyare diff har ett annat plan-id.", plan_id=plan.get("plan_id"))
        if plan.get("status") == "expired" or self.is_past(plan):
            self.mark_expired(plan, replace_fn=replace_fn)
            return _fail("Expired", "Planen har gått ut.", plan_id=plan.get("plan_id"), status="expired")
        return None

    def gate_guard(
        self,
        plan: dict[str, Any],
        base_sha256: str,
        work_sha256: str,
        *,
        replace_fn: Callable[[dict[str, Any]], None] | None = None,
    ) -> WpOpsResult | None:
        blocked = self.fresh_guard(plan, replace_fn=replace_fn)
        if blocked is not None:
            return blocked
        if base_sha256 != plan.get("base_sha256") or work_sha256 != plan.get("work_sha256"):
            return _fail("DigestMismatch", "Hasharna matchar inte planen.", plan_id=plan.get("plan_id"))
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
        except ReviewError as exc:
            return _fail(exc.error_class, exc.error_class)
        if (
            tree_hash(root / "base") != plan.get("base_sha256")
            or tree_hash(root / "work") != plan.get("work_sha256")
        ):
            return _fail("DigestMismatch", "Hasharna matchar inte arbetsytan.", plan_id=plan.get("plan_id"))
        return None

    def view(self, plan: dict[str, Any], *, include_diff: bool) -> dict[str, Any]:
        projected = public_plan(plan)
        if not include_diff:
            projected.pop("comments", None)
            projected.pop("summary", None)
            return projected
        try:
            root = workspace_root(self.data_dir, str(plan.get("site_id")), str(plan.get("plan_id")))
            projected["diff"] = read_diff(root)
        except ReviewError:
            projected["diff"] = ""
        return projected


__all__ = [
    "PlanStore",
    "_ARTIFACT_PNG",
    "_BASELINE_REUSE_TTL_S",
    "_FORK_BLOCKED_STATUSES",
    "_LockBoth",
    "_NON_EDITABLE",
    "_PLAN_GUARD",
    "_PLAN_LOCKS",
    "_PNG_MAGIC",
    "_PREVIEW_TTL",
    "_PUBLIC",
    "_SANDBOX_ONLY_FILES",
    "_STAGE_OK",
    "_STORE",
    "_TEMPLATE_HEADER",
    "_TERMINAL",
    "_TTL",
    "_UNREACHABLE",
    "_VARIANT_GROUP_RE",
    "_clean_variant_group",
    "_diff_ops",
    "_fail",
    "_generation",
    "_ok",
    "_parse_time",
    "_plan_lock",
    "_prepared",
    "_resolve_parent_theme",
    "_sha",
    "_text_files",
    "_unified",
    "public_plan",
    "same_target",
]
