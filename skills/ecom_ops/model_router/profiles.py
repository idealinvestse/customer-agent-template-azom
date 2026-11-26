"""Task-scoped model profiles with Oscar runtime overrides and hot-reload."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from ecom_ops.model_router.store import atomic_write_text, data_dir

_VALID_ROUTE_IDS = frozenset({"chatgpt_oauth", "xai_grok_build", "xai_api_key", "openrouter"})
_VALID_EFFORTS = frozenset({"", "none", "minimal", "low", "medium", "high", "xhigh"})
_VALID_PROMPT_REFS = frozenset(
    {"chat", "classify", "draft", "product_desc", "wp_coder", "research_planner", "research_synthesizer"}
)
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
_SECRET_WORDS = ("token", "secret", "password", "api_key", "apikey", "private_key", "auth_json")

_CACHE_KEY: tuple[Any, ...] | None = None
_CACHE_CATALOG: ProfileCatalog | None = None
_RUNTIME_ENV_SIG: tuple[str, int | None] | None = None


def _normalize_route_id(route_id: str) -> str:
    rid = (route_id or "").strip().lower()
    if rid == "openrouter_api_key":
        return "openrouter"
    return rid


def _config_dir() -> Path:
    return Path(os.environ.get("AZOM_CONFIG_DIR", "config"))


def _repo_profiles_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "model_profiles.yaml"


def _profiles_path() -> Path:
    candidate = _config_dir() / "model_profiles.yaml"
    if candidate.is_file():
        return candidate
    repo_fallback = _repo_profiles_path()
    if repo_fallback.is_file():
        return repo_fallback
    return candidate


def _override_path() -> Path:
    return data_dir() / "model_profiles.override.yaml"


def _epoch_path() -> Path:
    return data_dir() / "model_router_epoch"


def current_config_epoch() -> int:
    """Return a monotonically increasing integer epoch (never contains secrets)."""
    epoch = 0
    ep = _epoch_path()
    if ep.is_file():
        try:
            epoch = int(ep.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            epoch = 0
    rt = data_dir() / "runtime.env"
    rt_mtime = 0
    if rt.is_file():
        try:
            rt_mtime = int(rt.stat().st_mtime_ns // 1_000_000)
        except OSError:
            rt_mtime = 0
    return epoch + (rt_mtime % 1_000_000_000)


def bump_config_epoch() -> int:
    ep = _epoch_path()
    current = 0
    if ep.is_file():
        try:
            current = int(ep.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            current = 0
    nxt = current + 1
    atomic_write_text(ep, f"{nxt}\n", mode=0o600)
    return nxt


def refresh_runtime_env_if_changed() -> None:
    """Hot-reload ``runtime.env`` in long-lived processes when modified on disk."""
    global _RUNTIME_ENV_SIG
    rt = data_dir() / "runtime.env"
    try:
        mtime = rt.stat().st_mtime_ns if rt.is_file() else None
    except OSError:
        mtime = None
    sig = (str(rt), mtime)
    if sig == _RUNTIME_ENV_SIG:
        return
    _RUNTIME_ENV_SIG = sig
    if mtime is not None:
        from ecom_ops.runtime_env import apply_overlays

        apply_overlays(data_dir=data_dir())


@dataclass(frozen=True)
class MountSlot:
    slot_id: str
    route_id: str
    model_id: str = "inherit_env"
    recommended_model: str = ""
    tier: str = "balanced"
    enabled: bool = True
    provider_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileGeneration:
    max_output_tokens: int = 400
    temperature: float = 0.3
    reasoning_effort: str | None = None
    stream: bool = False
    response_format: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProfileTimeouts:
    idle_s: int = 30
    hard_s: int = 90
    phase_budget_s: int = 110


@dataclass(frozen=True)
class ProfileContextBudget:
    max_history_turns: int = 16
    max_history_chars: int = 16000
    max_assistant_history_chars: int = 2000
    max_total_input_chars: int = 24000


@dataclass(frozen=True)
class ProfileFallbackPolicy:
    allow_fallback: bool = True
    on_schema_error: str = "none"
    on_rate_limit: str = "immediate_failover"
    on_all_failed: str = "template_fallback"
    batch_mode: bool = False


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    description: str = ""
    workload_class: str = "interactive_chat"
    prompt_ref: str = "chat"
    runtime_policy_mode: str = "chat_core"
    untrusted_data_isolation: bool = False
    use_global_route_order: bool = True
    has_override: bool = False
    generation: ProfileGeneration = field(default_factory=ProfileGeneration)
    timeouts: ProfileTimeouts = field(default_factory=ProfileTimeouts)
    context_budget: ProfileContextBudget = field(default_factory=ProfileContextBudget)
    fallback_policy: ProfileFallbackPolicy = field(default_factory=ProfileFallbackPolicy)
    mounts: tuple[MountSlot, ...] = ()


@dataclass(frozen=True)
class ProfileCatalog:
    schema_version: int
    recommended_models: dict[str, list[dict[str, str]]]
    profiles: dict[str, ModelProfile]


def _builtin_raw_profiles() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "recommended_models": {},
        "profiles": {
            "cases_classify": {
                "prompt_ref": "classify",
                "runtime_policy_mode": "none",
                "untrusted_data_isolation": True,
                "generation": {"max_output_tokens": 512, "temperature": 0.0, "reasoning_effort": "low", "stream": False},
                "timeouts": {"idle_s": 15, "hard_s": 35, "phase_budget_s": 45},
                "context_budget": {"max_history_turns": 0, "max_history_chars": 0, "max_total_input_chars": 8000},
                "fallback_policy": {"allow_fallback": True, "on_schema_error": "escalate_next_slot"},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "grok-4.7-build-fast"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
            "cases_draft": {
                "prompt_ref": "draft",
                "runtime_policy_mode": "support_guardrails",
                "untrusted_data_isolation": True,
                "generation": {"max_output_tokens": 450, "temperature": 0.3, "reasoning_effort": "low", "stream": False},
                "timeouts": {"idle_s": 20, "hard_s": 60, "phase_budget_s": 75},
                "context_budget": {"max_history_turns": 0, "max_history_chars": 0, "max_total_input_chars": 12000},
                "fallback_policy": {"allow_fallback": True, "on_schema_error": "template_fallback"},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_quality", "route_id": "openrouter", "model_id": "anthropic/claude-sonnet-5.5"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
            "product_copy": {
                "prompt_ref": "product_desc",
                "runtime_policy_mode": "none",
                "generation": {"max_output_tokens": 650, "temperature": 0.3, "reasoning_effort": "low", "stream": False},
                "timeouts": {"idle_s": 25, "hard_s": 60, "phase_budget_s": 75},
                "fallback_policy": {"allow_fallback": True, "on_schema_error": "escalate_next_slot"},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_quality", "route_id": "openrouter", "model_id": "anthropic/claude-sonnet-5.5"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
            "chat_fast_tools": {
                "prompt_ref": "chat",
                "runtime_policy_mode": "chat_core",
                "generation": {"max_output_tokens": 500, "temperature": 0.15, "reasoning_effort": "low", "stream": True},
                "timeouts": {"idle_s": 20, "hard_s": 45, "phase_budget_s": 90},
                "context_budget": {"max_history_turns": 16, "max_history_chars": 16000, "max_assistant_history_chars": 2000},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "gpt-5.4-mini"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "grok-4.7-build-fast"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "grok-4.1-fast"},
                ],
            },
            "chat_general": {
                "prompt_ref": "chat",
                "runtime_policy_mode": "chat_core",
                "generation": {"max_output_tokens": 900, "temperature": 0.3, "reasoning_effort": "low", "stream": True},
                "timeouts": {"idle_s": 25, "hard_s": 90, "phase_budget_s": 110},
                "context_budget": {"max_history_turns": 16, "max_history_chars": 16000, "max_assistant_history_chars": 2000},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
            "wp_tool_router": {
                "prompt_ref": "chat",
                "runtime_policy_mode": "chat_wp_file",
                "untrusted_data_isolation": True,
                "generation": {"max_output_tokens": 800, "temperature": 0.1, "reasoning_effort": "low", "stream": False},
                "timeouts": {"idle_s": 18, "hard_s": 40, "phase_budget_s": 90},
                "context_budget": {"max_history_turns": 10, "max_history_chars": 6000, "max_assistant_history_chars": 1200},
                "mounts": [
                    {"slot_id": "primary", "route_id": "xai_grok_build", "model_id": "grok-4.7-build-fast"},
                    {"slot_id": "fallback_chatgpt", "route_id": "chatgpt_oauth", "model_id": "gpt-5.4-mini"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_or_long", "route_id": "openrouter", "model_id": "google/gemini-3.8-flash"},
                    {"slot_id": "fallback_or_coder", "route_id": "openrouter", "model_id": "qwen/qwen3-coder-next"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "grok-4.1-fast"},
                ],
            },
            "wp_code_deep": {
                "prompt_ref": "wp_coder",
                "runtime_policy_mode": "chat_wp_file",
                "untrusted_data_isolation": True,
                "generation": {"max_output_tokens": 4096, "temperature": 0.12, "reasoning_effort": "medium", "stream": True},
                "timeouts": {"idle_s": 35, "hard_s": 120, "phase_budget_s": 180},
                "context_budget": {"max_history_turns": 10, "max_history_chars": 6000, "max_assistant_history_chars": 1500},
                "mounts": [
                    {"slot_id": "primary", "route_id": "xai_grok_build", "model_id": "grok-4.7"},
                    {"slot_id": "fallback_chatgpt", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_quality", "route_id": "openrouter", "model_id": "anthropic/claude-sonnet-5.5"},
                    {"slot_id": "fallback_or_long", "route_id": "openrouter", "model_id": "google/gemini-3.8-flash"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "qwen/qwen3-coder-next"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "grok-4.7"},
                ],
            },
            "research_planner": {
                "workload_class": "batch_classify",
                "prompt_ref": "research_planner",
                "runtime_policy_mode": "none",
                "untrusted_data_isolation": True,
                "generation": {
                    "max_output_tokens": 1200,
                    "temperature": 0.1,
                    "reasoning_effort": "low",
                    "stream": False,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "research_planner_v1",
                            "strict": False,
                            "schema": {
                                "type": "object",
                                "additionalProperties": True,
                                "properties": {
                                    "sub_questions": {"type": "array", "items": {"type": "string"}},
                                    "queries": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                                    "sufficient": {"type": "boolean"},
                                    "gaps": {"type": "array", "items": {"type": "string"}},
                                    "follow_up_queries": {"type": "array", "items": {"type": "string"}},
                                    "rationale": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "timeouts": {"idle_s": 25, "hard_s": 60, "phase_budget_s": 90},
                "context_budget": {"max_history_turns": 0, "max_history_chars": 0, "max_total_input_chars": 48000},
                "fallback_policy": {
                    "allow_fallback": True,
                    "on_schema_error": "escalate_next_slot",
                    "batch_mode": True,
                },
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_or_fast", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5"},
                    {"slot_id": "fallback_or_long", "route_id": "openrouter", "model_id": "google/gemini-3.8-flash"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "grok-4.7-build-fast"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
            "research_synthesizer": {
                "workload_class": "deep_prose",
                "prompt_ref": "research_synthesizer",
                "runtime_policy_mode": "none",
                "untrusted_data_isolation": True,
                "generation": {
                    "max_output_tokens": 4096,
                    "temperature": 0.25,
                    "reasoning_effort": "medium",
                    "stream": False,
                },
                "timeouts": {"idle_s": 35, "hard_s": 120, "phase_budget_s": 180},
                "context_budget": {"max_history_turns": 0, "max_history_chars": 0, "max_total_input_chars": 80000},
                "fallback_policy": {"allow_fallback": True, "on_schema_error": "template_fallback"},
                "mounts": [
                    {"slot_id": "primary", "route_id": "chatgpt_oauth", "model_id": "inherit_env"},
                    {"slot_id": "fallback_grok_build", "route_id": "xai_grok_build", "model_id": "grok-4.3"},
                    {"slot_id": "fallback_or_quality", "route_id": "openrouter", "model_id": "anthropic/claude-sonnet-5.5"},
                    {"slot_id": "fallback_or_long", "route_id": "openrouter", "model_id": "google/gemini-3.8-flash"},
                    {"slot_id": "fallback_or_budget", "route_id": "openrouter", "model_id": "inherit_env"},
                    {"slot_id": "fallback_xai_api", "route_id": "xai_api_key", "model_id": "inherit_env"},
                ],
            },
        },
    }


def _parse_mount(raw: dict[str, Any], idx: int) -> MountSlot | None:
    if not isinstance(raw, dict):
        return None
    route_id = _normalize_route_id(str(raw.get("route_id") or ""))
    if route_id not in _VALID_ROUTE_IDS:
        return None
    model_id = str(raw.get("model_id") or "inherit_env").strip() or "inherit_env"
    if model_id != "inherit_env" and not _MODEL_ID_RE.fullmatch(model_id):
        return None
    pp = raw.get("provider_params")
    return MountSlot(
        slot_id=str(raw.get("slot_id") or f"slot_{idx}").strip() or f"slot_{idx}",
        route_id=route_id,
        model_id=model_id,
        recommended_model=str(raw.get("recommended_model") or "").strip(),
        tier=str(raw.get("tier") or "balanced").strip(),
        enabled=bool(raw.get("enabled", True)),
        provider_params=dict(pp) if isinstance(pp, dict) else {},
    )


def _parse_profile(pid: str, base_raw: dict[str, Any], ovr_raw: dict[str, Any] | None) -> ModelProfile:
    merged = dict(base_raw)
    has_override = isinstance(ovr_raw, dict) and bool(ovr_raw)
    if has_override and ovr_raw is not None:
        if ovr_raw.get("prompt_ref") in _VALID_PROMPT_REFS:
            merged["prompt_ref"] = ovr_raw["prompt_ref"]
        for section in ("generation", "timeouts", "context_budget", "fallback_policy"):
            if isinstance(ovr_raw.get(section), dict):
                cur = dict(merged.get(section) or {})
                cur.update(ovr_raw[section])
                merged[section] = cur
        if isinstance(ovr_raw.get("mounts"), list) and ovr_raw["mounts"]:
            merged["mounts"] = ovr_raw["mounts"]
        if "use_global_route_order" in ovr_raw:
            merged["use_global_route_order"] = bool(ovr_raw["use_global_route_order"])

    gen_raw = merged.get("generation") if isinstance(merged.get("generation"), dict) else {}
    rf = gen_raw.get("response_format")
    effort = gen_raw.get("reasoning_effort")
    effort_clean = str(effort).strip().lower() if effort is not None else None
    if effort_clean == "":
        effort_clean = None
    generation = ProfileGeneration(
        max_output_tokens=max(32, min(8192, int(gen_raw.get("max_output_tokens") or 400))),
        temperature=max(0.0, min(1.5, float(gen_raw.get("temperature") if gen_raw.get("temperature") is not None else 0.3))),
        reasoning_effort=effort_clean if effort_clean in _VALID_EFFORTS else None,
        stream=bool(gen_raw.get("stream", False)),
        response_format=dict(rf) if isinstance(rf, dict) else None,
    )

    to_raw = merged.get("timeouts") if isinstance(merged.get("timeouts"), dict) else {}
    timeouts = ProfileTimeouts(
        idle_s=max(3, min(300, int(to_raw.get("idle_s") or 30))),
        hard_s=max(5, min(600, int(to_raw.get("hard_s") or 90))),
        phase_budget_s=max(5, min(600, int(to_raw.get("phase_budget_s") or 110))),
    )

    cb_raw = merged.get("context_budget") if isinstance(merged.get("context_budget"), dict) else {}
    context_budget = ProfileContextBudget(
        max_history_turns=max(0, min(60, int(cb_raw.get("max_history_turns") if cb_raw.get("max_history_turns") is not None else 16))),
        max_history_chars=max(0, min(120000, int(cb_raw.get("max_history_chars") if cb_raw.get("max_history_chars") is not None else 16000))),
        max_assistant_history_chars=max(200, min(32000, int(cb_raw.get("max_assistant_history_chars") or 2000))),
        max_total_input_chars=max(1000, min(200000, int(cb_raw.get("max_total_input_chars") or 24000))),
    )

    fb_raw = merged.get("fallback_policy") if isinstance(merged.get("fallback_policy"), dict) else {}
    fallback_policy = ProfileFallbackPolicy(
        allow_fallback=bool(fb_raw.get("allow_fallback", True)),
        on_schema_error=str(fb_raw.get("on_schema_error") or "none"),
        on_rate_limit=str(fb_raw.get("on_rate_limit") or "immediate_failover"),
        on_all_failed=str(fb_raw.get("on_all_failed") or "template_fallback"),
        batch_mode=bool(fb_raw.get("batch_mode", False)),
    )

    mounts_list: list[MountSlot] = []
    for idx, item in enumerate(merged.get("mounts") or []):
        parsed = _parse_mount(item, idx)
        if parsed is not None:
            mounts_list.append(parsed)

    return ModelProfile(
        profile_id=pid,
        description=str(merged.get("description") or ""),
        workload_class=str(merged.get("workload_class") or "interactive_chat"),
        prompt_ref=str(merged.get("prompt_ref") or "chat"),
        runtime_policy_mode=str(merged.get("runtime_policy_mode") or "chat_core"),
        untrusted_data_isolation=bool(merged.get("untrusted_data_isolation", False)),
        use_global_route_order=bool(merged.get("use_global_route_order", True)),
        has_override=has_override,
        generation=generation,
        timeouts=timeouts,
        context_budget=context_budget,
        fallback_policy=fallback_policy,
        mounts=tuple(mounts_list),
    )


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def load_profiles(*, force: bool = False) -> ProfileCatalog:
    """Load base + Oscar override profiles with mtime hot-reload."""
    global _CACHE_KEY, _CACHE_CATALOG
    refresh_runtime_env_if_changed()
    base_path = _profiles_path()
    ovr_path = _override_path()
    try:
        base_mtime = base_path.stat().st_mtime_ns if base_path.is_file() else None
    except OSError:
        base_mtime = None
    try:
        ovr_mtime = ovr_path.stat().st_mtime_ns if ovr_path.is_file() else None
    except OSError:
        ovr_mtime = None
    sig = (str(base_path), base_mtime, str(ovr_path), ovr_mtime)
    if not force and _CACHE_CATALOG is not None and _CACHE_KEY == sig:
        return _CACHE_CATALOG

    base_data = _read_yaml_dict(base_path) or _builtin_raw_profiles()
    ovr_data = _read_yaml_dict(ovr_path)
    ovr_profiles = ovr_data.get("profiles") if isinstance(ovr_data.get("profiles"), dict) else {}

    rec_raw = base_data.get("recommended_models")
    recommended: dict[str, list[dict[str, str]]] = {}
    if isinstance(rec_raw, dict):
        for rid, rows in rec_raw.items():
            norm_rid = _normalize_route_id(str(rid))
            if norm_rid not in _VALID_ROUTE_IDS or not isinstance(rows, list):
                continue
            cleaned: list[dict[str, str]] = []
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    cleaned.append(
                        {
                            "id": str(row["id"]).strip(),
                            "tier": str(row.get("tier") or "balanced").strip(),
                            "note": str(row.get("note") or "").strip(),
                        }
                    )
            recommended[norm_rid] = cleaned

    raw_profiles = base_data.get("profiles") if isinstance(base_data.get("profiles"), dict) else {}
    if not raw_profiles:
        raw_profiles = _builtin_raw_profiles()["profiles"]
    profiles: dict[str, ModelProfile] = {}
    for pid, p_raw in raw_profiles.items():
        if isinstance(p_raw, dict):
            profiles[str(pid)] = _parse_profile(
                str(pid),
                p_raw,
                ovr_profiles.get(pid) if isinstance(ovr_profiles.get(pid), dict) else None,
            )
    catalog = ProfileCatalog(
        schema_version=int(base_data.get("schema_version") or 1),
        recommended_models=recommended,
        profiles=profiles,
    )
    _CACHE_KEY = sig
    _CACHE_CATALOG = catalog
    return catalog


def get_profile(profile_id: str) -> ModelProfile:
    catalog = load_profiles()
    if profile_id not in catalog.profiles:
        raise KeyError(f"Unknown model profile: {profile_id}")
    return catalog.profiles[profile_id]


def resolve_mounts_for_execution(
    profile: ModelProfile,
    *,
    route_order_list: list[str],
    route_override: str | None = None,
    model_pin: str | None = None,
) -> tuple[list[MountSlot], bool]:
    """Return ordered active ``MountSlot`` list and ``allow_fallback``."""
    pin = (model_pin or "").strip()
    if route_override:
        target = _normalize_route_id(route_override)
        matched = [m for m in profile.mounts if m.enabled and m.route_id == target]
        if not matched:
            matched = [MountSlot(slot_id="route_override", route_id=target, model_id="inherit_env")]
        if pin and target == "openrouter":
            matched = [replace(m, model_id=pin) for m in matched]
        return matched, False

    enabled = [m for m in profile.mounts if m.enabled]
    if profile.use_global_route_order and not profile.has_override:
        ordered: list[MountSlot] = []
        seen_routes: set[str] = set()
        for raw_rid in route_order_list:
            rid = _normalize_route_id(raw_rid)
            if rid in seen_routes:
                continue
            seen_routes.add(rid)
            matches = [m for m in enabled if m.route_id == rid]
            if matches:
                ordered.extend(matches)
            elif rid in _VALID_ROUTE_IDS:
                ordered.append(
                    MountSlot(slot_id=f"global_{rid}", route_id=rid, model_id="inherit_env")
                )
        enabled = ordered

    if pin:
        enabled = [
            replace(m, model_id=pin) if m.route_id == "openrouter" else m
            for m in enabled
        ]
    return enabled, profile.fallback_policy.allow_fallback


_SAFE_PROFILE_KEYS = frozenset({"max_output_tokens"})


def _assert_no_secret_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            low = str(k).lower()
            if low not in _SAFE_PROFILE_KEYS and any(word in low for word in _SECRET_WORDS):
                raise ValueError(f"Förbjudet nyckelnamn i profilkonfig: {k}")
            _assert_no_secret_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_secret_keys(item)


def save_profile_override(profile_id: str, payload: dict[str, Any]) -> ModelProfile:
    """Validate and atomically persist an Oscar override for one profile."""
    if profile_id == "isolated_test":
        raise ValueError("isolated_test kan inte ändras")
    catalog = load_profiles()
    if profile_id not in catalog.profiles:
        raise ValueError(f"Okänd modellprofil: {profile_id}")
    _assert_no_secret_keys(payload)

    clean: dict[str, Any] = {}
    if "prompt_ref" in payload and payload["prompt_ref"]:
        pref = str(payload["prompt_ref"]).strip()
        if pref not in _VALID_PROMPT_REFS:
            raise ValueError(f"Ogiltig prompt_ref: {pref}")
        clean["prompt_ref"] = pref

    if "use_global_route_order" in payload:
        clean["use_global_route_order"] = bool(payload["use_global_route_order"])

    if isinstance(payload.get("generation"), dict):
        g_in = payload["generation"]
        g_out: dict[str, Any] = {}
        if g_in.get("max_output_tokens") is not None:
            val = int(g_in["max_output_tokens"])
            if not (32 <= val <= 8192):
                raise ValueError("max_output_tokens måste vara mellan 32 och 8192")
            g_out["max_output_tokens"] = val
        if g_in.get("temperature") is not None:
            tval = float(g_in["temperature"])
            if not (0.0 <= tval <= 1.5):
                raise ValueError("temperature måste vara mellan 0.0 och 1.5")
            g_out["temperature"] = tval
        if "reasoning_effort" in g_in:
            eff = str(g_in.get("reasoning_effort") or "").strip().lower()
            if eff not in _VALID_EFFORTS:
                raise ValueError("Ogiltig reasoning_effort")
            g_out["reasoning_effort"] = eff or None
        if g_out:
            clean["generation"] = g_out

    if isinstance(payload.get("timeouts"), dict):
        t_in = payload["timeouts"]
        t_out: dict[str, int] = {}
        for key, lo, hi in (("idle_s", 3, 300), ("hard_s", 5, 600), ("phase_budget_s", 5, 600)):
            if t_in.get(key) is not None:
                v = int(t_in[key])
                if not (lo <= v <= hi):
                    raise ValueError(f"{key} måste vara mellan {lo} och {hi}")
                t_out[key] = v
        if t_out:
            clean["timeouts"] = t_out

    if isinstance(payload.get("mounts"), list):
        m_out: list[dict[str, Any]] = []
        for idx, slot in enumerate(payload["mounts"]):
            if not isinstance(slot, dict):
                continue
            rid = _normalize_route_id(str(slot.get("route_id") or ""))
            if rid not in _VALID_ROUTE_IDS:
                raise ValueError(f"Ogiltig route_id: {rid}")
            mid = str(slot.get("model_id") or "inherit_env").strip() or "inherit_env"
            if mid != "inherit_env" and not _MODEL_ID_RE.fullmatch(mid):
                raise ValueError(f"Ogiltigt modell-id: {mid}")
            sid = str(slot.get("slot_id") or f"slot_{idx}").strip() or f"slot_{idx}"
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", sid):
                raise ValueError(f"Ogiltigt slot_id: {sid}")
            m_out.append(
                {
                    "slot_id": sid,
                    "route_id": rid,
                    "model_id": mid,
                    "enabled": bool(slot.get("enabled", True)),
                }
            )
        if not m_out:
            raise ValueError("Minst en slot krävs")
        clean["mounts"] = m_out

    ovr_path = _override_path()
    existing = _read_yaml_dict(ovr_path)
    profiles_map = dict(existing.get("profiles") or {}) if isinstance(existing.get("profiles"), dict) else {}
    profiles_map[profile_id] = clean
    existing["schema_version"] = 1
    existing["profiles"] = profiles_map
    atomic_write_text(
        ovr_path,
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        mode=0o600,
    )
    bump_config_epoch()
    return load_profiles(force=True).profiles[profile_id]


def reset_profile_override(profile_id: str | None = None) -> None:
    ovr_path = _override_path()
    if profile_id is None:
        if ovr_path.is_file():
            ovr_path.unlink()
        bump_config_epoch()
        load_profiles(force=True)
        return
    if not ovr_path.is_file():
        return
    existing = _read_yaml_dict(ovr_path)
    profiles_map = dict(existing.get("profiles") or {}) if isinstance(existing.get("profiles"), dict) else {}
    profiles_map.pop(profile_id, None)
    if not profiles_map:
        ovr_path.unlink()
    else:
        existing["profiles"] = profiles_map
        atomic_write_text(
            ovr_path,
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
            mode=0o600,
        )
    bump_config_epoch()
    load_profiles(force=True)


def reset_profile_cache_for_tests() -> None:
    global _CACHE_KEY, _CACHE_CATALOG, _RUNTIME_ENV_SIG
    _CACHE_KEY = None
    _CACHE_CATALOG = None
    _RUNTIME_ENV_SIG = None
