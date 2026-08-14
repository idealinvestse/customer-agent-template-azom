"""Lexical FAQ search."""

from __future__ import annotations

from ecom_ops.faq.config import clear_faq_config_cache
from ecom_ops.faq.format import format_faq_citation_footer, format_faq_context_block
from ecom_ops.faq.models import FaqArticle
from ecom_ops.faq.search import market_from_language, search_faq
from ecom_ops.faq.store import FaqStore, clear_faq_store_cache


def test_market_from_language():
    assert market_from_language("sv") == "se"
    assert market_from_language("nb") == "no"
    assert market_from_language("da") == "dk"
    assert market_from_language("sv", market="no") == "no"


def test_search_shipping_tracking_se():
    clear_faq_store_cache()
    clear_faq_config_cache()
    hits = search_faq("Hur spårar jag mitt paket?", market="se", category="shipping")
    assert hits
    assert any("tracking" in h.article.id or "spår" in h.article.title.lower() for h in hits)


def test_never_returns_non_customer_safe():
    store = FaqStore(
        articles=[
            FaqArticle(
                id="internal-only",
                market="se",
                language="sv",
                category="shipping",
                title="Intern fraktregel",
                body="Hemlig leverantörskontrakt",
                tags=("frakt", "spårning"),
                customer_safe=False,
            ),
            FaqArticle(
                id="safe-one",
                market="se",
                language="sv",
                category="shipping",
                title="Spårning",
                body="Du får spårningslänk i mejl.",
                tags=("spårning",),
                customer_safe=True,
            ),
        ]
    )
    hits = search_faq("spårning", market="se", category="shipping", store=store)
    assert all(h.article.customer_safe for h in hits)
    assert all(h.article.id != "internal-only" for h in hits)


def test_format_context_block():
    clear_faq_config_cache()
    hits = search_faq("leveranstid", market="se", category="shipping", limit=2)
    block = format_faq_context_block(hits)
    if hits:
        assert hits[0].article.id in block
        footer = format_faq_citation_footer(hits)
        assert "FAQ refs" in footer


def test_synonym_expansion_finds_tracking():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("spåra paket", market="se", category="shipping")
    assert hits
    assert any("track" in h.article.id or "spår" in h.article.title.lower() for h in hits)


def test_min_score_filters_weak_category_only():
    clear_faq_config_cache()
    clear_faq_store_cache()
    # Nonsense query should not return weak category-only hits below min_score
    hits = search_faq("zzzznotatoken", market="se", category="shipping", min_score=1.0)
    assert hits == []


def test_search_respects_market():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("spårning", market="no", category="shipping")
    assert hits
    assert all(h.article.market == "no" for h in hits)


def test_format_context_empty():
    assert format_faq_context_block([]) == ""
    assert format_faq_citation_footer([]) == ""


def test_search_native_no_sporing():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("sporing", market="no", category="shipping")
    assert hits
    assert all(h.article.market == "no" for h in hits)
    assert any(h.article.id == "no-shipping-tracking" for h in hits)


def test_search_native_dk_fragt():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("fragt", market="dk", category="shipping")
    assert hits
    assert all(h.article.market == "dk" for h in hits)
    assert any("shipping" in h.article.id for h in hits)


def test_search_inflection_sparar_hits_tracking():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("spårar", market="se", category="shipping")
    assert hits
    assert any(
        h.article.id == "se-shipping-tracking" or "spår" in h.article.title.lower()
        for h in hits
    )


def test_search_empty_query_without_category_is_empty():
    clear_faq_config_cache()
    clear_faq_store_cache()
    assert search_faq("", market="se") == []


def test_search_shipping_ranks_above_return_for_tracking_query():
    clear_faq_config_cache()
    clear_faq_store_cache()
    hits = search_faq("spårning paket", market="se", category="shipping", limit=5)
    assert hits
    assert hits[0].article.category == "shipping"
    assert all(h.article.customer_safe for h in hits)


def test_search_title_phrase_bonus():
    store = FaqStore(
        articles=[
            FaqArticle(
                id="se-phrase-hit",
                market="se",
                language="sv",
                category="shipping",
                title="Spåra din leverans",
                body="Övrig text utan extra nyckelord.",
                tags=("annat",),
                customer_safe=True,
            ),
            FaqArticle(
                id="se-phrase-miss",
                market="se",
                language="sv",
                category="shipping",
                title="Retur av vara",
                body="Spåra din leverans nämns bara i brödtexten här.",
                tags=("spårning",),
                customer_safe=True,
            ),
        ]
    )
    hits = search_faq("Spåra din leverans", market="se", store=store, limit=2)
    assert hits
    assert hits[0].article.id == "se-phrase-hit"
