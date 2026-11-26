"""Tests for iterative WordPress UI/CSS workspace flows, DOM/CSS inspection, fast re-render, and risk/search fixes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.wp_live_preview import (
    build_interactive_sandbox_html,
    clear_live_html_cache,
)
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    ToolOutput,
    WpAgentTools,
    _review_notice,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.file_changes import risk_for
from ecom_ops.wp_ops.review_plan import ReviewPlans, review_risk
from ecom_ops.wp_ops.review_workspace import VerifiedTheme
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[2]
MOCK_REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
LIVE_REGISTRY = ROOT / "config" / "wp_sites.yaml"

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _setup_mock_wp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WpOpsService, WpAgentTools]:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-child"
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "style.css").write_text(
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n.single_add_to_cart_button { background: #222; color: #fff; }\n",
        encoding="utf-8",
    )
    (theme_dir / "functions.php").write_text("<?php\n// child theme\n", encoding="utf-8")
    service = WpOpsService(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    tools = WpAgentTools(service)
    return service, tools


def _tool_call(name: str, arguments: dict[str, Any]) -> str:
    return f"<tool_call>{json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False)}</tool_call>"


def test_variant_group_coexists_and_default_supersedes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_wp(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)

    v1 = plans.open(
        "mock-staging",
        kind="theme",
        slug="azom-child",
        actor="oscar",
        summary="Variant A",
        variant_group="btn-v2",
    )
    v2 = plans.open(
        "mock-staging",
        kind="theme",
        slug="azom-child",
        actor="oscar",
        summary="Variant B",
        variant_group="btn-v2",
    )
    assert v1.ok is True
    assert v2.ok is True
    p1_id = v1.data["plan_id"]
    p2_id = v2.data["plan_id"]
    assert v1.data["variant_group"] == "btn-v2"
    assert v2.data["variant_group"] == "btn-v2"

    # Both sibling variants remain active and not superseded
    v1_fresh = plans.get(p1_id, site_id="mock-staging")
    v2_fresh = plans.get(p2_id, site_id="mock-staging")
    assert v1_fresh is not None and v1_fresh["status"] == "draft"
    assert not v1_fresh.get("superseded_by")
    assert v2_fresh is not None and v2_fresh["status"] == "draft"
    assert not v2_fresh.get("superseded_by")

    # Opening without variant_group preserves default superseding behavior
    plain1 = plans.open("mock-staging", kind="theme", slug="azom-child", actor="oscar", summary="Plain 1")
    plain2 = plans.open("mock-staging", kind="theme", slug="azom-child", actor="oscar", summary="Plain 2")
    assert plain1.ok is True and plain2.ok is True
    plain1_id = plain1.data["plan_id"]
    plain2_id = plain2.data["plan_id"]
    stale_row = plans.get(plain1_id, site_id="mock-staging")
    assert stale_row is not None
    assert stale_row["superseded_by"] == plain2_id
    approved = plans.approve_staging(
        "mock-staging",
        plain1_id,
        actor="oscar",
        base_sha256=stale_row["base_sha256"],
        work_sha256=stale_row["work_sha256"],
    )
    assert approved.ok is False
    assert approved.data["error_class"] == "StalePlan"


def test_workspace_fork_and_base_plan_id_clone_work_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    opened = tools.execute(
        "wp_open_review_workspace",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "summary": "Initial CTA style",
            "variant_group": "cta-iter",
        },
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_a = opened.data["plan_id"]

    patched = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "path": "style.css",
            "old_text": "background: #222;",
            "new_text": "background: #1e3a8a; padding-left: 36px;",
        },
        actor="oscar",
    )
    assert patched.data["ok"] is True

    # Open a sibling variant in the same variant_group so plan_a stays active and forkable
    other = tools.execute(
        "wp_open_review_workspace",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "summary": "Sibling variant",
            "variant_group": "cta-iter",
        },
        actor="oscar",
    )
    assert other.data["ok"] is True

    # Fork via wp_workspace_fork from active validated plan_a
    forked = tools.execute(
        "wp_workspace_fork",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "summary": "Refined CTA with bigger radius",
            "variant_group": "cta-iter",
        },
        actor="oscar",
    )
    assert forked.data["ok"] is True
    plan_b = forked.data["plan_id"]
    assert plan_b != plan_a
    assert forked.data["forked_from"] == plan_a
    assert forked.data["variant_group"] == "cta-iter"
    assert any(
        (entry.get("path") if isinstance(entry, dict) else entry) == "style.css"
        for entry in forked.data["changed_files"]
    )

    read_b = tools.execute(
        "wp_workspace_read_file",
        {"site_id": "mock-staging", "plan_id": plan_b, "path": "style.css"},
        actor="oscar",
    )
    assert read_b.data["ok"] is True
    assert "#1e3a8a" in read_b.data["content"]

    # Fork via wp_open_review_workspace(base_plan_id=...)
    via_open = tools.execute(
        "wp_open_review_workspace",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "base_plan_id": plan_b,
            "variant_group": "cta-iter",
            "summary": "Third variant from B",
        },
        actor="oscar",
    )
    assert via_open.data["ok"] is True
    assert via_open.data["forked_from"] == plan_b
    assert any(
        (entry.get("path") if isinstance(entry, dict) else entry) == "style.css"
        for entry in via_open.data["changed_files"]
    )


def test_superseded_workspace_allows_readonly_inspection_and_blocks_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    open1 = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "First"},
        actor="oscar",
    )
    assert open1.data["ok"] is True
    p1 = open1.data["plan_id"]

    tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": p1,
            "path": "style.css",
            "old_text": "#222",
            "new_text": "#0f172a",
        },
        actor="oscar",
    )
    open2 = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Second"},
        actor="oscar",
    )
    assert open2.data["ok"] is True
    p2 = open2.data["plan_id"]
    assert p2 != p1

    # Read-only inspection on superseded p1 succeeds
    listed = tools.execute("wp_workspace_list_files", {"site_id": "mock-staging", "plan_id": p1}, actor="oscar")
    assert listed.data["ok"] is True
    read = tools.execute(
        "wp_workspace_read_file",
        {"site_id": "mock-staging", "plan_id": p1, "path": "style.css"},
        actor="oscar",
    )
    assert read.data["ok"] is True
    assert "#0f172a" in read.data["content"]
    diff = tools.execute("wp_workspace_diff", {"site_id": "mock-staging", "plan_id": p1}, actor="oscar")
    assert diff.data["ok"] is True
    assert "#0f172a" in diff.data["unified_diff"]

    # Mutating superseded p1 still fails closed with StalePlan
    mut = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": p1,
            "path": "style.css",
            "old_text": "#0f172a",
            "new_text": "#333",
        },
        actor="oscar",
    )
    assert mut.data["ok"] is False
    assert mut.data["error_class"] == "StalePlan"


def test_fetch_page_html_selector_and_dashboard_url_guard_and_inspect_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    clear_rbac_cache()
    clear_live_html_cache()
    tools = WpAgentTools(WpOpsService(data_dir=tmp_path, registry_path=LIVE_REGISTRY))

    # Dashboard preview URL passed to wp_fetch_page_html returns actionable recovery hint
    bad = tools.execute(
        "wp_fetch_page_html",
        {"site_id": "azom-no", "path": "/wp/azom-no/reviews/wr-1234567890ab/preview"},
        actor="oscar",
    )
    assert bad.data["ok"] is False
    assert bad.data["error_class"] == "InvalidPreviewPath"
    assert bad.data.get("recovery_tool") == "wp_inspect_element"
    assert "plan_id" in str(bad.data.get("hint") or "")

    sample_html = (
        "<html><head>"
        '<link rel="stylesheet" href="https://azom.no/wp-content/themes/shopkeeper/style.css">'
        "<style>.single_add_to_cart_button { background-image: url(/cart.svg); padding-left: 10px; }</style>"
        "</head><body>"
        '<main><button class="single_add_to_cart_button" id="buy-btn">Legg i handlekurv</button></main>'
        "</body></html>"
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/", **_kw: {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": sample_html,
            "bytes": len(sample_html),
        },
    )

    fetched = tools.execute(
        "wp_fetch_page_html",
        {
            "site_id": "azom-no",
            "path": "/produkt/test/",
            "selector": ".single_add_to_cart_button",
            "include_styles": True,
        },
        actor="oscar",
    )
    assert fetched.data["ok"] is True
    assert fetched.data["selector_matched"] is True
    assert "Legg i handlekurv" in fetched.data["html_excerpt"]
    assert "cart.svg" in fetched.data["styles"]

    # Test wp_inspect_element (mocking headless DOM inspection) + secret scrubbing
    inspected_calls: list[dict[str, Any]] = []

    def fake_inspect(site, **kwargs):
        inspected_calls.append(dict(kwargs))
        return {
            "found": True,
            "path": kwargs.get("path", "/"),
            "final_url": f"{site.canonical_url}{kwargs.get('path', '/')}",
            "selector": kwargs.get("selector"),
            "text": kwargs.get("text"),
            "viewports": {
                "desktop": {
                    "found": True,
                    "tag": "button",
                    "classes": ["single_add_to_cart_button"],
                    "outer_html": '<button class="single_add_to_cart_button">sk-or-v1-secret1234567890</button>',
                    "computed_styles": {
                        "backgroundImage": 'url("https://azom.no/cart.svg")',
                        "backgroundPosition": "10% 50%",
                        "paddingLeft": "12px",
                    },
                    "matched_rules": [
                        {"selector": ".single_add_to_cart_button", "css_text": "padding-left: 12px;"}
                    ],
                    "health_checks": {
                        "background_icon_overlap_risk": True,
                        "broken_images": 0,
                        "horizontal_overflow": False,
                        "warnings": ["background_icon_overlap_risk"],
                    },
                },
                "mobile": {"found": True, "tag": "button", "health_checks": {"warnings": []}},
            },
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.inspect_element", fake_inspect)

    res = tools.execute(
        "wp_inspect_element",
        {
            "site_id": "azom-no",
            "path": "/produkt/azom-box/",
            "selector": ".single_add_to_cart_button",
        },
        actor="oscar",
    )
    assert res.data["ok"] is True
    assert res.data["viewports"]["desktop"]["health_checks"]["background_icon_overlap_risk"] is True
    assert "sk-or-v1-secret1234567890" not in json.dumps(res.data)
    assert "[REDACTED]" in json.dumps(res.data)
    assert inspected_calls[-1]["path"] == "/produkt/azom-box/"


def test_live_preview_baseline_reuse_visual_health_and_focus_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_wp(tmp_path, monkeypatch)
    clear_live_html_cache()

    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    verified = VerifiedTheme("azom-child", "parent-base")
    p1_res = plans.open(
        "mock-staging",
        kind="theme",
        slug="azom-child",
        actor="oscar",
        summary="Button variant 1",
        variant_group="buy-btn",
        verified_theme=verified,
    )
    p2_res = plans.open(
        "mock-staging",
        kind="theme",
        slug="azom-child",
        actor="oscar",
        summary="Button variant 2",
        variant_group="buy-btn",
        verified_theme=verified,
    )
    assert p1_res.ok is True and p2_res.ok is True
    p1_id = p1_res.data["plan_id"]
    p2_id = p2_res.data["plan_id"]

    assert plans.patch_file("mock-staging", p1_id, "style.css", "#222", "#1e40af", actor="oscar").ok is True
    assert plans.patch_file("mock-staging", p2_id, "style.css", "#222", "#047857", actor="oscar").ok is True

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/", **_kw: {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": '<html><head></head><body><button class="single_add_to_cart_button">Köp</button></body></html>',
            "bytes": 96,
        },
    )

    captured_revisions: list[list[str]] = []

    def fake_capture(
        site,
        *,
        path="/",
        artifact_dir,
        revisions,
        preview_kind_override=None,
        **kwargs,
    ):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        rev_names = [label for label, _html in revisions]
        captured_revisions.append(rev_names)
        shots: list[str] = []
        if "live" in rev_names:
            for fname in ("live-desktop.png", "live-mobile.png"):
                (artifact_dir / fname).write_bytes(_PNG_1X1)
                shots.append(fname)
        for fname in ("preview-desktop.png", "preview-mobile.png", "focus-desktop.png"):
            (artifact_dir / fname).write_bytes(_PNG_1X1)
            shots.append(fname)
        (artifact_dir / "preview.json").write_text(
            json.dumps({"path": path, "full_page": bool(kwargs.get("full_page", False))}),
            encoding="utf-8",
        )
        return {
            "status": "passed",
            "preview_kind": preview_kind_override or "live_overlay",
            "path": path,
            "final_url": f"{site.canonical_url}{path}",
            "screenshots": shots,
            "visual_health": {
                "desktop": {"found": True, "background_icon_overlap_risk": False, "warnings": []},
                "mobile": {"found": True, "background_icon_overlap_risk": False, "warnings": []},
            },
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)

    # First preview captures live + preview
    r1 = plans.live_preview(
        "mock-staging",
        p1_id,
        path="/produkt/test/",
        focus_selector=".single_add_to_cart_button",
        reuse_live_baseline=True,
        actor="oscar",
    )
    assert r1.ok is True
    assert r1.data["preview_kind"] == "live_overlay"
    assert r1.data["preview_html_ready"] is True
    assert r1.data["reused_live_baseline"] is False
    assert captured_revisions == [["live", "preview"]]
    assert "visual_health" in r1.data
    p1_row = plans.get(p1_id, site_id="mock-staging")
    assert p1_row is not None
    assert p1_row["preview_path"] == "/produkt/test/"
    assert p1_row["focus_selector"] == ".single_add_to_cart_button"

    # Sibling variant p2 in same variant_group reuses p1's live baseline (preview-only capture)!
    r2 = plans.live_preview(
        "mock-staging",
        p2_id,
        path="/produkt/test/",
        focus_selector=".single_add_to_cart_button",
        reuse_live_baseline=True,
        actor="oscar",
    )
    assert r2.ok is True
    assert r2.data["preview_kind"] == "live_overlay"
    assert r2.data["reused_live_baseline"] is True
    assert captured_revisions == [["live", "preview"], ["preview"]]
    assert "live-desktop.png" in r2.data["screenshots"]
    assert "preview-desktop.png" in r2.data["screenshots"]

    # Interactive sandbox HTML receives focus_selector and doesn't blindly highlight <label>
    sandbox_html = build_interactive_sandbox_html(
        '<html><head></head><body><label>Qty</label><button class="single_add_to_cart_button">Köp</button></body></html>',
        site_canonical_url="https://staging.example.test",
        site_id="mock-staging",
        plan_id=p1_id,
        path="/produkt/test/",
        focus_selector=".single_add_to_cart_button",
    )
    assert ".single_add_to_cart_button" in sandbox_html
    assert "azom-sandbox-toolbar" in sandbox_html
    assert "azom-sandbox-jump" in sandbox_html


def test_pure_css_risk_low_and_large_css_bounded_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    # Pure CSS button/cart styling stays low risk
    css_btn = ".single_add_to_cart_button, .commerceaddtocartform { background: #1e3a8a; border-radius: 8px; }"
    assert risk_for("style.css", css_btn) == "low"
    assert review_risk("style.css", css_btn) == "low"

    # Dangerous CSS or PHP or checkout dir stays high risk
    assert risk_for("style.css", "body { color: red; } <?php echo 1; ?>") == "high"
    assert risk_for("style.css", "body { background: url(javascript:alert(1)); }") == "high"
    assert risk_for("checkout/style.css", ".btn { color: red; }") == "high"
    assert review_risk("shipping/rates.php", "order") == "high"

    # Bounded >64KB CSS read-only search succeeds while regular read/patch keeps 64KB cap
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-child"
    big_css = "/* pad */\n" * 8000 + ".single_add_to_cart_button { background: #123456; }\n"
    assert len(big_css.encode("utf-8")) > 65536
    (theme_dir / "large.css").write_text(big_css, encoding="utf-8")

    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "query": "single_add_to_cart_button",
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    assert any(hit["path"] == "large.css" for hit in found.data["hits"])
    assert len(json.dumps(found.data["hits"]).encode("utf-8")) <= 12000

    too_big = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "path": "large.css",
        },
        actor="oscar",
    )
    assert too_big.data["ok"] is False
    assert too_big.data["error_class"] == "FileTooLarge"


def test_file_loop_multi_variant_attachments_active_meta_review_notice_and_read_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_wp(tmp_path, monkeypatch)

    # _review_notice keeps the LATEST non-unknown risk for a plan
    notice = _review_notice(
        [
            {"site_id": "azom-no", "plan_id": "wr-aaaaaaaaaaaa", "review_path": "/wp/azom-no/reviews/wr-aaaaaaaaaaaa", "risk": "low"},
            {"site_id": "azom-no", "plan_id": "wr-aaaaaaaaaaaa", "review_path": "/wp/azom-no/reviews/wr-aaaaaaaaaaaa", "risk": "high"},
        ]
    )
    assert "risk high" in notice
    assert "risk low" not in notice

    # Synthetic fake tools for verifying dedup, deep promotion, multi-variant attachments, and active_review_meta
    p1_focus = tmp_path / "wr-111111111111" / "focus-desktop.png"
    p1_prev = tmp_path / "wr-111111111111" / "preview-desktop.png"
    p1_mob = tmp_path / "wr-111111111111" / "preview-mobile.png"
    p2_focus = tmp_path / "wr-222222222222" / "focus-desktop.png"
    p2_prev = tmp_path / "wr-222222222222" / "preview-desktop.png"
    p2_mob = tmp_path / "wr-222222222222" / "preview-mobile.png"
    for p in (p1_focus, p1_prev, p1_mob, p2_focus, p2_prev, p2_mob):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_PNG_1X1)

    class _FakeTools:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, Any]]] = []

        def execute(self, name: str, arguments: dict[str, Any], **_kw) -> ToolOutput:
            self.executed.append((name, dict(arguments)))
            if name == "wp_read_theme_plugin_file":
                return ToolOutput(
                    {"ok": True, "site_id": "mock-staging", "path": "style.css", "content": "/* css */"},
                    site_id="mock-staging",
                )
            if name == "wp_inspect_element":
                return ToolOutput(
                    {
                        "ok": True,
                        "site_id": "mock-staging",
                        "path": "/produkt/test/",
                        "viewports": {"desktop": {"found": True}},
                    },
                    site_id="mock-staging",
                )
            if name == "wp_open_review_workspace":
                pid = "wr-111111111111" if arguments.get("summary") == "V1" else "wr-222222222222"
                return ToolOutput(
                    {
                        "ok": True,
                        "site_id": "mock-staging",
                        "plan_id": pid,
                        "variant_group": "btn-v2",
                        "component_kind": "theme",
                        "component_slug": "azom-child",
                        "review_path": f"/wp/mock-staging/reviews/{pid}",
                        "risk": "low",
                        "changed_files": ["style.css"],
                    },
                    site_id="mock-staging",
                )
            if name == "wp_preview_page":
                pid = arguments["plan_id"]
                shots = (
                    (str(p1_focus), str(p1_prev), str(p1_mob))
                    if pid == "wr-111111111111"
                    else (str(p2_focus), str(p2_prev), str(p2_mob))
                )
                return ToolOutput(
                    {
                        "ok": True,
                        "site_id": "mock-staging",
                        "plan_id": pid,
                        "path": "/produkt/test/",
                        "focus_selector": ".single_add_to_cart_button",
                        "preview_html_ready": True,
                    },
                    site_id="mock-staging",
                    attachments=shots,
                )
            return ToolOutput({"ok": True, "site_id": "mock-staging"}, site_id="mock-staging")

    fake = _FakeTools()
    steps = iter(
        [
            # Duplicate identical file read -> should hit turn-local cache!
            _tool_call(
                "wp_read_theme_plugin_file",
                {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "path": "style.css"},
            ),
            # wp_inspect_element -> promotes to complete_deep!
            _tool_call(
                "wp_inspect_element",
                {"site_id": "mock-staging", "path": "/produkt/test/", "selector": ".single_add_to_cart_button"},
            ),
            _tool_call(
                "wp_open_review_workspace",
                {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "V1", "variant_group": "btn-v2"},
            ),
            _tool_call(
                "wp_preview_page",
                {"site_id": "mock-staging", "plan_id": "wr-111111111111", "path": "/produkt/test/", "focus_selector": ".single_add_to_cart_button"},
            ),
            _tool_call(
                "wp_open_review_workspace",
                {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "V2", "variant_group": "btn-v2"},
            ),
            _tool_call(
                "wp_preview_page",
                {"site_id": "mock-staging", "plan_id": "wr-222222222222", "path": "/produkt/test/", "focus_selector": ".single_add_to_cart_button"},
            ),
            "Här är två förslag att jämföra.",
        ]
    )
    deep_calls = {"n": 0}
    phases: list[dict[str, Any]] = []

    def complete_fast(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        return next(steps), 0.0

    def complete_deep(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        deep_calls["n"] += 1
        return next(steps), 0.0

    res = run_bounded_file_tool_loop(
        _tool_call(
            "wp_read_theme_plugin_file",
            {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "path": "style.css"},
        ),
        [],
        complete=complete_fast,
        complete_deep=complete_deep,
        on_phase_change=phases.append,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=fake,  # type: ignore[arg-type]
    )

    assert res.handled is True
    # Duplicate wp_read_theme_plugin_file executed only ONCE on backend despite 2 calls
    read_execs = [item for item in fake.executed if item[0] == "wp_read_theme_plugin_file"]
    assert len(read_execs) == 1
    # Deep phase promoted
    assert deep_calls["n"] >= 1
    assert any(p.get("to_profile") == "wp_code_deep" for p in phases)
    # Multi-variant attachments keep BOTH variants' primary focus-desktop.png (up to 6 PNGs)
    assert str(p1_focus) in res.attachments
    assert str(p2_focus) in res.attachments
    assert len(res.attachments) <= 6
    # active_review_meta matches contract
    meta = res.active_review_meta
    assert meta is not None
    assert meta["site_id"] == "mock-staging"
    assert meta["plan_ids"] == ["wr-111111111111", "wr-222222222222"]
    assert meta["latest_plan_id"] == "wr-222222222222"
    assert meta["variant_group"] == "btn-v2"
    assert meta["preview_path"] == "/produkt/test/"
    assert meta["focus_selector"] == ".single_add_to_cart_button"
    assert meta["theme_slug"] == "azom-child"
    assert "style.css" in meta["changed_files"]
    assert len(meta["preview_images"]) == len(res.attachments)
