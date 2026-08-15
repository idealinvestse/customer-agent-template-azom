"""Redacted classify/draft calibration export + report (Oscar ADMIN).

Never includes subject, body, from/to, or draft text. Humans still own
threshold changes and A1 soak outcome.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.cases.store import Case, CaseStore

FORBIDDEN_EXPORT_KEYS = frozenset(
    {
        "subject",
        "body",
        "from_addr",
        "to_addr",
        "draft",
        "draft_reply",
        "draft_before_regen",
        "text",
        "message",
    }
)

ALLOWED_SAMPLE_KEYS = (
    "id",
    "status",
    "category",
    "classify_confidence",
    "classify_method",
    "suggest_approve",
    "has_order_id",
    "mailbox_id",
    "market",
    "language",
    "shadow_eligible",
    "shadow_deny_reason",
    "human_outcome",
    "created_at",
    "updated_at",
)


def _human_outcome(case: Case) -> str:
    status = (case.status or "").strip().lower()
    if status == "replied":
        return "approved_sent"
    if status == "closed":
        return "closed_no_reply"
    if status == "escalated":
        return "escalated"
    if status == "sending":
        return "sending"
    return "open"


def redact_case_sample(case: Case) -> dict[str, Any]:
    """Allowlisted fields only — no PII / draft / subject / body."""
    return {
        "id": case.id,
        "status": case.status,
        "category": case.category,
        "classify_confidence": case.classify_confidence,
        "classify_method": case.classify_method,
        "suggest_approve": bool(case.suggest_approve),
        "has_order_id": bool((case.order_id or "").strip()),
        "mailbox_id": case.mailbox_id,
        "market": case.market,
        "language": case.language,
        "shadow_eligible": case.shadow_eligible,
        "shadow_deny_reason": case.shadow_deny_reason,
        "human_outcome": _human_outcome(case),
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


def export_calibration_samples(
    *,
    days: int = 30,
    store: CaseStore | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    days = max(1, int(days or 30))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    st = store or CaseStore()
    cases = st.list_cases(status="all", limit=limit)
    samples = [
        redact_case_sample(c)
        for c in cases
        if (c.updated_at or "") >= since
    ]
    for sample in samples:
        leaked = FORBIDDEN_EXPORT_KEYS.intersection(sample)
        if leaked:
            raise ValueError(f"calibration export leaked keys: {sorted(leaked)}")
    return {
        "ok": True,
        "days": days,
        "n": len(samples),
        "samples": samples,
        "message": (
            f"Redacted calibration export: {len(samples)} samples ({days}d). "
            "Does not mark live classify calibration complete."
        ),
    }


def build_calibration_report(
    *,
    days: int = 30,
    store: CaseStore | None = None,
    samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare classifier flags vs human outcomes (status)."""
    if samples is None:
        payload = export_calibration_samples(days=days, store=store)
        samples = list(payload["samples"])
        days = int(payload["days"])
    by_outcome: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    suggest_n = 0
    suggest_approved = 0
    suggest_closed = 0
    suggest_escalated = 0
    never_suggest_starred = 0
    never_cats = {"abuse", "return", "billing"}
    for s in samples:
        outcome = str(s.get("human_outcome") or "")
        cat = str(s.get("category") or "")
        by_outcome[outcome] += 1
        by_category[cat] += 1
        if s.get("suggest_approve"):
            suggest_n += 1
            if outcome == "approved_sent":
                suggest_approved += 1
            elif outcome == "closed_no_reply":
                suggest_closed += 1
            elif outcome == "escalated":
                suggest_escalated += 1
            if cat in never_cats:
                never_suggest_starred += 1
    return {
        "ok": True,
        "days": days,
        "n": len(samples),
        "by_outcome": dict(by_outcome),
        "by_category": dict(by_category),
        "suggest_n": suggest_n,
        "suggest_approved_sent": suggest_approved,
        "suggest_closed_no_reply": suggest_closed,
        "suggest_escalated": suggest_escalated,
        "never_suggest_starred": never_suggest_starred,
        "calibration_complete": False,
        "message": (
            f"Calibration report: {len(samples)} samples, {suggest_n} ★. "
            "Humans own threshold changes; agents must not lower rails."
        ),
    }


def reviewed_sample_to_fixture(sample: dict[str, Any]) -> dict[str, Any]:
    """Build a classify fixture from a *human-reviewed* sample that includes text.

    Redacted exports cannot be converted (no inbound text). Callers must supply
    ``text`` and ``expected_category`` after human review.
    """
    text = str(sample.get("text") or "").strip()
    expected = str(sample.get("expected_category") or sample.get("category") or "").strip()
    if not text or not expected:
        raise ValueError(
            "reviewed sample needs text + expected_category "
            "(redacted export is not enough)"
        )
    fid = str(sample.get("id") or "reviewed").replace("/", "_")[:48]
    return {
        "id": fid,
        "text": text,
        "expected_category": expected,
        "order_id_in_text": sample.get("order_id_in_text"),
        "suggest_with_llm_confidence": float(
            sample.get("suggest_with_llm_confidence")
            or sample.get("classify_confidence")
            or 0.0
        ),
        "expect_suggest_approve": bool(sample.get("expect_suggest_approve", False)),
    }


def write_fixture_from_reviewed(
    sample: dict[str, Any],
    *,
    directory: Path | str,
) -> Path:
    fixture = reviewed_sample_to_fixture(sample)
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{fixture['id']}.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
