"""SB4: keyword classify + suggest-approve regression from fixture pack."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.actions.support import classify_message, extract_order_id
from ecom_ops.cases.suggest import is_suggest_approve_eligible

FIX_DIR = Path(__file__).resolve().parent / "fixtures" / "support_classify"


def _load_fixtures() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(FIX_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    return rows


@pytest.mark.parametrize("fx", _load_fixtures(), ids=lambda f: f["id"])
def test_classify_fixture_keyword_and_suggest(fx: dict):
    text = fx["text"]
    cat = classify_message(text)
    assert cat.value == fx["expected_category"], fx["id"]

    expected_oid = fx.get("order_id_in_text")
    got_oid = extract_order_id(text)
    if expected_oid is None:
        assert got_oid is None
    else:
        assert got_oid == str(expected_oid)

    conf = float(fx.get("suggest_with_llm_confidence") or 0.0)
    # Escalated abuse never suggests
    escalated = cat.value == "abuse"
    suggest = is_suggest_approve_eligible(
        category=cat.value,
        confidence=conf,
        order_id=got_oid,
        escalated=escalated,
    )
    assert suggest is bool(fx["expect_suggest_approve"]), (
        f"{fx['id']}: suggest={suggest} conf={conf} cat={cat.value} oid={got_oid}"
    )


def test_never_suggest_return_billing_abuse_even_high_conf():
    for cat, oid in (
        ("return", "1001"),
        ("billing", "1001"),
        ("abuse", "1001"),
    ):
        assert (
            is_suggest_approve_eligible(
                category=cat,
                confidence=0.99,
                order_id=oid,
                escalated=cat == "abuse",
            )
            is False
        )


def test_fixture_pack_not_empty():
    assert len(_load_fixtures()) >= 6
