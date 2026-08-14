"""FAQ dataset/staging retention (GDPR hygiene, not a personal data register)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ecom_ops.faq.rbac_ingest import require_faq_promote
from ecom_ops.faq.staging import data_dir, staging_root
from ecom_ops.rbac import AccessDenied, Actor

DEFAULT_STAGING_RETENTION_DAYS = 90


@dataclass
class FaqRetentionResult:
    ok: bool
    message: str
    dry_run: bool = True
    dataset_rows_removed: int = 0
    files_removed: int = 0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "dry_run": self.dry_run,
            "dataset_rows_removed": self.dataset_rows_removed,
            "files_removed": self.files_removed,
            "details": self.details or {},
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(path: Path, now: datetime) -> float:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 86400.0


def purge_dataset_case_ids(case_ids: Iterable[str]) -> int:
    """Drop faq_dataset JSONL rows whose case_id is in ``case_ids``."""
    wanted = {str(c).strip() for c in case_ids if str(c).strip()}
    if not wanted:
        return 0
    ds = data_dir() / "faq_dataset"
    if not ds.is_dir():
        return 0
    removed = 0
    for path in sorted(ds.glob("*.jsonl")):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        kept: list[str] = []
        file_removed = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            cid = str(row.get("case_id") or "").strip()
            if cid in wanted:
                file_removed += 1
                continue
            kept.append(line)
        if file_removed == 0:
            continue
        removed += file_removed
        if kept:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
            man = path.with_name(path.name.replace(".jsonl", ".manifest.json"))
            if man.is_file():
                man.unlink(missing_ok=True)
    return removed


def _iter_staging_files() -> list[Path]:
    root = staging_root()
    out: list[Path] = []
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            out.append(path)
    ds = data_dir() / "faq_dataset"
    if ds.is_dir():
        for path in ds.iterdir():
            if path.is_file():
                out.append(path)
    return out


def purge_staging_older_than(
    *,
    days: int = DEFAULT_STAGING_RETENTION_DAYS,
    apply: bool = False,
    actor: Actor | str | None = None,
    now: datetime | None = None,
) -> FaqRetentionResult:
    """Remove staging/dataset files older than ``days`` (Oscar / FAQ_PUBLISH)."""
    try:
        actor_obj = require_faq_promote(actor)
    except AccessDenied as exc:
        return FaqRetentionResult(ok=False, message=str(exc), dry_run=not apply)

    if days < 1:
        return FaqRetentionResult(
            ok=False, message="days must be >= 1", dry_run=not apply
        )

    cutoff_now = now or _now()
    stale: list[str] = []
    for path in _iter_staging_files():
        try:
            age = _age_days(path, cutoff_now)
        except OSError:
            continue
        if age > days:
            stale.append(str(path))

    if not apply:
        return FaqRetentionResult(
            ok=True,
            message=f"Dry-run staging purge: {len(stale)} files older than {days}d",
            dry_run=True,
            files_removed=0,
            details={"actor": actor_obj.name, "eligible": stale[:50], "eligible_count": len(stale)},
        )

    removed = 0
    for raw in stale:
        path = Path(raw)
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return FaqRetentionResult(
        ok=True,
        message=f"Purged {removed} staging/dataset files older than {days}d",
        dry_run=False,
        files_removed=removed,
        details={"actor": actor_obj.name},
    )


def purge_faq_after_cases_retention(
    *,
    case_ids: Iterable[str],
    days: int = DEFAULT_STAGING_RETENTION_DAYS,
    apply: bool = True,
    actor: Actor | str | None = "oscar",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dataset case_id scrub + aged staging purge (best-effort)."""
    rows = 0
    if apply:
        rows = purge_dataset_case_ids(case_ids)
    staging = purge_staging_older_than(
        days=days, apply=apply, actor=actor, now=now
    )
    return {
        "dataset_rows_removed": rows,
        "staging": staging.to_dict(),
    }
