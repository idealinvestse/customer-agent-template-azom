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

from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.telemetry import Telemetry, default_telemetry

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
_COST_PER_PROMPT_TOKEN = 0.15 / 1_000_000
_COST_PER_COMPLETION_TOKEN = 0.60 / 1_000_000
_MIN_COST_USD = 0.0001


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    raw = (
        prompt_tokens * _COST_PER_PROMPT_TOKEN
        + completion_tokens * _COST_PER_COMPLETION_TOKEN
    )
    return max(_MIN_COST_USD, raw)


def openrouter_cap_usd() -> float:
    try:
        from ecom_ops.config import load_app_config

        return float(load_app_config().limits.openrouter_cap)
    except Exception:
        return float(os.environ.get("OPENROUTER_CAP_USD", "100") or 100)


class OpenRouterRoute:
    provider = "openrouter"
    route_id = "openrouter_api_key"

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
        tel = telemetry or default_telemetry
        api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if not api_key:
            raise ModelConfigError("OPENROUTER_API_KEY is not configured")
        cap = openrouter_cap_usd()
        if not tel.within_budget(cap):
            raise ModelConfigError("OpenRouter budget cap reached", code="budget_exhausted")

        model = request.model or self._model()
        payload = {
            "model": model,
            "messages": request.messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
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
                timeout=request.timeout,
            )
        except requests.RequestException as exc:
            raise ModelUnavailableError("OpenRouter endpoint unreachable") from exc

        if response.status_code == 401:
            raise ModelAuthError("OpenRouter API key was rejected")
        if response.status_code == 429:
            raise ModelRateLimitError("OpenRouter rate limit reached")
        if response.status_code >= 400:
            raise ModelUnavailableError(
                f"OpenRouter request rejected ({response.status_code})"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelUnavailableError("OpenRouter response was not JSON") from exc
        if not isinstance(data, dict):
            raise ModelUnavailableError("OpenRouter response was invalid")

        choices = data.get("choices") or []
        if not choices:
            raise ModelUnavailableError("OpenRouter response missing choices")
        content = str((choices[0].get("message") or {}).get("content") or "").strip()
        if not content:
            raise ModelUnavailableError("OpenRouter returned no content")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cost = estimate_cost_usd(prompt_tokens, completion_tokens)
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
