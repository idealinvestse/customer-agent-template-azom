"""On-page content, media SEO, internal link graph, cannibalization, and JSON-LD builders."""

from __future__ import annotations

import html as html_lib
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from ecom_ops.seo.models import SeoIssue, normalize_market
from ecom_ops.seo.policy import (
    ALT_TEXT_MAX,
    ALT_TEXT_MIN,
    IMAGE_OVERSIZE_HIGH_BYTES,
    IMAGE_OVERSIZE_WARN_BYTES,
    META_DESC_MAX,
    META_DESC_MIN,
    SEO_TITLE_MAX,
    SEO_TITLE_MIN,
    THIN_CONTENT_PAGE_WORDS,
    THIN_CONTENT_PRODUCT_WORDS,
    is_generic_alt,
    is_generic_anchor,
    is_generic_filename,
    score_to_grade,
)

_NORDIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "och",
        "og",
        "för",
        "for",
        "med",
        "til",
        "till",
        "av",
        "af",
        "på",
        "i",
        "en",
        "ett",
        "et",
        "den",
        "det",
        "de",
        "din",
        "ditt",
        "du",
        "vi",
        "som",
        "har",
        "kan",
        "the",
        "and",
        "with",
        "from",
        "your",
        "azom",
        "pro",
        "bäst",
        "bästa",
        "beste",
        "bedste",
        "guide",
        "köp",
        "kjøp",
        "køb",
    }
)

_SLUG_BAD_RE = re.compile(r"(?:[A-Z\s?&=]|^(?:product|page|post)-\d+$|^\d+$)")


def _norm_path(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "/"
    parsed = urlparse(raw)
    path = parsed.path or "/"
    return ("/" + path.lstrip("/")).rstrip("/") or "/"


def _strip_html(text: str) -> str:
    raw = str(text or "")
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(html_lib.unescape(no_tags).split()).strip()


def _significant_tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-z0-9åäöøæ]+", str(text or "").lower())
    return {t for t in raw_tokens if len(t) >= 3 and t not in _NORDIC_STOPWORDS}


def _suggest_alt_from_media(item: dict[str, Any]) -> str:
    title = " ".join(str(item.get("title") or "").strip().split())
    filename = str(item.get("filename") or "").rsplit(".", 1)[0]
    cleaned_file = " ".join(re.sub(r"[-_]+", " ", filename).split()).strip()
    if title and not is_generic_alt(title):
        return f"{title} – Azom produktbild"[:ALT_TEXT_MAX]
    if cleaned_file and not is_generic_filename(filename):
        return f"{cleaned_file.capitalize()} – Azom"[:ALT_TEXT_MAX]
    return "Azom trådlös CarPlay-produkt monterad i bil"


# --------------------------------------------------------------------------- #
# On-Page Product / Page / Post / Category SEO Audit
# --------------------------------------------------------------------------- #


def audit_entities_on_page(
    entities: list[dict[str, Any]],
    *,
    categories: list[dict[str, Any]] | None = None,
    seo_plugin: str = "yoast",
) -> dict[str, Any]:
    """Audit Woo products, WP pages/posts, and product categories for SEO completeness."""
    issues: list[dict[str, Any]] = []
    title_to_urls: dict[str, list[str]] = defaultdict(list)
    desc_to_urls: dict[str, list[str]] = defaultdict(list)

    items_out: list[dict[str, Any]] = []
    has_title = 0
    has_desc = 0
    has_kw = 0
    thin_count = 0
    non_published_count = 0

    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        status = str(ent.get("status") or "publish").strip().lower()
        if status != "publish":
            non_published_count += 1
            continue
        obj_type = str(ent.get("object_type") or "page").strip().lower()
        obj_id = str(ent.get("object_id") or "")
        title = str(ent.get("title") or "").strip()
        slug = str(ent.get("slug") or "").strip()
        url = str(ent.get("url") or f"/{slug}/").strip()
        word_count = int(ent.get("word_count") or 0)
        meta_title = str(ent.get("meta_title") or "").strip()
        meta_desc = str(ent.get("meta_description") or "").strip()
        focus_kw = str(ent.get("focus_keyphrase") or "").strip()
        canonical = str(ent.get("canonical") or "").strip()
        robots = str(ent.get("robots") or "").strip().lower()
        schema_type = str(ent.get("schema_type") or "").strip()
        imgs_missing_alt = int(ent.get("images_missing_alt") or 0)

        ent_issues: list[str] = []

        if meta_title:
            has_title += 1
            title_to_urls[meta_title.lower()].append(url)
            if len(meta_title) < SEO_TITLE_MIN:
                ent_issues.append("meta_title_too_short")
                issues.append(
                    SeoIssue(
                        code="meta_title_too_short",
                        severity="medium",
                        category="on_page",
                        title=f"För kort SEO-titel på {obj_type} #{obj_id}",
                        detail=f"'{meta_title}' ({len(meta_title)} tecken; mål {SEO_TITLE_MIN}–{SEO_TITLE_MAX}).",
                        target_type=obj_type,
                        target_id=obj_id,
                        url=url,
                        suggestion="Utöka SEO-titeln med huvudsökord och köpargument (30–60 tecken).",
                        fix_type="seo_meta",
                        proposed_fields={"meta_title": f"{title} | Trådlös CarPlay – Azom"[:60]},
                    ).to_dict()
                )
            elif len(meta_title) > SEO_TITLE_MAX:
                ent_issues.append("meta_title_too_long")
                issues.append(
                    SeoIssue(
                        code="meta_title_too_long",
                        severity="medium",
                        category="on_page",
                        title=f"För lång SEO-titel på {obj_type} #{obj_id}",
                        detail=f"SEO-titeln är {len(meta_title)} tecken (max rekommenderat {SEO_TITLE_MAX}).",
                        target_type=obj_type,
                        target_id=obj_id,
                        url=url,
                        suggestion="Korta ned SEO-titeln till <= 60 tecken för att undvika avklippning.",
                        fix_type="seo_meta",
                        proposed_fields={"meta_title": f"{title[:46].rstrip()} | Azom"},
                    ).to_dict()
                )
        else:
            ent_issues.append("missing_meta_title")
            issues.append(
                SeoIssue(
                    code="missing_meta_title",
                    severity="high",
                    category="on_page",
                    title=f"Saknar SEO-titel på {obj_type} '{title or obj_id}'",
                    detail=f"Ingen anpassad SEO-titel ({seo_plugin}) satt för {url}.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion=f"Sätt en unik SEO-titel på {SEO_TITLE_MIN}–{SEO_TITLE_MAX} tecken.",
                    fix_type="seo_meta",
                    proposed_fields={"meta_title": f"{title[:46].rstrip()} | Azom"},
                ).to_dict()
            )

        if meta_desc:
            has_desc += 1
            desc_to_urls[meta_desc.lower()].append(url)
            if len(meta_desc) < META_DESC_MIN or len(meta_desc) > META_DESC_MAX:
                ent_issues.append("meta_desc_length")
                issues.append(
                    SeoIssue(
                        code="meta_desc_length",
                        severity="low",
                        category="on_page",
                        title=f"Meta description utanför optimalt spann ({obj_type} #{obj_id})",
                        detail=f"Längd {len(meta_desc)} tecken (optimalt {META_DESC_MIN}–{META_DESC_MAX}).",
                        target_type=obj_type,
                        target_id=obj_id,
                        url=url,
                        suggestion=f"Skriv om meta description till {META_DESC_MIN}–{META_DESC_MAX} tecken med tydlig CTA.",
                        fix_type="seo_meta",
                    ).to_dict()
                )
        else:
            ent_issues.append("missing_meta_description")
            suggested_desc = (
                f"Upptäck {title or 'vår lösning'} hos Azom. Enkel plug & play-montering, "
                f"2 års garanti och snabb nordisk leverans. Beställ online idag!"
            )[:META_DESC_MAX]
            issues.append(
                SeoIssue(
                    code="missing_meta_description",
                    severity="high",
                    category="on_page",
                    title=f"Saknar meta description på {obj_type} '{title or obj_id}'",
                    detail=f"{url} saknar meta description.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion=f"Lägg till säljande meta description ({META_DESC_MIN}–{META_DESC_MAX} tecken).",
                    fix_type="seo_meta",
                    proposed_fields={"meta_description": suggested_desc},
                ).to_dict()
            )

        if focus_kw:
            has_kw += 1
        else:
            ent_issues.append("missing_focus_keyphrase")
            issues.append(
                SeoIssue(
                    code="missing_focus_keyphrase",
                    severity="medium",
                    category="on_page",
                    title=f"Saknar fokusnyckelord på {obj_type} '{title or obj_id}'",
                    detail=f"Inget focus_keyphrase konfigurerat i {seo_plugin} för {url}.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion="Tilldela ett unikt primärt fokusnyckelord kopplat till sidans sökintention.",
                    fix_type="seo_meta",
                ).to_dict()
            )

        # Thin content check
        min_words = THIN_CONTENT_PRODUCT_WORDS if obj_type == "product" else THIN_CONTENT_PAGE_WORDS
        if status == "publish" and word_count < min_words:
            thin_count += 1
            ent_issues.append("thin_content")
            issues.append(
                SeoIssue(
                    code="thin_content",
                    severity="high" if obj_type == "product" else "medium",
                    category="on_page",
                    title=f"Tunt innehåll på {obj_type} '{title or obj_id}' ({word_count} ord)",
                    detail=f"{url} har endast {word_count} ord (rekommenderat minst {min_words} ord).",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion=(
                        "Utöka med kompatibilitetslista, monteringsguide, tekniska specifikationer och FAQ."
                        if obj_type == "product"
                        else "Utöka sidan med tydliga H2-sektioner, konkreta svar och internlänkar."
                    ),
                    fix_type="product_seo" if obj_type == "product" else "page_seo",
                ).to_dict()
            )

        # Accidental noindex on published money pages
        if status == "publish" and "noindex" in robots:
            ent_issues.append("published_noindex")
            issues.append(
                SeoIssue(
                    code="published_noindex",
                    severity="critical",
                    category="technical",
                    title=f"Publicerad {obj_type} '{title or obj_id}' har noindex!",
                    detail=f"{url} har robots='{robots}' vilket döljer sidan i Google.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion="Ändra robots till 'index, follow' om sidan ska generera organisk trafik.",
                    fix_type="seo_meta",
                    proposed_fields={"robots": "index, follow"},
                ).to_dict()
            )

        # Slug check
        if slug and (_SLUG_BAD_RE.search(slug) or len(slug) > 75):
            ent_issues.append("unfriendly_slug")
            issues.append(
                SeoIssue(
                    code="unfriendly_slug",
                    severity="low",
                    category="on_page",
                    title=f"Ooptimerad URL-slug '{slug}'",
                    detail=f"Sluggen på {obj_type} #{obj_id} är numerisk, innehåller otillåtna tecken eller är > 75 tecken.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion="Använd en kort, beskrivande lowercase-slug med bindestreck (kom ihåg 301-redirect vid ändring).",
                ).to_dict()
            )

        if obj_type == "product" and schema_type and schema_type.lower() != "product":
            ent_issues.append("wrong_product_schema")
            issues.append(
                SeoIssue(
                    code="wrong_product_schema",
                    severity="medium",
                    category="schema",
                    title=f"Produktsida #{obj_id} har fel schema_type ('{schema_type}')",
                    detail=f"Förväntade 'Product' JSON-LD på {url}.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion="Sätt schema_type='Product' med offers, price, availability och sku.",
                    fix_type="schema_jsonld",
                ).to_dict()
            )

        if imgs_missing_alt > 0:
            ent_issues.append("content_images_missing_alt")
            issues.append(
                SeoIssue(
                    code="content_images_missing_alt",
                    severity="medium",
                    category="media",
                    title=f"{imgs_missing_alt} bild(er) utan alt-text i {obj_type} '{title or obj_id}'",
                    detail=f"Innehållet på {url} innehåller bilder utan alt-text.",
                    target_type=obj_type,
                    target_id=obj_id,
                    url=url,
                    suggestion="Komplettera alt-texter via Mediabiblioteket (media_alt-fixplan).",
                    fix_type="media_alt",
                ).to_dict()
            )

        items_out.append(
            {
                "object_type": obj_type,
                "object_id": obj_id,
                "title": title,
                "slug": slug,
                "url": url,
                "word_count": word_count,
                "meta_title": meta_title,
                "meta_description": meta_desc,
                "focus_keyphrase": focus_kw,
                "canonical": canonical,
                "robots": robots,
                "issue_codes": ent_issues,
            }
        )

    # Duplicate meta titles & descriptions
    dup_titles = {t: urls for t, urls in title_to_urls.items() if len(urls) > 1}
    for dup_t, urls in dup_titles.items():
        issues.append(
            SeoIssue(
                code="duplicate_meta_title",
                severity="high",
                category="on_page",
                title=f"Duplicerad SEO-titel på {len(urls)} sidor",
                detail=f"Titeln '{dup_t[:70]}' delas av: {', '.join(urls[:4])}",
                url=urls[0],
                suggestion="Gör varje sidas SEO-titel unik efter dess specifika sökintention och produktvariant.",
                fix_type="seo_meta",
            ).to_dict()
        )

    dup_descs = {d: urls for d, urls in desc_to_urls.items() if len(urls) > 1}
    for _dup_d, urls in dup_descs.items():
        issues.append(
            SeoIssue(
                code="duplicate_meta_description",
                severity="medium",
                category="on_page",
                title=f"Duplicerad meta description på {len(urls)} sidor",
                detail=f"Meta description delas av: {', '.join(urls[:4])}",
                url=urls[0],
                suggestion="Skriv unika meta descriptions som lyfter varje sidas unika värde.",
                fix_type="seo_meta",
            ).to_dict()
        )

    # Category descriptions audit
    cats_audited = 0
    for cat in categories or []:
        if not isinstance(cat, dict):
            continue
        cats_audited += 1
        cname = str(cat.get("name") or cat.get("slug") or "")
        cdesc = str(cat.get("description") or "").strip()
        cslug = str(cat.get("slug") or "")
        cid = str(cat.get("id") or "")
        if len(cdesc.split()) < 30:
            issues.append(
                SeoIssue(
                    code="category_thin_description",
                    severity="medium",
                    category="on_page",
                    title=f"Kategorisida '{cname}' saknar beskrivande SEO-text",
                    detail=f"Kategorin '{cname}' har bara {len(cdesc.split())} ord i beskrivningen.",
                    target_type="category",
                    target_id=cid,
                    url=f"/kategori/{cslug}/" if cslug else "",
                    suggestion="Lägg till en kort introtext + köpguide/FAQ under produktlistningen (150–300 ord).",
                    fix_type="seo_meta",
                ).to_dict()
            )

    total = len(items_out)
    return {
        "seo_plugin": seo_plugin,
        "entities_audited": total,
        "non_published_count": non_published_count,
        "categories_audited": cats_audited,
        "meta_title_coverage_pct": round((has_title / total) * 100.0, 1) if total else 0.0,
        "meta_desc_coverage_pct": round((has_desc / total) * 100.0, 1) if total else 0.0,
        "focus_kw_coverage_pct": round((has_kw / total) * 100.0, 1) if total else 0.0,
        "missing_meta_description": max(0, total - has_desc),
        "missing_focus_keyphrase": max(0, total - has_kw),
        "thin_content_count": thin_count,
        "duplicate_titles_count": len(dup_titles),
        "duplicate_descriptions_count": len(dup_descs),
        "items": items_out,
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# Media Library SEO Audit
# --------------------------------------------------------------------------- #


def audit_media_seo(media_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit WP Media Library items for missing/generic alt, filenames, WebP, size, and CLS."""
    issues: list[dict[str, Any]] = []
    alt_seen: dict[str, list[int]] = defaultdict(list)
    items_out: list[dict[str, Any]] = []

    missing_alt = 0
    generic_alt = 0
    generic_fn = 0
    oversized = 0
    non_webp = 0
    missing_dims = 0
    non_image_skipped = 0

    for row in media_items or []:
        if not isinstance(row, dict):
            continue
        mid = int(row.get("media_id") or row.get("id") or 0)
        filename = str(row.get("filename") or "").strip()
        url = str(row.get("url") or row.get("source_url") or "").strip()
        if not filename and url:
            filename = urlparse(url).path.rsplit("/", 1)[-1]
        mime = str(row.get("mime_type") or "").strip().lower()
        if not mime.startswith("image/"):
            non_image_skipped += 1
            continue
        is_svg = mime == "image/svg+xml" or filename.lower().endswith(".svg")
        alt = str(row.get("alt_text") or "").strip()
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
        filesize = int(row.get("filesize") or 0)
        attached = row.get("attached_post_id")

        item_flags: list[str] = []
        suggested_alt = _suggest_alt_from_media(row)

        if not alt:
            missing_alt += 1
            item_flags.append("missing_alt")
            issues.append(
                SeoIssue(
                    code="media_missing_alt",
                    severity="high" if attached else "medium",
                    category="media",
                    title=f"Bild #{mid} ({filename or 'okänd'}) saknar alt-text",
                    detail=f"Mediabiblioteksbild #{mid} saknar alt-attribut.",
                    target_type="media",
                    target_id=str(mid),
                    url=url,
                    suggestion=f"Föreslagen alt-text: '{suggested_alt}'",
                    fix_type="media_alt",
                    proposed_fields={"alt_text": suggested_alt},
                ).to_dict()
            )
        else:
            alt_seen[alt.lower()].append(mid)
            if is_generic_alt(alt):
                generic_alt += 1
                item_flags.append("generic_alt")
                issues.append(
                    SeoIssue(
                        code="media_generic_alt",
                        severity="medium",
                        category="media",
                        title=f"Generisk alt-text på bild #{mid}",
                        detail=f"Alt-texten '{alt}' beskriver inte motivet.",
                        target_type="media",
                        target_id=str(mid),
                        url=url,
                        suggestion=f"Byt till beskrivande alt-text, t.ex. '{suggested_alt}'.",
                        fix_type="media_alt",
                        proposed_fields={"alt_text": suggested_alt},
                    ).to_dict()
                )
            elif len(alt) < ALT_TEXT_MIN or len(alt) > ALT_TEXT_MAX:
                item_flags.append("alt_length")
                issues.append(
                    SeoIssue(
                        code="media_alt_length",
                        severity="low",
                        category="media",
                        title=f"Alt-text utanför {ALT_TEXT_MIN}–{ALT_TEXT_MAX} tecken på bild #{mid}",
                        detail=f"Alt-texten är {len(alt)} tecken.",
                        target_type="media",
                        target_id=str(mid),
                        url=url,
                        suggestion=f"Justera alt-texten till {ALT_TEXT_MIN}–{ALT_TEXT_MAX} tecken.",
                        fix_type="media_alt",
                        proposed_fields={"alt_text": alt[:ALT_TEXT_MAX] if len(alt) > ALT_TEXT_MAX else suggested_alt},
                    ).to_dict()
                )

        if filename and is_generic_filename(filename):
            generic_fn += 1
            item_flags.append("generic_filename")
            issues.append(
                SeoIssue(
                    code="media_generic_filename",
                    severity="low",
                    category="media",
                    title=f"Generiskt filnamn '{filename}' (bild #{mid})",
                    detail="Kamera-/skärmdumpsnamn ger inga bildsökssignaler.",
                    target_type="media",
                    target_id=str(mid),
                    url=url,
                    suggestion="Använd beskrivande filnamn med bindestreck vid uppladdning (t.ex. azom-carplay-skarm-pro.webp).",
                ).to_dict()
            )

        if mime in {"image/jpeg", "image/jpg", "image/png"}:
            non_webp += 1
            item_flags.append("non_webp")
            if filesize >= IMAGE_OVERSIZE_WARN_BYTES:
                issues.append(
                    SeoIssue(
                        code="media_non_webp_large",
                        severity="medium",
                        category="media",
                        title=f"Tung {mime.split('/')[-1].upper()}-bild #{mid} ({round(filesize / 1024)} KB)",
                        detail=f"{filename} bör serveras som WebP/AVIF för bättre Core Web Vitals (LCP).",
                        target_type="media",
                        target_id=str(mid),
                        url=url,
                        suggestion="Konvertera till WebP/AVIF eller aktivera automatisk bildoptimering i LiteSpeed Cache.",
                    ).to_dict()
                )

        if filesize >= IMAGE_OVERSIZE_HIGH_BYTES:
            oversized += 1
            item_flags.append("oversized")
            issues.append(
                SeoIssue(
                    code="media_oversized",
                    severity="high",
                    category="media",
                    title=f"Överdimensionerad bildfil #{mid} ({round(filesize / 1024)} KB)",
                    detail=f"{filename} överstiger 500 KB vilket skadar sidhastighet och LCP.",
                    target_type="media",
                    target_id=str(mid),
                    url=url,
                    suggestion="Komprimera bilden till < 150 KB och anpassa upplösningen (max ~1600px bredd).",
                ).to_dict()
            )
        elif filesize >= IMAGE_OVERSIZE_WARN_BYTES:
            oversized += 1
            item_flags.append("large_filesize")

        if not is_svg and (width <= 0 or height <= 0):
            missing_dims += 1
            item_flags.append("missing_dimensions")
            issues.append(
                SeoIssue(
                    code="media_missing_dimensions",
                    severity="low",
                    category="media",
                    title=f"Bild #{mid} saknar bredd/höjd-metadata (CLS-risk)",
                    detail=f"width={width}, height={height} för {filename}.",
                    target_type="media",
                    target_id=str(mid),
                    url=url,
                    suggestion="Regenerera bildmetadata så att WordPress renderar width/height och förhindrar CLS.",
                ).to_dict()
            )

        items_out.append(
            {
                "media_id": mid,
                "filename": filename,
                "url": url,
                "mime_type": mime,
                "alt_text": alt,
                "suggested_alt": suggested_alt,
                "width": width,
                "height": height,
                "filesize": filesize,
                "attached_post_id": attached,
                "flags": item_flags,
            }
        )

    for dup_alt, mids in alt_seen.items():
        if len(mids) > 1:
            issues.append(
                SeoIssue(
                    code="media_duplicate_alt",
                    severity="low",
                    category="media",
                    title=f"Samma alt-text på {len(mids)} bilder",
                    detail=f"Alt-texten '{dup_alt[:60]}' används på bild-ID: {', '.join(str(m) for m in mids[:5])}.",
                    target_type="media",
                    target_id=str(mids[0]),
                    suggestion="Differentiera alt-texterna så varje bild beskriver sin unika vinkel/produktvariant.",
                    fix_type="media_alt",
                ).to_dict()
            )

    total = len(items_out)
    alt_ok = total - missing_alt
    return {
        "total_media": total,
        "non_image_skipped": non_image_skipped,
        "missing_alt_count": missing_alt,
        "generic_alt_count": generic_alt,
        "generic_filename_count": generic_fn,
        "oversized_count": oversized,
        "non_webp_count": non_webp,
        "missing_dimensions_count": missing_dims,
        "alt_coverage_pct": round((alt_ok / total) * 100.0, 1) if total else 100.0,
        "items": items_out,
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# Internal Link Graph & Orphan Detector
# --------------------------------------------------------------------------- #


def audit_internal_links(
    entities: list[dict[str, Any]],
    *,
    base_url: str = "",
) -> dict[str, Any]:
    """Build internal link graph, find orphan pages, generic anchors, and linking opportunities."""
    nodes: dict[str, dict[str, Any]] = {}
    inbound: dict[str, set[str]] = defaultdict(set)
    outbound: dict[str, set[str]] = defaultdict(set)
    generic_anchors: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []

    base_host = (urlparse(str(base_url or "")).hostname or "").lower()
    pub_entities: list[dict[str, Any]] = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("status") or "publish").strip().lower() != "publish":
            continue
        pub_entities.append(ent)
        url = str(ent.get("url") or f"/{ent.get('slug', '')}/").strip()
        if not base_host and "://" in url:
            base_host = (urlparse(url).hostname or "").lower()
        p = _norm_path(url)
        nodes[p] = {
            "path": p,
            "url": url,
            "object_type": str(ent.get("object_type") or "page"),
            "object_id": str(ent.get("object_id") or ""),
            "title": str(ent.get("title") or ""),
            "focus_keyphrase": str(ent.get("focus_keyphrase") or ""),
            "tokens": _significant_tokens(
                f"{ent.get('title', '')} {ent.get('focus_keyphrase', '')} {ent.get('slug', '')}"
            ),
        }

    total_links = 0
    for ent in pub_entities:
        src_path = _norm_path(str(ent.get("url") or f"/{ent.get('slug', '')}/"))
        for raw_link in ent.get("internal_links") or []:
            if isinstance(raw_link, dict):
                href = str(raw_link.get("href") or raw_link.get("url") or "").strip()
                anchor = str(raw_link.get("anchor") or raw_link.get("text") or "").strip()
            else:
                href = str(raw_link or "").strip()
                anchor = ""
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            if "://" in href:
                link_host = (urlparse(href).hostname or "").lower()
                if (
                    base_host
                    and link_host
                    and link_host != base_host
                    and link_host != f"www.{base_host}"
                    and base_host != f"www.{link_host}"
                ):
                    continue
            dst_path = _norm_path(href)
            if dst_path == src_path:
                continue
            outbound[src_path].add(dst_path)
            inbound[dst_path].add(src_path)
            total_links += 1
            if anchor and is_generic_anchor(anchor):
                generic_anchors.append({"from": src_path, "to": dst_path, "anchor": anchor})

    orphan_pages: list[dict[str, Any]] = []
    dead_end_pages: list[dict[str, Any]] = []
    node_rows: list[dict[str, Any]] = []

    for path, meta in nodes.items():
        in_cnt = len(inbound.get(path, set()))
        out_cnt = len(outbound.get(path, set()))
        row = {
            "path": path,
            "url": meta["url"],
            "object_type": meta["object_type"],
            "object_id": meta["object_id"],
            "title": meta["title"],
            "inbound_count": in_cnt,
            "outbound_count": out_cnt,
        }
        node_rows.append(row)
        if path != "/" and in_cnt == 0:
            orphan_pages.append(row)
            issues.append(
                SeoIssue(
                    code="orphan_page",
                    severity="high" if meta["object_type"] == "product" else "medium",
                    category="links",
                    title=f"Föräldralös sida (0 internlänkar): {meta['title'] or path}",
                    detail=f"Ingen annan analyserad sida länkar internt till {path}.",
                    target_type=meta["object_type"],
                    target_id=meta["object_id"],
                    url=meta["url"],
                    suggestion="Lägg till kontextuella internlänkar från relevanta guider, kategorisidor och relaterade produkter.",
                ).to_dict()
            )
        if out_cnt == 0:
            dead_end_pages.append(row)

    if generic_anchors:
        issues.append(
            SeoIssue(
                code="generic_internal_anchors",
                severity="low",
                category="links",
                title=f"{len(generic_anchors)} internlänk(ar) med generisk ankartext",
                detail=f"Exempel: '{generic_anchors[0]['anchor']}' ({generic_anchors[0]['from']} -> {generic_anchors[0]['to']}).",
                url=generic_anchors[0]["from"],
                suggestion="Byt ut 'läs mer'/'klicka här' mot beskrivande, ämnesrelevanta ankartexter.",
            ).to_dict()
        )

    # Suggest internal link opportunities from guides/posts to orphan or under-linked products
    opportunities: list[dict[str, Any]] = []
    for orphan in orphan_pages[:10]:
        o_tokens = nodes[orphan["path"]]["tokens"]
        best_src: tuple[int, str, str] | None = None
        for src_path, src_meta in nodes.items():
            if src_path == orphan["path"]:
                continue
            if orphan["path"] in outbound.get(src_path, set()):
                continue
            overlap = len(o_tokens & src_meta["tokens"])
            if overlap > 0 and (best_src is None or overlap > best_src[0]):
                best_src = (overlap, src_path, src_meta["title"])
        if best_src is not None:
            opportunities.append(
                {
                    "from_path": best_src[1],
                    "from_title": best_src[2],
                    "to_path": orphan["path"],
                    "to_title": orphan["title"],
                    "recommended_anchor": nodes[orphan["path"]]["focus_keyphrase"] or orphan["title"],
                    "reason": "Föräldralös målsida med ämnesöverlapp",
                }
            )

    return {
        "total_nodes": len(node_rows),
        "total_internal_links": total_links,
        "orphan_count": len(orphan_pages),
        "orphan_pages": orphan_pages,
        "dead_end_pages": dead_end_pages,
        "generic_anchors": generic_anchors[:25],
        "link_opportunities": opportunities,
        "nodes": node_rows,
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# Keyword Cannibalization Matrix
# --------------------------------------------------------------------------- #


def detect_keyword_cannibalization(
    entities: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect multiple URLs competing for overlapping focus keyphrases, titles, or slugs."""
    conflicts: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    kw_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ent_list = [
        e
        for e in (entities or [])
        if isinstance(e, dict) and str(e.get("status") or "publish").strip().lower() == "publish"
    ]

    for ent in ent_list:
        kw = " ".join(str(ent.get("focus_keyphrase") or "").strip().lower().split())
        if kw:
            kw_groups[kw].append(ent)

    seen_pairs: set[frozenset[str]] = set()

    # 1. Exact focus keyphrase collisions
    for kw, group in kw_groups.items():
        if len(group) < 2:
            continue
        sorted_group = sorted(
            group,
            key=lambda e: (int(e.get("word_count") or 0), len(e.get("internal_links") or [])),
            reverse=True,
        )
        winner = sorted_group[0]
        urls = [str(e.get("url") or f"/{e.get('slug', '')}/") for e in sorted_group]
        for u in urls:
            for v in urls:
                if u != v:
                    seen_pairs.add(frozenset({u, v}))
        conflict = {
            "keyword": kw,
            "match_type": "exact_focus_keyphrase",
            "severity": "high",
            "competing_urls": [
                {
                    "url": str(e.get("url") or f"/{e.get('slug', '')}/"),
                    "object_type": str(e.get("object_type") or ""),
                    "object_id": str(e.get("object_id") or ""),
                    "title": str(e.get("title") or ""),
                    "word_count": int(e.get("word_count") or 0),
                }
                for e in sorted_group
            ],
            "recommended_primary_url": str(winner.get("url") or f"/{winner.get('slug', '')}/"),
            "recommended_action": (
                "Behåll primärsidan för huvudnyckelordet; differentiera fokusnyckelord & sökintention "
                "på övriga sidor eller slå ihop överlappande innehåll med 301-redirect."
            ),
        }
        conflicts.append(conflict)
        issues.append(
            SeoIssue(
                code="keyword_cannibalization_exact",
                severity="high",
                category="cannibalization",
                title=f"Sökordskannibalisering: '{kw}' ({len(sorted_group)} sidor)",
                detail=f"Flera sidor konkurrerar om exakt samma fokusnyckelord: {', '.join(urls[:4])}",
                url=urls[0],
                suggestion=conflict["recommended_action"],
                fix_type="seo_meta",
            ).to_dict()
        )

    # 2. Title + slug token overlap (Jaccard >= 0.55 on >= 2 shared tokens)
    token_sets = [
        (
            ent,
            _significant_tokens(
                f"{ent.get('title', '')} {ent.get('meta_title', '')} {str(ent.get('slug', '')).replace('-', ' ')}"
            ),
        )
        for ent in ent_list
    ]
    for i in range(len(token_sets)):
        e1, t1 = token_sets[i]
        u1 = str(e1.get("url") or f"/{e1.get('slug', '')}/")
        if len(t1) < 2:
            continue
        for j in range(i + 1, len(token_sets)):
            e2, t2 = token_sets[j]
            u2 = str(e2.get("url") or f"/{e2.get('slug', '')}/")
            if u1 == u2 or frozenset({u1, u2}) in seen_pairs or len(t2) < 2:
                continue
            shared = t1 & t2
            union = t1 | t2
            jaccard = len(shared) / len(union) if union else 0.0
            if len(shared) >= 2 and jaccard >= 0.55:
                seen_pairs.add(frozenset({u1, u2}))
                winner = e1 if int(e1.get("word_count") or 0) >= int(e2.get("word_count") or 0) else e2
                shared_label = " ".join(sorted(shared)[:4])
                conflict = {
                    "keyword": shared_label,
                    "match_type": "title_slug_overlap",
                    "severity": "medium",
                    "overlap_score": round(jaccard, 2),
                    "competing_urls": [
                        {
                            "url": u1,
                            "object_type": str(e1.get("object_type") or ""),
                            "object_id": str(e1.get("object_id") or ""),
                            "title": str(e1.get("title") or ""),
                            "word_count": int(e1.get("word_count") or 0),
                        },
                        {
                            "url": u2,
                            "object_type": str(e2.get("object_type") or ""),
                            "object_id": str(e2.get("object_id") or ""),
                            "title": str(e2.get("title") or ""),
                            "word_count": int(e2.get("word_count") or 0),
                        },
                    ],
                    "recommended_primary_url": str(winner.get("url") or f"/{winner.get('slug', '')}/"),
                    "recommended_action": (
                        "Förtydliga respektive sidas unika sökintention (t.ex. köp vs guide/jämförelse) "
                        "och länka från stödsidan till primärsidan."
                    ),
                }
                conflicts.append(conflict)
                issues.append(
                    SeoIssue(
                        code="keyword_cannibalization_overlap",
                        severity="medium",
                        category="cannibalization",
                        title=f"Överlappande söktema: '{shared_label}'",
                        detail=f"{u1} och {u2} har hög titel-/slug-överlappning ({round(jaccard * 100)}%).",
                        url=u1,
                        suggestion=conflict["recommended_action"],
                    ).to_dict()
                )

    return conflicts, issues


# --------------------------------------------------------------------------- #
# Scoring & JSON-LD Schema Generators
# --------------------------------------------------------------------------- #


def compute_seo_score(issues: list[dict[str, Any]]) -> tuple[int, str]:
    """Compute a 0–100 SEO health score and A–F grade from prioritized issues."""
    penalties = {"critical": 14, "high": 6, "medium": 3, "low": 1}
    # Cap cumulative deduction per severity tier so a single noisy category never flattens everything
    caps = {"critical": 45, "high": 35, "medium": 20, "low": 10}
    buckets: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in issues or []:
        sev = str(issue.get("severity") or "low").lower()
        if sev not in buckets:
            sev = "low"
        buckets[sev] = min(caps[sev], buckets[sev] + penalties[sev])
    score = max(0, min(100, 100 - sum(buckets.values())))
    return score, score_to_grade(score)


def build_product_jsonld(product: dict[str, Any], *, market: str = "no") -> dict[str, Any]:
    """Generate clean Schema.org Product JSON-LD for a WooCommerce product."""
    mkt = normalize_market(market)
    currency = {"no": "NOK", "se": "SEK", "dk": "DKK"}[mkt]
    stock_status = str(product.get("stock_status") or "instock").strip().lower()
    if stock_status == "outofstock":
        availability = "https://schema.org/OutOfStock"
    elif stock_status == "onbackorder":
        availability = "https://schema.org/BackOrder"
    else:
        availability = "https://schema.org/InStock"

    raw_desc = (
        product.get("meta_description")
        or product.get("short_description")
        or product.get("description")
        or ""
    )
    clean_desc = _strip_html(str(raw_desc))[:500]

    offers: dict[str, Any] = {
        "@type": "Offer",
        "url": str(product.get("url") or product.get("permalink") or ""),
        "priceCurrency": str(product.get("currency") or currency),
        "availability": availability,
        "itemCondition": "https://schema.org/NewCondition",
    }
    raw_price = product.get("price")
    if raw_price is None or str(raw_price).strip() == "":
        raw_price = product.get("regular_price")
    if raw_price is not None and str(raw_price).strip() != "":
        offers["price"] = str(raw_price).strip()

    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": str(product.get("name") or product.get("title") or "Azom Produkt").strip(),
        "description": clean_desc,
        "sku": str(product.get("sku") or f"AZOM-{product.get('id') or product.get('object_id') or '001'}"),
        "brand": {"@type": "Brand", "name": str(product.get("brand") or "Azom")},
        "offers": offers,
    }
    img = product.get("image")
    if not img and isinstance(product.get("images"), list) and product["images"]:
        first_img = product["images"][0]
        if isinstance(first_img, dict):
            img = first_img.get("src") or first_img.get("url")
        elif isinstance(first_img, str):
            img = first_img
    if img:
        payload["image"] = img
    return payload


def build_faq_jsonld(qa_pairs: list[dict[str, str]]) -> dict[str, Any]:
    """Generate clean Schema.org FAQPage JSON-LD from question/answer pairs."""
    entities: list[dict[str, Any]] = []
    for row in qa_pairs or []:
        q = str(row.get("question") or row.get("q") or "").strip()
        a = str(row.get("answer") or row.get("a") or "").strip()
        if q and a:
            entities.append(
                {
                    "@type": "Question",
                    "name": q,
                    "acceptedAnswer": {"@type": "Answer", "text": a},
                }
            )
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }


def build_breadcrumb_jsonld(crumbs: list[dict[str, str]]) -> dict[str, Any]:
    """Generate clean Schema.org BreadcrumbList JSON-LD from ordered ``{name, url}`` items."""
    elements: list[dict[str, Any]] = []
    for idx, row in enumerate(crumbs or [], start=1):
        name = str(row.get("name") or "").strip()
        url = str(row.get("url") or row.get("item") or "").strip()
        if name and url:
            elements.append(
                {
                    "@type": "ListItem",
                    "position": idx,
                    "name": name,
                    "item": url,
                }
            )
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": elements,
    }


# --------------------------------------------------------------------------- #
# GSC Strike-Distance, Low-CTR & Query Cannibalization Analyzer (Proposal 2)
# --------------------------------------------------------------------------- #

_GSC_CTR_BENCHMARKS: dict[int, float] = {
    1: 0.28,
    2: 0.15,
    3: 0.10,
    4: 0.07,
    5: 0.05,
    6: 0.04,
    7: 0.03,
    8: 0.025,
    9: 0.02,
    10: 0.018,
}


def _expected_ctr_for_position(position: float) -> float:
    pos_int = max(1, int(round(position)))
    if pos_int in _GSC_CTR_BENCHMARKS:
        return _GSC_CTR_BENCHMARKS[pos_int]
    return 0.012


def analyze_gsc_quick_wins(
    gsc_rows: list[dict[str, Any]],
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Identify GSC strike-distance terms (pos 4-15), low-CTR opportunities, and query cannibalization."""
    entity_by_path: dict[str, dict[str, Any]] = {}
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        url = str(ent.get("url") or f"/{ent.get('slug', '')}/").strip()
        if url:
            entity_by_path[_norm_path(url)] = ent

    clean_rows: list[dict[str, Any]] = []
    for raw in gsc_rows or []:
        if not isinstance(raw, dict):
            continue
        query = " ".join(str(raw.get("query") or "").strip().split())
        page = str(raw.get("page") or "").strip()
        if not query or not page:
            continue
        clicks = max(0, int(round(float(raw.get("clicks") or 0))))
        impressions = max(0, int(round(float(raw.get("impressions") or 0))))
        position = float(raw.get("position") or 0.0)
        raw_ctr = raw.get("ctr")
        if raw_ctr is not None:
            ctr = float(raw_ctr)
            if ctr > 1.0:
                ctr = ctr / 100.0
        else:
            ctr = (clicks / impressions) if impressions > 0 else 0.0
        ctr = max(0.0, min(1.0, ctr))

        ent = entity_by_path.get(_norm_path(page))
        row_obj: dict[str, Any] = {
            "query": query,
            "page": page,
            "clicks": clicks,
            "impressions": impressions,
            "ctr": round(ctr, 4),
            "ctr_pct": round(ctr * 100.0, 2),
            "position": round(position, 2),
        }
        if ent is not None:
            row_obj["object_type"] = str(ent.get("object_type") or "")
            row_obj["object_id"] = str(ent.get("object_id") or "")
            row_obj["entity_title"] = str(ent.get("title") or "")
        clean_rows.append(row_obj)

    strike_distance: list[dict[str, Any]] = []
    low_ctr: list[dict[str, Any]] = []
    query_pages: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for row in clean_rows:
        pos = row["position"]
        imps = row["impressions"]
        clicks = row["clicks"]
        ctr = row["ctr"]

        if 4.0 <= pos <= 15.0 and imps > 0:
            target_ctr = 0.10  # Top-3 SERP benchmark
            upside = max(0, int(round(imps * target_ctr - clicks)))
            opp_score = round(imps * (1.0 - min(ctr, 0.30)) * (16.0 - pos) / 12.0, 1)
            strike_distance.append(
                {
                    **row,
                    "opportunity_score": opp_score,
                    "estimated_click_upside": upside,
                    "recommended_action": (
                        "Förstärk H1/SEO-titel, lägg till FAQ-avsnitt och 2–3 kontextuella internlänkar "
                        "för att lyfta till topp 3."
                    ),
                }
            )

        expected = _expected_ctr_for_position(pos)
        if imps >= 200 and ctr < expected * 0.75:
            upside = max(0, int(round(imps * expected - clicks)))
            low_ctr.append(
                {
                    **row,
                    "expected_ctr": round(expected, 4),
                    "expected_ctr_pct": round(expected * 100.0, 2),
                    "ctr_gap_pct": round((expected - ctr) * 100.0, 2),
                    "estimated_click_upside": upside,
                    "recommended_action": (
                        "Skriv om SEO-titel (<=60 tecken) och meta description (<=155 tecken) med "
                        "tydligare värdeerbjudande, pris/garanti och CTA."
                    ),
                }
            )

        q_key = row["query"].lower()
        p_norm = _norm_path(row["page"])
        existing = query_pages[q_key].get(p_norm)
        if existing is None or imps > existing["impressions"]:
            query_pages[q_key][p_norm] = row

    strike_distance.sort(
        key=lambda r: (r["impressions"], r["opportunity_score"]),
        reverse=True,
    )
    low_ctr.sort(
        key=lambda r: (r["estimated_click_upside"], r["impressions"]),
        reverse=True,
    )

    cannibalization: list[dict[str, Any]] = []
    for _q_key, page_map in query_pages.items():
        if len(page_map) < 2:
            continue
        pages_sorted = sorted(
            page_map.values(),
            key=lambda r: (r["clicks"], r["impressions"], -r["position"]),
            reverse=True,
        )
        total_imps = sum(p["impressions"] for p in pages_sorted)
        total_clicks = sum(p["clicks"] for p in pages_sorted)
        primary = pages_sorted[0]
        secondary_imps = total_imps - primary["impressions"]
        split_pct = round((secondary_imps / total_imps) * 100.0, 1) if total_imps > 0 else 0.0
        cannibalization.append(
            {
                "query": primary["query"],
                "page_count": len(pages_sorted),
                "total_impressions": total_imps,
                "total_clicks": total_clicks,
                "primary_page": primary["page"],
                "secondary_impression_share_pct": split_pct,
                "pages": [
                    {
                        "page": p["page"],
                        "clicks": p["clicks"],
                        "impressions": p["impressions"],
                        "ctr_pct": p["ctr_pct"],
                        "position": p["position"],
                    }
                    for p in pages_sorted
                ],
                "recommended_action": (
                    f"Konsolidera sökintentionen till {primary['page']} och differentiera "
                    "titel/fokusnyckelord eller internlänka från konkurrerande sidor."
                ),
            }
        )

    cannibalization.sort(key=lambda c: c["total_impressions"], reverse=True)

    total_clicks = sum(r["clicks"] for r in clean_rows)
    total_impressions = sum(r["impressions"] for r in clean_rows)
    weighted_pos = (
        round(
            sum(r["position"] * max(1, r["impressions"]) for r in clean_rows)
            / sum(max(1, r["impressions"]) for r in clean_rows),
            2,
        )
        if clean_rows
        else 0.0
    )
    total_upside = sum(r["estimated_click_upside"] for r in strike_distance)

    return {
        "strike_distance": strike_distance[:30],
        "low_ctr": low_ctr[:30],
        "cannibalization": cannibalization[:20],
        "summary": {
            "total_rows": len(clean_rows),
            "total_clicks": total_clicks,
            "total_impressions": total_impressions,
            "avg_position": weighted_pos,
            "strike_distance_count": len(strike_distance),
            "low_ctr_count": len(low_ctr),
            "cannibalization_count": len(cannibalization),
            "estimated_click_upside": total_upside,
        },
    }


__all__ = [
    "analyze_gsc_quick_wins",
    "audit_entities_on_page",
    "audit_internal_links",
    "audit_media_seo",
    "build_breadcrumb_jsonld",
    "build_faq_jsonld",
    "build_product_jsonld",
    "compute_seo_score",
    "detect_keyword_cannibalization",
]
