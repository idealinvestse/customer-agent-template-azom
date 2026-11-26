"""Focused unit tests for the foundational ``wp_ops/tools/`` leaf modules."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops import agent_tools
from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.review_workspace import workspace_root
from ecom_ops.wp_ops.tools import (
    WP_DRIFT_TOOL_GUIDE,
    WP_TOOL_GUIDE,
    ToolContext,
    ToolLoopResult,
    ToolOutput,
    authorize_editor_site,
    context_for_storage,
    context_matches,
    extract_html_by_selector,
    extract_json_ld_products,
    extract_product_dom_fields,
    parse_html_meta,
    parse_product_links,
    parse_tool_call,
    read_workspace_json,
    scrub_bounded,
    scrub_keep_newlines,
    scrub_nested,
    strip_html_plain,
    upsert_workspace_json,
    utf8_window,
    valid_context,
)
from ecom_ops.wp_ops.tools.validation import (
    optional_bool_arg,
    optional_str_arg,
    parse_line_range,
)


def test_tools_leaves_have_no_banned_tokens_or_integrations_imports() -> None:
    tools_dir = Path("skills/ecom_ops/wp_ops/tools")
    leaf_names = {
        "__init__.py",
        "types.py",
        "guides.py",
        "context.py",
        "validation.py",
        "scrub.py",
        "html_parse.py",
        "workspace_io.py",
        "catalog.py",
    }
    all_files = sorted(tools_dir.glob("*.py"))
    assert len(all_files) >= 8
    banned = ("requests", "urlopen", "http.client", "urllib.request", "subprocess", "os.system", "integrations.wordpress")
    for path in all_files:
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{token} found in {path}"
        if path.name in leaf_names:
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "integrations" not in alias.name, f"integrations import in {path}"
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert "integrations" not in node.module, f"integrations import in {path}"


def test_agent_tools_reexports_match_tools_leaves_identity() -> None:
    assert agent_tools.ToolOutput is ToolOutput
    assert agent_tools.ToolLoopResult is ToolLoopResult
    assert agent_tools.WP_DRIFT_TOOL_GUIDE is WP_DRIFT_TOOL_GUIDE
    assert agent_tools.WP_TOOL_GUIDE is WP_TOOL_GUIDE
    assert agent_tools.context_for_storage is context_for_storage
    assert agent_tools.context_matches is context_matches
    assert agent_tools._valid_context is valid_context
    assert agent_tools._parse_tool_call is parse_tool_call
    assert agent_tools._utf8_window is utf8_window
    ctx = ToolContext(actor="oscar", site_id="azom-se", channel="dashboard", conversation_id="conv-1")
    assert ctx.actor == "oscar"
    assert ctx.site_id == "azom-se"


def test_validation_context_and_scrub_helpers() -> None:
    text, nxt = utf8_window("åäö".encode(), 0, 2)
    assert text == "å"
    assert nxt == 2
    assert parse_tool_call('<tool_call>{"name":"wp_list_sites","arguments":{}}</tool_call>') == (
        "wp_list_sites",
        {},
    )
    assert parse_line_range({"start_line": 2, "end_line": 10}) == (2, 10, True)
    assert parse_line_range({"start_line": 10, "end_line": 2}) == (None, None, False)
    assert parse_line_range({"start_line": True}) == (None, None, False)
    assert optional_str_arg({"path": "/produkt/x"}, "path") == ("/produkt/x", True)
    assert optional_str_arg({"path": 123}, "path") == (None, False)
    assert optional_bool_arg({"dry_run": True}, "dry_run") == (True, True)
    assert optional_bool_arg({"dry_run": 1}, "dry_run") == (False, False)

    stored = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="chat-1",
        site_id="azom-se",
        operation_ids=("wp-plan-0123456789abcdef", "bad-id"),
    )
    assert stored is not None
    assert context_matches(stored, actor="oscar", channel="telegram", conversation_id="chat-1")
    assert not context_matches(stored, actor="jonatan", channel="telegram", conversation_id="chat-1")
    assert valid_context(stored, actor="oscar", channel="telegram", conversation_id="chat-1") == {
        "site_id": "azom-se",
        "operation_ids": ["wp-plan-0123456789abcdef"],
    }

    scrubbed = scrub_bounded(None, "ok line\nBearer ghp_1234567890abcdef1234567890abcdef", max_len=80)
    assert "ghp_1234567890abcdef1234567890abcdef" not in scrubbed
    kept, clipped = scrub_keep_newlines(None, "a\nb\nc", max_bytes=3)
    assert clipped is True
    assert kept == "a\nb"
    nested = scrub_nested({"k": ["token=secret123", 42]}, extra_secrets=["secret123"])
    assert "secret123" not in nested["k"][0]
    assert nested["k"][1] == 42


def test_html_parse_helpers() -> None:
    sample = (
        '<html lang="sv"><head><title>Butik &amp; Hem</title>'
        '<meta name="description" content="Kort beskrivning">'
        '<meta property="og:image" content="https://azom.se/img/p.jpg">'
        '<link rel="canonical" href="https://azom.se/produkt/kudde/">'
        '<script type="application/ld+json">{"@type":"Product","name":"Kudde","sku":"KD-1",'
        '"offers":{"price":"299","priceCurrency":"SEK"}}</script>'
        '</head><body><h1>Kudde Pro</h1>'
        '<div class="woocommerce-product-details__short-description"><p>Mjuk <b>kudde</b></p></div>'
        '<p class="price"><del>399 kr</del><ins>299 kr</ins></p>'
        '<button class="single_add_to_cart_button">Köp nu</button>'
        '<span class="posted_in"><a href="/kat/hem">Hem</a></span>'
        '<a href="/produkt/kudde/" title="Kudde Pro">Kudde</a>'
        '<img src="https://cdn.example.com/a.png">'
        "</body></html>"
    )
    meta = parse_html_meta(sample, site_host="azom.se")
    assert meta.lang == "sv"
    assert "".join(meta.title_parts) == "Butik & Hem"
    assert meta.h1_list == ["Kudde Pro"]
    assert meta.images == 1
    assert meta.images_missing_alt == 1
    assert meta.external_hosts == ["cdn.example.com"]

    links = parse_product_links(sample)
    assert any(href == "/produkt/kudde/" for href, _label in links)
    snippet, matches = extract_html_by_selector(sample, "button.single_add_to_cart_button")
    assert matches == 1
    assert "Köp nu" in snippet
    assert strip_html_plain("<p>Hej<script>alert(1)</script> &amp; hå</p>") == "Hej & hå"

    ld = extract_json_ld_products(sample)
    assert len(ld) == 1 and ld[0]["sku"] == "KD-1"
    dom = extract_product_dom_fields(sample)
    assert dom["h1"] == "Kudde Pro"
    assert dom["regular_price"] == "399 kr"
    assert dom["sale_price"] == "299 kr"
    assert dom["button_text"] == "Köp nu"
    assert dom["categories"] == ["Hem"]
    assert dom["og_image"] == "https://azom.se/img/p.jpg"


def test_workspace_io_helpers(tmp_path: Path) -> None:
    calls: list[str] = []
    work_dir = workspace_root(tmp_path, "azom-se", "wr-0123456789ab") / "work"
    work_dir.mkdir(parents=True)

    class _FakeOps:
        def workspace_read_file(self, site_id: str, plan_id: str, filename: str, *, actor: str) -> WpOpsResult:
            p = work_dir / filename
            if not p.is_file():
                return WpOpsResult(ok=False, message="missing", data={"error_class": "FileMissing"})
            return WpOpsResult(ok=True, message="ok", data={"text": p.read_text(encoding="utf-8")})

        def create_file(self, site_id: str, plan_id: str, filename: str, content: str, *, actor: str) -> WpOpsResult:
            calls.append("create")
            (work_dir / filename).write_text(content, encoding="utf-8")
            return WpOpsResult(ok=True, message="created", data={"status": "validated"})

        def patch_file(
            self, site_id: str, plan_id: str, filename: str, old: str, new: str, *, actor: str
        ) -> WpOpsResult:
            calls.append("patch")
            (work_dir / filename).write_text(new, encoding="utf-8")
            return WpOpsResult(ok=True, message="patched", data={"status": "validated"})

        def refresh(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
            calls.append("refresh")
            return WpOpsResult(ok=True, message="refreshed", data={"status": "validated"})

    ops = _FakeOps()
    exists, raw, data, err = read_workspace_json(
        ops, tmp_path, "azom-se", "wr-0123456789ab", "page_draft.json", actor="oscar"
    )
    assert (exists, raw, data, err) == (False, "", {}, None)

    payload1 = json.dumps({"slug": "test"}) + "\n"
    res1 = upsert_workspace_json(ops, tmp_path, "azom-se", "wr-0123456789ab", "page_draft.json", payload1, actor="oscar")
    assert res1.ok
    res2 = upsert_workspace_json(ops, tmp_path, "azom-se", "wr-0123456789ab", "page_draft.json", payload1, actor="oscar")
    assert res2.ok
    payload2 = json.dumps({"slug": "test-2"}) + "\n"
    res3 = upsert_workspace_json(ops, tmp_path, "azom-se", "wr-0123456789ab", "page_draft.json", payload2, actor="oscar")
    assert res3.ok
    assert calls == ["create", "refresh", "patch"]

    class _FakeEditor:
        def _site(self, site_id: str, actor: str, *, write: bool = False):
            return SimpleNamespace(id=site_id), None

        def need(self, actor: str, perm: Permission, *, site=None, action: str = ""):
            if actor != "oscar":
                return WpOpsResult(ok=False, message="denied", data={"error_class": "AccessDenied"})
            return None

    fake_service = SimpleNamespace(_file_editor=lambda: _FakeEditor())
    _ed, site_ok, denied_none = authorize_editor_site(
        fake_service, "azom-se", "oscar", extra_permission=Permission.WP_OPS_ADMIN
    )
    assert site_ok is not None and denied_none is None
    _ed, site_none, denied_res = authorize_editor_site(
        fake_service, "azom-se", "jonatan", extra_permission=Permission.WP_OPS_ADMIN
    )
    assert site_none is None and denied_res is not None and not denied_res.ok
