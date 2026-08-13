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
