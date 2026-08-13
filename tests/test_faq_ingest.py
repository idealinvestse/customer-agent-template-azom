"""Tests for FAQ site/product ingest, suggest, and promote."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ecom_ops.faq.ingest_config import clear_faq_ingest_config_cache
from ecom_ops.faq.ingest_products import ingest_products
from ecom_ops.faq.ingest_site import ingest_site
from ecom_ops.faq.promote import promote_article
from ecom_ops.faq.staging import articles_staging_dir, staging_counts
from ecom_ops.faq.suggest_articles import suggest_articles
from ecom_ops.integrations.wordpress import (
    InMemoryWpTransport,
    WordPressClient,
    extract_plain_text,
)


@pytest.fixture
def data_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    cfg = tmp_path / "config"
    (cfg / "faq" / "se").mkdir(parents=True)
    data.mkdir()
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    # Promote writes under AZOM_CONFIG_DIR; leave sites/rbac on repo config.
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_FAQ_INGEST_KILL", raising=False)
    clear_faq_ingest_config_cache()
    return data


@pytest.fixture
def oscar_actor():
    from ecom_ops.rbac import Actor

    return Actor(name="oscar", role="full_admin")


def test_extract_plain_text() -> None:
    html = "<p>Hej <strong>världen</strong></p><br/>Nästa rad"
    text = extract_plain_text(html)
    assert "Hej" in text and "världen" in text
    assert "<" not in text


def test_list_all_pages_and_posts() -> None:
    client = WordPressClient(
        base_url="https://mock.local", transport=InMemoryWpTransport()
    )
    pages = list(client.list_all_pages(status="publish"))
    posts = list(client.list_all_posts(status="publish"))
    assert any(p.title == "Om oss" for p in pages)
    assert any(p.title == "Hej från Azom" for p in posts)


def test_ingest_site_writes_staging(data_env: Path, oscar_actor) -> None:
    result = ingest_site(market="se", actor=oscar_actor, use_mock=True)
    assert result.ok, result.message
    assert result.written >= 1
    site_dir = data_env / "faq_staging" / "site" / "se"
    assert any(site_dir.glob("page_*.json"))
    counts = staging_counts("se")
    assert counts["site_docs"] >= 1


def test_ingest_products_and_suggest_promote(
    data_env: Path, oscar_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.integrations.woocommerce import (
        InMemoryWooTransport,
        WooCommerceClient,
    )

    transport = InMemoryWooTransport()
    transport.products["501"] = {
        "id": 501,
        "name": "Azom Pro Headset",
        "sku": "AZ-HS-PRO",
        "description": (
            "<p>Trådlöst headset med brusreducering för bil och kontor. "
            "Kompatibelt med vanliga Bluetooth-enheter.</p>"
        ),
        "short_description": "<p>Headset för daglig användning.</p>",
        "type": "simple",
        "categories": [{"id": 1, "name": "Tillbehör"}],
        "attributes": [{"name": "Anslutning", "options": ["Bluetooth"]}],
        "permalink": "https://azom.se/product/headset",
        "stock_status": "instock",
    }
    client = WooCommerceClient(
        base_url="https://mock.local", transport=transport, domain="se"
    )
    prod = ingest_products(
        market="se",
        actor=oscar_actor,
        use_mock=True,
        fetch_guides=False,
        client=client,
    )
    assert prod.ok, prod.message
    assert prod.products_written >= 1

    sug = suggest_articles(
        market="se",
        source="products",
        actor=oscar_actor,
        use_mock=True,
        limit=5,
    )
    assert sug.ok, sug.message
    assert sug.written >= 1

    drafts = list(articles_staging_dir("se").glob("*.yaml"))
    assert drafts
    raw = yaml.safe_load(drafts[0].read_text(encoding="utf-8"))
    article = raw[0] if isinstance(raw, list) else raw
    aid = article["id"]
    assert article["customer_safe"] is False
    assert article.get("needs_review") is True

    dry = promote_article(aid, market="se", apply=False, actor=oscar_actor)
    assert dry.ok and dry.dry_run
    dest = Path(dry.dest or "")
    assert not dest.exists()

    applied = promote_article(aid, market="se", apply=True, actor=oscar_actor)
    assert applied.ok and not applied.dry_run
    assert Path(applied.dest or "").is_file()

    from ecom_ops.rbac import Actor

    denied = promote_article(
        aid, market="se", apply=True, actor=Actor("jonatan", "viewer")
    )
    assert not denied.ok

    # Staging may request customer_safe; promote always forces false.
    article["customer_safe"] = True
    drafts[0].write_text(
        yaml.safe_dump([article], allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    forced = promote_article(aid, market="se", apply=True, actor=oscar_actor)
    assert forced.ok, forced.message
    written = yaml.safe_load(Path(forced.dest or "").read_text(encoding="utf-8"))
    row = written[0] if isinstance(written, list) else written
    assert row["customer_safe"] is False


def test_mock_product_ingest_skips_guide_http(
    data_env: Path, oscar_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.faq.ingest_products import _host_allowed, ingest_products

    assert _host_allowed("https://azom.se/guide", ("azom.se",))
    assert not _host_allowed("http://azom.se/guide", ("azom.se",))
    assert not _host_allowed("https://evil.example/x", ("azom.se",))

    import os

    cfg_dir = Path(os.environ["AZOM_CONFIG_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "faq_ingest.yaml").write_text(
        "guide_allowlist_hosts: [azom.se]\n"
        "guide_urls: [https://azom.se/guide]\n"
        "max_products_per_market: 10\n",
        encoding="utf-8",
    )
    clear_faq_ingest_config_cache()

    called = {"n": 0}

    def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("guide fetch must not run under mock")

    monkeypatch.setattr("ecom_ops.faq.ingest_products.requests.get", _boom)
    from ecom_ops.integrations.woocommerce import (
        InMemoryWooTransport,
        WooCommerceClient,
    )

    client = WooCommerceClient(
        base_url="https://mock.local",
        transport=InMemoryWooTransport(),
        domain="se",
    )
    result = ingest_products(
        market="se",
        actor=oscar_actor,
        use_mock=True,
        fetch_guides=True,
        client=client,
    )
    assert result.ok, result.message
    assert called["n"] == 0
    assert result.guides_written == 0
