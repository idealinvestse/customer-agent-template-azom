"""Comprehensive tests for Feature C1 (HITL WordPress Draft-Page Push) and Feature C2 (Comparison Composites)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _helpers import make_solid_png as _make_solid_png

from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient, wp_client_from_env
from ecom_ops.integrations.wp_browser_smoke import _PNG_MAGIC, _png_dimensions
from ecom_ops.integrations.wp_live_preview import build_comparison_composite
from ecom_ops.wp_ops.agent_tools import WpAgentTools, run_bounded_file_tool_loop
from ecom_ops.wp_ops.review_workspace import workspace_root


def _create_draft_plan(tools: WpAgentTools, *, slug: str = "takk-for-bestillingen") -> tuple[str, dict[str, Any]]:
    opened = tools.execute(
        "wp_open_review_workspace",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "summary": "Ny tacksida",
            "variant_group": "ty-c1",
        },
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]
    drafted = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": slug,
            "title": "Takk for bestillingen",
            "page_type": "thankyou",
            "body_html": '<section class="ty"><h1>Takk!</h1><p>Order AZ-TEST-1042 till test@example.invalid.</p></section>',
            "page_css": ".ty { padding: 32px; background: #fff; }",
            "synthetic_context": {"order": "AZ-TEST-1042", "email": "test@example.invalid"},
        },
        actor="oscar",
    )
    assert drafted.data["ok"] is True
    row = tools._review_plans().get(plan_id, site_id="mock-staging")
    assert row is not None
    return plan_id, row


# ---------------------------------------------------------------------------
# Feature C1: WordPressClient.create_page & HITL push_page_draft
# ---------------------------------------------------------------------------


def test_wordpress_client_create_page_mock_and_live_mock_and_refuses_publish() -> None:
    mock_client = wp_client_from_env(base_url="https://azom.no", use_mock=True)
    created = mock_client.create_page(
        title="Takk for bestillingen",
        content="<!-- wp:html --><section><h1>Takk!</h1></section><!-- /wp:html -->",
        slug="takk-for-bestillingen",
        status="draft",
    )
    assert created["id"] >= 100
    assert created["slug"] == "takk-for-bestillingen"
    assert created["title"] == "Takk for bestillingen"
    assert created["status"] == "draft"
    assert created["edit_url"] == f"https://azom.no/wp-admin/post.php?post={created['id']}&action=edit"

    # HARD SAFETY RULE: any status != "draft" is strictly refused
    for bad_status in ("publish", "private", "pending", "future", ""):
        with pytest.raises(ValueError, match="draft"):
            mock_client.create_page(
                title="Should fail",
                content="<p>No</p>",
                status=bad_status,
            )

    # Live-mock transport verifies exact POST /wp-json/wp/v2/pages payload with status='draft'
    captured: list[tuple[str, str, dict[str, Any] | None]] = []

    class _RecordingTransport:
        def request(
            self,
            method: str,
            url: str,
            *,
            auth: tuple[str, str] | None = None,
            json: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
            timeout: float = 30,
        ) -> Any:
            del auth, params, timeout
            captured.append((method, url, json))
            return {
                "id": 777,
                "slug": "kampanje-2026",
                "title": {"rendered": "Kampanje 2026"},
                "status": "draft",
                "link": "https://azom.no/?page_id=777",
            }

    live_client = WordPressClient(
        base_url="https://azom.no",
        username="oscar",
        app_password="app-password-placeholder",
        transport=_RecordingTransport(),
    )
    live_res = live_client.create_page(
        title="Kampanje 2026",
        content="<!-- wp:html --><p>Hej</p><!-- /wp:html -->",
        slug="kampanje-2026",
        status="draft",
    )
    assert len(captured) == 1
    assert captured[0][0] == "POST"
    assert captured[0][1] == "https://azom.no/wp-json/wp/v2/pages"
    assert captured[0][2] is not None and captured[0][2]["status"] == "draft"
    assert live_res["id"] == 777
    assert live_res["status"] == "draft"
    assert live_res["edit_url"] == "https://azom.no/wp-admin/post.php?post=777&action=edit"


def test_push_page_draft_enforces_rbac_digests_pii_and_persists_draft_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", with_child_theme=True)
    plan_id, row = _create_draft_plan(tools)
    base_sha = str(row["base_sha256"])
    work_sha = str(row["work_sha256"])

    # 1. Viewer (jonatan) & operator (agent) denied
    for non_admin in ("jonatan", "agent"):
        denied = service.push_page_draft(
            "mock-staging",
            plan_id,
            actor=non_admin,
            base_sha256=base_sha,
            work_sha256=work_sha,
        )
        assert denied["ok"] is False
        assert denied["error_class"] == "AccessDenied"

    # 2. Missing or mismatched digest fails closed (DigestMismatch)
    bad_hash = service.push_page_draft(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base_sha,
        work_sha256="0" * 64,
    )
    assert bad_hash["ok"] is False
    assert bad_hash["error_class"] == "DigestMismatch"

    empty_hash = service.push_page_draft(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256="",
        work_sha256=work_sha,
    )
    assert empty_hash["ok"] is False
    assert empty_hash["error_class"] == "DigestMismatch"

    # 3. Oscar push succeeds in mock mode, wraps Gutenberg HTML, persists draft_page + audit
    pushed = service.push_page_draft(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base_sha,
        work_sha256=work_sha,
    )
    assert pushed["ok"] is True
    assert pushed["plan_id"] == plan_id
    assert pushed["site_id"] == "mock-staging"
    dp = pushed["draft_page"]
    assert dp["status"] == "draft"
    assert dp["slug"] == "takk-for-bestillingen"
    assert dp["pushed_by"] == "oscar"
    assert "/wp-admin/post.php?post=" in dp["edit_url"]
    assert "&action=edit" in dp["edit_url"]

    # Persisted on plan record
    reloaded = tools._review_plans().get(plan_id, site_id="mock-staging")
    assert reloaded is not None
    assert reloaded["draft_page"]["edit_url"] == dp["edit_url"]
    assert reloaded["draft_page"]["status"] == "draft"

    # Audit log contains wp_review_push_draft
    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"wp_review_push_draft"' in line for line in audit_lines)

    # 4. Tampering work/page_draft.json to inject real PII before push is rejected (RealPiiRefused)
    plan2_id, _row2 = _create_draft_plan(tools, slug="pii-test")
    ws_root2 = workspace_root(tmp_path, "mock-staging", plan2_id)
    draft_file = ws_root2 / "work" / "page_draft.json"
    tampered = json.loads(draft_file.read_text(encoding="utf-8"))
    tampered["body_html"] = "<p>Kontakt kund@riktig-doman.se nu</p>"
    draft_file.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    refreshed = tools._review_plans().refresh("mock-staging", plan2_id, actor="oscar")
    assert refreshed.ok is True
    row2_fresh = tools._review_plans().get(plan2_id, site_id="mock-staging")
    assert row2_fresh is not None

    pii_blocked = service.push_page_draft(
        "mock-staging",
        plan2_id,
        actor="oscar",
        base_sha256=row2_fresh["base_sha256"],
        work_sha256=row2_fresh["work_sha256"],
        client=WordPressClient(base_url="https://mock.local", transport=InMemoryWpTransport()),
    )
    assert pii_blocked["ok"] is False
    assert pii_blocked["error_class"] == "RealPiiRefused"


# ---------------------------------------------------------------------------
# Feature C2: Side-by-Side Comparison Composite PNGs
# ---------------------------------------------------------------------------


def test_build_comparison_composite_2_panel_and_3_panel_dimensions_and_crop(tmp_path: Path) -> None:
    p1 = tmp_path / "live-desktop.png"
    p2 = tmp_path / "preview-desktop.png"
    p3 = tmp_path / "variant-3.png"
    p1.write_bytes(_make_solid_png(1280, 1600, (30, 58, 138)))
    p2.write_bytes(_make_solid_png(1280, 900, (22, 163, 74)))
    p3.write_bytes(_make_solid_png(600, 500, (217, 119, 6)))

    # 2-panel before/after composite scales wide panels to <= 720px and crops height at max_height
    out_2 = tmp_path / "compare-2.png"
    res_2 = build_comparison_composite(
        [("Före (Live)", p1), ("Efter (wr-0123456789ab)", p2)],
        out_2,
        max_panel_width=400,
        max_height=300,
    )
    assert res_2 == out_2
    assert out_2.is_file()
    raw_2 = out_2.read_bytes()
    assert raw_2[:8] == _PNG_MAGIC
    dims_2 = _png_dimensions(raw_2)
    assert dims_2 is not None
    # Two 400px panels + 2px divider = 802px wide; header 38px + cropped 300px = 338px tall
    assert dims_2[0] == 400 + 2 + 400
    assert dims_2[1] == 38 + 300

    # 3-panel variant_group composite
    out_3 = tmp_path / "compare-3.png"
    res_3 = build_comparison_composite(
        [
            ("Förslag 1 (wr-111111111111)", p1),
            ("Förslag 2 (wr-222222222222)", p2),
            ("Förslag 3 (wr-333333333333)", p3),
        ],
        out_3,
        max_panel_width=320,
        max_height=240,
    )
    assert res_3 == out_3
    dims_3 = _png_dimensions(out_3.read_bytes())
    assert dims_3 is not None
    assert dims_3[0] == 320 + 2 + 320 + 2 + 320
    assert dims_3[1] == 38 + 240


def test_live_preview_generates_before_after_compare_and_loop_builds_variant_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", with_child_theme=True)
    plans = tools._review_plans()

    # Create 3 variants in the same variant_group with desktop preview PNGs
    variant_ids: list[str] = []
    colors = [(30, 58, 138), (22, 163, 74), (185, 28, 28)]
    for idx, color in enumerate(colors, start=1):
        opened = tools.execute(
            "wp_open_review_workspace",
            {
                "site_id": "mock-staging",
                "kind": "theme",
                "slug": "azom-child",
                "summary": f"Variant {idx}",
                "variant_group": "btn-ab",
            },
            actor="oscar",
        )
        assert opened.data["ok"] is True
        pid = opened.data["plan_id"]
        variant_ids.append(pid)
        art_dir = workspace_root(tmp_path, "mock-staging", pid) / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "focus-desktop.png").write_bytes(_make_solid_png(480, 320, color))

    # build_variant_comparison produces 3-panel compare-desktop.png on the latest variant
    comp = plans.build_variant_comparison("mock-staging", variant_ids)
    assert comp is not None
    assert comp.name == "compare-desktop.png"
    assert comp.parent == workspace_root(tmp_path, "mock-staging", variant_ids[-1]) / "artifacts"
    dims = _png_dimensions(comp.read_bytes())
    assert dims is not None
    assert dims[0] == 480 * 3 + 4
    assert dims[1] == 38 + 320

    # Verify run_bounded_file_tool_loop places compare-desktop.png FIRST when >=2 variants are previewed
    # and populates active_review_meta['page_draft'] cleanly when a page_draft is created/previewed
    def _fake_execute(
        name: str,
        arguments: dict[str, Any],
        *,
        actor: str,
        sticky_site_id: str | None = None,
        channel: str = "telegram",
        conversation_id: str = "c1",
    ) -> Any:
        from ecom_ops.wp_ops.agent_tools import ToolOutput

        del actor, sticky_site_id, channel, conversation_id
        pid = str(arguments.get("plan_id") or "")
        shot = workspace_root(tmp_path, "mock-staging", pid) / "artifacts" / "focus-desktop.png"
        return ToolOutput(
            {
                "ok": True,
                "status": "previewed",
                "site_id": "mock-staging",
                "plan_id": pid,
                "path": "/",
                "variant_group": "btn-ab",
                "preview_kind": "standalone_page",
                "page_draft": {"slug": "tack", "title": "Tack", "page_type": "thankyou"},
                "screenshots": ["focus-desktop.png"],
                "screenshot_paths": [str(shot)],
            },
            site_id="mock-staging",
            attachments=(str(shot),),
        )

    monkeypatch.setattr(tools, "execute", _fake_execute)
    calls_iter = iter(
        [
            (f'<tool_call>{json.dumps({"name": "wp_preview_page", "arguments": {"site_id": "mock-staging", "plan_id": variant_ids[1]}})}</tool_call>', 0.0),
            ("Här är jämförelsen mellan båda varianterna.", 0.0),
        ]
    )
    first_call = f'<tool_call>{json.dumps({"name": "wp_preview_page", "arguments": {"site_id": "mock-staging", "plan_id": variant_ids[0]}})}</tool_call>'
    loop_res = run_bounded_file_tool_loop(
        first_call,
        [{"role": "user", "content": "Visa två varianter"}],
        complete=lambda _msgs: next(calls_iter),
        actor="oscar",
        channel="telegram",
        conversation_id="c1",
        context={"site_id": "mock-staging"},
        tools=tools,
    )
    assert loop_res.handled is True
    assert loop_res.attachments
    assert Path(loop_res.attachments[0]).name == "compare-desktop.png"
    assert loop_res.active_review_meta is not None
    assert loop_res.active_review_meta["page_draft"]["slug"] == "tack"
    assert loop_res.active_review_meta["page_draft"]["page_type"] == "thankyou"
    assert loop_res.active_review_meta["page_draft"]["preview_kind"] == "standalone_page"


def test_live_preview_automatically_generates_compare_desktop_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", with_child_theme=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "CTA color"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]
    plans = tools._review_plans()

    def _fake_fetch(_site: Any, path: str = "/", **_kwargs: Any) -> dict[str, Any]:
        return {
            "html": "<!doctype html><html><head><title>Shop</title></head><body><button class='btn'>Köp</button></body></html>",
            "final_url": f"https://staging.example.test{path}",
            "status_code": 200,
        }

    def _fake_capture(_site: Any, *, artifact_dir: Path, **_kwargs: Any) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "live-desktop.png").write_bytes(_make_solid_png(640, 400, (15, 23, 42)))
        (artifact_dir / "preview-desktop.png").write_bytes(_make_solid_png(640, 400, (34, 197, 94)))
        return {
            "screenshots": ["live-desktop.png", "preview-desktop.png"],
            "blocked_counts": {},
            "focus_matched": False,
            "focus_target": None,
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.fetch_live_html", _fake_fetch)
    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", _fake_capture)

    res = plans.live_preview("mock-staging", plan_id, actor="oscar", path="/")
    assert res.ok is True
    assert "compare-desktop.png" in res.data["screenshots"]
    assert res.data["screenshots"][0] == "compare-desktop.png"
    comp_path = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts" / "compare-desktop.png"
    assert comp_path.is_file()
    dims = _png_dimensions(comp_path.read_bytes())
    assert dims == (640 * 2 + 2, 38 + 400)
