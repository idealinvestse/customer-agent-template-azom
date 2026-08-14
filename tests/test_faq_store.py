"""FAQ corpus store loading."""

from __future__ import annotations

import yaml

from ecom_ops.faq.store import FaqStore, clear_faq_store_cache


def test_load_seed_corpus_se(monkeypatch):
    clear_faq_store_cache()
    store = FaqStore()
    se = store.list(market="se", customer_safe_only=True)
    assert len(se) >= 5
    ids = {a.id for a in se}
    assert "se-shipping-tracking" in ids
    assert "se-return-window" in ids


def test_overlay_wins_on_same_id(tmp_path, monkeypatch):
    clear_faq_store_cache()
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    overlay = tmp_path / "faq" / "se"
    overlay.mkdir(parents=True)
    (overlay / "override.yaml").write_text(
        yaml.dump(
            [
                {
                    "id": "se-shipping-tracking",
                    "market": "se",
                    "language": "sv",
                    "category": "shipping",
                    "title": "Overlay title",
                    "body": "Override body for tracking.",
                    "customer_safe": True,
                    "tags": ["overlay"],
                }
            ]
        ),
        encoding="utf-8",
    )
    store = FaqStore()
    art = store.get("se-shipping-tracking")
    assert art is not None
    assert art.title == "Overlay title"


def test_parse_from_explicit_list():
    store = FaqStore(
        articles=[]
    )
    # empty store
    assert store.list() == []


def test_coverage_counts_seed_corpus():
    clear_faq_store_cache()
    store = FaqStore()
    cov = store.coverage()
    assert cov["total"] >= 5
    assert cov["markets"]["se"]["total"] >= 5
    assert cov["markets"]["se"]["categories"].get("shipping", 0) >= 1
    assert "gaps_vs_se" in cov
    assert cov["gaps_vs_se"]["no"]["missing_categories"] == []
    assert cov["gaps_vs_se"]["dk"]["missing_categories"] == []
    assert cov["gaps_vs_se"]["no"]["missing_suffixes"] == []
    assert cov["gaps_vs_se"]["dk"]["missing_suffixes"] == []


def test_needs_review_parsed_and_missing_customer_safe_warns(tmp_path, monkeypatch):
    from ecom_ops.faq.store import load_faq_corpus

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    overlay = tmp_path / "faq" / "se"
    overlay.mkdir(parents=True)
    (overlay / "review.yaml").write_text(
        yaml.dump(
            [
                {
                    "id": "se-overlay-review",
                    "market": "se",
                    "language": "sv",
                    "category": "shipping",
                    "title": "Utkast som behöver granskning",
                    "body": "Tillräckligt lång brödtext för en giltig artikel.",
                    "needs_review": True,
                    "customer_safe": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    report = load_faq_corpus()
    art = next(a for a in report.articles if a.id == "se-overlay-review")
    assert art.needs_review is True
    assert art.customer_safe is False

    (overlay / "omit-safe.yaml").write_text(
        yaml.dump(
            [
                {
                    "id": "se-overlay-omit-safe",
                    "market": "se",
                    "language": "sv",
                    "category": "shipping",
                    "title": "Saknar customer_safe-nyckel",
                    "body": "Tillräckligt lång brödtext för en giltig artikel.",
                }
            ]
        ),
        encoding="utf-8",
    )
    report2 = load_faq_corpus()
    assert any(
        i.level == "warning" and "customer_safe omitted" in i.message
        for i in report2.issues
        if i.article_id == "se-overlay-omit-safe"
    )


def test_unsafe_parity_articles_excluded_from_default_search():
    from ecom_ops.faq.search import search_faq

    clear_faq_store_cache()
    hits = search_faq("faktura", market="no", category="billing")
    assert all(h.article.customer_safe for h in hits)
    assert all(h.article.id != "no-billing-invoice" for h in hits)


def test_load_faq_corpus_reports_invalid_overlay(tmp_path, monkeypatch):
    from ecom_ops.faq.store import load_faq_corpus

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    overlay = tmp_path / "faq" / "se"
    overlay.mkdir(parents=True)
    (overlay / "bad.yaml").write_text(
        yaml.dump(
            [
                {
                    "id": "",
                    "market": "se",
                    "language": "sv",
                    "category": "shipping",
                    "title": "Saknar id",
                    "body": "x",
                }
            ]
        ),
        encoding="utf-8",
    )
    report = load_faq_corpus()
    assert any(i.level == "error" and "missing id" in i.message for i in report.issues)
