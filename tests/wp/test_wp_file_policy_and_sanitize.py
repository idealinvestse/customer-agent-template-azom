"""Unit and leaf-isolation tests for ``wp_ops/file_policy.py`` and ``wp_ops/html_sanitize.py``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ecom_ops.wp_ops import agent_tools, file_changes, file_policy, html_sanitize, preview_overlay

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# file_policy tests
# ---------------------------------------------------------------------------


def test_normalize_css_for_check_strips_comments_and_decodes_escapes() -> None:
    assert "url(" in file_policy.normalize_css_for_check(r"u/*x*/\72 l(https://evil.test)")
    assert "javascript:" in file_policy.normalize_css_for_check(r"\6a avascript:alert(1)")
    assert "@import" in file_policy.normalize_css_for_check(r"@\69 mport 'evil.css';")
    assert "\ufffd" in file_policy.normalize_css_for_check(r"\0 \110000")


@pytest.mark.parametrize(
    ("path", "css", "expected"),
    [
        ("style.css", ".btn { color: #fff; padding: 12px 20px; }", True),
        ("assets/css/theme.css", ".hero { border-radius: 8px; }", True),
        ("checkout/style.css", ".btn { color: red; }", False),
        ("cart/style.css", ".btn { color: red; }", False),
        ("style.css", "body { background: url(https://evil.test/x.png); }", False),
        ("style.css", r"body { background: u\72 l(https://evil.test/x.png); }", False),
        ("style.css", "body { color: red; } <?php echo 1; ?>", False),
        ("style.css", "body { behavior: url(xss.htc); }", False),
        ("style.css", "@import 'https://evil.test/a.css';", False),
        ("functions.php", ".btn { color: red; }", False),
    ],
)
def test_is_safe_pure_css_file_and_risk_for(path: str, css: str, expected: bool) -> None:
    assert file_policy.is_safe_pure_css_file(path, css) is expected
    if path.endswith(".css"):
        assert file_policy.risk_for(path, css) == ("low" if expected else "high")


def test_risk_for_non_css_and_production_deploy_block() -> None:
    assert file_policy.risk_for("templates/hero.php", "<h1>Hello</h1>") == "low"
    assert file_policy.risk_for("woocommerce/checkout/thankyou.php", "<h1>Thanks</h1>") == "high"
    assert file_policy.risk_for("inc/helper.php", "update_payment_gateway();") == "high"

    block = file_policy.production_deploy_block()
    assert block == {
        "executed": False,
        "error_class": "BackupNotVerified",
        "backup_offsite": False,
        "bytes_copied": False,
        "encryption": "unconfigured",
        "restore": "RestoreNotInMvp",
        "live_calls": False,
    }


# ---------------------------------------------------------------------------
# html_sanitize tests
# ---------------------------------------------------------------------------


def test_sanitize_page_draft_html_strips_xss_hardens_links_and_enforces_hosts() -> None:
    dirty = (
        '<section class="hero" onclick="evil()">'
        '<script>alert(1)</script>'
        '<iframe src="https://evil.test"></iframe>'
        '<a href="javascript:alert(1)">Bad</a>'
        '<a href="https://azom.no/guide" target="_blank">Guide</a>'
        '<img src="https://azom.no/img.png" alt="OK">'
        '<img src="https://evil.test/track.png" alt="Blocked">'
        '<img src="https://127.0.0.1/internal.png" alt="PrivateIP">'
        '<p style="color:red;background:u\\72 l(https://evil.test)">Styled</p>'
        '<p style="color:#111;padding:8px">SafeStyle</p>'
        '<button type="submit">Go</button>'
        "</section>"
    )
    cleaned = html_sanitize.sanitize_page_draft_html(
        dirty, allowed_hosts=frozenset({"azom.no"})
    )
    assert "<script" not in cleaned
    assert "<iframe" not in cleaned
    assert "onclick" not in cleaned
    assert "javascript:" not in cleaned
    assert 'href="https://azom.no/guide"' in cleaned
    assert 'rel="noopener noreferrer"' in cleaned
    assert 'src="https://azom.no/img.png"' in cleaned
    assert "evil.test" not in cleaned
    assert "127.0.0.1" not in cleaned
    assert 'style="color:#111;padding:8px"' in cleaned
    assert 'type="button"' in cleaned
    assert 'type="submit"' not in cleaned


def test_sanitize_rich_html_handles_plain_text_and_strips_unsafe_tags() -> None:
    assert html_sanitize.sanitize_rich_html("Rad 1\nRad 2\n\nStycke 2") == (
        "<p>Rad 1<br>Rad 2</p>\n<p>Stycke 2</p>"
    )
    cleaned = html_sanitize.sanitize_rich_html(
        '<p onclick="x"><strong>Hej</strong><script>bad()</script><em>värld</em></p>'
    )
    assert cleaned == "<p><strong>Hej</strong><em>värld</em></p>"
    assert html_sanitize.sanitize_rich_html("<script>only bad</script>") == ""
    assert html_sanitize.sanitize_rich_html("<b></b>plain") == "<b></b>plain"
    assert html_sanitize.sanitize_rich_html("<u>wrapped</u>") == "<p>wrapped</p>"


def test_neutralize_overlay_fragment_strips_scripts_events_and_js_urls() -> None:
    raw = (
        '<div/onclick="alert(1)" data-x="a>b" onmouseover=bad()>'
        '<a href="javascript:evil()">link</a>'
        "<script>boom()</script></script>"
        "</div>"
    )
    cleaned = html_sanitize._neutralize_overlay_fragment(raw)
    assert "<script" not in cleaned.lower()
    assert "onclick" not in cleaned.lower()
    assert "onmouseover" not in cleaned.lower()
    assert "javascript:" not in cleaned.lower()
    assert 'data-x="a>b"' in cleaned


def test_harden_preview_html_strips_all_on_disk_scripts_and_injects_canonical_guard() -> None:
    captured_calls: list[dict[str, str]] = []

    def fake_builder(*, nonce: str, highlight_token: str = "", focus_text: str = "", focus_selector: str = "") -> str:
        captured_calls.append(
            {
                "nonce": nonce,
                "highlight_token": highlight_token,
                "focus_text": focus_text,
                "focus_selector": focus_selector,
            }
        )
        return f'<script nonce="{nonce}" data-azom-guard="1">/* canonical */</script>'

    genuine_html = (
        '<html><body><div id="azom-sandbox-toolbar"></div>'
        '<a href="javascript:alert(1)" onclick="pwn()">Buy</a>'
        '<script data-azom-guard="1">/* spoofed without markers */ alert("spoof");</script>'
        '<script data-azom-guard="1">'
        'var highlightToken = "Köp nu";'
        'var focusText = "Knapp";'
        'var focusSelector = ".single_add_to_cart_button";'
        'var msg = "Blockerad i sandbox-läget"; var t = "#azom-sandbox-toast";'
        "</script>"
        "<script>evilOnDisk();</script>"
        "</body></html>"
    )
    hardened = html_sanitize.harden_preview_html(
        genuine_html,
        nonce="abc123nonce",
        guard_script_builder=fake_builder,
    )
    assert "evilOnDisk" not in hardened
    assert "spoof" not in hardened
    assert "onclick" not in hardened
    assert "javascript:" not in hardened
    assert '<script nonce="abc123nonce" data-azom-guard="1">/* canonical */</script>\n</body>' in hardened
    assert captured_calls == [
        {
            "nonce": "abc123nonce",
            "highlight_token": "Köp nu",
            "focus_text": "Knapp",
            "focus_selector": ".single_add_to_cart_button",
        }
    ]


def test_harden_preview_html_does_not_inject_guard_without_toolbar_or_genuine_script() -> None:
    called = False

    def fake_builder(**_kwargs: str) -> str:
        nonlocal called
        called = True
        return "<script>should-not-run</script>"

    spoof_only = (
        '<html><body><script data-azom-guard="1">alert("spoof")</script><p>Plain</p></body></html>'
    )
    hardened = html_sanitize.harden_preview_html(
        spoof_only,
        nonce="n1",
        guard_script_builder=fake_builder,
    )
    assert called is False
    assert "<script" not in hardened
    assert "<p>Plain</p>" in hardened


def test_preview_csp_header_enforces_opaque_sandbox_and_nonce() -> None:
    with_nonce = html_sanitize.preview_csp_header("tok-xyz")
    assert "sandbox allow-scripts;" in with_nonce
    assert "default-src 'none';" in with_nonce
    assert "script-src 'nonce-tok-xyz';" in with_nonce
    assert "connect-src 'none';" in with_nonce
    assert "form-action 'none';" in with_nonce
    assert "object-src 'none';" in with_nonce
    assert "frame-ancestors 'self'" in with_nonce

    without_nonce = html_sanitize.preview_csp_header("")
    assert "script-src 'none';" in without_nonce


# ---------------------------------------------------------------------------
# Leaf isolation & compatibility re-export checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "skills/ecom_ops/wp_ops/file_policy.py",
        "skills/ecom_ops/wp_ops/html_sanitize.py",
    ],
)
def test_leaf_modules_have_zero_integrations_or_io_imports(rel_path: str) -> None:
    tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    banned_roots = {"os", "subprocess", "socket", "requests", "http", "paramiko"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "integrations" not in alias.name
                assert alias.name != "urllib.request"
                assert alias.name.split(".")[0] not in banned_roots
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "integrations" not in mod
            assert mod != "urllib.request"
            assert mod.split(".")[0] not in banned_roots


def test_compatibility_reexports_point_to_canonical_leaves() -> None:
    assert file_changes._normalize_css_for_check is file_policy.normalize_css_for_check
    assert file_changes.is_safe_pure_css_file is file_policy.is_safe_pure_css_file
    assert file_changes.risk_for is file_policy.risk_for
    assert file_changes.production_deploy_block is file_policy.production_deploy_block

    assert preview_overlay._normalize_css_for_security is file_policy.normalize_css_for_check
    assert preview_overlay._is_safe_url is html_sanitize._is_safe_url
    assert preview_overlay._strip_tag_event_handlers is html_sanitize._strip_tag_event_handlers
    assert preview_overlay._neutralize_overlay_fragment is html_sanitize._neutralize_overlay_fragment
    assert preview_overlay.sanitize_page_draft_html is html_sanitize.sanitize_page_draft_html
    assert preview_overlay._sanitize_rich_html is html_sanitize.sanitize_rich_html

    assert agent_tools._UNSAFE_PRODUCT_HTML_RE is html_sanitize.UNSAFE_PRODUCT_HTML_RE
    assert agent_tools.sanitize_page_draft_html is html_sanitize.sanitize_page_draft_html
    assert agent_tools.is_safe_pure_css_file is file_policy.is_safe_pure_css_file
