"""Export Q&A pairs from cases.db (inbound → sent outbound)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ecom_ops.cases.store import Case, CaseStore
from ecom_ops.faq.ingest_config import (
    effective_use_mock,
    faq_ingest_killed,
    load_faq_ingest_config,
)
from ecom_ops.faq.pii import redact_pii
from ecom_ops.faq.rbac_ingest import require_faq_ingest
from ecom_ops.rbac import AccessDenied, Actor


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None


def _dataset_dir() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    path = base / "faq_dataset"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class QaPair:
    case_id: str
    market: str
    language: str
    category: str
    question: str
    answer: str
    answered_at: str
    order_id_present: bool
    stale: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetExportResult:
    ok: bool
    message: str
    path: str | None = None
    manifest_path: str | None = None
    pair_count: int = 0
    stale_count: int = 0
    truncated: bool = False
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "path": self.path,
            "manifest_path": self.manifest_path,
            "pair_count": self.pair_count,
            "stale_count": self.stale_count,
            "truncated": self.truncated,
            "details": self.details or {},
        }


def _pair_messages(
    case: Case,
    messages: list[Any],
    *,
    min_reply_chars: int,
    max_age_days: int,
    now: datetime,
) -> list[QaPair]:
    pairs: list[QaPair] = []
    pending_q: str | None = None
    for msg in messages:
        direction = getattr(msg, "direction", "") or ""
        body = (getattr(msg, "body", None) or "").strip()
        if direction == "inbound" and body:
            pending_q = body
            continue
        if direction == "outbound" and body and pending_q:
            if len(body) < min_reply_chars:
                pending_q = None
                continue
            answered = _parse_ts(getattr(msg, "created_at", None)) or now
            age_days = (now - answered).total_seconds() / 86400.0
            stale = age_days > max_age_days
            pairs.append(
                QaPair(
                    case_id=case.id,
                    market=(case.market or "se").lower(),
                    language=case.language or "sv",
                    category=case.category or "other",
                    question=pending_q,
                    answer=body,
                    answered_at=answered.isoformat(),
                    order_id_present=bool(case.order_id),
                    stale=stale,
                    status=case.status,
                )
            )
            pending_q = None
    return pairs


def export_qa_pairs(
    *,
    market: str | None = None,
    since_days: int | None = None,
    include_stale: bool = True,
    redact: bool = True,
    actor: Actor | str | None = None,
    store: CaseStore | None = None,
    limit_cases: int = 5000,
    use_mock: bool | None = False,
) -> DatasetExportResult:
    """Export inbound→outbound Q&A pairs from cases.db."""
    mock = effective_use_mock(use_mock)
    try:
        actor_obj = require_faq_ingest(actor, use_mock=mock)
    except AccessDenied as exc:
        return DatasetExportResult(ok=False, message=str(exc))

    if faq_ingest_killed():
        return DatasetExportResult(
            ok=False, message="FAQ ingest kill-switch active"
        )
    if redact is False and actor_obj.name != "oscar":
        return DatasetExportResult(
            ok=False,
            message="Raw (--raw) dataset export requires actor oscar",
        )

    cfg = load_faq_ingest_config()
    case_store = store or CaseStore()
    cases = case_store.list_cases(status="replied,closed", limit=limit_cases)
    truncated = len(cases) >= limit_cases
    mkt_filter = market.strip().lower() if market else None
    now = _now()
    since_cutoff = None
    if since_days is not None and since_days > 0:
        since_cutoff = now - timedelta(days=since_days)

    pairs: list[QaPair] = []
    for case in cases:
        if mkt_filter and (case.market or "").lower() != mkt_filter:
            continue
        cat = (case.category or "").lower()
        if cat in cfg.dataset_exclude_categories:
            continue
        msgs = case_store.messages(case.id)
        for pair in _pair_messages(
            case,
            msgs,
            min_reply_chars=cfg.dataset_min_reply_chars,
            max_age_days=cfg.dataset_max_age_days,
            now=now,
        ):
            answered = _parse_ts(pair.answered_at)
            if since_cutoff and answered and answered < since_cutoff:
                continue
            if not include_stale and pair.stale:
                continue
            if redact:
                pair.question = redact_pii(pair.question)
                pair.answer = redact_pii(pair.answer)
            pairs.append(pair)

    day = now.strftime("%Y%m%d")
    label = mkt_filter or "all"
    suffix = ".raw.jsonl" if not redact else ".jsonl"
    out_path = _dataset_dir() / f"qa_{label}_{day}{suffix}"
    with out_path.open("w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")

    stale_count = sum(1 for p in pairs if p.stale)
    manifest = {
        "created_at": now.isoformat(),
        "market": label,
        "pair_count": len(pairs),
        "stale_count": stale_count,
        "redacted": redact,
        "actor": actor_obj.name,
        "path": str(out_path),
        "max_age_days": cfg.dataset_max_age_days,
        "exclude_categories": list(cfg.dataset_exclude_categories),
        "truncated": truncated,
        "limit_cases": limit_cases,
    }
    man_stem = f"qa_{label}_{day}.raw" if not redact else f"qa_{label}_{day}"
    man_path = _dataset_dir() / f"{man_stem}.manifest.json"
    man_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return DatasetExportResult(
        ok=True,
        message=(
            f"Exported {len(pairs)} Q&A pairs ({stale_count} stale)"
            + (f"; truncated at {limit_cases} cases" if truncated else "")
        ),
        path=str(out_path),
        manifest_path=str(man_path),
        pair_count=len(pairs),
        stale_count=stale_count,
        truncated=truncated,
        details=manifest,
    )
