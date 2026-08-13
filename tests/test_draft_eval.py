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
    # evaluate_drafts may nest differently — also check fixture load
    from ecom_ops.draft_eval import load_draft_fixtures

    fixture_ids = {f["id"] for f in load_draft_fixtures()}
    assert "billing_sv" in fixture_ids
    assert "billing_nb" in fixture_ids
    assert "return_missing_oid_sv" in fixture_ids
    assert result["ok"] is True
    assert result["avg_score"] >= 0.8


def test_faq_fixture_must_include_and_forbid_refund():
    from ecom_ops.draft_eval import load_draft_fixtures

    fixtures = {f["id"]: f for f in load_draft_fixtures()}
    fx = fixtures["faq_shipping_sv"]
    good = (
        "Hej Anna,\n\nLeverans tar 1–3 arbetsdagar. Du kan spåra order 1001 "
        "via länken i bekräftelsen.\n\nMed vänlig hälsning\nAzom Support"
    )
    checks = _check_draft(good, fx)
    assert checks["checks"]["must_include_any"] is True
    assert checks["checks"]["no_refund_promise"] is True
    assert checks["checks"]["must_not_include"] is True

    bad = (
        "Hej,\n\nÅterbetalning garanteras och refund guaranteed för order 1001.\n\n"
        "Azom Support"
    )
    bad_checks = _check_draft(bad, fx)
    assert bad_checks["checks"]["no_refund_promise"] is False
    assert bad_checks["checks"]["must_not_include"] is False
