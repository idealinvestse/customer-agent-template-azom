"""OpenRouter compatibility route.

This is a fallback, not a prerequisite for OAuth routes. Dollar cost is an
estimate. The route telemetry event stores that estimate in meta and leaves
``cost_usd`` at 0 so callers can record the same estimate once against the
OpenRouter cap.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from ecom_ops.model_router.base import (
    ModelRequest,
    ModelResult,
    RouteCapabilities,
    format_chat_completions_messages,
)
from ecom_ops.model_router.base import emit_phase as _emit_phase
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.streaming import read_chat_completion_sse
from ecom_ops.telemetry import Telemetry, default_telemetry

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
_COST_PER_PROMPT_TOKEN = 0.15 / 1_000_000
_COST_PER_COMPLETION_TOKEN = 0.60 / 1_000_000
_MIN_COST_USD = 0.0001

# USD per 1M tokens (input, output) for recommended models.
_MODEL_RATES_PER_M: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-opus-5.5": (4.00, 20.00),
    "anthropic/claude-fable-5.2": (8.00, 40.00),
    "google/gemini-3.8-flash": (0.75, 3.75),
    "google/gemini-4-hydrogen": (1.50, 7.50),
    "google/gemini-4-neon": (3.00, 15.00),
    "openai/gpt-6.2-sol": (2.00, 10.00),
    "openai/gpt-6.2-astra": (8.00, 40.00),
    "openai/gpt-5.4-mini": (0.75, 4.50),
    "openai/gpt-6-luna": (0.10, 0.50),
    "x-ai/grok-4.7": (2.00, 6.00),
    "x-ai/grok-4.1-fast": (0.20, 0.50),
    "qwen/qwen3-coder-next": (0.12, 0.80),
    "qwen/qwen3-coder": (0.30, 1.00),
    "deepseek/deepseek-v4.1-flash-1017": (0.15, 0.60),
    "deepseek/deepseek-v4.1-flash": (0.15, 0.60),
}


def _model_rates(model: str | None) -> tuple[float, float]:
    slug = (model or "").strip().lower().split(":", 1)[0]
    if slug in _MODEL_RATES_PER_M:
        in_r, out_r = _MODEL_RATES_PER_M[slug]
        return in_r / 1_000_000, out_r / 1_000_000
    return _COST_PER_PROMPT_TOKEN, _COST_PER_COMPLETION_TOKEN


def estimate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    model: str | None = None,
    *,
    actual_cost: float | None = None,
) -> float:
    if isinstance(actual_cost, (int, float)) and not isinstance(actual_cost, bool) and actual_cost >= 0:
        return max(_MIN_COST_USD, float(actual_cost))
    in_rate, out_rate = _model_rates(model)
    raw = prompt_tokens * in_rate + completion_tokens * out_rate
    return max(_MIN_COST_USD, raw)


def _parse_retry_after(headers: Any) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        val = float(str(raw).strip())
        return val if val >= 0 else None
    except (TypeError, ValueError):
        return None


def openrouter_cap_usd() -> float:
    try:
        from ecom_ops.config import load_app_config

        return float(load_app_config().limits.openrouter_cap)
    except Exception:
        return float(os.environ.get("OPENROUTER_CAP_USD", "100") or 100)


class OpenRouterRoute:
    provider = "openrouter"
    route_id = "openrouter_api_key"
    capabilities = RouteCapabilities(
        supports_temperature=True,
        supports_reasoning_effort=True,
        supports_json_schema=True,
        supports_streaming=True,
        supports_vision=True,
        cost_kind="estimated",
        subject_to_openrouter_cap=True,
    )

    def __init__(self) -> None:
        pass

    def _model(self) -> str:
        return (os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()

    def is_configured(self) -> bool:
        return bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "connected": self.is_configured(),
            "refresh_owner": "operator",
            "app_owned_refresh": False,
            "model": self._model(),
            "cost_kind": "estimated",
        }

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        import time

        tel = telemetry or default_telemetry
        api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if not api_key:
            raise ModelConfigError("OPENROUTER_API_KEY is not configured")
        cap = openrouter_cap_usd()
        if not tel.within_budget(cap):
            raise ModelConfigError("OpenRouter budget cap reached", code="budget_exhausted")

        model = request.model or self._model()
        try:
            formatted_messages = format_chat_completions_messages(request.messages)
        except ValueError as exc:
            raise ModelConfigError(
                "Invalid image attachment for OpenRouter",
                code="invalid_image",
            ) from exc
        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.response_format:
            payload["response_format"] = request.response_format
        if (
            request.reasoning_effort
            and isinstance(request.provider_params, dict)
            and request.provider_params.get("send_reasoning_effort")
        ):
            payload["reasoning"] = {"effort": request.reasoning_effort}
        streaming = request.on_text_delta is not None
        if streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        _emit_phase(
            request,
            {"phase": "waiting", "route_id": self.route_id, "model": model},
        )
        started = time.monotonic()
        hard_deadline = (
            started + float(request.hard_timeout)
            if request.hard_timeout and request.hard_timeout > 0
            else None
        )
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://azom.se",
                    "X-Title": "AzomOps-Agent",
                },
                json=payload,
                stream=streaming,
                timeout=request.timeout,
            )
        except requests.RequestException as exc:
            raise ModelUnavailableError("OpenRouter endpoint unreachable") from exc

        if response.status_code == 401:
            response.close()
            raise ModelAuthError("OpenRouter API key was rejected")
        if response.status_code == 403:
            response.close()
            raise ModelAuthError("OpenRouter request forbidden", code="forbidden")
        if response.status_code == 429:
            retry_after = _parse_retry_after(getattr(response, "headers", None))
            response.close()
            raise ModelRateLimitError("OpenRouter rate limit reached", retry_after=retry_after)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            code = "unknown_model" if status in {400, 404} else f"http_{status}"
            raise ModelUnavailableError(
                f"OpenRouter request rejected ({status})",
                code=code,
            )

        try:
            response.encoding = "utf-8"
            if streaming:
                delta_cb = request.on_text_delta
                if request.on_phase_change is not None or hard_deadline is not None:
                    saw_delta = False
                    orig_delta = request.on_text_delta

                    def _wrapped_delta(chunk: str) -> None:
                        nonlocal saw_delta
                        if hard_deadline is not None and time.monotonic() > hard_deadline:
                            raise ModelUnavailableError("OpenRouter stream hard timeout", code="hard_timeout")
                        if not saw_delta:
                            saw_delta = True
                            _emit_phase(
                                request,
                                {
                                    "phase": "streaming",
                                    "route_id": self.route_id,
                                    "model": model,
                                },
                            )
                        if orig_delta is not None:
                            orig_delta(chunk)

                    delta_cb = _wrapped_delta
                content, usage = read_chat_completion_sse(
                    response.iter_lines(decode_unicode=True),
                    on_text_delta=delta_cb,
                    hard_deadline=hard_deadline,
                    timeout_error_factory=lambda: ModelUnavailableError(
                        "OpenRouter stream hard timeout", code="hard_timeout"
                    ),
                )
            else:
                data = response.json()
                if not isinstance(data, dict):
                    raise ModelUnavailableError("OpenRouter response was invalid")
                choices = data.get("choices") or []
                if not choices:
                    raise ModelUnavailableError("OpenRouter response missing choices")
                content = str((choices[0].get("message") or {}).get("content") or "").strip()
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        except ModelUnavailableError:
            raise
        except requests.RequestException as exc:
            raise ModelUnavailableError("OpenRouter stream interrupted", code="stream_error") from exc
        except ValueError as exc:
            raise ModelUnavailableError("OpenRouter response was not JSON") from exc
        finally:
            response.close()
        if not content:
            raise ModelUnavailableError("OpenRouter returned no content")

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        raw_cost = usage.get("cost")
        actual_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        cost = estimate_cost_usd(prompt_tokens, completion_tokens, model=model, actual_cost=actual_cost)
        tel.record(
            action="model_openrouter",
            site=request.site,
            units=total_tokens,
            unit_type="tokens",
            cost_usd=0.0,
            case_id=request.case_id,
            actor=request.actor,
            meta={
                "provider": self.provider,
                "route_id": self.route_id,
                "model": model,
                "usage_known": bool(usage),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_kind": "estimated",
                "estimated_cost_usd": cost,
            },
        )
        return ModelResult(
            content=content,
            provider=self.provider,
            route_id=self.route_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            usage_known=bool(usage),
            raw_usage=dict(usage) if usage else None,
        )
