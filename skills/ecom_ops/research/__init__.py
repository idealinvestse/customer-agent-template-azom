"""Universal web search and async deep research package (Wave 0 leaf exports)."""

from __future__ import annotations

from ecom_ops.research.budget import (
    can_run_fetch,
    can_run_search,
    research_budget_status,
)
from ecom_ops.research.config import (
    DepthTierConfig,
    ResearchBudgetsConfig,
    ResearchCacheConfig,
    ResearchConfig,
    ResearchEgressConfig,
    ResearchRetentionConfig,
    clear_research_config_cache,
    effective_allowlist_domains,
    effective_route_order,
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    research_enabled,
    web_fetch_enabled,
    web_search_enabled,
)
from ecom_ops.research.firewall import (
    OutboundQueryFirewall,
    QueryTaintContext,
)
from ecom_ops.research.models import (
    RESEARCH_ID_RE,
    Citation,
    ClaimKind,
    ConflictNote,
    EvaluatedSource,
    EvidenceStatus,
    ResearchBrief,
    ResearchJob,
    ResearchSpec,
    SearchPolicyError,
    SearchProfile,
    SourceType,
    new_research_id,
)
from ecom_ops.research.profiles import get_profile, list_profiles
from ecom_ops.research.sanitizer import (
    frame_untrusted_web_data,
    sanitize_untrusted_text,
)

__all__ = [
    "RESEARCH_ID_RE",
    "Citation",
    "ClaimKind",
    "ConflictNote",
    "DepthTierConfig",
    "EvaluatedSource",
    "EvidenceStatus",
    "OutboundQueryFirewall",
    "QueryTaintContext",
    "ResearchBrief",
    "ResearchBudgetsConfig",
    "ResearchCacheConfig",
    "ResearchConfig",
    "ResearchEgressConfig",
    "ResearchJob",
    "ResearchRetentionConfig",
    "ResearchSpec",
    "SearchPolicyError",
    "SearchProfile",
    "SourceType",
    "can_run_fetch",
    "can_run_search",
    "clear_research_config_cache",
    "effective_allowlist_domains",
    "effective_route_order",
    "frame_untrusted_web_data",
    "get_profile",
    "is_mock_mode",
    "kill_switch_active",
    "list_profiles",
    "load_research_config",
    "new_research_id",
    "research_budget_status",
    "research_enabled",
    "sanitize_untrusted_text",
    "web_fetch_enabled",
    "web_search_enabled",
]
