"""Direct xAI API route using an operator-managed API key.

This route does not use OAuth. Direct app-owned xAI refresh is not implemented.
See ``docs/MODEL_ROUTER.md``.
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

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_XAI_MODEL = "grok-4"

# External pay-per-token xAI API rates (USD per 1M tokens: input, output).
# Never added to OpenRouter's USD cap, but tracked for transparency.
_XAI_RATES_PER_M: dict[str, tuple[float, float]] = {
    "grok-4.7": (2.00, 6.00),
    "grok-4.3": (1.25, 2.50),
    "grok-build-0.1": (1.00, 2.00),
    "grok-4.1-fast": (0.20, 0.50),
    "grok-4": (2.00, 6.00),
}


def estimate_xai_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    model: str | None = None,
) -> float:
    slug = (model or DEFAULT_XAI_MODEL).strip().lower()
    in_r, out_r = _XAI_RATES_PER_M.get(slug, (2.00, 6.00))
    raw = (prompt_tokens * in_r + completion_tokens * out_r) / 1_000_000
    return max(0.0001, raw) if (prompt_tokens or completion_tokens) else 0.0


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


class XaiApiKeyRoute:
    provider = "xai"
    route_id = "xai_api_key"
    capabilities = RouteCapabilities(
        supports_temperature=True,
        supports_reasoning_effort=False,
        supports_json_schema=True,
        supports_streaming=True,
        supports_vision=True,
        cost_kind="estimated_external",
        subject_to_openrouter_cap=False,
    )

    def _model(self) -> str:
        return (os.environ.get("XAI_MODEL") or DEFAULT_XAI_MODEL).strip()

    def is_configured(self) -> bool:
        return bool((os.environ.get("XAI_API_KEY") or "").strip())

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "connected": self.is_configured(),
            "refresh_owner": "operator",
            "app_owned_refresh": False,
            "model": self._model(),
            "cost_kind": "estimated_external",
        }

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        import time

        tel = telemetry or default_telemetry
        api_key = (os.environ.get("XAI_API_KEY") or "").strip()
        if not api_key:
            raise ModelConfigError("XAI_API_KEY is not configured")

        model = request.model or self._model()
        streaming = request.on_text_delta is not None
        try:
            formatted_messages = format_chat_completions_messages(request.messages)
        except ValueError as exc:
            raise ModelConfigError(
                "Invalid image attachment for xAI API",
                code="invalid_image",
            ) from exc
        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": streaming,
        }
        if streaming:
            payload["stream_options"] = {"include_usage": True}
        if request.response_format:
            payload["response_format"] = request.response_format
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
                XAI_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                stream=streaming,
                timeout=request.timeout,
            )
        except requests.RequestException as exc:
            raise ModelUnavailableError("xAI API endpoint unreachable") from exc

        if response.status_code == 401:
            response.close()
            raise ModelAuthError("xAI API key was rejected")
        if response.status_code == 403:
            response.close()
            raise ModelAuthError("xAI API request forbidden", code="forbidden")
        if response.status_code == 429:
            retry_after = _parse_retry_after(getattr(response, "headers", None))
            response.close()
            raise ModelRateLimitError("xAI API rate limit reached", retry_after=retry_after)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            code = "unknown_model" if status in {400, 404} else f"http_{status}"
            raise ModelUnavailableError(f"xAI API request rejected ({status})", code=code)

        try:
            if streaming:
                delta_cb = request.on_text_delta
                if request.on_phase_change is not None or hard_deadline is not None:
                    saw_delta = False
                    orig_delta = request.on_text_delta

                    def _wrapped_delta(chunk: str) -> None:
                        nonlocal saw_delta
                        if hard_deadline is not None and time.monotonic() > hard_deadline:
                            raise ModelUnavailableError("xAI API stream hard timeout", code="hard_timeout")
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
                )
            else:
                data = response.json()
                if not isinstance(data, dict):
                    raise ModelUnavailableError("xAI API response was invalid")
                choices = data.get("choices") or []
                if not choices:
                    raise ModelUnavailableError("xAI API response missing choices")
                content = str((choices[0].get("message") or {}).get("content") or "").strip()
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        except ModelUnavailableError:
            raise
        except requests.RequestException as exc:
            raise ModelUnavailableError("xAI API stream interrupted", code="stream_error") from exc
        except ValueError as exc:
            raise ModelUnavailableError("xAI API response was not JSON") from exc
        finally:
            response.close()
        if not content:
            raise ModelUnavailableError("xAI API returned no content")

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        est_external = estimate_xai_cost_usd(prompt_tokens, completion_tokens, model=model)
        tel.record(
            action="model_xai_api",
            site=request.site,
            units=total_tokens or 1,
            unit_type="tokens" if usage else "api_calls",
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
                "cost_kind": "estimated_external",
                "estimated_cost_usd": est_external,
                "refresh_owner": "operator",
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
            cost_usd=0.0,
            usage_known=bool(usage),
            raw_usage=dict(usage) if usage else None,
            estimated_external_cost_usd=est_external,
        )
