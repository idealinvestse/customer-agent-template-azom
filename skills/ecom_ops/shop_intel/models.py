"""Data models, ID generators, constants, and NL intent patterns for Shop Intelligence."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

SHOP_INTEL_RUN_ID_RE = re.compile(r"^si-[a-f0-9]{12}$")
SHOP_INTEL_DIFF_ID_RE = re.compile(r"^sid-[a-f0-9]{12}$")
SHOP_INTEL_FINDING_ID_RE = re.compile(r"^sif-[a-f0-9]{12}$")

VALID_DOMAINS: tuple[str, ...] = (
    "platform_rules",
    "catalog_pricing",
    "theme_architecture",
    "brand_voice_copy",
    "policies_legal",
    "plugins_ecosystem",
    "security_posture",
)
VALID_DOMAINS_SET: frozenset[str] = frozenset(VALID_DOMAINS)

VALID_MODES: frozenset[str] = frozenset({"full", "quick", "security"})

VALID_PURPOSES: frozenset[str] = frozenset(
    {
        "support_draft",
        "product_copy",
        "wp_coder",
        "chat_general",
        "seo",
        "security_audit",
    }
)

VALID_SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
VALID_SEVERITIES_SET: frozenset[str] = frozenset(VALID_SEVERITIES)

# Swedish & English NL intent regex for Shop Intelligence.
# Deliberately avoids collisions with order lookup, cases/support triage,
# SEO studio commands, and Google Ads/GA4 marketing intents.
SHOP_INTEL_INTENT_RE = re.compile(
    r"\b(?:"
    r"shop[_\s-]?intel(?:ligence)?|"
    r"butiks(?:intelligens|analys|kunskap|profil|dossier|fakta|översikt|oversikt)|"
    r"store\s+(?:intel(?:ligence)?|dossier|profile|knowledge|learning)|"
    r"(?:l[äa]r|scanna|skanna|kartl[äa]gg)\s+(?:dig\s+)?(?:om\s+)?(?:butiken|shopen|webbshoppen|webbutiken|sajten)|"
    r"(?:vad\s+vet\s+du\s+om|ber[äa]tta\s+om)\s+(?:v[åa]r\s+)?(?:butik|shop|webbshop|webbutik|policy|fraktregler|betalmetoder)|"
    r"fraktregler|fraktzon(?:er)?|fri\s+frakt(?:-?gr[äa]ns)?|"
    r"returpolicy|returr[äa]tt|returregler|[åa]ngerr[äa]tt|k[öo]pvillkor|"
    r"betalningsmetoder|betalmetoder|betals[äa]tt|betalgateway(?:s)?|"
    r"shipping\s+(?:zones?|rules?|methods?)|free\s+shipping\s+threshold|payment\s+gateways?|tax\s+classes?|return\s+policy|"
    r"s[äa]kerhets(?:analys|po[äa]ng|l[äa]ge)|security\s+(?:posture|findings?|score)|"
    r"drift\s*(?:alert|check|rapport)|plugin-?landskap|tema-?arkitektur|"
    r"si-[a-f0-9]{12}|sid-[a-f0-9]{12}|sif-[a-f0-9]{12}"
    r")\b",
    re.I,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    return f"si-{uuid.uuid4().hex[:12]}"


def new_diff_id() -> str:
    return f"sid-{uuid.uuid4().hex[:12]}"


def new_finding_id() -> str:
    return f"sif-{uuid.uuid4().hex[:12]}"


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


@dataclass
class IntelFact:
    domain: str
    key: str
    value: Any
    source: str
    confidence: float = 1.0
    observed_at: str = field(default_factory=utc_now_iso)
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_hash:
            self.evidence_hash = canonical_sha256({"domain": self.domain, "key": self.key, "value": self.value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityFinding:
    finding_id: str
    run_id: str
    site_id: str
    severity: str
    category: str
    code: str
    title: str
    detail: str
    recommendation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "open"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IntelDiff:
    diff_id: str
    site_id: str
    prev_run_id: str
    curr_run_id: str
    severity: str
    domain: str
    category: str
    summary: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IntelRun:
    run_id: str
    site_id: str
    mode: str
    provenance: str
    actor: str
    domains_collected: list[str]
    status: str
    security_score: int
    findings_count: int
    diffs_count: int
    cost_usd: float = 0.0
    duration_ms: int = 0
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dossier: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IntelSchedule:
    site_id: str
    enabled: bool = False
    interval_hours: int = 24
    mode: str = "full"
    last_run_at: str | None = None
    next_run_at: str | None = None
    updated_by: str = "oscar"
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "IntelDiff",
    "IntelFact",
    "IntelRun",
    "IntelSchedule",
    "SHOP_INTEL_DIFF_ID_RE",
    "SHOP_INTEL_FINDING_ID_RE",
    "SHOP_INTEL_INTENT_RE",
    "SHOP_INTEL_RUN_ID_RE",
    "SecurityFinding",
    "VALID_DOMAINS",
    "VALID_DOMAINS_SET",
    "VALID_MODES",
    "VALID_PURPOSES",
    "VALID_SEVERITIES",
    "VALID_SEVERITIES_SET",
    "canonical_sha256",
    "new_diff_id",
    "new_finding_id",
    "new_run_id",
    "utc_now_iso",
]
