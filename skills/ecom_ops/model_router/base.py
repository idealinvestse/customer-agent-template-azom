"""Common model-route contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ecom_ops.telemetry import Telemetry


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


class ModelRoute(Protocol):
    provider: str
    route_id: str

    def is_configured(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult: ...
