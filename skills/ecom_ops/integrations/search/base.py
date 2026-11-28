"""Common dataclasses and Protocol contract for web search routes and fetching."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable

from ecom_ops.research.firewall import QueryTaintContext


@dataclass(frozen=True)
class SearchCapabilities:
    """Declares optional query/response capabilities supported by a search route."""

    supports_country: bool = False
    supports_language: bool = False
    supports_freshness: bool = False
    supports_domain_filter: bool = False
    supports_content: bool = False
    supports_extract: bool = False
    supports_deep_mode: bool = False


def _normalize_domains(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        items = [part.strip().lower().lstrip(".") for part in raw.split(",")]
        return tuple(item for item in items if item)
    return tuple(
        d for d in (str(item or "").strip().lower().lstrip(".") for item in raw) if d
    )


@dataclass(frozen=True)
class SearchRequest:
    """Normalized request passed through ``SearchRouter`` to a search provider."""

    query: str
    objective: str | None = None
    profile_id: str = "deep_general"
    country: str | None = None
    language: str | None = None
    freshness_days: int | None = None
    max_results: int = 8
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    include_content: bool = False
    mode: str = "quick"
    taint: QueryTaintContext | None = None
    actor: str | None = None
    site: str = "azom"
    case_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "include_domains", _normalize_domains(self.include_domains))
        object.__setattr__(self, "exclude_domains", _normalize_domains(self.exclude_domains))
        clamped_max = max(1, min(20, int(self.max_results)))
        object.__setattr__(self, "max_results", clamped_max)
        norm_mode = str(self.mode or "quick").strip().lower()
        if norm_mode not in {"quick", "deep"}:
            norm_mode = "quick"
        object.__setattr__(self, "mode", norm_mode)
        if self.freshness_days is not None:
            object.__setattr__(self, "freshness_days", max(1, int(self.freshness_days)))


@dataclass(frozen=True)
class SearchHit:
    """Single normalized web search result."""

    url: str
    title: str
    snippet: str
    published_at: str | None = None
    provider: str = ""
    score: float = 0.0
    author: str | None = None
    raw_excerpts: tuple[str, ...] = ()
    content: str | None = None
    corroborated_by: tuple[str, ...] = ()
    provider_count: int = 1
    rrf_score: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.raw_excerpts, tuple):
            object.__setattr__(
                self,
                "raw_excerpts",
                tuple(str(x) for x in (self.raw_excerpts or ()) if x),
            )
        if not isinstance(self.corroborated_by, tuple):
            object.__setattr__(
                self,
                "corroborated_by",
                tuple(
                    str(x).strip()
                    for x in (self.corroborated_by or ())
                    if str(x or "").strip()
                ),
            )
        if not self.corroborated_by and self.provider:
            parts = tuple(p.strip() for p in str(self.provider).split("+") if p.strip())
            if parts:
                object.__setattr__(self, "corroborated_by", parts)
        count = max(int(self.provider_count or 1), len(self.corroborated_by) or 1)
        object.__setattr__(self, "provider_count", count)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["raw_excerpts"] = list(self.raw_excerpts)
        data["corroborated_by"] = list(self.corroborated_by)
        return data


@dataclass(frozen=True)
class SearchResult:
    """Normalized result returned by ``SearchRoute`` and ``SearchRouter``."""

    ok: bool
    route_id: str
    provider: str
    hits: tuple[SearchHit, ...] | list[SearchHit]
    provenance: str
    latency_ms: int
    cached: bool = False
    error_class: str | None = None
    error_code: str | None = None
    estimated_cost_usd: float = 0.0
    providers_used: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.hits, tuple):
            object.__setattr__(self, "hits", tuple(self.hits or ()))
        if not isinstance(self.providers_used, tuple):
            object.__setattr__(
                self,
                "providers_used",
                tuple(
                    str(p).strip()
                    for p in (self.providers_used or ())
                    if str(p or "").strip()
                ),
            )
        if not self.providers_used and self.provider:
            parts = tuple(p.strip() for p in str(self.provider).split("+") if p.strip())
            if parts:
                object.__setattr__(self, "providers_used", parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "route_id": self.route_id,
            "provider": self.provider,
            "providers_used": list(self.providers_used),
            "hits": [h.to_dict() for h in self.hits],
            "provenance": self.provenance,
            "latency_ms": self.latency_ms,
            "cached": self.cached,
            "error_class": self.error_class,
            "error_code": self.error_code,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class FetchResult:
    """Normalized result from ``SafePublicHttpClient`` or provider extract."""

    ok: bool
    url: str
    final_url: str
    title: str
    text: str
    content_type: str
    status_code: int
    latency_ms: int
    truncated: bool = False
    error_class: str | None = None
    error_code: str | None = None
    provider: str = "http"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class SearchRoute(Protocol):
    """Protocol implemented by all search provider adapters."""

    route_id: str
    capabilities: SearchCapabilities

    def is_configured(self) -> bool:
        """Return True if the provider has the required config/credentials."""

    def search(self, request: SearchRequest) -> SearchResult:
        """Execute a search request or raise ``SearchRouteError`` / ``SearchPolicyError``."""

    def status(self) -> dict[str, Any]:
        """Return safe non-secret status metadata for diagnostics."""


__all__ = [
    "FetchResult",
    "SearchCapabilities",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "SearchRoute",
]
