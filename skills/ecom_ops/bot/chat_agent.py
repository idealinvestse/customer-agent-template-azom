"""Hybrid OpenClaw-style LLM chat for Telegram free text (read-only tools)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.bot.store import clamp_messages
from ecom_ops.llm import DEFAULT_MODEL, chat_completion, openrouter_cap_usd
from ecom_ops.order_context import format_order_context_block
from ecom_ops.security import validate_order_id
from ecom_ops.telemetry import Telemetry, default_telemetry

ORDER_RE = re.compile(
    r"\b(?:order|ordernr|order\s*nr|#)\s*(\d{4,12})\b"
    r"|(?:\b(?:status|kolla|hämta|visa)\b.{0,40}\b(\d{4,12})\b)"
    r"|(?:\b(\d{4,12})\b.{0,20}\b(?:order|status)\b)",
    re.I,
)
BARE_ORDER_RE = re.compile(r"^\s*(\d{4,12})\s*$")
CASE_ID_RE = re.compile(r"\b([0-9a-f]{8})\b", re.I)
CASES_INTENT_RE = re.compile(
    r"\b("
    r"ärende|ärenden|cases?|kö(?:n)?|inkorg|"
    r"öppna\s+ärenden|hur många.*ärende|"
    r"visa\s+(?:ärende|case)|lista\s+(?:ärende|case)|"
    r"triage|supportkö"
    r")\b",
    re.I,
)
SUGGEST_INTENT_RE = re.compile(
    r"\b("
    r"föreslag(?:na|et)?|suggest(?:ed)?|"
    r"vad kan jag godkänna|redo att skicka|att godkänna|"
    r"★|stjärn"
    r")\b",
    re.I,
)
# NL "godkänn abcdef01" — never auto-send; return confirm UX only
APPROVE_NL_RE = re.compile(
    r"\b(?:godkänn|approve|skicka)\b.{0,24}\b([0-9a-f]{8})\b"
    r"|\b([0-9a-f]{8})\b.{0,16}\b(?:godkänn|approve)\b",
    re.I,
)
OPS_INTENT_RE = re.compile(
    r"\b("
    r"status|hälsa|health|budget|kostnad|usage|"
    r"tasks?|uppgifter|översikt|brief|"
    r"hur mår|hur går det|läget"
    r")\b",
    re.I,
)
ESCALATE_RE = re.compile(
    r"\b(eskalera|eskale|escalate|critical)\b"
    r"|(?:till\s+oscar)",
    re.I,
)
ASSISTANT_ESCALATE_HINT_RE = re.compile(
    r"\b(eskalera|eskale|oscar|godkänn\s+eskalering)\b",
    re.I,
)

# Keep aligned with repository root SOUL.md (OpenClaw identity).
SYSTEM_PROMPT = (
    "Du är AzomOps, Jonatans/Oscars Telegram-kollega för Azom e-handel. "
    "Svara på svenska, kort och mänskligt — som i en vanlig OpenClaw-chat. "
    "Använd tool-resultaten och tool_digest när de finns; hitta aldrig på "
    "orderfakta, tracking eller refunds. Du får ALDRIG påstå att kundmail "
    "skickats — det kräver explicit /cases approve eller knappen Godkänn & skicka. "
    "Vid utskick: föreslå /cases show <id8> eller approve-knappen. "
    "Om användaren vill eskalera: säg att de kan skriva eskalera — sätt inte "
    "igång utskick själv. Håll svar under ~120 ord om det inte behövs mer detalj. "
    "Suggest-approve (★) betyder redo för human confirm — aldrig silent send."
)

FALLBACK_NO_KEY = (
    "LLM är inte tillgänglig just nu (saknar OPENROUTER_API_KEY). "
    "Använd /order, /cases, /status eller /help — "
    "eller fråga om order/ärenden så hämtar jag fakta utan LLM."
)
FALLBACK_BUDGET = (
    "OpenRouter-budgeten är slut för tillfället. "
    "Jag kan fortfarande hämta order/ärenden/status utan LLM — "
    "eller be Oscar höja cap."
)
FALLBACK_ERROR = (
    "Kunde inte nå LLM just nu. "
    "Prova igen, eller fråga om order/ärenden/status så kör jag verktygen direkt."
)

SOFT_ESCALATE_NUDGE = "Säg *eskalera* om du vill skicka till Oscar."


@dataclass
class ToolPrefetch:
    """Typed read-only tool prefetch (no __meta_* hacks)."""

    results: list[tuple[str, str]] = field(default_factory=list)
    case_id8: str | None = None
    suggest_case_ids: list[str] = field(default_factory=list)
    digest: str = ""
    approve_confirm_only: bool = False


@dataclass
class ChatResult:
    """One hybrid chat turn — text plus optional Telegram UX hints."""

    text: str
    messages: list[dict[str, str]] = field(default_factory=list)
    cost_usd: float = 0.0
    case_id8: str | None = None
    suggest_case_ids: list[str] = field(default_factory=list)
    offer_escalate: bool = False
    soft_escalate_nudge: bool = False
    tool_digest: str = ""
    approve_confirm_only: bool = False


def wants_escalate(text: str) -> bool:
    return bool(ESCALATE_RE.search(text or ""))


def wants_hard_escalate_confirm(text: str) -> bool:
    """
    Clear escalate intent → confirm buttons without a full LLM turn.
    Mixed questions (order + maybe escalate) go through chat instead.
    """
    t = (text or "").strip()
    if not wants_escalate(t):
        return False
    lower = t.lower()
    if lower.startswith(("eskalera", "escalate", "eskale")):
        return True
    return len(t) <= 36


def parse_approve_nl(text: str) -> str | None:
    """Return case id8 if user asked to approve in free text (never auto-send)."""
    m = APPROVE_NL_RE.search(text or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def _session_model(session: dict[str, Any]) -> str | None:
    pin = (session.get("model") or "").strip()
    if not pin or pin == "default":
        return None
    return pin


def _think_temperature(session: dict[str, Any]) -> float:
    think = str(session.get("think") or "default").lower()
    if think in {"high", "max", "deep"}:
        return 0.5
    if think in {"low", "min", "off"}:
        return 0.1
    return 0.35


def _max_tokens(session: dict[str, Any]) -> int:
    think = str(session.get("think") or "default").lower()
    if think in {"high", "max", "deep"}:
        return 700
    return 450


def _extract_order_id(text: str) -> str | None:
    m = ORDER_RE.search(text or "")
    if m:
        for g in m.groups():
            if g:
                return g
    m2 = BARE_ORDER_RE.match(text or "")
    return m2.group(1) if m2 else None


def tool_lookup_order(order_id: str) -> str:
    """Shared order lookup for chat tools and slash/fast-path handlers."""
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        oid = validate_order_id(order_id)
        woo = client_from_env(use_mock=None)
        order = woo.get_order(oid)
        block = format_order_context_block(order)
        return f"{block}\n(read-only – ändra via operator/CLI)"
    except Exception as exc:
        return f"Order lookup failed: {exc}"


def tool_list_cases(*, limit: int = 8, suggest_only: bool = False) -> tuple[str, list[str]]:
    """Return (text, suggest_approve id8 list)."""
    try:
        from ecom_ops.cases.service import CaseService

        cases = CaseService().list_open(limit=max(limit, 20))
        if suggest_only:
            cases = [c for c in cases if getattr(c, "suggest_approve", False)]
        cases = cases[:limit]
        if not cases:
            if suggest_only:
                return "Inga ★-föreslagna ärenden just nu.", []
            return "Inga öppna/eskalerade ärenden.", []
        suggest_ids: list[str] = []
        title = "★ Föreslagna (suggest-approve):" if suggest_only else "Öppna/eskalerade:"
        lines = [f"{title} {len(cases)}"]
        for c in cases:
            star = ""
            conf_s = ""
            if getattr(c, "suggest_approve", False):
                star = " ★"
                suggest_ids.append(c.id[:8])
                conf = getattr(c, "classify_confidence", None)
                if isinstance(conf, (int, float)):
                    conf_s = f" {conf:.0%}"
            lines.append(
                f"- {c.id[:8]} | {c.status} | {c.category}{star}{conf_s} | {c.subject[:40]}"
            )
        lines.append("Godkänn utskick: tryck knappen eller /cases approve <id8>.")
        return "\n".join(lines), suggest_ids
    except Exception as exc:
        return f"Cases list failed: {exc}", []


def tool_show_case(id_prefix: str) -> tuple[str, str | None]:
    """Return (text, case_id8 or None)."""
    try:
        from ecom_ops.bot.openclaw_commands import _format_case_show
        from ecom_ops.cases.service import CaseService

        svc = CaseService()
        case = svc.store.resolve_id_prefix(id_prefix) or svc.get(id_prefix)
        if not case:
            return f"Hittade inte case {id_prefix!r}", None
        return _format_case_show(case), case.id[:8]
    except Exception as exc:
        return f"Case show failed: {exc}", None


def _count_open_escalations() -> int:
    try:
        path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "escalations.jsonl"
        if not path.is_file():
            return 0
        open_n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if obj.get("status", "open") == "open":
                    open_n += 1
            except json.JSONDecodeError:
                pass
        return open_n
    except Exception:
        return 0


def tool_ops_snapshot() -> str:
    try:
        from ecom_ops import __version__
        from ecom_ops.cases.service import CaseService
        from ecom_ops.config import load_app_config

        cfg = load_app_config()
        cost = Telemetry().sum_cost_usd()
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
        n_cases = 0
        try:
            n_cases = len(CaseService().list_open(limit=50))
        except Exception:
            pass
        n_esc = _count_open_escalations()
        return (
            f"Version {__version__} · {'mock' if mock else 'live'}\n"
            f"Customer: {cfg.customer.customer}\n"
            f"OpenRouter: ${cost:.4f} / ${cfg.limits.openrouter_cap}\n"
            f"Öppna cases: {n_cases}\n"
            f"Öppna eskaleringar: {n_esc}"
        )
    except Exception as exc:
        return f"Ops snapshot failed: {exc}"


def _make_digest(results: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for name, body in results[:2]:
        first = (body or "").strip().splitlines()[:2]
        snippet = " | ".join(ln.strip() for ln in first if ln.strip())[:160]
        parts.append(f"{name}: {snippet}")
    return "\n".join(parts)


def gather_tool_results(user_message: str) -> ToolPrefetch:
    """Deterministic read-only tool prefetch (up to 2 tools)."""
    pref = ToolPrefetch()
    text = user_message or ""

    # NL approve → show case + confirm button only (never send)
    approve_id = parse_approve_nl(text)
    if approve_id:
        body, case_id8 = tool_show_case(approve_id)
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.approve_confirm_only = True
        pref.digest = _make_digest(pref.results)
        return pref

    oid = _extract_order_id(text)
    if oid and len(pref.results) < 2:
        pref.results.append(("lookup_order", tool_lookup_order(oid)))

    id_m = CASE_ID_RE.search(text)
    show_case = bool(
        id_m
        and (
            CASES_INTENT_RE.search(text)
            or any(k in text.lower() for k in ("show", "visa", "detalj", "case "))
            or re.match(r"^\s*[0-9a-f]{8}\s*$", text, re.I)
        )
    )
    if show_case and id_m and len(pref.results) < 2:
        body, case_id8 = tool_show_case(id_m.group(1))
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
    elif SUGGEST_INTENT_RE.search(text) and len(pref.results) < 2:
        body, suggest_ids = tool_list_cases(suggest_only=True)
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids
    elif CASES_INTENT_RE.search(text) and len(pref.results) < 2:
        body, suggest_ids = tool_list_cases()
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids

    if OPS_INTENT_RE.search(text) and len(pref.results) < 2:
        pref.results.append(("ops_snapshot", tool_ops_snapshot()))

    pref.digest = _make_digest(pref.results)
    return pref


def _format_tools_human(tool_bits: list[tuple[str, str]], *, approve_confirm: bool = False) -> str:
    """Friendly tool-only reply when LLM is unavailable."""
    parts: list[str] = []
    for name, body in tool_bits:
        if name == "lookup_order":
            parts.append(f"Här är orderläget:\n{body}")
        elif name == "list_cases":
            parts.append(f"Här är kön:\n{body}")
        elif name == "show_case":
            parts.append(body)
        elif name == "ops_snapshot":
            parts.append(f"Snabbstatus:\n{body}")
        else:
            parts.append(body)
    if not parts:
        return FALLBACK_NO_KEY
    text = "\n\n".join(parts)
    if approve_confirm:
        text += (
            "\n\nJag skickar inte automatiskt. "
            "Tryck Godkänn & skicka om du vill skicka draften."
        )
    return text


def run_chat(
    user_message: str,
    *,
    history: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    prior_digest: str = "",
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> ChatResult:
    """Run one hybrid chat turn with tools + optional LLM phrasing."""
    session = dict(session or {})
    tel = telemetry or default_telemetry
    hist = clamp_messages(history)
    user_message = (user_message or "").strip()
    if not user_message:
        return ChatResult(text="Skriv något, eller /help.", messages=hist)

    def _with_turn(assistant: str) -> list[dict[str, str]]:
        return clamp_messages(
            hist
            + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant},
            ]
        )

    pref = gather_tool_results(user_message)
    tool_bits = pref.results
    case_id8 = pref.case_id8
    suggest_ids = pref.suggest_case_ids
    digest = pref.digest or prior_digest
    hard_esc = wants_hard_escalate_confirm(user_message)
    user_esc = wants_escalate(user_message)
    # Sticky escalate only on hard user intent — never from assistant soft hints alone
    offer_escalate = hard_esc
    soft_nudge = bool(user_esc and not hard_esc)

    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        if tool_bits:
            text = _format_tools_human(
                tool_bits, approve_confirm=pref.approve_confirm_only
            )
        else:
            text = FALLBACK_NO_KEY
        return ChatResult(
            text=text,
            messages=_with_turn(text),
            case_id8=case_id8,
            suggest_case_ids=suggest_ids,
            offer_escalate=offer_escalate,
            soft_escalate_nudge=soft_nudge,
            tool_digest=digest,
            approve_confirm_only=pref.approve_confirm_only,
        )

    cap = openrouter_cap_usd()
    if not tel.within_budget(cap):
        tel.record(
            action="llm_budget_skip",
            site=site,
            cost_usd=0.0,
            meta={
                "cap_usd": cap,
                "spent_usd": tel.sum_cost_usd(),
                "kind": "telegram_chat",
            },
        )
        if tool_bits:
            text = _format_tools_human(
                tool_bits, approve_confirm=pref.approve_confirm_only
            )
            text = text + "\n\n(LLM-budget slut — rå data ovan.)"
        else:
            text = FALLBACK_BUDGET
        return ChatResult(
            text=text,
            messages=_with_turn(text),
            case_id8=case_id8,
            suggest_case_ids=suggest_ids,
            offer_escalate=offer_escalate,
            soft_escalate_nudge=soft_nudge,
            tool_digest=digest,
            approve_confirm_only=pref.approve_confirm_only,
        )

    tool_block = ""
    if tool_bits:
        parts = [f"[{name}]\n{body}" for name, body in tool_bits]
        tool_block = "Tool results:\n" + "\n\n".join(parts)
    elif prior_digest:
        tool_block = f"Prior tool digest (follow-up context):\n{prior_digest}"

    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(hist)
    if tool_block:
        messages.append({"role": "system", "content": tool_block})
    messages.append({"role": "user", "content": user_message})

    model = _session_model(session) or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    try:
        content, cost = chat_completion(
            messages,
            model=model,
            max_tokens=_max_tokens(session),
            temperature=_think_temperature(session),
        )
    except Exception as exc:
        tel.record(
            action="llm_telegram_chat_error",
            site=site,
            cost_usd=0.0,
            meta={"error": str(exc)[:200]},
        )
        if tool_bits:
            text = _format_tools_human(
                tool_bits, approve_confirm=pref.approve_confirm_only
            )
            text = text + "\n\n(LLM nere — visar verktygsdata.)"
        else:
            text = FALLBACK_ERROR
        return ChatResult(
            text=text,
            messages=_with_turn(text),
            case_id8=case_id8,
            suggest_case_ids=suggest_ids,
            offer_escalate=offer_escalate,
            soft_escalate_nudge=soft_nudge,
            tool_digest=digest,
            approve_confirm_only=pref.approve_confirm_only,
        )

    soft_extra = False
    if ASSISTANT_ESCALATE_HINT_RE.search(content) and not hard_esc:
        soft_extra = True
        tel.record(
            action="escalate_soft_suppressed",
            site=site,
            cost_usd=0.0,
            meta={"kind": "telegram_chat"},
        )

    tel.record(
        action="llm_telegram_chat",
        site=site,
        unit_type="tokens",
        units=1.0,
        cost_usd=cost,
        meta={
            "model": model,
            "tools": [n for n, _ in tool_bits],
            "offer_escalate": offer_escalate,
            "soft_escalate_nudge": soft_nudge or soft_extra,
        },
    )

    verbose = session.get("verbose") in {"on", "full"}
    reply = content
    if pref.approve_confirm_only and "skickar inte" not in content.lower():
        reply = (
            f"{content}\n\n"
            "Jag skickar inte automatiskt — tryck Godkänn & skicka för att bekräfta."
        )
    if verbose:
        reply = f"[model={model}]\n{reply}"
    usage_mode = str(session.get("usage") or "off").lower()
    if usage_mode in {"cost", "full", "tokens", "on"}:
        reply = f"{reply}\n\n— usage ~${cost:.4f}"

    return ChatResult(
        text=reply,
        messages=_with_turn(content),
        cost_usd=cost,
        case_id8=case_id8,
        suggest_case_ids=suggest_ids,
        offer_escalate=offer_escalate,
        soft_escalate_nudge=soft_nudge or soft_extra,
        tool_digest=digest or prior_digest,
        approve_confirm_only=pref.approve_confirm_only,
    )
