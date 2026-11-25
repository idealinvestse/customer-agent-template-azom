"""Provider router with explicit route order and no silent success."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.base import emit_phase as _emit_phase
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelRouteError,
    ModelUnavailableError,
)
from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute
from ecom_ops.model_router.openrouter import OpenRouterRoute
from ecom_ops.model_router.store import data_dir
from ecom_ops.model_router.xai_api_key import XaiApiKeyRoute
from ecom_ops.model_router.xai_grok_build import XaiGrokBuildRoute
from ecom_ops.telemetry import Telemetry, default_telemetry

DEFAULT_ROUTE_ORDER = "chatgpt_oauth,xai_grok_build,xai_api_key,openrouter"
ROUTE_IDS = ("chatgpt_oauth", "xai_grok_build", "xai_api_key", "openrouter")
TEST_PROMPT = "Reply with the single word PONG. Do not call tools."

_SAFE_TEST_KEYS = ("route_id", "ok", "error_class", "error_code", "model", "provider", "at")


def route_order() -> list[str]:
    raw = (os.environ.get("AZOM_MODEL_ROUTE_ORDER") or DEFAULT_ROUTE_ORDER).strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def last_test_path():
    return data_dir() / "model_router_status.json"


def load_last_test() -> dict[str, Any]:
    path = last_test_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: raw.get(key) for key in _SAFE_TEST_KEYS if key in raw}


def save_last_test(**fields: Any) -> dict[str, Any]:
    payload = {key: fields.get(key) for key in _SAFE_TEST_KEYS}
    payload["at"] = datetime.now(UTC).isoformat()
    path = last_test_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


class ModelRouter:
    def __init__(self) -> None:
        self.chatgpt = OpenAIChatGPTOAuthRoute()
        self.grok_build = XaiGrokBuildRoute()
        self.xai_api = XaiApiKeyRoute()
        self.openrouter = OpenRouterRoute()

    def ordered_route_ids(self) -> list[str]:
        return route_order()

    def _route(self, route_id: str):
        if route_id == "chatgpt_oauth":
            return self.chatgpt
        if route_id == "xai_grok_build":
            return self.grok_build
        if route_id == "xai_api_key":
            return self.xai_api
        if route_id == "openrouter":
            return self.openrouter
        raise ModelConfigError(f"Unknown model route: {route_id}")

    def routes(self) -> dict[str, Any]:
        return {route_id: self._route(route_id).status() for route_id in ROUTE_IDS}

    def any_configured(self) -> bool:
        return any(self._route(route_id).is_configured() for route_id in ROUTE_IDS)

    def has_route_outside_openrouter_budget(self) -> bool:
        """True when a configured route does not spend the OpenRouter dollar cap."""
        for route_id in self.ordered_route_ids():
            if route_id == "openrouter":
                continue
            try:
                route = self._route(route_id)
            except ModelConfigError:
                continue
            if route.is_configured():
                return True
        return False

    def preferred_route_id(self) -> str:
        for route_id in self.ordered_route_ids():
            try:
                route = self._route(route_id)
            except ModelConfigError:
                continue
            if route.is_configured():
                return route_id
        return ""

    def _next_configured_route(self, remaining: list[str]) -> str:
        for candidate in remaining:
            try:
                if self._route(candidate).is_configured():
                    return candidate
            except ModelConfigError:
                continue
        return ""

    def complete_route(
        self,
        route_id: str,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        """Call one route. Never falls through to another provider."""
        route = self._route(route_id)
        if not route.is_configured():
            raise ModelConfigError(f"{route_id} is not configured")
        _emit_phase(request, {"phase": "starting", "route_id": route_id})
        result = route.complete(request, telemetry=telemetry)
        _emit_phase(
            request,
            {"phase": "completed", "route_id": result.route_id, "model": result.model},
        )
        return result

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        tel = telemetry or default_telemetry
        errors: list[str] = []
        ordered = self.ordered_route_ids()
        for idx, route_id in enumerate(ordered):
            try:
                route = self._route(route_id)
            except ModelConfigError:
                errors.append(f"{route_id}: unknown")
                tel.record(
                    action="model_route_error",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={"route_id": route_id, "error_class": "ModelConfigError", "error_code": "unknown"},
                )
                continue
            if not route.is_configured():
                errors.append(f"{route_id}: not_configured")
                continue
            _emit_phase(request, {"phase": "starting", "route_id": route_id})
            try:
                result = route.complete(request, telemetry=tel)
            except ModelRateLimitError as exc:
                next_route_id = self._next_configured_route(ordered[idx + 1 :])
                _emit_phase(
                    request,
                    {
                        "phase": "route_switch",
                        "from_route": route_id,
                        "error_class": type(exc).__name__,
                        "error_code": getattr(exc, "code", ""),
                        "to_route": next_route_id,
                    },
                )
                self._reset_partial_stream(request)
                errors.append(f"{route_id}: ModelRateLimitError")
                tel.record(
                    action="model_route_rate_limited",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={
                        "route_id": route_id,
                        "error_class": "ModelRateLimitError",
                        "error_code": exc.code,
                    },
                )
                continue
            except (ModelAuthError, ModelConfigError, ModelUnavailableError) as exc:
                next_route_id = self._next_configured_route(ordered[idx + 1 :])
                _emit_phase(
                    request,
                    {
                        "phase": "route_switch",
                        "from_route": route_id,
                        "error_class": type(exc).__name__,
                        "error_code": getattr(exc, "code", ""),
                        "to_route": next_route_id,
                    },
                )
                self._reset_partial_stream(request)
                errors.append(f"{route_id}: {type(exc).__name__}")
                tel.record(
                    action="model_route_error",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={
                        "route_id": route_id,
                        "error_class": type(exc).__name__,
                        "error_code": exc.code,
                    },
                )
                continue
            _emit_phase(
                request,
                {"phase": "completed", "route_id": result.route_id, "model": result.model},
            )
            tel.record(
                action="model_route_selected",
                site=request.site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=request.case_id,
                actor=request.actor,
                meta={
                    "selected_route": route_id,
                    "route_id": result.route_id,
                    "provider": result.provider,
                    "model": result.model,
                    "usage_known": result.usage_known,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "total_tokens": result.total_tokens,
                },
            )
            return result
        raise ModelRouteError("No configured model route succeeded: " + "; ".join(errors))

    @staticmethod
    def _reset_partial_stream(request: ModelRequest) -> None:
        if request.on_text_reset:
            try:
                request.on_text_reset()
            except Exception:
                pass


_router: ModelRouter | None = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_model_router() -> None:
    global _router
    _router = None
