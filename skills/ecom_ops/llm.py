"""LLM helpers backed by the Azom model router.

Public functions stay compatible with existing callers. OpenRouter is one
route, not a hard requirement. Availability is decided by the router.
"""

from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Callable
from typing import Any

from ecom_ops.model_router import get_model_router
from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.errors import ModelRouteError
from ecom_ops.model_router.openrouter import (
    DEFAULT_MODEL,
    OPENROUTER_URL,
    estimate_cost_usd,
    openrouter_cap_usd,
)
from ecom_ops.telemetry import Telemetry, default_telemetry

__all__ = [
    "DEFAULT_MODEL",
    "OPENROUTER_URL",
    "chat_completion",
    "classify_support_with_llm",
    "draft_support_with_llm",
    "estimate_cost_usd",
    "generate_product_desc_with_llm",
    "get_last_result",
    "openrouter_cap_usd",
]


_served_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "azom_llm_served_model",
    default="",
)
_last_result: contextvars.ContextVar[ModelResult | None] = contextvars.ContextVar(
    "azom_llm_last_result",
    default=None,
)


def get_last_result() -> ModelResult | None:
    """Return the full ``ModelResult`` from the most recent ``chat_completion``."""
    return _last_result.get()


def _safe_error(exc: BaseException) -> str:
    code = getattr(exc, "code", "") or ""
    if code:
        return f"{type(exc).__name__}:{code}"[:80]
    return type(exc).__name__


def _complete_request(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    timeout: int | None = None,
    telemetry: Telemetry | None = None,
    site: str = "azom",
    case_id: str | None = None,
    actor: str | None = None,
    route_id: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_text_reset: Callable[[], None] | None = None,
    on_phase_change: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort: str | None = None,
    hard_timeout: int | None = None,
    profile_id: str | None = None,
    response_format: dict[str, Any] | None = None,
    batch_mode: bool = False,
    model_pin: str | None = None,
    validator: Callable[[str], Any] | None = None,
) -> ModelResult:
    """Run one router completion and return the full result, including model."""
    router = get_model_router()
    use_profile = bool(profile_id and hasattr(router, "complete_profile"))
    resolved_max_tokens = (
        max_tokens if use_profile else (400 if max_tokens is None else max_tokens)
    )
    resolved_timeout = (
        (45 if timeout is None else timeout)
        if use_profile
        else (30 if timeout is None else timeout)
    )
    request = ModelRequest(
        messages=messages,
        model=model,
        max_output_tokens=resolved_max_tokens,  # type: ignore[arg-type]
        temperature=temperature,
        timeout=resolved_timeout,
        site=site,
        case_id=case_id,
        actor=actor,
        on_text_delta=on_text_delta,
        on_text_reset=on_text_reset,
        on_phase_change=on_phase_change,
        reasoning_effort=reasoning_effort,
        hard_timeout=hard_timeout,
        profile_id=profile_id,
        response_format=response_format,
    )
    if use_profile:
        return router.complete_profile(
            profile_id,  # type: ignore[arg-type]
            request,
            telemetry=telemetry,
            route_override=route_id,
            model_pin=model_pin,
            batch_mode=batch_mode,
            validator=validator,
        )
    if route_id:
        return router.complete_route(route_id, request, telemetry=telemetry)
    return router.complete(request, telemetry=telemetry)


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    timeout: int | None = None,
    telemetry: Telemetry | None = None,
    site: str = "azom",
    case_id: str | None = None,
    actor: str | None = None,
    route_id: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_text_reset: Callable[[], None] | None = None,
    on_phase_change: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort: str | None = None,
    hard_timeout: int | None = None,
    profile_id: str | None = None,
    response_format: dict[str, Any] | None = None,
    batch_mode: bool = False,
    model_pin: str | None = None,
    validator: Callable[[str], Any] | None = None,
) -> tuple[str, float]:
    """Run a chat completion through the configured model router."""
    result = _complete_request(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        telemetry=telemetry,
        site=site,
        case_id=case_id,
        actor=actor,
        route_id=route_id,
        on_text_delta=on_text_delta,
        on_text_reset=on_text_reset,
        on_phase_change=on_phase_change,
        reasoning_effort=reasoning_effort,
        hard_timeout=hard_timeout,
        profile_id=profile_id,
        response_format=response_format,
        batch_mode=batch_mode,
        model_pin=model_pin,
        validator=validator,
    )
    _last_result.set(result)
    _served_model.set(result.model or "")
    return result.content, float(result.cost_usd or 0.0)


def classify_support_with_llm(
    *,
    customer_message: str,
    language: str = "sv",
    telemetry: Telemetry | None = None,
    site: str = "azom",
    case_id: str | None = None,
    actor: str | None = None,
    batch_mode: bool = False,
) -> tuple[str, float] | None:
    """Classify a support message. Returns None so the caller can use keywords."""
    tel = telemetry or default_telemetry
    cap = openrouter_cap_usd()
    router = get_model_router()
    if not tel.within_budget(cap) and not router.has_route_outside_openrouter_budget():
        tel.record(
            action="llm_budget_skip",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=0.0,
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd(), "kind": "classify", "profile_id": "cases_classify"},
        )
        return None

    from ecom_ops.prompts import get_prompt

    system, prompt_version = get_prompt("classify")
    user = (
        f"Language hint: {(language or 'sv').lower()}\n"
        f"Customer message:\n{customer_message[:4000]}"
    )
    _served_model.set("")
    _last_result.set(None)
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=512,
            temperature=0.0,
            telemetry=tel,
            site=site,
            case_id=case_id,
            actor=actor,
            profile_id="cases_classify",
            batch_mode=batch_mode,
            validator=lambda txt: _parse_classify_json(txt) is not None,
        )
    except ModelRouteError as exc:
        tel.record(
            action="llm_classify_error",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=0.0,
            meta={"error": _safe_error(exc), "profile_id": "cases_classify"},
        )
        return None

    res = _last_result.get()
    model = _served_model.get()
    parsed = _parse_classify_json(content)
    if not parsed:
        tel.record(
            action="llm_classify_error",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=cost,
            meta={
                "error": "unparseable_classify",
                "model": model,
                "profile_id": "cases_classify",
                "route_id": res.route_id if res else "",
            },
        )
        return None

    category, confidence = parsed
    tel.record(
        action="llm_support_classify",
        site=site,
        case_id=case_id,
        actor=actor,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={
            "category": category,
            "confidence": confidence,
            "model": model,
            "route_id": res.route_id if res else "",
            "profile_id": "cases_classify",
            "fallback_used": res.fallback_used if res else False,
            "prompt_version": prompt_version,
        },
    )
    return category, confidence


def _parse_classify_json(content: str) -> tuple[str, float] | None:
    text = (content or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        cat_m = re.search(r'"category"\s*:\s*"([a-z_]+)"', text, re.I)
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
    case_id: str | None = None,
    actor: str | None = None,
    batch_mode: bool = False,
) -> str | None:
    """Generate a support draft. Returns None so the caller uses the template."""
    tel = telemetry or default_telemetry
    cap = openrouter_cap_usd()
    router = get_model_router()
    if not tel.within_budget(cap) and not router.has_route_outside_openrouter_budget():
        tel.record(
            action="llm_budget_skip",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=0.0,
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd(), "profile_id": "cases_draft"},
        )
        return None

    lang = (language or "sv").lower()
    name = customer_name or ("customer" if lang == "en" else "kunden")
    oid = order_id or "(okänt)"
    from ecom_ops.prompts import get_prompt

    system, draft_prompt_version = get_prompt("draft", language=lang)
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
    _served_model.set("")
    _last_result.set(None)
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            telemetry=tel,
            site=site,
            case_id=case_id,
            actor=actor,
            profile_id="cases_draft",
            batch_mode=batch_mode,
        )
    except ModelRouteError as exc:
        tel.record(
            action="llm_draft_error",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=0.0,
            meta={"error": _safe_error(exc), "category": category, "profile_id": "cases_draft"},
        )
        return None

    res = _last_result.get()
    tel.record(
        action="llm_support_draft",
        site=site,
        case_id=case_id,
        actor=actor,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={
            "category": category,
            "order_id": order_id,
            "model": _served_model.get(),
            "route_id": res.route_id if res else "",
            "profile_id": "cases_draft",
            "fallback_used": res.fallback_used if res else False,
            "prompt_version": draft_prompt_version,
        },
    )
    flagged = _guardrail_check(content)
    if flagged:
        tel.record(
            action="llm_draft_guardrail_block",
            site=site,
            case_id=case_id,
            actor=actor,
            cost_usd=0.0,
            meta={
                "category": category,
                "flags": flagged,
                "order_id": order_id,
                "model": _served_model.get(),
                "route_id": res.route_id if res else "",
                "profile_id": "cases_draft",
            },
        )
        return None
    return content


_GUARDRAIL_PATTERNS = [
    (
        "refund_promise",
        [
            "jag kommer att återbetala",
            "vi återbetalar hela",
            "full refund guaranteed",
            "i will refund you",
        ],
    ),
    (
        "legal_admission",
        [
            "vi medger fel",
            "we admit liability",
            "det är vårt fel i alla fall",
        ],
    ),
    ("ssn_pattern", ["19\\d{10}", "\\d{12}"]),
    ("profanity", ["fuck you", "du är en idiot", "screw you"]),
]


def _guardrail_check(text: str) -> list[str]:
    """Check draft for guardrail violations. Returns flag names."""
    flags: list[str] = []
    lower = (text or "").lower()
    for flag, patterns in _GUARDRAIL_PATTERNS:
        for pat in patterns:
            if pat.startswith("\\d") or pat.startswith("19\\d"):
                if re.search(pat, text or ""):
                    flags.append(flag)
                    break
            elif pat.lower() in lower:
                flags.append(flag)
                break
    return flags


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
    actor: str | None = None,
) -> tuple[str, str] | None:
    """Generate short and long product copy. None means use the template."""
    tel = telemetry or default_telemetry
    cap = openrouter_cap_usd()
    router = get_model_router()
    if not tel.within_budget(cap) and not router.has_route_outside_openrouter_budget():
        tel.record(
            action="llm_budget_skip",
            site=site,
            actor=actor,
            cost_usd=0.0,
            meta={"cap_usd": cap, "spent_usd": tel.sum_cost_usd(), "kind": "product_desc", "profile_id": "product_copy"},
        )
        return None

    lang = (language or "sv").lower()
    from ecom_ops.prompts import get_prompt

    system, prompt_version = get_prompt("product_desc")
    user = (
        f"Language: {lang}\n"
        f"Product name: {name}\n"
        f"Features: {features or '(none supplied)'}"
    )
    _served_model.set("")
    _last_result.set(None)
    try:
        content, cost = chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=600,
            telemetry=tel,
            site=site,
            actor=actor,
            profile_id="product_copy",
            validator=lambda txt: _parse_short_long(txt) is not None,
        )
    except ModelRouteError as exc:
        tel.record(
            action="llm_draft_error",
            site=site,
            actor=actor,
            cost_usd=0.0,
            meta={"error": _safe_error(exc), "kind": "product_desc", "prompt_version": prompt_version, "profile_id": "product_copy"},
        )
        return None

    res = _last_result.get()
    model = _served_model.get()
    parsed = _parse_short_long(content)
    if not parsed:
        tel.record(
            action="llm_draft_error",
            site=site,
            actor=actor,
            cost_usd=cost,
            meta={
                "error": "unparseable_product_desc",
                "kind": "product_desc",
                "model": model,
                "route_id": res.route_id if res else "",
                "profile_id": "product_copy",
                "prompt_version": prompt_version,
            },
        )
        return None

    tel.record(
        action="llm_product_desc",
        site=site,
        actor=actor,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={
            "product_name": name[:80],
            "model": model,
            "route_id": res.route_id if res else "",
            "profile_id": "product_copy",
            "fallback_used": res.fallback_used if res else False,
            "prompt_version": prompt_version,
        },
    )
    return parsed
