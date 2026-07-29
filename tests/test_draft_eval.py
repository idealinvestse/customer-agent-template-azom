"""Draft-eval harness regression (azom.no vNext T1)."""

from __future__ import annotations

from ecom_ops.actions.support import SupportCategory, draft_reply
from ecom_ops.draft_eval import _check_draft, evaluate_drafts


def test_evaluate_drafts_runs_without_order_id_kwarg():
    result = evaluate_drafts()
    assert result["n"] >= 1
    assert "avg_score" in result
    assert result["ok"] is True


def test_check_draft_language_nb():
    draft = draft_reply(
        category=SupportCategory.ORDER_STATUS,
        customer_name="Ola",
        order_id="1001",
        language="nb",
    )
    checks = _check_draft(draft, {"language": "nb", "order_id": "1001"})
    assert checks["checks"].get("language_nb") is True
    assert checks["score"] >= 0.8
