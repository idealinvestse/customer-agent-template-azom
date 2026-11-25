"""Tests for in-memory HTML preview overlay and child CSS injection."""

from __future__ import annotations

import pytest

from ecom_ops.wp_ops.preview_overlay import (
    OverlayHunk,
    OverlayResult,
    apply_overlay,
    apply_product_edits_overlay,
    distinctive_token,
    strip_css_header,
)


def test_static_footer_address_replace_and_highlight_token() -> None:
    live_html = (
        "<html><head><title>Azom</title></head>"
        "<body><footer><p>Gatan 1, 111 22 Stockholm</p></footer></body></html>"
    )
    before = "<footer>\n<p>Gatan 1, 111 22 Stockholm</p>\n</footer>\n"
    after = "<footer>\n<p>testadress123, 111 22 Stockholm</p>\n</footer>\n"

    result = apply_overlay(live_html, [("footer.php", before, after)])

    assert isinstance(result, OverlayResult)
    assert "testadress123" in result.html
    assert "Gatan 1" not in result.html
    assert result.highlight_token == "testadress123"
    assert len(result.hunks) == 1
    assert result.hunks[0].kind == "applied"
    assert result.hunks[0].path == "footer.php"
    assert result.injected_css_bytes == 0


def test_whitespace_and_indentation_differences_still_match() -> None:
    live_html = "<html><body><div>Hello</div><div>Hello</div></body></html>"
    before = "  <div>\n    Hello  </div>\n"
    after = "  <div>\n    Updated greeting\n  </div>\n"

    result = apply_overlay(live_html, [("templates/banner.HTML", before, after)])

    assert result.hunks[0].kind == "applied"
    assert "Updated greeting" in result.html
    # Only the first match is replaced.
    assert result.html.endswith("<div>Hello</div></body></html>")


def test_php_hunk_is_skipped_and_html_unchanged() -> None:
    live_html = "<html><body><p>Pris: 199 kr</p></body></html>"
    before = "<p>Pris: <?php echo $price; ?> kr</p>\n"
    after = "<p>Pris: <?php echo esc_html($price); ?> kr</p>\n"

    result = apply_overlay(live_html, [("parts/price.php", before, after)])

    assert result.html == live_html
    assert len(result.hunks) == 1
    assert result.hunks[0].kind == "php_skipped"
    assert result.highlight_token is None


def test_insert_anchored_and_unanchored() -> None:
    live_html = "<html><body><header><h1>Butik</h1></header><main><p>Hej</p></main></body></html>"

    before_anchored = "<header><h1>Butik</h1></header>\n<main><p>Hej</p></main>\n"
    after_anchored = (
        "<header><h1>Butik</h1></header>\n"
        "<div class=\"notice\">Fri frakt idag</div>\n"
        "<main><p>Hej</p></main>\n"
    )
    anchored = apply_overlay(live_html, [("header.php", before_anchored, after_anchored)])

    assert anchored.hunks[0].kind == "applied"
    assert '<header><h1>Butik</h1></header>\n<div class="notice">Fri frakt idag</div>' in anchored.html
    assert anchored.highlight_token == "frakt"

    before_unanchored = "<?php get_header(); ?>\n<main><p>Hej</p></main>\n"
    after_unanchored = "<?php get_header(); ?>\n<div class=\"notice\">Toppnotis</div>\n<main><p>Hej</p></main>\n"
    unanchored = apply_overlay(live_html, [("header.php", before_unanchored, after_unanchored)])

    assert unanchored.html == live_html
    assert unanchored.hunks[0].kind == "insert_unanchored"
    assert unanchored.highlight_token is None


def test_unmatched_hunk_leaves_html_unchanged() -> None:
    live_html = "<html><body><p>Aktuell text</p></body></html>"
    before = "<section><p>Finns inte i live html</p></section>\n"
    after = "<section><p>Ny text</p></section>\n"

    result = apply_overlay(live_html, [("templates/home.html", before, after)])

    assert result.html == live_html
    assert len(result.hunks) == 1
    assert result.hunks[0].kind == "unmatched"
    assert result.highlight_token is None


def test_child_css_injection_and_header_stripping() -> None:
    css = (
        "/*\n"
        "Theme Name: Azom Child\n"
        "Version: 1.0\n"
        "*/\n"
        ".site-footer { color: #111; }\n"
    )
    assert strip_css_header(css) == ".site-footer { color: #111; }"

    html_with_head = "<html><HEAD><title>Test</title></HEAD><body><p>X</p></body></html>"
    res_head = apply_overlay(html_with_head, [], child_css=css)
    expected_tag = '<style data-azom-preview="child-css">\n.site-footer { color: #111; }\n</style>'
    assert expected_tag + "\n</HEAD>" in res_head.html
    assert "Theme Name:" not in res_head.html
    assert res_head.injected_css_bytes == len(expected_tag.encode("utf-8"))
    assert res_head.injected_css_bytes > 0

    # Fallback 1: no </head>, inject right after <body...>
    html_with_body = '<html><body class="home"><p>X</p></body></html>'
    res_body = apply_overlay(html_with_body, [], child_css=css)
    assert f'<body class="home">\n{expected_tag}<p>X</p>' in res_body.html
    assert res_body.injected_css_bytes > 0

    # Fallback 2: neither </head> nor <body>, inject at start of HTML
    html_fragment = "<main><p>Fragment</p></main>"
    res_frag = apply_overlay(html_fragment, [], child_css=css)
    assert res_frag.html.startswith(expected_tag + "\n<main>")
    assert res_frag.injected_css_bytes > 0

    # Header-only CSS is not injected
    header_only = "/*\nTheme Name: Empty Child\n*/\n   "
    res_empty = apply_overlay(html_with_head, [], child_css=header_only)
    assert res_empty.html == html_with_head
    assert res_empty.injected_css_bytes == 0

    # </style in CSS cannot break out of injected style tag
    sneaky_css = ".x { content: '</STYLE><script>alert(1)</script>'; }"
    res_sneaky = apply_overlay(html_with_head, [], child_css=sneaky_css)
    assert "</STYLE><script>" not in res_sneaky.html
    assert r"<\/style><script>" in res_sneaky.html


def test_oversize_html_and_hunk_cap() -> None:
    oversize_input = "a" * 3_000_001
    with pytest.raises(ValueError, match="HtmlTooLarge"):
        apply_overlay(oversize_input, [])

    # Multi-byte UTF-8 > 3_000_000 bytes even when len(str) <= 3_000_000
    oversize_utf8 = "ö" * 1_600_000
    with pytest.raises(ValueError, match="HtmlTooLarge"):
        apply_overlay(oversize_utf8, [])

    # Output pushed over 3_000_000 by CSS injection
    near_limit = "<html><head></head><body>" + ("x" * 2_999_960) + "</body></html>"
    with pytest.raises(ValueError, match="HtmlTooLarge"):
        apply_overlay(near_limit, [], child_css=".a { color: red; }")

    # > 50 hunks caps cleanly at 50
    before_lines = [f"<p>Line {i}</p>" for i in range(65)]
    after_lines = [f"<p>Changed {i}</p>" for i in range(65)]
    # Interleave unchanged lines so SequenceMatcher produces > 50 separate hunks
    before_spaced: list[str] = []
    after_spaced: list[str] = []
    for b_line, a_line in zip(before_lines, after_lines, strict=True):
        before_spaced.extend([b_line, "<hr>"])
        after_spaced.extend([a_line, "<hr>"])

    live_html = "<html><body>" + "".join(before_spaced) + "</body></html>"
    result = apply_overlay(
        live_html,
        [("templates/list.html", "\n".join(before_spaced), "\n".join(after_spaced))],
    )

    assert len(result.hunks) == 50
    assert all(isinstance(h, OverlayHunk) for h in result.hunks)
    assert "Changed 49" in result.html
    assert "Changed 50" not in result.html


def test_additional_edge_cases() -> None:
    # Non-php/html files ignored; delete hunk removes matched text
    live_html = "<html><body><p>Keep</p><p>RemoveMe</p></body></html>"
    result = apply_overlay(
        live_html,
        [
            ("assets/app.js", "console.log(1);", "console.log(2);"),
            ("style.css", "body{}", "body{margin:0}"),
            ("parts/card.php", "<p>Keep</p>\n<p>RemoveMe</p>\n", "<p>Keep</p>\n"),
        ],
    )
    assert len(result.hunks) == 1
    assert result.hunks[0].kind == "applied"
    assert result.html == "<html><body><p>Keep</p></body></html>"
    assert result.highlight_token is None

    # Excerpts are single-line and <= 160 chars
    long_old = "<p>\n  " + ("alpha " * 40) + "\n</p>"
    long_new = "<p>\n  " + ("beta " * 40) + "\n</p>"
    res_long = apply_overlay("<html><body></body></html>", [("x.html", long_old, long_new)])
    assert len(res_long.hunks[0].old_excerpt) <= 160
    assert len(res_long.hunks[0].new_excerpt) <= 160
    assert "\n" not in res_long.hunks[0].old_excerpt
    assert "\n" not in res_long.hunks[0].new_excerpt

    # distinctive_token handles Swedish characters, fallback, and empty text
    assert distinctive_token("<p>Gammal</p>", "<p>Ny &Aring;terförsäljning här</p>") == "Återförsäljning"
    assert distinctive_token("<p>Old</p>", "<p>AB 12</p>") == "AB 12"
    assert distinctive_token("<p>Old</p>", "<p>   </p>") is None


def test_wp_i18n_literal_tags_match_rendered_html() -> None:
    live_html = (
        '<footer><div class="text_normal footer">'
        '<span class="text-span-contact-footer">0605, Oslo<br>Tångringsgatan 6,<br>784 30 Borlänge</span>'
        "</div></footer>"
    )
    before = (
        '  <div class="text_normal footer"><span class="text-span-contact-footer">'
        "<?php _e('0605, Oslo', 'azom-no-woocommerce') ?><br>"
        "<?php _e('Tångringsgatan 6,', 'azom-no-woocommerce') ?><br>"
        "<?php _e('784 30 Borlänge', 'azom-no-woocommerce') ?></span></div>\n"
    )
    after = (
        '  <div class="text_normal footer"><span class="text-span-contact-footer">'
        "testadress123 i Falun</span></div>\n"
    )
    result = apply_overlay(live_html, [("page-index.php", before, after)])
    assert result.hunks[0].kind == "applied"
    assert "testadress123 i Falun" in result.html
    assert result.highlight_token == "testadress123"


def test_footer_br_variants_short_echo_and_multiline_address_overlay() -> None:
    live_html = (
        '<footer class="f01"><div class="f01_title">OM\xa0AZOM</div>'
        '<div class="text_normal footer"><span class="text-span-contact-footer">'
        "0605, Oslo<br>Tångringsgatan 6,<br>784 30 Borlänge</span></div></footer>"
    )
    before = (
        '  <div class="text_normal footer"><span class="text-span-contact-footer">'
        "<?= __('0605, Oslo', 'azom-no-woocommerce') ?><br/>"
        "<?php _e('Tångringsgatan 6,') ?><br />"
        "<?php _ex('784 30 Borlänge', 'address', 'azom-no-woocommerce') ?></span></div>\n"
    )
    after = (
        '  <div class="text_normal footer"><span class="text-span-contact-footer">'
        "Testsidan123,<br>falun</span></div>\n"
    )
    result = apply_overlay(live_html, [("page-index.php", before, after)])
    assert result.hunks[0].kind == "applied"
    assert "Testsidan123,<br>falun" in result.html
    assert "0605, Oslo" not in result.html
    assert result.highlight_token == "Testsidan123"


def test_nbsp_entity_matches_unicode_nbsp_in_live_html() -> None:
    live_html = '<footer><span class="title">OM\xa0AZOM</span></footer>'
    before = '<span class="title">OM&nbsp;AZOM</span>\n'
    after = '<span class="title">OM\xa0AZOM NORGE</span>\n'
    result = apply_overlay(live_html, [("footer.php", before, after)])
    assert result.hunks[0].kind == "applied"
    assert "OM\xa0AZOM NORGE" in result.html
    assert result.highlight_token == "NORGE"


def test_apply_product_edits_overlay_all_fields_and_nested_containers() -> None:
    live_html = (
        "<html><head><title>CarPlay Pro - Azom</title>"
        '<meta property="og:title" content="CarPlay Pro"></head>'
        "<body>"
        '<main class="product">'
        '<h1 class="product_title entry-title">CarPlay Pro</h1>'
        '<p class="price"><span class="woocommerce-Price-amount amount"><bdi>3 990 kr</bdi></span></p>'
        '<div class="woocommerce-product-details__short-description"><div class="inner"><p>Gammal korttext</p></div></div>'
        '<button type="submit" class="single_add_to_cart_button button alt">Legg i handlekurv</button>'
        '<div id="tab-description" class="woocommerce-Tabs-panel--description">'
        "<div><h2>Beskrivelse</h2><p>Gammal beskrivning</p></div>"
        "</div>"
        '<div class="delivery-note">Rask   levering 2-4 dager</div>'
        "</main></body></html>"
    )
    edits = {
        "schema_version": 1,
        "product_path": "/produkt/carplay-pro/",
        "product_id": 42,
        "title": "Testprodukt 123",
        "regular_price": "3 990 kr",
        "sale_price": "2 990 kr",
        "short_description": "<p>Ny <strong>snabb</strong> sammanfattning</p>",
        "description": "Rad ett\n\nRad två",
        "button_text": "Köp direkt",
        "badge_text": "Kampanj -25%",
        "replacements": [{"old_text": "Rask levering 2-4 dager", "new_text": "Expressleverans 1-2 dagar"}],
    }

    res = apply_product_edits_overlay(live_html, edits)

    assert '<h1 class="product_title entry-title">Testprodukt 123</h1>' in res.html
    assert "<del " in res.html and "3 990 kr" in res.html
    assert "<ins>" in res.html and "2 990 kr" in res.html
    assert "<p>Ny <strong>snabb</strong> sammanfattning</p>" in res.html
    assert "<p>Rad ett</p>\n<p>Rad två</p>" in res.html
    assert ">Köp direkt</button>" in res.html
    assert 'data-azom-sandbox-badge="1"' in res.html and "Kampanj -25%" in res.html
    assert "Expressleverans 1-2 dagar" in res.html
    # Meta tag in <head> stays untouched by visible body replacements
    assert '<meta property="og:title" content="CarPlay Pro">' in res.html
    assert res.highlight_token == "Testprodukt"
    assert all(h.kind == "applied" for h in res.hunks)
    assert len(res.hunks) == 7


def test_apply_product_edits_overlay_xss_sanitization_and_unmatched() -> None:
    live_html = (
        "<html><body>"
        '<span class="onsale">Salg</span>'
        "<h1>Original</h1>"
        '<span class="woocommerce-Price-amount"><bdi>500 kr</bdi></span>'
        '<div class="woocommerce-product-details__short-description"><p>Old</p></div>'
        '<input type="submit" class="single_add_to_cart_button" value="Buy">'
        "<p>Garanti 1 år</p>"
        "</body></html>"
    )
    edits = {
        "title": '<script>alert("xss")</script>Ny Titel',
        "price": "<b>499 kr</b>",
        "short_description": '<p onclick="alert(1)">Trygg <em>text</em></p><script>evil()</script><img src=x onerror=alert(1)>',
        "description": "Ingen beskrivningsbox finns på denna sida",
        "button_text": 'Köp "nu" <script>',
        "badge_text": "REA <img onerror=1>",
        "replacements": [
            {"old_text": "Garanti 1 år", "new_text": "<script>bad()</script>Garanti 2 år"},
            {"old_text": "Finns inte alls", "new_text": "X"},
        ],
    }

    res = apply_product_edits_overlay(live_html, edits)

    assert "<script>" not in res.html
    assert "<img" not in res.html
    assert "evil()" not in res.html
    assert "onclick=" not in res.html
    assert "onerror=alert" not in res.html
    assert "&lt;script&gt;" in res.html
    assert '<span class="woocommerce-Price-amount"><bdi>&lt;b&gt;499 kr&lt;/b&gt;</bdi></span>' in res.html
    assert "<p>Trygg <em>text</em></p>" in res.html
    assert 'value="Köp &quot;nu&quot; &lt;script&gt;"' in res.html
    assert '<span class="onsale">REA &lt;img onerror=1&gt;</span>' in res.html
    assert "Garanti 2 år" in res.html

    kinds_by_path = {h.path: h.kind for h in res.hunks}
    assert kinds_by_path["product_edits.json#title"] == "applied"
    assert kinds_by_path["product_edits.json#price"] == "applied"
    assert kinds_by_path["product_edits.json#short_description"] == "applied"
    assert kinds_by_path["product_edits.json#description"] == "unmatched"
    assert kinds_by_path["product_edits.json#button_text"] == "applied"
    assert kinds_by_path["product_edits.json#badge_text"] == "applied"
    assert kinds_by_path["product_edits.json#replacements[0]"] == "applied"
    assert kinds_by_path["product_edits.json#replacements[1]"] == "unmatched"


def test_apply_overlay_combines_child_css_and_product_edits() -> None:
    live_html = "<html><head></head><body><h1>Old Product</h1></body></html>"
    res = apply_overlay(
        live_html,
        [],
        child_css="h1 { color: #16a34a; }",
        product_edits={"title": "Updated Product"},
    )
    assert "Updated Product" in res.html
    assert res.injected_css_bytes > 0
    assert res.highlight_token == "Updated"
    assert len(res.hunks) == 1
    assert res.hunks[0].path == "product_edits.json#title"
