"""Common model-route contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ecom_ops.telemetry import Telemetry


@dataclass(frozen=True)
class RouteCapabilities:
    supports_temperature: bool = True
    supports_reasoning_effort: bool = False
    supports_json_schema: bool = False
    supports_streaming: bool = True
    cost_kind: str = "estimated"
    subject_to_openrouter_cap: bool = False


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, str]]
    model: str | None = None
    max_output_tokens: int = 400
    temperature: float = 0.3
    timeout: int = 45
    site: str = "azom"
    case_id: str | None = None
    actor: str | None = None
    on_text_delta: Callable[[str], None] | None = None
    on_text_reset: Callable[[], None] | None = None
    on_phase_change: Callable[[dict[str, Any]], None] | None = None
    reasoning_effort: str | None = None
    hard_timeout: int | None = None
    profile_id: str | None = None
    slot_id: str | None = None
    response_format: dict[str, Any] | None = None
    provider_params: dict[str, Any] | None = None


def emit_phase(request: ModelRequest, info: dict[str, Any]) -> None:
    """Safely notify ``request.on_phase_change`` without ever breaking model execution."""
    callback = request.on_phase_change
    if callback is None:
        return
    try:
        callback(dict(info))
    except Exception:
        pass


@dataclass(frozen=True)
class ModelResult:
    content: str
    provider: str
    route_id: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    usage_known: bool = False
    raw_usage: dict[str, Any] | None = None
    profile_id: str | None = None
    slot_id: str | None = None
    fallback_used: bool = False
    primary_route: str = ""
    primary_error_class: str = ""
    primary_error_code: str = ""
    attempts_count: int = 1
    latency_ms: int = 0
    ttft_ms: int | None = None
    estimated_external_cost_usd: float = 0.0


class ModelRoute(Protocol):
    provider: str
    route_id: str
    capabilities: RouteCapabilities

    def is_configured(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult: ...
