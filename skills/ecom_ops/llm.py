"""OpenRouter chat helper with budget awareness."""

from __future__ import annotations

import os
import re
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


def classify_support_with_llm(
    *,
    customer_message: str,
    language: str = "sv",
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> tuple[str, float] | None:
    """
    Classify a support message via OpenRouter.
    Returns (category_value, confidence) or None to use keyword fallback.
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
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd(), "kind": "classify"},
        )
        return None

    allowed = (
        "order_status,shipping,product,return,billing,technical,abuse,other"
    )
    system = (
        "You classify Nordic e-commerce support emails for Azom. "
        "Reply with ONLY a single JSON object, no markdown: "
        '{"category":"<one of: '
        + allowed
        + '>", "confidence": <float 0..1>}. '
        "Use abuse for legal threats, chargebacks, self-harm, or violence. "
        "Prefer order_status when the customer asks where an order is."
    )
    user = (
        f"Language hint: {(language or 'sv').lower()}\n"
        f"Customer message:\n{customer_message[:4000]}"
    )
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=80,
            temperature=0.0,
        )
    except Exception as exc:
        tel.record(
            action="llm_classify_error",
            site=site,
            cost_usd=0.0,
            meta={"error": str(exc)[:200]},
        )
        return None

    parsed = _parse_classify_json(content)
    if not parsed:
        tel.record(
            action="llm_classify_error",
            site=site,
            cost_usd=0.0,
            meta={"error": "unparseable classify", "excerpt": content[:120]},
        )
        return None

    category, confidence = parsed
    tel.record(
        action="llm_support_classify",
        site=site,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={"category": category, "confidence": confidence, "model": DEFAULT_MODEL},
    )
    return category, confidence


def _parse_classify_json(content: str) -> tuple[str, float] | None:
    text = (content or "").strip()
    if not text:
        return None
    # Strip optional fences
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        import json

        data = json.loads(text)
    except Exception:
        # Best-effort extract
        cat_m = re.search(
            r'"category"\s*:\s*"([a-z_]+)"', text, re.I
        )
        conf_m = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', text, re.I)
        if not cat_m or not conf_m:
            return None
        category = cat_m.group(1).lower()
        confidence = float(conf_m.group(1))
        return category, max(0.0, min(1.0, confidence))
    if not isinstance(data, dict):
        return None
    category = str(data.get("category") or "").strip().lower()
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not category:
        return None
    return category, max(0.0, min(1.0, confidence))


def draft_support_with_llm(
    *,
    customer_message: str,
    category: str,
    language: str,
    customer_name: str | None,
    order_id: str | None,
    order_context: str | None = None,
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> str | None:
    """
    Generate a support draft via OpenRouter when key + budget allow.
    Returns None to signal caller should use the template fallback.

    When ``order_context`` is provided (Woo status/total/currency), include it
    in the prompt so the model can reference real order data — never invent
    tracking numbers.
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
        "numbers, refunds, or legal promises. Use only order facts from the "
        "provided order context when present. Keep under 180 words."
    )
    ctx = (order_context or "").strip()
    context_block = f"Order context:\n{ctx}\n" if ctx else "Order context: (none)\n"
    user = (
        f"Language: {lang}\n"
        f"Category: {category}\n"
        f"Customer name: {name}\n"
        f"Order id: {oid}\n"
        f"{context_block}"
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


def _parse_short_long(content: str) -> tuple[str, str] | None:
    """Parse SHORT:/LONG: blocks from model output."""
    text = (content or "").strip()
    if not text:
        return None
    short = ""
    long = ""
    short_m = re.search(r"(?is)\bSHORT\s*:\s*(.*?)(?=\bLONG\s*:|$)", text)
    long_m = re.search(r"(?is)\bLONG\s*:\s*(.*)\Z", text)
    if short_m:
        short = short_m.group(1).strip()
    if long_m:
        long = long_m.group(1).strip()
    if not short and not long:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paras:
            return None
        short = paras[0][:500]
        long = text if len(paras) == 1 else "\n\n".join(paras[1:])
    if not short:
        short = long[:200] if long else text[:200]
    if not long:
        long = f"<p>{short}</p>"
    return short, long


def generate_product_desc_with_llm(
    *,
    name: str,
    features: str,
    language: str,
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> tuple[str, str] | None:
    """
    Generate short+long product copy via OpenRouter when key + budget allow.
    Returns None so the caller can fall back to the template generator.
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
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd(), "kind": "product_desc"},
        )
        return None

    lang = (language or "sv").lower()
    system = (
        "You write WooCommerce product copy for Azom (Nordic e-commerce). "
        "Respond with exactly two labeled blocks:\n"
        "SHORT: <one-line short_description, max ~120 chars>\n"
        "LONG: <HTML long description with <p> and optional <ul><li>>\n"
        "No markdown fences. Do not invent certifications or warranty claims."
    )
    user = (
        f"Language: {lang}\n"
        f"Product name: {name}\n"
        f"Features: {features or 'premium quality'}"
    )
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=600,
        )
    except Exception as exc:
        tel.record(
            action="llm_draft_error",
            site=site,
            cost_usd=0.0,
            meta={"error": str(exc)[:200], "kind": "product_desc"},
        )
        return None

    parsed = _parse_short_long(content)
    if not parsed:
        tel.record(
            action="llm_draft_error",
            site=site,
            cost_usd=0.0,
            meta={"error": "unparseable product desc", "kind": "product_desc"},
        )
        return None

    tel.record(
        action="llm_product_desc",
        site=site,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={"product_name": name[:80], "model": DEFAULT_MODEL},
    )
    return parsed
