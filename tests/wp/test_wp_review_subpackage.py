"""Unit tests for the ``ecom_ops.wp_ops.review`` subpackage and compatibility facades."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ecom_ops.wp_ops import (
    page_review,
    page_review_intent,
    page_review_loop,
    preview_overlay,
    review_checks,
    review_knowledge,
    review_runner,
)
from ecom_ops.wp_ops.review import checks, knowledge, risk, runner
from ecom_ops.wp_ops.review.page import context as page_context
from ecom_ops.wp_ops.review.page import formatters as page_formatters
from ecom_ops.wp_ops.review.page import intent as page_intent
from ecom_ops.wp_ops.review.page import loop as page_loop
from ecom_ops.wp_ops.review.page import steps as page_steps
from ecom_ops.wp_ops.review.preview import diff_overlay, product_overlay, shell

ROOT = Path(__file__).resolve().parents[2]


def test_review_knowledge_all_contexts_and_no_permissions_or_os() -> None:
    for ctx in knowledge.TEMPLATE_CONTEXTS:
        result = knowledge.template_candidates(ctx, slug="demo", theme_slug="storefront_child")
        assert result["context"] == ctx
        assert result["candidates"]
        assert result["block_candidates"]
        assert "Evaluated for theme 'storefront_child'." in result["notes"]

    product_single = knowledge.template_candidates("single", slug="jacket", post_type="product")
    assert product_single["candidates"][:2] == ["single-product-jacket.php", "single-product.php"]
    assert "woocommerce/single-product.php" in product_single["woo_candidates"]

    cat_tax = knowledge.template_candidates("taxonomy", taxonomy="product_cat", slug="shoes")
    assert cat_tax["candidates"][:2] == ["taxonomy-product_cat-shoes.php", "taxonomy-product_cat.php"]
    assert "woocommerce/taxonomy-product_cat.php" in cat_tax["woo_candidates"]

    with pytest.raises(ValueError):
        knowledge.template_candidates("unsupported_context")
    with pytest.raises(ValueError):
        knowledge.template_candidates("page", slug="../etc/passwd")

    for path in (
        ROOT / "skills" / "ecom_ops" / "wp_ops" / "review" / "knowledge.py",
        ROOT / "skills" / "ecom_ops" / "wp_ops" / "review_knowledge.py",
    ):
        src = path.read_text(encoding="utf-8")
        assert "WP_OPS_" not in src
        assert "import os" not in src


def test_review_risk_and_runner_factories() -> None:
    assert risk.review_risk("style.css", "body { color: #111; }") == "low"
    assert risk.review_risk("woocommerce/shipping-calc.php", "<?php // ok") == "high"
    assert risk.review_risk("page_draft.json", '{"html": "<p>Hej</p>"}') == "low"
    assert risk.review_risk("page_draft.json", '{"html": "<script>alert(1)</script>"}') == "high"
    assert risk.tree_risk("theme", [("style.css", b"body { color: red; }")]) == "low"
    assert risk.tree_risk("theme", [("functions.php", b"<?php // order frakt")]) == "high"
    assert risk.text_is_review_approval("godkänn") is False
    assert checks._ORDER is risk.ORDER_RISK_RE

    pub = runner.public_sandbox_record("plan-1")
    assert pub["plan_id"] == "plan-1"
    assert pub["state"] == "unavailable"
    assert pub["payments_enabled"] is False
    assert pub["published_port"] is False

    destroyed = runner.unavailable_destroyed_record("plan-2")
    assert destroyed["plan_id"] == "plan-2"
    assert destroyed["state"] == "destroyed"
    assert destroyed["availability"] == "unavailable"
    assert destroyed["docker_socket"] is False


def test_page_review_slug_allows_underscore_and_not_run_rendered_dom(tmp_path: Path) -> None:
    assert page_context._SLUG.fullmatch("storefront_child")
    assert page_context._SLUG.fullmatch("my-theme_2")
    assert not page_context._SLUG.fullmatch("_leading")
    assert not page_context._SLUG.fullmatch("../escape")
    assert not page_context._SLUG.fullmatch("UPPER")

    full_dom = page_context.not_run_rendered_dom()
    assert full_dom["status"] == "not_run"
    assert full_dom["verification"] == "not_run"
    short_dom = page_context.not_run_rendered_dom(include_verification=False)
    assert "verification" not in short_dom
    assert short_dom["availability"] == "unavailable"

    class _FakeClient:
        def get_settings(self):
            return {"show_on_front": "posts", "page_on_front": 0, "page_for_posts": 0}

        def list_themes(self):
            return [{"status": "active", "stylesheet": "storefront_child", "template": "storefront_parent"}]

    class _FakeTransport:
        def __init__(self) -> None:
            self._files = {
                "/srv/www/shop/wp-content/themes/storefront_child/front-page.php": b"<?php // hero\n",
            }

        def list_dir(self, absolute: str):
            if absolute == "/srv/www/shop/wp-content/themes/storefront_child":
                return ["front-page.php", "style.css"]
            return []

        def read_bytes(self, absolute: str):
            return self._files.get(absolute)

    ctx = page_context.ReviewContext(
        site_id="mock-staging",
        actor_name="oscar",
        include_evidence=True,
        allowed=True,
        site=SimpleNamespace(id="mock-staging", files_root="/srv/www/shop"),
        client=_FakeClient(),
        transport=_FakeTransport(),
        data_dir=tmp_path,
    )
    home = page_steps.step_home(ctx)
    assert home["status"] == "read"
    assert home["stylesheet"] == "storefront_child"
    assert home["template"] == "storefront_parent"
    assert home["theme_relationship"] == "child"
    assert home["selected_template"] == "themes/storefront_child/front-page.php"


def test_review_facades_preserve_object_identity() -> None:
    assert review_knowledge.template_candidates is knowledge.template_candidates
    assert review_knowledge.hierarchy_candidates is knowledge.hierarchy_candidates
    assert review_checks.run_catalog is checks.run_catalog
    assert review_checks.run_is_publishable is checks.run_is_publishable
    assert review_runner.SandboxGate is runner.SandboxGate
    assert review_runner.public_sandbox_record is runner.public_sandbox_record
    assert preview_overlay.extract_site_shell is shell.extract_site_shell
    assert preview_overlay.apply_overlay is diff_overlay.apply_overlay
    assert preview_overlay.apply_product_edits_overlay is product_overlay.apply_product_edits_overlay
    assert page_review_intent.is_page_review is page_intent.is_page_review
    assert page_review.prepare_context is page_context.prepare_context
    assert page_review.step_home is page_steps.step_home
    assert page_review_loop.execute_page_review_tool is page_loop.execute_page_review_tool
    assert page_review_loop.run_bounded_page_review_loop is page_loop.run_bounded_page_review_loop
    assert page_review_loop._format is page_formatters._format
