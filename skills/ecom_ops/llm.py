"""OpenRouter chat helper with budget awareness."""

from __future__ import annotations

import os
from typing import Any

import requests

from ecom_ops.config import load_app_config
from ecom_ops.telemetry import Telemetry, default_telemetry

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
# Rough USD estimate when API omits cost (~$0.15/1M in + $0.60/1M out for mini).
_COST_PER_PROMPT_TOKEN = 0.15 / 1_000_000
_COST_PER_COMPLETION_TOKEN = 0.60 / 1_000_000
_MIN_COST_USD = 0.0001


def openrouter_cap_usd() -> float:
    try:
        return float(load_app_config().limits.openrouter_cap)
    except Exception:
        return float(os.environ.get("OPENROUTER_CAP_USD", "100") or 100)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    raw = (
        prompt_tokens * _COST_PER_PROMPT_TOKEN
        + completion_tokens * _COST_PER_COMPLETION_TOKEN
    )
    return max(_MIN_COST_USD, raw)


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 400,
    temperature: float = 0.3,
    timeout: int = 30,
) -> tuple[str, float]:
    """Call OpenRouter chat completions. Returns (content, cost_usd)."""
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    payload: dict[str, Any] = {
        "model": model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://azom.se",
            "X-Title": "AzomOps-Agent",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter response missing choices")
    content = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("OpenRouter empty content")
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    cost = estimate_cost_usd(prompt_tokens, completion_tokens)
    return content, cost


def draft_support_with_llm(
    *,
    customer_message: str,
    category: str,
    language: str,
    customer_name: str | None,
    order_id: str | None,
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> str | None:
    """
    Generate a support draft via OpenRouter when key + budget allow.
    Returns None to signal caller should use the template fallback.
    """
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return None
    tel = telemetry or default_telemetry
    cap = openrouter_cap_usd()
    if not tel.within_budget(cap):
        tel.record(
            action="llm_budget_skip",
            site=site,
            cost_usd=0.0,
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd()},
        )
        return None

    lang = (language or "sv").lower()
    name = customer_name or ("customer" if lang == "en" else "kunden")
    oid = order_id or "(okänt)"
    system = (
        "You write short, professional customer-support email drafts for Azom "
        "(Nordic e-commerce). Output only the email body — no subject line, "
        "no markdown fences. Sign as Azom Support. Do not invent tracking "
        "numbers, refunds, or legal promises. Keep under 180 words."
    )
    user = (
        f"Language: {lang}\n"
        f"Category: {category}\n"
        f"Customer name: {name}\n"
        f"Order id: {oid}\n"
        f"Customer message:\n{customer_message[:4000]}"
    )
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
    except Exception as exc:
        tel.record(
            action="llm_draft_error",
            site=site,
            cost_usd=0.0,
            meta={"error": str(exc)[:200], "category": category},
        )
        return None

    tel.record(
        action="llm_support_draft",
        site=site,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={"category": category, "order_id": order_id, "model": DEFAULT_MODEL},
    )
    return content
