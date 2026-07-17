"""Draft-quality evaluation harness (P4.2).

Scores LLM-generated drafts against a fixture pack with expected quality
signals: presence of order_id, absence of fabricated tracking, sign-off,
language match, and length within bounds.

Run via CLI: ``python -m ecom_ops draft-eval``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "draft_quality"
)

# Quality signals checked per fixture
MAX_DRAFT_WORDS = 200
SIGN_OFF = "Azom Support"


def load_draft_fixtures(directory: Path | str | None = None) -> list[dict[str, Any]]:
    base = Path(directory) if directory else DEFAULT_FIXTURE_DIR
    rows: list[dict[str, Any]] = []
    if not base.is_dir():
        return rows
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    return rows


def _check_draft(draft: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """Check a single draft against expected quality signals."""
    text = (draft or "").strip()
    checks: dict[str, bool] = {}

    # 1. Non-empty
    checks["non_empty"] = bool(text)

    # 2. Word count within bounds
    word_count = len(text.split())
    checks["length_ok"] = word_count <= MAX_DRAFT_WORDS

    # 3. Contains sign-off
    checks["has_signoff"] = SIGN_OFF.lower() in text.lower()

    # 4. Contains order_id if expected
    exp_oid = fixture.get("order_id")
    if exp_oid:
        checks["has_order_id"] = str(exp_oid) in text
    else:
        checks["has_order_id"] = True  # N/A

    # 5. Does NOT contain fabricated tracking (unless provided in context)
    # Heuristic: look for sequences of 10+ digits that aren't the order_id
    import re

    digit_runs = re.findall(r"\d{10,}", text)
    expected_nums = {str(exp_oid)} if exp_oid else set()
    fabricated = [r for r in digit_runs if r not in expected_nums]
    checks["no_fabricated_tracking"] = len(fabricated) == 0

    # 6. Language match (if specified)
    exp_lang = fixture.get("language", "sv")
    if exp_lang == "sv":
        checks["language_sv"] = any(w in text.lower() for w in ["hej", "tack", "vänlig", "med vänlig", "ärende"])
    elif exp_lang == "en":
        checks["language_en"] = any(w in text.lower() for w in ["hello", "thank", "regards", "case"])
    else:
        checks["language_ok"] = True

    # 7. Does not contain abuse/legal promises (if flagged)
    if fixture.get("must_not_promise_refund"):
        checks["no_refund_promise"] = not any(
            w in text.lower() for w in ["återbetala", "refund", "pengarna tillbaka", "compensate"]
        )
    else:
        checks["no_refund_promise"] = True

    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    return {
        "checks": checks,
        "passed": passed,
        "total": total,
        "score": round(passed / total, 4) if total > 0 else 0.0,
        "word_count": word_count,
    }


def evaluate_drafts(
    drafts: list[str] | None = None,
    *,
    fixtures: list[dict[str, Any]] | None = None,
    directory: Path | str | None = None,
) -> dict[str, Any]:
    """Evaluate drafts against fixtures. If ``drafts`` is None, uses template fallback."""
    fx = fixtures if fixtures is not None else load_draft_fixtures(directory)
    if not fx:
        return {"ok": True, "n": 0, "message": "No draft fixtures found — skipping"}
    if drafts is None:
        # Use template fallback path to generate drafts
        from ecom_ops.actions.support import SupportService

        svc = SupportService()
        drafts = []
        for item in fx:
            msg = str(item.get("text") or "")
            oid = item.get("order_id")
            result = svc.handle(msg, order_id=oid, language=item.get("language", "sv"))
            drafts.append(result.reply or "")
    results = []
    total_score = 0.0
    for i, fixture in enumerate(fx):
        draft = drafts[i] if i < len(drafts) else ""
        check_result = _check_draft(draft, fixture)
        results.append({"id": fixture.get("id"), **check_result, "draft_excerpt": draft[:120]})
        total_score += check_result["score"]
    avg_score = round(total_score / len(fx), 4) if fx else 0.0
    return {
        "ok": avg_score >= 0.8,  # CI gate: 80% average quality
        "n": len(fx),
        "avg_score": avg_score,
        "results": results,
        "message": f"Draft eval: {len(fx)} fixtures, avg score {avg_score:.1%}",
    }
