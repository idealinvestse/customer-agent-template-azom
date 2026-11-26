"""Phrase-matrix & parity tests for unified WP intent regexes and shared page-review runner."""

from __future__ import annotations

import pytest

from ecom_ops.bot import change_clarifier, chat_agent, site_content, wp_intents
from ecom_ops.bot.change_clarifier import ChangeAssessment, build_fallback_clarification
from ecom_ops.bot.page_review_runtime import run_page_review_turn


@pytest.mark.parametrize(
    "phrase",
    [
        "ändra färgen på knappen",
        "fixa padding på kundvagnsikonen",
        "justera marginalen på köpknappen",
        "uppdatera style.css i barntemat",
        "skapa en ny tacksida med exempeldata",
        "kör wp_inspect_element på produktsidan",
        "kolla wc_template_overrides_audit för woocommerce/",
        "öppna arbetsyta wr-1234abcd",
    ],
)
def test_wp_code_intent_and_fallthrough_agree_on_ui_and_code_phrases(phrase: str) -> None:
    assert wp_intents.WP_CODE_INTENT_RE.search(phrase), f"WP_CODE_INTENT_RE missed: {phrase}"
    assert wp_intents.WP_CODE_FALLTHROUGH_RE.search(phrase), (
        f"WP_CODE_FALLTHROUGH_RE missed: {phrase}"
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "Kolla order 1001",
        "Hur många öppna ärenden har vi i supporten?",
        "Hej!",
        "Visa senaste 5 ordrarna",
    ],
)
def test_non_wp_phrases_do_not_trigger_wp_code_intent_or_fallthrough(phrase: str) -> None:
    assert not wp_intents.WP_CODE_INTENT_RE.search(phrase), phrase
    assert not wp_intents.WP_CODE_FALLTHROUGH_RE.search(phrase), phrase


def test_refusal_patterns_block_theme_switch_and_install_but_allow_theme_json() -> None:
    assert wp_intents.THEME_FALLTHROUGH_REFUSE_RE.search("byt tema till storefront")
    assert wp_intents.PACKAGE_FALLTHROUGH_REFUSE_RE.search("installera plugin yoast")
    assert wp_intents.SITE_REFUSAL_RE.search("byt tema till storefront")
    assert wp_intents.SITE_REFUSAL_RE.search("radera inlägg 5")

    assert not wp_intents.THEME_FALLTHROUGH_REFUSE_RE.search("uppdatera färger i theme.json")
    assert not wp_intents.PACKAGE_FALLTHROUGH_REFUSE_RE.search("uppdatera färger i theme.json")
    assert not wp_intents.SITE_REFUSAL_RE.search("uppdatera färger i theme.json")


@pytest.mark.parametrize(
    "phrase",
    [
        "gör en ny sida",
        "göra en helt ny tacksida",
        "gör en ny mall",
        "skapa en tacksida",
        "designa en landningssida",
        "ny tacksida",
    ],
)
def test_new_page_create_re_matches_genuine_new_page_requests(phrase: str) -> None:
    assert wp_intents.NEW_PAGE_CREATE_RE.search(phrase), phrase


@pytest.mark.parametrize(
    "phrase",
    [
        "gör sidan snyggare",
        "göra om sidan",
        "gör denna sida snabbare",
    ],
)
def test_new_page_create_re_does_not_match_iterative_edits(phrase: str) -> None:
    assert not wp_intents.NEW_PAGE_CREATE_RE.search(phrase), phrase


def test_bot_modules_reexport_canonical_wp_intents() -> None:
    assert chat_agent.WP_CODE_INTENT_RE is wp_intents.WP_CODE_INTENT_RE
    assert chat_agent.WP_OPS_INTENT_RE is wp_intents.WP_OPS_INTENT_RE
    assert site_content._WP_CODE_FALLTHROUGH_RE is wp_intents.WP_CODE_FALLTHROUGH_RE
    assert site_content._WP_WORKSPACE_INTENT_RE is wp_intents.WP_WORKSPACE_INTENT_RE
    assert change_clarifier._SITE_REFUSAL_RE is wp_intents.SITE_REFUSAL_RE
    assert change_clarifier._NEW_PAGE_CREATION_RE is wp_intents.NEW_PAGE_CREATE_RE


def test_run_page_review_turn_respects_require_site_id_and_loop_fn() -> None:
    calls: list[dict[str, object]] = []

    def fake_loop(**kwargs):
        calls.append(kwargs)
        return type("Outcome", (), {"text": "OK", "attachments": ()})()

    blocked = run_page_review_turn(
        text="granska startsidans källkod",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:7",
        wp_ops_context=None,
        loop_fn=fake_loop,
        require_site_id=True,
    )
    assert "/wp use" in blocked.text
    assert calls == []

    passthrough = run_page_review_turn(
        text="granska startsidans källkod",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:7",
        wp_ops_context=None,
        loop_fn=fake_loop,
        require_site_id=False,
    )
    assert passthrough.text == "OK"
    assert len(calls) == 1 and calls[0]["site_id"] == ""


@pytest.mark.parametrize(
    ("category", "screenshot", "expected_first_opt"),
    [
        ("wp_visual", False, "css"),
        ("wp_visual", True, "css"),
        ("wp_code", False, "workspace"),
        ("page_content", False, "text"),
        ("product", False, "price_stock"),
        ("site_settings", False, "identity"),
        ("order", False, "status"),
        ("general_change", False, "site_ui"),
    ],
)
def test_build_fallback_clarification_table_covers_all_categories(
    category: str, screenshot: bool, expected_first_opt: str
) -> None:
    assessment = ChangeAssessment(
        is_change_request=True,
        is_ambiguous=True,
        category=category,
        reasons=("oklarhet",),
        mentions_screenshot_or_annotation=screenshot,
    )
    proposal = build_fallback_clarification("ändra detta", assessment, site_id="mock-staging")
    assert proposal.category == category
    assert len(proposal.options) == 3
    assert proposal.options[0].id == expected_first_opt
    assert proposal.options[0].recommended is True
    if screenshot:
        assert "bild eller markering" in proposal.understanding
