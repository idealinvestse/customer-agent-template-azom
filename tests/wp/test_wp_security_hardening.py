"""Adversarial security, concurrency, and regression tests for WP ops & sandbox hardening."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.wp_live_preview import (
    _AllowlistedSiteRedirectHandler,
    build_interactive_sandbox_html,
    validate_preview_path,
)
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.file_changes import (
    is_safe_pure_css_file,
    risk_for,
    search_texts,
    validate_safe_regex,
)
from ecom_ops.wp_ops.files import LocalTree, PathRejected, TransportError
from ecom_ops.wp_ops.preview_overlay import (
    apply_overlay,
    apply_product_edits_overlay,
    compose_standalone_page,
    sanitize_page_draft_html,
)
from ecom_ops.wp_ops.review_plan import ReviewPlans, review_risk
from ecom_ops.wp_ops.review_workspace import ReviewError, bulk_replace_work, place_work_file
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[2]
MOCK_REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"


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
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n.btn { color: #111; }\n",
        encoding="utf-8",
    )
    (theme_dir / "functions.php").write_text("<?php\n// child theme\n", encoding="utf-8")
    service = WpOpsService(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    tools = WpAgentTools(service)
    return service, tools


def test_interactive_sandbox_strips_all_untrusted_scripts_and_events_keeps_guard_marker() -> None:
    malicious_html = (
        "<!doctype html><html><head><title>Shop</title>"
        '<script src="https://evil.example/steal.js"></script>'
        "<script>window.__pwned = true;</script>"
        "</head><body>"
        '<img src="https://azom.no/a.png" onerror="alert(1)" onclick="fetch(\'/wp-admin\')">'
        '<a href="javascript:alert(2)" onmouseover="evil()">Buy</a>'
        "</body></html>"
    )
    out = build_interactive_sandbox_html(
        malicious_html,
        site_canonical_url="https://azom.no",
        site_id="azom-no",
        plan_id="wr-0123456789ab",
        path="/",
    )
    assert "evil.example/steal.js" not in out
    assert "__pwned" not in out
    assert "onerror=" not in out.lower()
    assert "onclick=" not in out.lower()
    assert "onmouseover=" not in out.lower()
    assert "javascript:alert" not in out.lower()
    # Exactly one <script ...> tag — our guard script with both attributes
    assert out.lower().count("<script") == 1
    assert '<script data-azom-sandbox="guard" data-azom-guard="1">' in out


def test_apply_overlay_neutralizes_script_and_event_handlers_in_hunks_and_i18n() -> None:
    live_html = "<html><body><div class=\"cta\"><span>Buy now</span></div></body></html>"
    before_tpl = "<span>Buy now</span>"
    after_tpl = (
        "<span><?php _e('<script>alert(1)</script><b onclick=\"evil()\">Order</b>', 'azom'); ?></span>"
        "<img src=/x onerror=alert(2)>"
    )
    res = apply_overlay(live_html, [("template.php", before_tpl, after_tpl)])
    assert "<script" not in res.html.lower()
    assert "onclick=" not in res.html.lower()
    assert "onerror=" not in res.html.lower()
    assert "Order" in res.html


def test_approve_staging_refuses_sandbox_blueprint_only_and_never_deploys_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Blueprint only"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    drafted = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack",
            "page_type": "thankyou",
            "body_html": "<section><h1>Tack!</h1></section>",
            "page_css": ".ok { color: #111; }",
        },
        actor="oscar",
    )
    assert drafted.data["ok"] is True

    plans = tools._review_plans()
    row = plans.get(plan_id, site_id="mock-staging")
    assert row is not None
    # Simulate passed check evidence so approve_staging reaches the deployable diff gate
    row["status"] = "tests_passed"
    plans._replace(row)
    monkeypatch.setattr(plans, "_require_current_evidence", lambda _plan: None)

    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
    )
    assert staged.ok is False
    assert staged.data["error_class"] == "SandboxBlueprintOnly"
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-child"
    assert not (theme_dir / "page_draft.json").exists()
    assert not (theme_dir / "product_edits.json").exists()


def test_review_plans_fork_refuses_draft_superseded_and_rejected_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)
    plans = tools._review_plans()

    # 1. Unvalidated draft cannot be forked
    draft = plans.open("mock-staging", kind="theme", slug="azom-child", actor="oscar", summary="Empty draft")
    assert draft.ok is True
    draft_id = draft.data["plan_id"]
    fork_draft = plans.fork("mock-staging", draft_id, actor="oscar")
    assert fork_draft.ok is False
    assert fork_draft.data["error_class"] == "ForkRefused"

    # 2. Validated plan that gets superseded cannot be forked (StalePlan)
    open_a = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Validated A"},
        actor="oscar",
    )
    assert open_a.data["ok"] is True
    plan_a = open_a.data["plan_id"]
    assert tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "path": "style.css",
            "old_text": ".btn { color: #111; }",
            "new_text": ".btn { color: #222; }",
        },
        actor="oscar",
    ).data["ok"]
    open_b = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Supersedes A"},
        actor="oscar",
    )
    assert open_b.data["ok"] is True
    plan_b = open_b.data["plan_id"]
    fork_stale = plans.fork("mock-staging", plan_a, actor="oscar")
    assert fork_stale.ok is False
    assert fork_stale.data["error_class"] == "StalePlan"

    # 3. Denied / rejected / fork_not_approved / rolled_back plans cannot be forked (ForkRefused)
    for bad_status in ("denied", "rejected", "fork_not_approved", "rolled_back"):
        row_b = plans.get(plan_b, site_id="mock-staging")
        assert row_b is not None
        row_b["status"] = bad_status
        plans._replace(row_b)
        fork_bad = plans.fork("mock-staging", plan_b, actor="oscar")
        assert fork_bad.ok is False, f"Expected ForkRefused for status={bad_status}"
        assert fork_bad.data["error_class"] == "ForkRefused"


def test_page_draft_blocks_encoded_pii_phones_personnummer_and_dict_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "PII test"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    adversarial_cases: list[dict[str, Any]] = [
        {"title": "Tack", "body_html": "<p>anna&#64;gmail.com</p>"},
        {"title": "Tack", "body_html": "<p>anna%40gmail.com</p>"},
        {"title": "Beställning för anna@gmail.com", "body_html": "<p>Tack!</p>"},
        {"title": "Tack", "body_html": "<p>OK</p>", "page_css": "/* anna@gmail.com */ .x { color: #111; }"},
        {"title": "Tack", "body_html": "<p>OK</p>", "synthetic_context": {"anna@gmail.com": "val"}},
        {"title": "Tack", "body_html": "<p>Ring +46 70-123 45 67</p>"},
        {"title": "Tack", "body_html": "<p>Ring 070-123 45 67</p>"},
        {"title": "Tack", "body_html": "<p>ID: 19900101-1234</p>"},
    ]
    for case in adversarial_cases:
        res = tools.execute(
            "wp_workspace_page_draft",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "slug": "tack",
                "page_type": "thankyou",
                **case,
            },
            actor="oscar",
        )
        assert res.data["ok"] is False, f"Expected RealPiiRefused for {case!r}"
        assert res.data["error_class"] == "RealPiiRefused"

    # Synthetic fixture still allowed
    ok_res = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack för beställningen (AZ-TEST-1042)",
            "page_type": "thankyou",
            "body_html": "<p>Bekräftelse till test@example.invalid — datum 2026-11-26.</p>",
            "synthetic_context": {"email": "test@example.invalid", "order": "AZ-TEST-1042"},
        },
        actor="oscar",
    )
    assert ok_res.data["ok"] is True


def test_css_and_inline_style_escape_and_url_bypasses_blocked() -> None:
    css_bypasses = [
        r"@\69 mport 'https://evil.test/x.css';",
        "body { width: expr/**/ession(alert(1)); }",
        "body { background: url(https://evil.test/track.png); }",
        r"body { background: \75 rl(https://evil.test/track.png); }",
        "body { background: image-set('https://evil.test/a.png' 1x); }",
        "body { behavior: url(xss.htc); }",
        "body { -moz-binding: url(xbl.xml); }",
        "body { color: red; } </style><img src=x onerror=alert(1)>",
    ]
    for payload in css_bypasses:
        assert is_safe_pure_css_file("style.css", payload) is False, f"Bypass allowed: {payload!r}"
        assert risk_for("style.css", payload) == "high"

    # Page draft review_risk catches svg / onerror / obfuscated @import
    assert review_risk("page_draft.json", '{"body_html":"<svg onload=alert(1)>"}') == "high"
    assert review_risk("page_draft.json", r'{"page_css":"@\69 mport \"x\";"}') == "high"
    assert review_risk("page_draft.json", '{"body_html":"<p>Safe</p>","page_css":".a{color:#111;}"}') == "low"

    # Inline styles + URLs + target="_blank" hardening in sanitize_page_draft_html
    sanitized = sanitize_page_draft_html(
        '<div style="background:\\75 rl(https://evil.test/a.png);position:fixed;">'
        '<a href="/\\evil.test/a" target="_blank">Backslash</a>'
        '<a href="https://169.254.169.254/latest/meta-data" target="_blank">IMDS</a>'
        '<a href="https://azom.no/guide/" target="_blank">Good</a>'
        '<img src="https://evil.test/track.png" alt="bad">'
        '<img src="https://azom.no/img/ok.png" alt="ok">'
        "</div>",
        allowed_hosts=frozenset({"azom.no"}),
    )
    assert "evil.test" not in sanitized
    assert "169.254.169.254" not in sanitized
    assert "position:fixed" not in sanitized
    assert 'href="https://azom.no/guide/"' in sanitized
    assert 'rel="noopener noreferrer"' in sanitized
    assert 'src="https://azom.no/img/ok.png"' in sanitized


def test_redos_patterns_rejected_in_search_and_bulk_replace(tmp_path: Path) -> None:
    bad_patterns = ["(a+)+$", "([a-z]+)*b", "(a*)+", "(a+){2,}", "(a|aa)+", r"(foo)\1"]
    for pat in bad_patterns:
        with pytest.raises(ValueError):
            validate_safe_regex(pat)
        with pytest.raises(ValueError):
            search_texts({"style.css": "aaaaaa"}, pat, mode="regex")

    # bulk_replace_work rejects ReDoS cleanly with InvalidSearch
    ws = tmp_path / "ws"
    (ws / "base").mkdir(parents=True)
    (ws / "work").mkdir(parents=True)
    (ws / "base" / "style.css").write_text(".a { color: #111; }\n", encoding="utf-8")
    (ws / "work" / "style.css").write_text(".a { color: #111; }\n", encoding="utf-8")
    with pytest.raises(ReviewError) as exc_info:
        bulk_replace_work(ws, "(a+)+$", "x", mode="regex", dry_run=True)
    assert exc_info.value.error_class == "InvalidSearch"

    # Safe regexes still work
    hits = search_texts({"style.css": ".azom_btn { color: #1e3a8a; }\n"}, r"azom_[a-z]+", mode="regex")
    assert len(hits["hits"]) == 1


def test_preview_path_blocks_swedish_checkout_and_multi_encoded_paths() -> None:
    for bad in (
        "/varukorg/",
        "/kassa/",
        "/kassan/",
        "/mitt-konto/",
        "/betalning/",
        "/kurv/",
        "/%252563heckout/",
        "/%25252563art/",
    ):
        with pytest.raises(ReviewError) as exc_info:
            validate_preview_path(bad)
        assert exc_info.value.error_class == "PreviewPathRefused", f"Expected refusal for {bad}"

    # Redirect handler re-checks DNS public IP resolution
    handler = _AllowlistedSiteRedirectHandler(frozenset({"azom.no"}))
    req = urllib.request.Request("https://azom.no/")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ecom_ops.integrations.wp_live_preview._host_resolves_public", lambda _h: False)
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://azom.no/butikk/")


def test_re_sub_backreferences_in_title_and_button_text_do_not_crash() -> None:
    shell = "<!doctype html><html><head><title>Old</title></head><body><main>Old</main></body></html>"
    composed = compose_standalone_page(
        shell,
        title=r"Rabatt \1 och \g<99> på order",
        body_html="<p>OK</p>",
    )
    assert r"Rabatt \1 och \g&lt;99&gt; på order" in composed.html

    prod_html = '<html><body><input type="submit" class="single_add_to_cart_button" value="Köp"></body></html>'
    overlay = apply_product_edits_overlay(prod_html, {"button_text": r"Köp \1 nu \g<99>"})
    assert r'\1' in overlay.html
    assert r'\g&lt;99&gt;' in overlay.html


def test_file_editor_read_redacts_secrets_and_local_tree_blocks_symlinks_and_place_work_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _tools = _setup_mock_wp(tmp_path, monkeypatch)
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-child"
    (theme_dir / "config-helper.php").write_text(
        "<?php\n$api_key = 'sk_live_supersecret123456';\n$db_password = 'top-secret-password';\n$ok = 'hello';\n",
        encoding="utf-8",
    )
    editor = service._file_editor()
    res = editor.read_file("mock-staging", "theme", "azom-child", "config-helper.php", actor="oscar")
    assert res.ok is True
    content = str(res.data.get("content") or "")
    assert "sk_live_supersecret123456" not in content
    assert "top-secret-password" not in content
    assert "[REDACTED]" in content
    assert "$ok = 'hello';" in content

    # place_work_file rejects sensitive names and invalid extensions
    ws = tmp_path / "ws2"
    (ws / "base").mkdir(parents=True)
    (ws / "work").mkdir(parents=True)
    with pytest.raises(ReviewError):
        place_work_file(ws, ".env", b"SECRET=1")
    with pytest.raises(ReviewError):
        place_work_file(ws, "wp-config.php", b"<?php")

    # LocalTree.read_css_search_bytes enforces max_bytes without OOM
    big_css = theme_dir / "huge.css"
    big_css.write_bytes(b"a" * 2048)
    tree = LocalTree("/var/www/staging", tmp_path / "wp_files_mock" / "mock-staging")
    with pytest.raises(TransportError) as exc_info:
        tree.read_css_search_bytes(
            "/var/www/staging/wp-content/themes/azom-child/huge.css",
            max_bytes=1024,
        )
    assert exc_info.value.error_class == "FileTooLarge"

    # Symlink refusal on read_bytes / write_bytes (when OS permits symlink creation)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link_path = theme_dir / "linked.css"
    try:
        os.symlink(outside, link_path)
    except OSError:
        pytest.skip("OS does not permit unprivileged symlink creation")
    with pytest.raises(PathRejected):
        tree.read_bytes("/var/www/staging/wp-content/themes/azom-child/linked.css")
    with pytest.raises(PathRejected):
        tree.write_bytes("/var/www/staging/wp-content/themes/azom-child/linked.css", b"overwrite")


def test_review_plans_concurrent_replace_preserves_all_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_wp(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    created_ids: list[str] = []
    for idx in range(6):
        res = plans.open(
            "mock-staging",
            kind="theme",
            slug="azom-child",
            actor="oscar",
            summary=f"Concurrent {idx}",
            variant_group="conc",
        )
        assert res.ok is True
        created_ids.append(res.data["plan_id"])

    def _update(pid: str) -> None:
        row = plans.get(pid, site_id="mock-staging")
        assert row is not None
        row["summary"] = f"Updated {pid}"
        plans._replace(row)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_update, created_ids))

    raw = json.loads((tmp_path / "wp_review_plans.json").read_text(encoding="utf-8"))
    saved_ids = {row["plan_id"] for row in raw["plans"]}
    assert set(created_ids).issubset(saved_ids)


def test_r2_r3_r4_slash_event_handlers_optional_chain_redos_and_numeric_ip_urls() -> None:
    # R2: Slash-separated and quoted-'>' inline event handlers stripped in BOTH sandbox HTML and apply_overlay
    tricky_html = (
        "<!doctype html><html><head><title>R2</title></head><body>"
        '<svg/onload="void(1)">'
        '<img src="https://azom.no/a.png"/onerror="void(2)">'
        '<img alt=">" onerror="void(3)">'
        '<div title="safe onclick=1 text">OK</div>'
        "</body></html>"
    )
    sandboxed = build_interactive_sandbox_html(
        tricky_html,
        site_canonical_url="https://azom.no",
        site_id="azom-no",
        plan_id="wr-0123456789ab",
        path="/",
    )
    guard_idx = sandboxed.index('<script data-azom-sandbox="guard" data-azom-guard="1">')
    before_guard = sandboxed[:guard_idx].lower()
    assert "onload=" not in before_guard
    assert "onerror=" not in before_guard
    assert 'title="safe onclick=1 text"' in sandboxed

    overlay_res = apply_overlay(
        "<html><body><span>Old</span></body></html>",
        [
            (
                "tpl.php",
                "<span>Old</span>",
                '<svg/onload="void(1)"><img src="x"/onerror="void(2)"><img alt=">" onerror="void(3)"><span>New</span>',
            )
        ],
    )
    assert "onload=" not in overlay_res.html.lower()
    assert "onerror=" not in overlay_res.html.lower()
    assert "<span>New</span>" in overlay_res.html

    # R3: Optional-chain ReDoS patterns rejected
    for optional_redos in ("(a?){24}a{24}", "(a?){8}a{8}", "(a?)+", "(?:a{0,1}){10}"):
        with pytest.raises(ValueError):
            validate_safe_regex(optional_redos)
    # Benign optional regex still allowed
    validate_safe_regex(r"https?://[a-z0-9.-]+")

    # R4: Legacy numeric / shorthand / octal / hex IPv4 hosts blocked in _is_safe_url
    sanitized = sanitize_page_draft_html(
        '<a href="https://2130706433/">DecIP</a>'
        '<a href="https://127.1/">ShortIP</a>'
        '<a href="https://0x7f000001/">HexIP</a>'
        '<a href="https://0177.0.0.1/">OctIP</a>'
        '<a href="https://azom.no/butikk/">Valid</a>',
        allowed_hosts=frozenset({"azom.no"}),
    )
    assert "2130706433" not in sanitized
    assert "127.1" not in sanitized
    assert "0x7f000001" not in sanitized
    assert "0177.0.0.1" not in sanitized
    assert 'href="https://azom.no/butikk/"' in sanitized
