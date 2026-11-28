"""Pure domain models and policy errors for web search and deep research.

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
or ``subprocess`` imports.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

RESEARCH_ID_RE = re.compile(r"^res-[0-9a-f]{12}$")


def new_research_id() -> str:
    """Return a canonical ``res-<12hex>`` identifier."""
    return f"res-{secrets.token_hex(6)}"


class SearchPolicyError(ValueError):
    """Raised when an outbound query, URL, or research policy check blocks egress."""


class EvidenceStatus(StrEnum):
    GROUNDED = "grounded"
    INSPIRATION_ONLY = "inspiration_only"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MOCK = "mock"


class ClaimKind(StrEnum):
    FACT = "fact"
    BEST_PRACTICE = "best_practice"
    INSPIRATION = "inspiration"
    OPINION = "opinion"


class SourceType(StrEnum):
    OFFICIAL_DOCS = "official_docs"
    REGULATORY_GOV = "regulatory_gov"
    CARRIER_STATUS = "carrier_status"
    UX_RESEARCH = "ux_research"
    COMPETITOR_STORE = "competitor_store"
    TRADE_PRESS = "trade_press"
    FORUM_COMMUNITY = "forum_community"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SearchProfile:
    id: str
    description: str = ""
    preferred_providers: tuple[str, ...] = ("parallel", "exa", "brave", "tavily")
    market_defaults: tuple[str, ...] = ("SE", "en")
    freshness_days: int = 365
    corroboration_mode: str = "triangulated_factual"
    authority_domains: tuple[str, ...] = ()
    pii_strictness: str = "high"
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "relevance": 0.35,
            "authority": 0.35,
            "freshness": 0.15,
            "signal_density": 0.15,
        }
    )
    max_fetch_default: int = 3
    title: str = ""
    goal: str = ""
    ambition: str = "standard"
    combination_mode: str = "failover"
    fanout_by_depth: dict[str, int] = field(
        default_factory=lambda: {"quick": 1, "standard": 2, "deep": 3}
    )
    round_providers: dict[int, tuple[str, ...]] = field(default_factory=dict)
    fetch_strategy: str = "direct"
    output_format_default: str = "brief"
    when_to_use: str = ""
    query_templates: tuple[str, ...] = ()
    deliverable_sections: tuple[str, ...] = ()
    recommended_tools: tuple[str, ...] = (
        "web_search",
        "web_fetch_extract",
        "web_research_start",
    )
    skill_doc: str = "skills/research-profiles/SKILL.md"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title or self.id,
            "description": self.description,
            "goal": self.goal,
            "ambition": self.ambition,
            "combination_mode": self.combination_mode,
            "fanout_by_depth": dict(self.fanout_by_depth),
            "round_providers": {str(k): list(v) for k, v in self.round_providers.items()},
            "fetch_strategy": self.fetch_strategy,
            "output_format_default": self.output_format_default,
            "when_to_use": self.when_to_use,
            "query_templates": list(self.query_templates),
            "deliverable_sections": list(self.deliverable_sections),
            "recommended_tools": list(self.recommended_tools),
            "skill_doc": self.skill_doc,
            "preferred_providers": list(self.preferred_providers),
            "market_defaults": list(self.market_defaults),
            "freshness_days": self.freshness_days,
            "corroboration_mode": self.corroboration_mode,
            "authority_domains": list(self.authority_domains),
            "pii_strictness": self.pii_strictness,
            "weights": dict(self.weights),
            "max_fetch_default": self.max_fetch_default,
        }


@dataclass
class EvaluatedSource:
    source_id: str
    url: str
    canonical_url: str
    domain: str
    title: str
    snippet: str = ""
    extracted_text: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    trust_tier: str = "standard"
    published_at: str | None = None
    accessed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    provider: str = ""
    provenance: str = "mock_fixture"
    relevance_score: float = 0.0
    authority_score: float = 0.0
    freshness_score: float = 0.0
    signal_density: float = 0.0
    score: float = 0.0
    corroborated_by: tuple[str, ...] = ()
    provider_count: int = 1
    rrf_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_type"] = str(self.source_type)
        data["corroborated_by"] = list(self.corroborated_by)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvaluatedSource:
        st_raw = str(raw.get("source_type") or SourceType.UNKNOWN)
        try:
            source_type = SourceType(st_raw)
        except ValueError:
            source_type = SourceType.UNKNOWN
        corr_raw = raw.get("corroborated_by") or ()
        corroborated_by = (
            tuple(str(p).strip().lower() for p in corr_raw if str(p or "").strip())
            if isinstance(corr_raw, (list, tuple))
            else ()
        )
        if not corroborated_by and raw.get("provider"):
            corroborated_by = tuple(
                p.strip().lower() for p in str(raw.get("provider") or "").split("+") if p.strip()
            )
        provider_count = max(
            1,
            int(raw.get("provider_count") or (len(corroborated_by) if corroborated_by else 1)),
        )
        return cls(
            source_id=str(raw.get("source_id") or ""),
            url=str(raw.get("url") or ""),
            canonical_url=str(raw.get("canonical_url") or raw.get("url") or ""),
            domain=str(raw.get("domain") or ""),
            title=str(raw.get("title") or ""),
            snippet=str(raw.get("snippet") or ""),
            extracted_text=str(raw.get("extracted_text") or ""),
            source_type=source_type,
            trust_tier=str(raw.get("trust_tier") or "standard"),
            published_at=raw.get("published_at"),
            accessed_at=str(raw.get("accessed_at") or ""),
            provider=str(raw.get("provider") or ""),
            provenance=str(raw.get("provenance") or "mock_fixture"),
            relevance_score=float(raw.get("relevance_score") or 0.0),
            authority_score=float(raw.get("authority_score") or 0.0),
            freshness_score=float(raw.get("freshness_score") or 0.0),
            signal_density=float(raw.get("signal_density") or 0.0),
            score=float(raw.get("score") or 0.0),
            corroborated_by=corroborated_by,
            provider_count=provider_count,
            rrf_score=float(raw.get("rrf_score") or 0.0),
        )


@dataclass
class Citation:
    id: str
    title: str
    url: str
    domain: str
    published_at: str | None = None
    accessed_at: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    trust_tier: str = "standard"
    score: float = 0.0
    claim_kind: ClaimKind = ClaimKind.FACT

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_type"] = str(self.source_type)
        data["claim_kind"] = str(self.claim_kind)
        return data


@dataclass
class ConflictNote:
    topic: str
    summary: str
    source_ids: list[str] = field(default_factory=list)
    severity: str = "minor"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchSpec:
    topic: str
    profile_id: str = "deep_general"
    depth: str = "standard"
    markets: list[str] = field(default_factory=list)
    locale: str = "sv"
    country: str = "SE"
    focus_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    target_ref: str | None = None
    output_format: str = "brief"
    queries_seed: list[str] = field(default_factory=list)
    max_rounds: int | None = None
    max_queries: int | None = None
    max_fetches: int | None = None
    max_wall_s: int | None = None
    max_llm_calls: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchSpec:
        return cls(
            topic=str(raw.get("topic") or ""),
            profile_id=str(raw.get("profile_id") or "deep_general"),
            depth=str(raw.get("depth") or "standard"),
            markets=[str(m) for m in (raw.get("markets") or [])],
            locale=str(raw.get("locale") or "sv"),
            country=str(raw.get("country") or "SE"),
            focus_domains=[str(d) for d in (raw.get("focus_domains") or [])],
            exclude_domains=[str(d) for d in (raw.get("exclude_domains") or [])],
            target_ref=raw.get("target_ref"),
            output_format=str(raw.get("output_format") or "brief"),
            queries_seed=[str(q) for q in (raw.get("queries_seed") or [])],
            max_rounds=raw.get("max_rounds"),
            max_queries=raw.get("max_queries"),
            max_fetches=raw.get("max_fetches"),
            max_wall_s=raw.get("max_wall_s"),
            max_llm_calls=raw.get("max_llm_calls"),
        )


@dataclass
class ResearchBrief:
    research_id: str
    topic: str
    profile_id: str
    evidence_status: EvidenceStatus = EvidenceStatus.INSUFFICIENT
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    conflicts: list[ConflictNote] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    markdown: str = ""
    provenance: str = "mock_fixture"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "topic": self.topic,
            "profile_id": self.profile_id,
            "evidence_status": str(self.evidence_status),
            "summary": self.summary,
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "gaps": list(self.gaps),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "citations": [c.to_dict() for c in self.citations],
            "markdown": self.markdown,
            "provenance": self.provenance,
            "created_at": self.created_at,
        }


@dataclass
class ResearchJob:
    research_id: str
    actor: str
    status: str = "queued"
    spec: ResearchSpec | None = None
    target_ref: str | None = None
    visibility: str = "private"
    evidence_status: EvidenceStatus | None = None
    pinned: bool = False
    cancel_requested: bool = False
    delivery_state: str = "pending"
    channel: str = ""
    peer_id: str = ""
    status_message_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    current_round: int = 0
    queries_used: int = 0
    fetches_used: int = 0
    llm_calls_used: int = 0
    estimated_search_usd: float = 0.0
    error_class: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "actor": self.actor,
            "status": self.status,
            "spec": self.spec.to_dict() if self.spec is not None else None,
            "target_ref": self.target_ref,
            "visibility": self.visibility,
            "evidence_status": str(self.evidence_status) if self.evidence_status is not None else None,
            "pinned": self.pinned,
            "cancel_requested": self.cancel_requested,
            "delivery_state": self.delivery_state,
            "channel": self.channel,
            "peer_id": self.peer_id,
            "status_message_id": self.status_message_id,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "current_round": self.current_round,
            "queries_used": self.queries_used,
            "fetches_used": self.fetches_used,
            "llm_calls_used": self.llm_calls_used,
            "estimated_search_usd": self.estimated_search_usd,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


__all__ = [
    "RESEARCH_ID_RE",
    "Citation",
    "ClaimKind",
    "ConflictNote",
    "EvaluatedSource",
    "EvidenceStatus",
    "ResearchBrief",
    "ResearchJob",
    "ResearchSpec",
    "SearchPolicyError",
    "SearchProfile",
    "SourceType",
    "new_research_id",
]
