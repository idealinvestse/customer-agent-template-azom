"""Tests for read-only live preview, request filtering, sandbox honesty, and WP preview tools."""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ecom_ops.bot.reply import safe_workspace_attachments
from ecom_ops.integrations.wp_browser_smoke import _classify_theme_render
from ecom_ops.integrations.wp_live_preview import (
    FOCUS_SECTIONS,
    _allowed_site_hosts,
    _AllowlistedSiteRedirectHandler,
    _compute_focus_clip,
    _finalize_summary,
    _order_screenshots,
    build_interactive_sandbox_html,
    capture_preview,
    classify_request,
    live_preview_enabled,
    validate_focus_options,
    validate_preview_path,
)
from ecom_ops.wp_ops.agent_tools import (
    WpAgentTools,
    _provenance_for,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import ReviewError

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "wp_sites.yaml"
MOCK_REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.parametrize(
    ("method", "url", "expected_ok", "expected_reason"),
    [
        ("POST", "https://azom.no/", False, "method_blocked"),
        ("PUT", "https://azom.no/style.css", False, "method_blocked"),
        ("GET", "https://azom.no/wp-admin/index.php", False, "site_path_blocked:wp-admin"),
        ("GET", "https://azom.no/wp-admin/admin-ajax.php", False, "site_path_blocked:wp-admin"),
        ("GET", "https://azom.no/?wc-ajax=get_refreshed_fragments", False, "site_path_blocked:wc-ajax"),
        ("GET", "https://azom.no/wp-json/wp/v2/posts", False, "site_path_blocked:wp-json"),
        ("GET", "https://azom.no/checkout", False, "site_path_blocked:checkout"),
        ("GET", "https://azom.no/%61dmin-ajax.php", False, "site_path_blocked:admin-ajax"),
        ("GET", "https://azom.no:8443/", False, "port_blocked"),
        ("GET", "https://u:p@azom.no/", False, "userinfo_blocked"),
        ("GET", "https://www.googletagmanager.com/gtm.js", False, "tracker_blocked"),
        ("GET", "https://connect.facebook.net/en_US/fbevents.js", False, "tracker_blocked"),
        ("GET", "https://evil.example/payload.js", False, "foreign_host_blocked"),
        ("GET", "http://azom.no/style.css", False, "non_https_blocked"),
        ("GET", "https://127.0.0.1/secret", False, "host_blocked"),
        ("GET", "https://azom.no/wp-content/themes/azom/style.css", True, "site_asset"),
        ("GET", "https://www.azom.no/wp-content/uploads/hero.jpg", True, "site_asset"),
        ("HEAD", "https://fonts.googleapis.com/css2?family=Inter", True, "cdn_asset"),
        ("GET", "data:image/png;base64,AAAA", True, "inline"),
    ],
)
def test_classify_request_enforces_read_only_host_and_path_allowlist(
    method: str, url: str, expected_ok: bool, expected_reason: str
):
    ok, reason = classify_request(method, url, site_host="azom.no")
    assert ok is expected_ok
    assert reason == expected_reason


@pytest.mark.parametrize(
    "bad_path",
    [
        "/wp-admin",
        "/wp-login.php",
        "/wp-json/wp/v2/pages",
        "/checkout",
        "/cart",
        "/handlekurv",
        "/kasse",
        "/min-konto",
        "/my-account",
        "/%77p-admin",
        "/%63heckout",
        "/%2e%2e/wp-admin",
        "/../secret",
        "//evil.com",
        "/page?foo=bar",
        "/page#frag",
    ],
)
def test_validate_preview_path_refuses_admin_checkout_and_traversal(bad_path: str):
    with pytest.raises(ReviewError) as exc:
        validate_preview_path(bad_path)
    assert exc.value.error_class == "PreviewPathRefused"


def test_validate_preview_path_normalizes_valid_paths():
    assert validate_preview_path(None) == "/"
    assert validate_preview_path("") == "/"
    assert validate_preview_path("produkt/carplay") == "/produkt/carplay"


def test_live_preview_enabled_requires_env_flag_and_live_mode(monkeypatch):
    monkeypatch.delenv("AZOM_WP_LIVE_PREVIEW", raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    assert live_preview_enabled() is False

    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    assert live_preview_enabled() is False

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    assert live_preview_enabled() is True


def test_redirect_handler_refuses_foreign_host():
    handler = _AllowlistedSiteRedirectHandler(_allowed_site_hosts("azom.no"))
    req = urllib.request.Request("https://azom.no/")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/login")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://azom.no/wp-admin/")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://azom.no/checkout")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://azom.no:8443/")


def test_sandbox_honesty_marks_synthetic_host_or_parent_fatal_unrepresentative():
    revs = {
        "base": {"executed": True, "ok": True, "viewports": {"desktop": {"theme_marker": "azom-plugin-host"}}},
        "work": {"executed": True, "ok": True, "viewports": {"desktop": {"theme_marker": "azom-plugin-host"}}},
    }
    rep, reason = _classify_theme_render(revs, slug="azom-no-woocommerce-26-1-5-child", php_errors={})
    assert rep is False
    assert reason == "sandbox_not_representative"

    rep_host, _ = _classify_theme_render(revs, slug="azom-plugin-host", php_errors={})
    assert rep_host is True

    revs_ok = {"work": {"executed": True, "ok": False, "viewports": {"desktop": {"theme_marker": ""}}}}
    rep_fatal, reason_fatal = _classify_theme_render(
        revs_ok,
        slug="my-child",
        php_errors={"work": ["PHP Fatal error: Call to undefined function udesly_get_social_share_url()"]},
        synthetic_parent=True,
    )
    assert rep_fatal is False
    assert reason_fatal == "sandbox_not_representative"


def test_review_plans_live_preview_uses_origin_and_returns_ordered_shots(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_LIVE_PREVIEW", raising=False)

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    disabled = plans.live_preview("azom-no", "wr-abcdef012345", actor="oscar")
    assert disabled.ok is False
    assert disabled.data["error_class"] == "LivePreviewDisabled"

    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    plan_id = "wr-abcdef012345"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "origin").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child */\nh1 { color: red; }\n", encoding="utf-8")
    (ws_dir / "origin" / "index.php").write_text("<footer>Old Street 1\nOslo</footer>", encoding="utf-8")
    (ws_dir / "work" / "index.php").write_text("<footer>testadress123\nFalun</footer>", encoding="utf-8")
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": "<html><head></head><body><footer>Old Street 1 Oslo</footer></body></html>",
            "bytes": 68,
        },
    )

    captured_calls: list[dict] = []

    def fake_capture(site, *, path="/", artifact_dir: Path, revisions, highlight_token=None, **_kwargs):
        captured_calls.append({"path": path, "revisions": revisions, "highlight_token": highlight_token})
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("live-desktop.png", "preview-desktop.png", "change-desktop.png", "live-mobile.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {"tracker_blocked": 2},
            "screenshots": ["live-desktop.png", "preview-desktop.png", "change-desktop.png", "live-mobile.png"],
            "revisions": {"live": {}, "preview": {}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    res = plans.live_preview("azom-no", plan_id, actor="oscar", path="/")
    assert res.ok is True
    assert res.data["live_calls"] is True
    assert res.data["overlay"]["counts"]["applied"] == 1
    assert captured_calls[0]["highlight_token"] == "testadress123"
    assert res.data["screenshots"] == [
        "change-desktop.png",
        "live-desktop.png",
        "preview-desktop.png",
        "live-mobile.png",
    ]


def test_agent_tools_fetch_html_preview_attachments_and_provenance(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_LIVE_PREVIEW", raising=False)

    tools = WpAgentTools()
    disabled = tools.execute("wp_fetch_page_html", {"site_id": "azom-no"}, actor="jonatan")
    assert disabled.data["error_class"] == "LivePreviewDisabled"

    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    refused = tools.execute("wp_fetch_page_html", {"site_id": "azom-no", "path": "/checkout"}, actor="jonatan")
    assert refused.data["error_class"] == "PreviewPathRefused"

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": (
                '<html lang="nb"><head><title>Azom NO</title>'
                '<meta name="description" content="Bilmultimedia">'
                '<link rel="canonical" href="https://azom.no/">'
                "<script>console.log('x')</script></head>"
                '<body><h1>Velkommen</h1><img src="https://azom.no/a.jpg"><a href="/shop">Shop</a></body></html>'
            ),
            "bytes": 240,
        },
    )
    fetched = tools.execute("wp_fetch_page_html", {"site_id": "azom-no", "path": "/"}, actor="jonatan")
    assert fetched.data["ok"] is True
    assert fetched.data["title"] == "Azom NO"
    assert fetched.data["h1"] == ["Velkommen"]
    assert fetched.data["counts"]["images_missing_alt"] == 1
    assert "<script>…</script>" in fetched.data["html_excerpt"]
    assert fetched.data["live_calls"] is True

    def fake_capture(site, *, path="/", artifact_dir: Path, revisions, **_kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("live-desktop.png", "live-mobile.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "screenshots": ["live-desktop.png", "live-mobile.png"],
            "revisions": {"live": {"desktop": {}, "mobile": {}}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    responses = iter([("Skärmdumpar skapade.", 0.0)])
    loop_res = run_bounded_file_tool_loop(
        '<tool_call>{"name":"wp_preview_page","arguments":{"site_id":"azom-no","path":"/"}}</tool_call>',
        [{"role": "user", "content": "Ta en skärmdump av startsidan på azom.no"}],
        complete=lambda _m: next(responses),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:1",
        tools=tools,
    )
    assert loop_res.handled is True
    assert len(loop_res.attachments) == 2
    assert all(p.endswith(("live-desktop.png", "live-mobile.png")) for p in loop_res.attachments)

    assert _provenance_for("wp_fetch_page_html", {"live_calls": True}) == "live"
    assert _provenance_for("wp_read_theme_plugin_file", {"ssh": True, "live_calls": False}) == "live·sftp"
    assert _provenance_for("wp_workspace_patch_file", {"live_calls": False}) == "arbetsyta"
    assert _provenance_for("wp_list_sites", {"live_calls": False}) == "mock"


def test_reply_attachments_and_page_review_live_preview(monkeypatch, tmp_path: Path):
    ws_art = tmp_path / "wp_workspaces" / "azom-no" / "wr-aaaaaaaaaaaa" / "artifacts"
    ws_art.mkdir(parents=True)
    good = ws_art / "change-desktop.png"
    evil = ws_art / "evil.png"
    good.write_bytes(_PNG_BYTES)
    evil.write_bytes(_PNG_BYTES)
    validated = safe_workspace_attachments([str(good), str(evil)], data_dir=tmp_path)
    assert validated == (str(good.resolve()),)

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": "<html><body>Live</body></html>",
            "bytes": 24,
        },
    )

    def fake_capture(site, *, path="/", artifact_dir: Path, revisions, **_kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        d = artifact_dir / "live-desktop.png"
        m = artifact_dir / "live-mobile.png"
        d.write_bytes(_PNG_BYTES)
        m.write_bytes(_PNG_BYTES)
        return {
            "screenshots": ["live-desktop.png", "live-mobile.png"],
            "revisions": {
                "live": {
                    "desktop": {"http_status": 200, "screenshot_path": str(d), "console_errors": [], "network_errors": []},
                    "mobile": {"http_status": 200, "screenshot_path": str(m), "console_errors": [], "network_errors": []},
                }
            },
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    outcome = run_bounded_page_review_loop(
        site_id="azom-no",
        actor="oscar",
        registry_path=REGISTRY,
        data_dir=tmp_path,
    )
    assert outcome.layers["rendered_dom"]["status"] == "read"
    assert len(outcome.attachments) == 2
    assert "Desktop-bild: bifogad" in outcome.text


def test_capture_preview_and_summary_security_hardening(monkeypatch, tmp_path: Path):
    site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert site is not None

    with pytest.raises(ReviewError) as exc:
        capture_preview(
            site,
            path="/",
            artifact_dir=tmp_path,
            revisions=[("live", "<html></html>")],
            target_url="https://azom.no/checkout",
            browser_binary="x",
        )
    assert exc.value.error_class == "PreviewPathRefused"

    monkeypatch.setenv(site.secret_ref, "FAKE-SECRET-1234567")
    summary = {
        "preview_kind": "live_read_only",
        "site_id": site.id,
        "target_url": "https://azom.no/",
        "path": "/",
        "allowed_requests": 1,
        "blocked_counts": {"foreign_host_blocked": 1},
        "blocked_sample": [{"url": "https://azom.no/?tok=FAKE-SECRET-1234567", "reason": "blocked"}],
        "highlight_token": "FAKE-SECRET-1234567",
        "revisions": {
            "live": {
                "desktop": {
                    "console_errors": ["Auth failed for FAKE-SECRET-1234567"],
                    "network_errors": [],
                }
            }
        },
        "screenshots": [],
    }
    clean = _finalize_summary(site, summary, tmp_path / "artifacts")
    raw_json = (tmp_path / "artifacts" / "preview.json").read_text(encoding="utf-8")
    assert "FAKE-SECRET-1234567" not in json.dumps(clean)
    assert "FAKE-SECRET-1234567" not in raw_json
    assert "***" in raw_json


def test_review_plans_live_preview_scrubs_bound_secret(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")

    site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert site is not None
    monkeypatch.setenv(site.secret_ref, "FAKE-SECRET-1234567")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    plan_id = "wr-abcdef012346"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "origin").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "origin" / "index.php").write_text("<footer>Old Street 1\nOslo</footer>", encoding="utf-8")
    (ws_dir / "work" / "index.php").write_text(
        "<footer>testadress123 FAKE-SECRET-1234567\nFalun</footer>",
        encoding="utf-8",
    )
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda _site, path="/": {
            "status": 200,
            "final_url": f"https://azom.no{path}",
            "html": "<html><head></head><body><footer>Old Street 1 Oslo</footer></body></html>",
            "bytes": 68,
        },
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.capture_preview",
        lambda _site, *, path="/", artifact_dir, revisions, highlight_token=None, **_kwargs: {
            "blocked_counts": {},
            "screenshots": [],
            "revisions": {"live": {}, "preview": {}},
        },
    )
    res = plans.live_preview("azom-no", plan_id, actor="oscar", path="/")
    assert res.ok is True
    assert "FAKE-SECRET-1234567" not in json.dumps(res.data)


def test_preview_page_argument_normalization_and_schema_hints(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": "<html><body>Ok</body></html>",
            "bytes": 24,
        },
    )

    def fake_capture(site, *, path="/", artifact_dir: Path, revisions, **_kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("live-desktop.png", "live-mobile.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "screenshots": ["live-desktop.png", "live-mobile.png"],
            "revisions": {"live": {"desktop": {}, "mobile": {}}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    tools = WpAgentTools()

    res_none = tools.execute(
        "wp_preview_page",
        {"site_id": "azom-no", "plan_id": None, "path": None},
        actor="oscar",
    )
    assert res_none.data["ok"] is True

    res_bad = tools.execute(
        "wp_preview_page",
        {"site_id": "azom-no", "url": "https://azom.no/"},
        actor="oscar",
    )
    assert res_bad.data["error_class"] == "InvalidToolArguments"
    assert res_bad.data["allowed_arguments"] == [
        "focus_section",
        "focus_selector",
        "focus_text",
        "full_page",
        "path",
        "plan_id",
        "reuse_live_baseline",
        "site_id",
    ]

    res_tick = tools.execute(
        "wp_preview_page",
        {"site_id": "azom-no", "plan_id": "`wr-abcdef012345`"},
        actor="oscar",
    )
    assert res_tick.data["error_class"] == "UnknownPlan"


def test_review_plans_live_preview_falls_back_to_parent_when_origin_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    plan_id = "wr-abcdef012347"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child\nTemplate: parent-theme\n*/\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child\nTemplate: parent-theme\n*/\n", encoding="utf-8")
    (ws_dir / "work" / "index.php").write_text("<footer>testadress123</footer>", encoding="utf-8")

    parent_root = tmp_path / "remote_root"
    parent_theme = parent_root / "wp-content" / "themes" / "parent-theme"
    parent_theme.mkdir(parents=True)
    (parent_theme / "index.php").write_text("<footer><?php _e('Old Street 1', 't') ?></footer>", encoding="utf-8")

    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )

    real_site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert real_site is not None
    test_site = dataclasses.replace(real_site, files_root="/var/www/azom-test")
    monkeypatch.setattr(plans, "_site", lambda _sid: (test_site, None))
    monkeypatch.setattr(plans, "_transport", lambda _site: LocalTree("/var/www/azom-test", parent_root))
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda _site, path="/": {
            "status": 200,
            "final_url": f"https://azom.no{path}",
            "html": "<html><head></head><body><footer>Old Street 1</footer></body></html>",
            "bytes": 63,
        },
    )
    captured: list[dict] = []

    def fake_capture(_site, *, path="/", artifact_dir: Path, revisions, highlight_token=None, **_kwargs):
        captured.append({"highlight_token": highlight_token})
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("change-desktop.png", "live-desktop.png", "preview-desktop.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "screenshots": ["live-desktop.png", "preview-desktop.png", "change-desktop.png"],
            "revisions": {"live": {}, "preview": {}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    res = plans.live_preview("azom-no", plan_id, actor="oscar", path="/")
    assert res.ok is True
    assert res.data["overlay"]["counts"]["applied"] == 1
    assert captured[0]["highlight_token"] == "testadress123"


def test_validate_focus_options_and_clip_helpers() -> None:
    for sec in FOCUS_SECTIONS:
        assert validate_focus_options(focus_section=sec.upper()) == (sec, None, None, False)
    assert validate_focus_options(focus_section="", focus_selector="   ", focus_text="  ") == (
        None,
        None,
        None,
        False,
    )
    assert validate_focus_options(
        focus_section="footer",
        focus_selector="footer.f01 > div[data-w-id='abc']",
        focus_text="  Testsidan123,   falun  ",
        full_page=True,
    ) == ("footer", "footer.f01 > div[data-w-id='abc']", "Testsidan123, falun", True)

    for bad_kwargs in [
        {"focus_section": "sidebar"},
        {"focus_section": 123},
        {"focus_selector": "footer; alert(1)"},
        {"focus_selector": "body{display:none}"},
        {"focus_selector": "<script>alert(1)</script>"},
        {"focus_selector": "javascript:alert(1)"},
        {"focus_selector": "url(https://evil.example)"},
        {"focus_selector": "expression(1)"},
        {"focus_selector": "a\\b"},
        {"focus_selector": "a" * 121},
        {"focus_text": "x" * 121},
        {"focus_text": "bad\x00text"},
        {"full_page": "yes"},
    ]:
        with pytest.raises(ReviewError) as exc:
            validate_focus_options(**bad_kwargs)
        assert exc.value.error_class == "InvalidFocusTarget"

    y_bot, h_bot = _compute_focus_clip(4800, 400, 5400, 800, is_mobile=False, target_kind="section:bottom")
    assert y_bot == 4500.0 and h_bot == 900.0

    y_top, h_top = _compute_focus_clip(0, 800, 5400, 800, is_mobile=False, target_kind="section:top")
    assert y_top == 0.0 and h_top == 900.0

    y_foot, h_foot = _compute_focus_clip(4590, 470, 5440, 800, is_mobile=False, target_kind="section:footer")
    assert y_foot == 4550.0 and h_foot == 560.0

    assert _order_screenshots(
        ["live-desktop.png", "focus-mobile.png", "live-mobile.png", "focus-desktop.png"]
    ) == ["focus-desktop.png", "focus-mobile.png", "live-desktop.png", "live-mobile.png"]
    assert _order_screenshots(
        ["live-desktop.png", "preview-desktop.png", "change-mobile.png", "change-desktop.png"]
    ) == ["change-desktop.png", "change-mobile.png", "preview-desktop.png", "live-desktop.png"]


def test_capture_preview_rejects_invalid_focus_before_browser(tmp_path: Path) -> None:
    site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert site is not None
    with pytest.raises(ReviewError) as exc:
        capture_preview(
            site,
            path="/",
            artifact_dir=tmp_path,
            revisions=[("live", "<html></html>")],
            focus_selector="body; alert(1)",
            browser_binary="nonexistent-browser",
        )
    assert exc.value.error_class == "InvalidFocusTarget"


def test_focus_pngs_allowed_in_attachments_and_review_plans_focus_threading(monkeypatch, tmp_path: Path) -> None:
    ws_art = tmp_path / "wp_workspaces" / "azom-no" / "wr-bbbbbbbbbbbb" / "artifacts"
    ws_art.mkdir(parents=True)
    fd = ws_art / "focus-desktop.png"
    fm = ws_art / "focus-mobile.png"
    fd.write_bytes(_PNG_BYTES)
    fm.write_bytes(_PNG_BYTES)
    assert safe_workspace_attachments([str(fd), str(fm)], data_dir=tmp_path) == (
        str(fd.resolve()),
        str(fm.resolve()),
    )

    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert site is not None
    monkeypatch.setenv(site.secret_ref, "TOP-SECRET-TOKEN-999")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    plan_id = "wr-bbbbbbbbbbbb"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True, exist_ok=True)
    (ws_dir / "work").mkdir(parents=True, exist_ok=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child */\nfooter { color: #fff; }\n", encoding="utf-8")
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )

    art_file, art_err = plans.artifact_file("azom-no", plan_id, "focus-desktop.png", actor="oscar")
    assert art_err is None
    assert art_file == fd.resolve()

    bad_focus = plans.live_preview("azom-no", plan_id, actor="oscar", focus_section="unknown")
    assert bad_focus.ok is False
    assert bad_focus.data["error_class"] == "InvalidFocusTarget"

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda _site, path="/": {
            "status": 200,
            "final_url": f"https://azom.no{path}",
            "html": "<html><head></head><body><footer>KONTAKT</footer></body></html>",
            "bytes": 62,
        },
    )
    seen_kwargs: dict = {}

    def fake_capture(_site, *, path="/", artifact_dir: Path, revisions, **kwargs):
        seen_kwargs.update(kwargs)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("live-desktop.png", "preview-desktop.png", "focus-desktop.png", "focus-mobile.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "focus_matched": True,
            "focus_target": "text:TOP-SECRET-TOKEN-999",
            "screenshots": ["focus-desktop.png", "focus-mobile.png", "preview-desktop.png", "live-desktop.png"],
            "revisions": {"live": {}, "preview": {}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    res = plans.live_preview(
        "azom-no",
        plan_id,
        actor="oscar",
        path="/",
        focus_section="footer",
        focus_selector="footer.f01",
        focus_text="KONTAKT",
        full_page=True,
    )
    assert res.ok is True
    assert seen_kwargs["focus_section"] == "footer"
    assert seen_kwargs["focus_selector"] == "footer.f01"
    assert seen_kwargs["focus_text"] == "KONTAKT"
    assert seen_kwargs["full_page"] is True
    assert res.data["focus_matched"] is True
    assert res.data["full_page"] is True
    assert "TOP-SECRET-TOKEN-999" not in json.dumps(res.data)
    assert res.data["screenshots"][:2] == ["focus-desktop.png", "focus-mobile.png"]


def test_run_checks_distinguishes_unavailable_sandbox_from_successful_live_preview(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    plan_id = "wr-cccccccccccc"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child */\nbody { color: #111; }\n", encoding="utf-8")
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )

    monkeypatch.setattr(
        plans,
        "_run_sandbox_catalog",
        lambda *_a, **_kw: [
            {"job": "php_lint", "status": "passed", "executed": True, "simulated": False},
            {
                "job": "browser_smoke",
                "status": "unavailable",
                "reason": "sandbox_not_representative",
                "executed": True,
                "simulated": False,
            },
        ],
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda _site, path="/": {
            "status": 200,
            "final_url": f"https://azom.no{path}",
            "html": "<html><head></head><body><footer>Azom</footer></body></html>",
            "bytes": 59,
        },
    )

    def fake_capture(_site, *, path="/", artifact_dir: Path, revisions, **_kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("live-desktop.png", "preview-desktop.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "focus_matched": False,
            "focus_target": None,
            "screenshots": ["preview-desktop.png", "live-desktop.png"],
            "revisions": {"live": {}, "preview": {}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)
    res = plans.run_checks("azom-no", plan_id, actor="oscar")
    assert res.ok is True
    assert res.data["sandbox_representative"] is False
    assert res.data["preview_status"] == "ok"
    assert "Live-overlay-förhandsvisning lyckades" in res.data["preview_note"]
    assert "lokal PHP-sandlåda är inte representativ" in res.message
    assert res.data["screenshots"] == ["live-desktop.png", "preview-desktop.png"]


def test_build_interactive_sandbox_html_base_tracker_strip_toolbar_and_guard() -> None:
    overlay_html = (
        "<!doctype html><html><head>"
        '<base href="https://old.example/">'
        '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-1"></script>'
        '<script src="https://www.google-analytics.com/analytics.js"></script>'
        '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
        '<script src="https://static.hotjar.com/c/hotjar.js"></script>'
        '<script src="https://www.clarity.ms/tag/abc"></script>'
        '<script src="https://cdn-cookieyes.com/client_data/abc/script.js"></script>'
        '<script src="https://cdn.wiserreview.com/widget.js"></script>'
        '<script src="https://googleads.g.doubleclick.net/pagead/viewthrough.js"></script>'
        '<script src="https://azom.no/wp-content/themes/azom/app.js"></script>'
        "</head><body>"
        '<h1 class="product_title">Testprodukt 123</h1>'
        '<form action="/checkout" method="post"><button type="submit">Köp</button></form>'
        "</body></html>"
    )
    rendered = build_interactive_sandbox_html(
        overlay_html,
        site_canonical_url="https://azom.no",
        site_id="azom-no",
        plan_id="wr-abcdef012345",
        path="/produkt/carplay-pro/",
        highlight_token='Testprodukt</script><script>alert(1)</script>',
        focus_text="Testprodukt 123",
    )

    assert '<base href="https://azom.no/">' in rendered
    assert "https://old.example/" not in rendered
    for tracker in (
        "googletagmanager",
        "google-analytics",
        "facebook.net",
        "doubleclick",
        "hotjar",
        "clarity.ms",
        "cookieyes",
        "wiserreview",
    ):
        assert tracker not in rendered
    # All untrusted scripts (including site theme scripts) are stripped; only the Azom guard remains
    assert "https://azom.no/wp-content/themes/azom/app.js" not in rendered
    guard_marker = '<script data-azom-sandbox="guard" data-azom-guard="1">'
    assert guard_marker in rendered
    assert "<script" not in rendered[: rendered.index(guard_marker)].lower()
    # Toolbar + jump button + mutation guard
    assert "AZOM SANDBOX · wr-abcdef012345 · /produkt/carplay-pro/" in rendered
    assert "Skrivskyddad förhandsvisning — inga ändringar på live" in rendered
    assert "Hoppa till ändringen" in rendered
    assert "Blockerad i sandbox-läget — inga ändringar skickas till live" in rendered
    assert "window.fetch" in rendered
    assert "XMLHttpRequest.prototype.open" in rendered
    assert "#22c55e" in rendered
    # Script breakout escaped safely
    assert "</script><script>alert(1)" not in rendered


def test_review_plans_live_preview_applies_product_edits_auto_path_and_writes_scrubbed_html(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_SANDBOX_PREVIEW_BASE_URL", "http://127.0.0.1:8080")

    site = site_by_id(load_sites(REGISTRY), "azom-no")
    assert site is not None
    monkeypatch.setenv(site.secret_ref, "TOP-SECRET-PRODUCT-CRED")

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    plan_id = "wr-dddddddddddd"
    ws_dir = tmp_path / "wp_workspaces" / "azom-no" / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "base" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "style.css").write_text("/* Theme Name: Child */\n", encoding="utf-8")
    (ws_dir / "work" / "product_edits.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product_path": "/produkt/carplay-bmw/",
                "product_id": 77,
                "title": "Testprodukt 123 TOP-SECRET-PRODUCT-CRED",
                "price": "2 990 kr",
                "button_text": "Köp nu",
            }
        ),
        encoding="utf-8",
    )
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": "azom-no",
                "environment": "production",
                "kind": "theme",
                "slug": "azom-no-woocommerce-26-1-5-child",
                "work_slug": "azom-no-woocommerce-26-1-5-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/azom-no/reviews/{plan_id}",
            }
        ]
    )

    fetched_paths: list[str] = []

    def fake_fetch(_site, path="/"):
        fetched_paths.append(path)
        return {
            "status": 200,
            "final_url": f"https://azom.no{path}",
            "html": (
                "<html><head><script src='https://www.googletagmanager.com/gtm.js'></script></head>"
                "<body>"
                '<h1 class="product_title">CarPlay BMW</h1>'
                '<p class="price"><span class="woocommerce-Price-amount"><bdi>3 490 kr</bdi></span></p>'
                '<button class="single_add_to_cart_button">Legg i handlekurv</button>'
                "</body></html>"
            ),
            "bytes": 280,
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.fetch_live_html", fake_fetch)

    captured_revs: list[dict] = []

    def fake_capture(_site, *, path="/", artifact_dir: Path, revisions, highlight_token=None, **_kwargs):
        captured_revs.append({"path": path, "revisions": revisions, "highlight_token": highlight_token})
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in ("change-desktop.png", "change-mobile.png"):
            (artifact_dir / name).write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "focus_matched": True,
            "focus_target": f"highlight:{highlight_token}",
            "screenshots": ["change-desktop.png", "change-mobile.png"],
            "revisions": {"live": {}, "preview": {}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)

    # Caller leaves path="/" -> auto-uses product_edits.json product_path
    res = plans.live_preview("azom-no", plan_id, actor="oscar", path="/")
    assert res.ok is True
    assert fetched_paths == ["/produkt/carplay-bmw/"]
    assert res.data["path"] == "/produkt/carplay-bmw/"
    assert res.data["preview_path"] == f"/wp/azom-no/reviews/{plan_id}/preview"
    assert res.data["preview_url"] == f"http://127.0.0.1:8080/wp/azom-no/reviews/{plan_id}/preview"
    assert res.data["preview_html_ready"] is True
    assert res.data["overlay"]["counts"]["applied"] == 3
    assert captured_revs[0]["path"] == "/produkt/carplay-bmw/"
    preview_rev_html = dict(captured_revs[0]["revisions"])["preview"]
    assert "Testprodukt 123" in preview_rev_html
    assert "2 990 kr" in preview_rev_html
    assert ">Köp nu</button>" in preview_rev_html

    # artifacts/preview.html exists, has sandbox toolbar & guard, strips GTM, and scrubs bound secret
    preview_file = ws_dir / "artifacts" / "preview.html"
    assert preview_file.is_file()
    html_disk = preview_file.read_text(encoding="utf-8")
    assert '<base href="https://azom.no/">' in html_disk
    assert "AZOM SANDBOX" in html_disk
    assert "Testprodukt 123" in html_disk
    assert "googletagmanager" not in html_disk
    assert "TOP-SECRET-PRODUCT-CRED" not in html_disk
    assert "TOP-SECRET-PRODUCT-CRED" not in json.dumps(res.data)
