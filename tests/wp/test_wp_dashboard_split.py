"""Focused tests for the PR 6.2 stateless dashboard/wp/ split and preview hardening wiring."""

from __future__ import annotations

from pathlib import Path

from _helpers import load_dashboard_app

from ecom_ops.wp_ops.html_sanitize import preview_csp_header

REPO_ROOT = Path(__file__).resolve().parents[2]
WP_DASH_DIR = REPO_ROOT / "infrastructure" / "dashboard" / "wp"
WP_ROUTES_FACADE = REPO_ROOT / "infrastructure" / "dashboard" / "wp_routes.py"


def test_dashboard_wp_modules_have_no_direct_disk_reads_or_regex_guard_scraping() -> None:
    files = sorted(WP_DASH_DIR.glob("*.py")) + [WP_ROUTES_FACADE]
    assert len(files) >= 7
    forbidden_tokens = (
        "wp_review_plans.json",
        "workspace_root",
        "_safe_preview_html_path",
        "_build_canonical_guard_script",
        "build_interactive_sandbox_html",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"{path.name} must not contain {token!r}"


def test_dashboard_wp_preview_hardening_and_csp_delegate_to_leaf_sanitizer() -> None:
    app = load_dashboard_app()
    assert app is not None
    import wp_routes

    raw = (
        '<html><body><div id="azom-sandbox-toolbar">ok</div>'
        '<script>alert("xss")</script>'
        '<a href="javascript:alert(1)" onclick="evil()">x</a>'
        "</body></html>"
    )
    hardened = wp_routes._harden_preview_html(raw, "nonce-test-123")
    assert 'alert("xss")' not in hardened
    assert "onclick=" not in hardened
    assert "javascript:" not in hardened
    assert 'nonce="nonce-test-123"' in hardened
    assert 'data-azom-guard="1"' in hardened
    assert wp_routes._preview_csp_for_site("mock-staging", nonce="nonce-test-123") == (
        preview_csp_header(nonce="nonce-test-123")
    )


def test_dashboard_reload_registers_all_wp_routes_cleanly() -> None:
    app1 = load_dashboard_app()
    app2 = load_dashboard_app()
    assert app1 is not app2
    rules = {rule.rule for rule in app2.url_map.iter_rules()}
    expected = {
        "/wp",
        "/wp/<site_id>",
        "/wp/<site_id>/check",
        "/wp/<site_id>/backup",
        "/wp/<site_id>/restore-drill",
        "/wp/<site_id>/ssh",
        "/wp/<site_id>/files",
        "/wp/<site_id>/files/propose",
        "/wp/<site_id>/files/apply",
        "/wp/<site_id>/files/plan/<plan_id>",
        "/wp/<site_id>/files/deploy",
        "/wp/<site_id>/reviews/<plan_id>",
        "/wp/<site_id>/reviews/<plan_id>/comment",
        "/wp/<site_id>/reviews/<plan_id>/staging",
        "/wp/<site_id>/reviews/<plan_id>/production",
        "/wp/<site_id>/reviews/<plan_id>/preview",
        "/wp/<site_id>/reviews/<plan_id>/checks",
        "/wp/<site_id>/reviews/<plan_id>/extend",
        "/wp/<site_id>/reviews/<plan_id>/push-draft",
        "/wp/<site_id>/reviews/<plan_id>/artifact/<name>",
    }
    assert expected.issubset(rules)
