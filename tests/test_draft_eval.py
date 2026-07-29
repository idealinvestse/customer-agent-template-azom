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


def test_check_draft_must_ask_order_id():
    draft = draft_reply(
        category=SupportCategory.RETURN,
        customer_name="Anna",
        order_id=None,
        language="sv",
    )
    checks = _check_draft(
        draft,
        {
            "language": "sv",
            "must_ask_order_id": True,
            "must_not_promise_refund": True,
        },
    )
    assert checks["checks"].get("asks_order_id") is True
    assert checks["checks"].get("no_refund_promise") is True


def test_billing_fixtures_in_pack():
    result = evaluate_drafts()
    ids = {r["id"] for r in result.get("results", [])} or set()
    # evaluate_drafts may nest differently — also check fixture load
    from ecom_ops.draft_eval import load_draft_fixtures

    fixture_ids = {f["id"] for f in load_draft_fixtures()}
    assert "billing_sv" in fixture_ids
    assert "billing_nb" in fixture_ids
    assert "return_missing_oid_sv" in fixture_ids
    assert result["ok"] is True
    assert result["avg_score"] >= 0.8
