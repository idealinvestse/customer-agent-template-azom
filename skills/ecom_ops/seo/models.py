"""Data models, ID generators, and canonical hashing helpers for Azom SEO Studio."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

SEO_AUDIT_ID_RE = re.compile(r"^seoa-[0-9a-f]{12}$")
SEO_FIX_ID_RE = re.compile(r"^seom-[0-9a-f]{12}$")
SEO_BATCH_ID_RE = re.compile(r"^seob-[0-9a-f]{12}$")
SEO_STRATEGY_ID_RE = re.compile(r"^seos-[0-9a-f]{12}$")
SEO_CAMPAIGN_ID_RE = re.compile(r"^seoc-[0-9a-f]{12}$")
SEO_LINK_PKG_ID_RE = re.compile(r"^seol-[0-9a-f]{12}$")

VALID_AUDIT_SCOPES: frozenset[str] = frozenset(
    {
        "site",
        "technical",
        "page",
        "product",
        "media",
        "links",
        "cannibalization",
        "gsc",
        "parity",
        "deep_crawl",
    }
)
VALID_FIX_TYPES: frozenset[str] = frozenset(
    {"seo_meta", "media_alt", "product_seo", "page_seo", "schema_jsonld"}
)
VALID_TARGET_TYPES: frozenset[str] = frozenset(
    {"product", "page", "post", "media", "category", "site"}
)
VALID_MARKETS: frozenset[str] = frozenset({"no", "se", "dk"})
VALID_CAMPAIGN_TYPES: tuple[str, ...] = (
    "commercial",
    "seasonal",
    "product_launch",
    "content_hub",
    "technical_sprint",
    "category",
    "informational",
    "technical",
)

LINK_PACKAGE_STATUSES: tuple[str, ...] = (
    "draft",
    "ready_for_review",
    "approved_for_manual_order",
    "ordered_external",
    "delivered",
    "cancelled",
)

GSC_NOT_CONNECTED_NOTE = (
    "Google Search Console (GSC) är inte kopplat ännu — analys, prioritering och "
    "rekommendationer baseras på WP/Woo-innehåll, Azom Ops Bridge, teknisk HTML/JSON-LD, "
    "GA4-landningssidor, Google Ads-söktermer och Web Research."
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_seo_id(prefix: str) -> str:
    clean = str(prefix or "seoa").strip().lower()
    if clean not in {"seoa", "seom", "seob", "seos", "seoc", "seol"}:
        raise ValueError(f"Unsupported SEO ID prefix: {prefix!r}")
    return f"{clean}-{uuid.uuid4().hex[:12]}"


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def normalize_market(market: str | None) -> str:
    m = str(market or "no").strip().lower()
    if m in {"nb", "nb-no", "no-no", "norge", "norway"}:
        return "no"
    if m in {"sv", "sv-se", "se-se", "sverige", "sweden"}:
        return "se"
    if m in {"da", "da-dk", "dk-dk", "danmark", "denmark"}:
        return "dk"
    if m in VALID_MARKETS:
        return m
    raise ValueError(f"Unsupported market {market!r}. Allowed: {sorted(VALID_MARKETS)}")


@dataclass
class SeoIssue:
    code: str
    severity: str  # critical | high | medium | low
    category: str  # technical | on_page | media | links | cannibalization | schema | hreflang
    title: str
    detail: str
    target_type: str = "site"
    target_id: str = ""
    url: str = ""
    suggestion: str = ""
    fix_type: str = ""
    proposed_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeoAuditReport:
    audit_id: str
    site_id: str
    market: str
    scope: str
    score: int
    grade: str
    summary: dict[str, Any]
    issues: list[dict[str, Any]]
    technical: dict[str, Any]
    on_page: dict[str, Any]
    media: dict[str, Any]
    links: dict[str, Any]
    cannibalization: list[dict[str, Any]]
    data_sources: list[str]
    gsc_note: str = GSC_NOT_CONNECTED_NOTE
    gsc_status: dict[str, Any] = field(default_factory=dict)
    gsc_quick_wins: dict[str, Any] = field(default_factory=dict)
    parity: dict[str, Any] = field(default_factory=dict)
    deep_crawl: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeoFixPlan:
    plan_id: str
    site_id: str
    market: str
    fix_type: str
    target_type: str
    target_id: str
    summary: str
    before: dict[str, Any]
    proposed: dict[str, Any]
    before_sha256: str
    plan_sha256: str
    status: str = "pending"
    executed: bool = False
    approval_required: bool = True
    created_at: str = field(default_factory=utc_now_iso)
    expires_at: str = ""
    actor: str = ""
    applied_change_id: str = ""
    applied_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeoFixBatch:
    batch_id: str
    site_id: str
    audit_id: str
    market: str
    scope: str
    summary: str
    items: list[dict[str, Any]]
    item_count: int
    plan_sha256: str
    status: str = "pending"
    executed: bool = False
    approval_required: bool = True
    created_at: str = field(default_factory=utc_now_iso)
    expires_at: str = ""
    actor: str = ""
    applied_count: int = 0
    failed_count: int = 0
    applied_change_ids: list[str] = field(default_factory=list)
    applied_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KeywordCluster:
    cluster_name: str
    pillar_url: str
    primary_keyword: str
    secondary_keywords: list[str]
    search_intent: str  # transactional | commercial | informational | navigational
    funnel_stage: str  # bofu | mofu | tofu
    market: str
    priority: str  # P1 | P2 | P3
    estimated_impact: str
    content_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeoStrategy:
    strategy_id: str
    site_id: str
    market: str
    title: str
    goals: str
    focus_topics: list[str]
    executive_summary: str
    market_positioning: dict[str, Any]
    topic_clusters: list[dict[str, Any]]
    roadmap_90d: dict[str, list[dict[str, Any]]]
    action_tracks: dict[str, list[str]]
    kpi_targets: list[dict[str, Any]]
    data_sources: list[str]
    gsc_note: str = GSC_NOT_CONNECTED_NOTE
    research_brief_id: str | None = None
    research_summary: str = ""
    serp_research: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeoCampaign:
    campaign_id: str
    site_id: str
    market: str
    name: str
    campaign_type: str
    objective: str
    strategy_id: str | None
    research_brief_id: str | None
    keyword_clusters: list[dict[str, Any]]
    url_keyword_map: list[dict[str, Any]]
    on_page_checklist: list[dict[str, Any]]
    internal_linking_plan: list[dict[str, Any]]
    schema_plan: list[dict[str, Any]]
    content_briefs: list[dict[str, Any]]
    kpi_milestones: list[dict[str, Any]]
    data_sources: list[str]
    gsc_note: str = GSC_NOT_CONNECTED_NOTE
    serp_research: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    created_by: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LinkTargetSpec:
    index: int
    target_url: str
    anchor_text: str
    anchor_type: str  # brand | naked_url | generic | partial_match | exact_match
    keyword: str
    article_angle: str
    target_week: int
    min_dr: int
    language: str
    tld_preference: str
    rel_guidance: str
    placement_type: str
    estimated_cost_sek: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnchorMixReport:
    total_links: int
    counts: dict[str, int]
    percentages: dict[str, float]
    brand_url_generic_pct: float
    partial_match_pct: float
    exact_match_pct: float
    max_single_url_share_pct: float
    max_single_url: str
    warnings: list[str]
    blocking_reasons: list[str]
    guardrail_blocked: bool
    status: str  # healthy | warn | blocked

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LinkPackageOrder:
    package_id: str
    site_id: str
    market: str
    name: str
    campaign_id: str | None
    status: str
    budget_sek: float
    link_count: int
    targets: list[dict[str, Any]]
    anchor_mix: dict[str, Any]
    quality_criteria: dict[str, Any]
    drip_plan: list[dict[str, Any]]
    cost_breakdown: dict[str, Any]
    guardrail_warnings: list[str]
    guardrail_blocked: bool
    blocking_reasons: list[str]
    brief_markdown: str
    notes: str = ""
    recommended_vendors: list[dict[str, Any]] = field(default_factory=list)
    selected_vendor_id: str | None = None
    vendor_vetting_summary: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    created_by: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    no_auto_purchase_notice: str = (
        "Endast manuellt beställningsunderlag — Azom utför aldrig automatiska köp eller "
        "betalningar hos externa länkleverantörer."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "AnchorMixReport",
    "GSC_NOT_CONNECTED_NOTE",
    "KeywordCluster",
    "LINK_PACKAGE_STATUSES",
    "LinkPackageOrder",
    "LinkTargetSpec",
    "SEO_AUDIT_ID_RE",
    "SEO_BATCH_ID_RE",
    "SEO_CAMPAIGN_ID_RE",
    "SEO_FIX_ID_RE",
    "SEO_LINK_PKG_ID_RE",
    "SEO_STRATEGY_ID_RE",
    "SeoAuditReport",
    "SeoCampaign",
    "SeoFixBatch",
    "SeoFixPlan",
    "SeoIssue",
    "SeoStrategy",
    "VALID_AUDIT_SCOPES",
    "VALID_CAMPAIGN_TYPES",
    "VALID_FIX_TYPES",
    "VALID_MARKETS",
    "VALID_TARGET_TYPES",
    "canonical_sha256",
    "new_seo_id",
    "normalize_market",
    "utc_now_iso",
]
