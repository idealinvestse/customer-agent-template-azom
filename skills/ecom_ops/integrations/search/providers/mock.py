"""Deterministic offline mock search provider (``AZOM_USE_MOCK=1`` ONLY)."""

from __future__ import annotations

from typing import Any

from ecom_ops.integrations.search.base import (
    SearchCapabilities,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from ecom_ops.integrations.search.errors import SearchConfigError
from ecom_ops.integrations.search.providers import scrub_query_for_provider
from ecom_ops.research.config import is_mock_mode

_PROFILE_FIXTURES: dict[str, tuple[dict[str, Any], ...]] = {
    "design_inspiration": (
        {
            "url": "https://baymard.com/blog/order-confirmation-page-ux",
            "title": "Order Confirmation & Thank-You Page UX Best Practices",
            "snippet": (
                "High-converting e-commerce thank-you pages show clear order timeline, "
                "delivery expectations, account/tracking CTA, and relevant post-purchase setup guides."
            ),
            "published_at": "2025-09-18",
            "score": 0.94,
            "content": (
                "# Thank-You Page UX Patterns\n"
                "- Confirm order number and estimated dispatch window above the fold.\n"
                "- Provide clear next-step installation or onboarding CTAs.\n"
                "- Keep mobile tap targets >= 44px and avoid distracting checkout popups."
            ),
        },
        {
            "url": "https://www.nngroup.com/articles/confirmation-pages/",
            "title": "Confirmation Pages: Reducing Post-Purchase Anxiety",
            "snippet": (
                "Clear hierarchy, visible support contact, warranty assurance, and scannable "
                "order summary reduce WISMO support tickets after purchase."
            ),
            "published_at": "2025-06-10",
            "score": 0.91,
            "content": (
                "# Post-Purchase Confirmation Hierarchy\n"
                "- Reassure customers with email confirmation notice and support channel.\n"
                "- Present structured tracking milestones and return/warranty links."
            ),
        },
        {
            "url": "https://www.smashingmagazine.com/2025/02/nordic-ecommerce-post-purchase-ux/",
            "title": "Nordic E-Commerce Post-Purchase UX Benchmarks",
            "snippet": (
                "Nordic stores emphasize trust badges, 3-year warranty reassurance, carrier "
                "tracking clarity, and non-intrusive accessory recommendations."
            ),
            "published_at": "2025-02-14",
            "score": 0.87,
            "content": (
                "# Nordic Post-Purchase UX\n"
                "- Highlight local carrier delivery timelines (PostNord / Bring / DHL).\n"
                "- Pair technical products with concise installation guides."
            ),
        },
    ),
    "technical_docs": (
        {
            "url": "https://developer.woocommerce.com/docs/woocommerce-thankyou-hook/",
            "title": "WooCommerce 9.x Thank-You Page Hooks & Block Templates",
            "snippet": (
                "Official WooCommerce 9.x documentation for woocommerce_thankyou action hook, "
                "Order Confirmation block customization, and HPOS-compatible order reads."
            ),
            "published_at": "2025-11-02",
            "score": 0.96,
            "content": (
                "# WooCommerce 9.x Order Confirmation Hooks\n"
                "Use `woocommerce_thankyou` for classic templates or register an inner block "
                "inside `woocommerce/order-confirmation-status` for Block Checkout."
            ),
        },
        {
            "url": "https://developer.wordpress.org/block-editor/reference-guides/theme-json/",
            "title": "WordPress theme.json Global Settings and Styles Reference",
            "snippet": (
                "Configure palette tokens, fluid typography presets, spacing scale, and block "
                "button styles in WordPress block themes."
            ),
            "published_at": "2025-08-20",
            "score": 0.92,
            "content": (
                "# theme.json Reference\n"
                "Define `settings.color.palette` and `styles.elements.button` cleanly without "
                "overriding WooCommerce block specificity unnecessarily."
            ),
        },
        {
            "url": "https://woocommerce.com/document/woocommerce-blocks/",
            "title": "WooCommerce Blocks & High-Performance Order Storage (HPOS)",
            "snippet": (
                "Guidance on using wc_get_order() instead of get_post_meta() for HPOS "
                "compatibility in WooCommerce 9.x."
            ),
            "published_at": "2025-07-12",
            "score": 0.89,
            "content": (
                "# HPOS Compatibility\n"
                "Always read order metadata via `$order->get_meta()` and `wc_get_order()`."
            ),
        },
    ),
    "competitor_market": (
        {
            "url": "https://www.brl.se/sv/artiklar/bilstereo-apple-carplay",
            "title": "Nordic Car Multimedia & Apple CarPlay Positioning Benchmark",
            "snippet": (
                "Swedish car audio retailers emphasize vehicle-specific fitment guarantees, "
                "plug-and-play CANBUS harnesses, and Swedish technical support."
            ),
            "published_at": "2025-10-05",
            "score": 0.90,
            "content": (
                "# Swedish Car Multimedia Market\n"
                "Key buyer objections: steering-wheel control retention, OEM camera support, "
                "and clear model-year fitment verification."
            ),
        },
        {
            "url": "https://www.bilradiospes.no/produkter/bilstereo/apple-carplay",
            "title": "Norwegian Car Multimedia: DAB+ & Cold-Weather Reliability",
            "snippet": (
                "Norwegian automotive electronics stores highlight DAB+ adapter integration, "
                "5-year consumer warranty expectations, and fast shipping across Norway."
            ),
            "published_at": "2025-09-22",
            "score": 0.88,
            "content": (
                "# Norwegian Car Audio Positioning\n"
                "DAB+ reception and clear Norwegian installation guides build strong trust."
            ),
        },
        {
            "url": "https://www.carsound.dk/produkter/multimedia-carplay",
            "title": "Danish Car Infotainment: Wireless CarPlay & OEM Integration",
            "snippet": (
                "Danish aftermarket multimedia sellers lead with wireless CarPlay/Android Auto "
                "and transparent installation accessory bundles."
            ),
            "published_at": "2025-08-30",
            "score": 0.85,
            "content": (
                "# Danish Car Infotainment Positioning\n"
                "Clear bundle checklists reduce post-purchase cable incompatibility returns."
            ),
        },
    ),
    "support_fact_check": (
        {
            "url": "https://www.postnord.se/privat/skicka/driftinformation",
            "title": "PostNord Driftinformation & Aktuella Leveransstörningar",
            "snippet": (
                "Officiell driftstatus för paket och brev i Sverige samt uppdaterade "
                "leveranstider till ombud och paketbox."
            ),
            "published_at": "2026-11-20",
            "score": 0.95,
            "content": (
                "# PostNord Driftinformation\n"
                "Normala leveranstider gäller för huvudstråk; enstaka förseningar i norra "
                "Sverige kan lägga till 1 arbetsdag."
            ),
        },
        {
            "url": "https://www.konsumentverket.se/for-foretag/regler-for-e-handel/",
            "title": "Konsumentverket: Ångerrätt och reklamation vid e-handel i Sverige",
            "snippet": (
                "Vid distanshandel har konsumenten 14 dagars ångerrätt enligt "
                "distansavtalslagen samt tre års reklamationsrätt enligt konsumentköplagen."
            ),
            "published_at": "2026-03-01",
            "score": 0.94,
            "content": (
                "# Svenska e-handelsregler\n"
                "- 14 dagars ångerrätt från mottagen vara.\n"
                "- Återbetalning ska ske inom 14 dagar från att näringsidkaren tagit emot "
                "ångermeddelandet (kan hållas inne tills varan eller bevis på retur mottagits)."
            ),
        },
        {
            "url": "https://www.forbrukerradet.no/forside/okonomi-og-betaling/angrerett/",
            "title": "Forbrukerrådet: Angrerett ved netthandel i Norge",
            "snippet": (
                "14 dagers angrerett på nettkjøp i Norge etter angrerettloven."
            ),
            "published_at": "2026-02-15",
            "score": 0.89,
            "content": (
                "# Angrerett i Norge\n"
                "Forbruker har 14 dagers angrerett fra varen er mottatt."
            ),
        },
    ),
    "marketing_intel": (
        {
            "url": "https://support.google.com/google-ads/answer/10000067",
            "title": "Google Ads Consent Mode v2 & Conversion Modeling in EEA",
            "snippet": (
                "Consent Mode v2 is required for EEA/Nordic audience and conversion modeling "
                "across Google Ads and GA4."
            ),
            "published_at": "2025-11-10",
            "score": 0.93,
            "content": (
                "# Consent Mode v2\n"
                "Ensure `ad_storage`, `analytics_storage`, `ad_user_data`, and "
                "`ad_personalization` signals are configured cleanly."
            ),
        },
        {
            "url": "https://developers.google.com/tag-platform/security/guides/consent",
            "title": "Google Tag Platform: Consent Configuration Reference",
            "snippet": (
                "Official developer guide to Consent Mode defaults, region-specific settings "
                "for SE/NO/DK, and GA4 behavioral modeling."
            ),
            "published_at": "2025-10-15",
            "score": 0.90,
            "content": (
                "# Consent Configuration\n"
                "Verify tag firing order and default denied state before user consent."
            ),
        },
        {
            "url": "https://blog.google/products/ads-commerce/nordic-retail-trends/",
            "title": "Google Nordic Retail Search Trends & Peak Season Planning",
            "snippet": (
                "High-intent automotive accessories queries spike in spring road-trip and "
                "autumn commuter seasons across Sweden, Norway, and Denmark."
            ),
            "published_at": "2025-09-01",
            "score": 0.85,
            "content": (
                "# Nordic Retail Search Trends\n"
                "Separate universal 2-DIN queries from model-specific OEM fitment searches."
            ),
        },
    ),
    "deep_general": (
        {
            "url": "https://www.nordic-ecom.se/rapporter/nordisk-ehandel-eftermarknad",
            "title": "Nordisk E-handel & Eftermarknad: Kundupplevelse och Konvertering",
            "snippet": (
                "Nordiska e-handlare som tydliggör kompatibilitet, garanti och leveransstatus "
                "minskar både returer och inkommande supportärenden."
            ),
            "published_at": "2025-10-12",
            "score": 0.90,
            "content": (
                "# Nordisk E-handel Rapport\n"
                "Tydlig produktdata och snabb eftermarknadsservice driver återköp."
            ),
        },
        {
            "url": "https://baymard.com/lists/cart-and-checkout-usability",
            "title": "Baymard Checkout & Post-Purchase Usability Benchmarks",
            "snippet": (
                "Empirical UX guidelines covering mobile layout, trust signals, and clear "
                "order confirmation summaries."
            ),
            "published_at": "2025-08-19",
            "score": 0.88,
            "content": (
                "# Checkout & Post-Purchase Benchmarks\n"
                "Keep confirmation actions scannable and accessible on mobile screens."
            ),
        },
        {
            "url": "https://developer.woocommerce.com/docs/hpos-overview/",
            "title": "WooCommerce Architecture & Store Operations Overview",
            "snippet": (
                "Best practices for scalable WooCommerce order storage, REST integrations, "
                "and safe staging validation."
            ),
            "published_at": "2025-07-05",
            "score": 0.86,
            "content": (
                "# WooCommerce Operations\n"
                "Validate template and plugin changes in staging before production rollout."
            ),
        },
    ),
}


def _host_matches(url: str, domain: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    d = domain.strip().lower().lstrip(".")
    return bool(d) and (host == d or host.endswith(f".{d}"))


class MockSearchRoute:
    """Offline deterministic fixture search route (never performs network I/O)."""

    route_id = "mock"
    capabilities = SearchCapabilities(
        supports_country=True,
        supports_language=True,
        supports_freshness=True,
        supports_domain_filter=True,
        supports_content=True,
        supports_extract=False,
        supports_deep_mode=True,
    )

    def is_configured(self) -> bool:
        return is_mock_mode()

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "mode": "mock_fixture",
        }

    def search(self, request: SearchRequest) -> SearchResult:
        if not is_mock_mode():
            raise SearchConfigError(
                "mock search route is forbidden in live mode",
                code="mock_disabled",
            )
        scrub_query_for_provider(request.query, taint=request.taint)
        profile_key = str(request.profile_id or "deep_general").strip().lower()
        rows = _PROFILE_FIXTURES.get(profile_key, _PROFILE_FIXTURES["deep_general"])

        hits: list[SearchHit] = []
        for row in rows:
            url = str(row["url"])
            if request.exclude_domains and any(_host_matches(url, d) for d in request.exclude_domains):
                continue
            if request.include_domains and not any(_host_matches(url, d) for d in request.include_domains):
                continue
            content = str(row["content"]) if request.include_content else None
            hits.append(
                SearchHit(
                    url=url,
                    title=str(row["title"]),
                    snippet=str(row["snippet"]),
                    published_at=str(row["published_at"]),
                    provider="mock",
                    score=float(row["score"]),
                    raw_excerpts=(str(row["snippet"]),),
                    content=content,
                )
            )
            if len(hits) >= request.max_results:
                break

        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="mock",
            hits=tuple(hits),
            provenance="mock_fixture",
            latency_ms=1,
            cached=False,
            estimated_cost_usd=0.0,
        )


__all__ = ["MockSearchRoute"]
