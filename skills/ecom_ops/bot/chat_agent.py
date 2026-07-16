"""Hybrid OpenClaw-style LLM chat for Telegram (tools + multi-turn + write rails)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.bot.dialog_actions import (
    PendingAction,
    parse_order_status_intent,
    parse_product_desc_intent,
    parse_regenerate_nl,
    wants_case_followup,
    wants_order_followup,
)
from ecom_ops.bot.store import clamp_messages
from ecom_ops.llm import DEFAULT_MODEL, chat_completion, openrouter_cap_usd
from ecom_ops.order_context import format_order_context_block
from ecom_ops.security import validate_order_id
from ecom_ops.telemetry import Telemetry, default_telemetry

# Prefer shared extractor (SB1) for SV/NO/DK labels + near-status forms.
BARE_ORDER_RE = re.compile(r"^\s*#?\s*(\d{4,12})\s*$")
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
APPROVE_NL_RE = re.compile(
    r"\b(?:godkänn|approve|skicka)\b.{0,24}\b([0-9a-f]{8})\b"
    r"|\b([0-9a-f]{8})\b.{0,16}\b(?:godkänn|approve)\b",
    re.I,
)
OPS_INTENT_RE = re.compile(
    r"\b("
    r"status|hälsa|health|budget|kostnad|usage|"
    r"tasks?|uppgifter|översikt|brief|"
    r"hur mår|hur går det|läget|"
    r"vad kan du|hur funkar|hjälp\s+mig"
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

# OpenClaw-like: colleague thread, tools-first truth, write only after confirm.
SYSTEM_PROMPT = (
    "Du är AzomOps — Jonatans/Oscars dedikerade Telegram-kollega för Azom "
    "(Woo SE/NO/DK). Prata som i en vanlig OpenClaw-dialog: flytande svenska, "
    "naturligt, hjälpsamt, utan robotic bullet-spam. "
    "Fortsätt tråden: använd dialoghistorik, tool results och prior digest. "
    "När användaren säger 'den ordern'/'samma ärende' — antag sticky context. "
    "Använd ALLTID tool-data för order/ärenden/status; hitta aldrig på tracking, "
    "refunds eller orderfakta. "
    "Skriv gärna 1–3 korta stycken; max ~180 ord om det inte krävs mer detalj. "
    "Kundmail skickas ALDRIG av dig — det kräver /cases approve eller knappen "
    "Godkänn & skicka. Suggest-approve (★) = human confirm, inte auto. "
    "Ändringar på sajten (orderstatus, publicera produkttext) görs bara efter "
    "explicit bekräftelse via knappar/flows — föreslå bekräftelse, påstå inte "
    "att det redan skett. "
    "Vid eskalering: uppmuntra användaren att bekräfta — skicka inte mail själv."
)

FALLBACK_NO_KEY = (
    "LLM är inte kopplad just nu (saknar OPENROUTER_API_KEY), men jag kan "
    "fortfarande hämta order, ärenden och status — fråga fritt, eller /help."
)
FALLBACK_BUDGET = (
    "OpenRouter-budgeten är slut just nu. Jag hämtar fortfarande order/ärenden/"
    "status utan LLM — be Oscar höja cap om du vill ha mer dialog."
)
FALLBACK_ERROR = (
    "Kunde inte nå LLM just nu. Fråga om order/ärenden/status så kör jag "
    "verktygen direkt, eller prova igen om en stund."
)

SOFT_ESCALATE_NUDGE = "Säg *eskalera* om du vill skicka till Oscar."


@dataclass
class ToolPrefetch:
    """Typed tool prefetch + optional write proposal (confirm-only)."""

    results: list[tuple[str, str]] = field(default_factory=list)
    case_id8: str | None = None
    suggest_case_ids: list[str] = field(default_factory=list)
    digest: str = ""
    approve_confirm_only: bool = False
    pending_action: PendingAction | None = None
    sticky_order_id: str | None = None
    sticky_case_id8: str | None = None


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
    pending_action: PendingAction | None = None
    sticky_order_id: str | None = None
    sticky_case_id8: str | None = None


def wants_escalate(text: str) -> bool:
    return bool(ESCALATE_RE.search(text or ""))


def wants_hard_escalate_confirm(text: str) -> bool:
    t = (text or "").strip()
    if not wants_escalate(t):
        return False
    lower = t.lower()
    if lower.startswith(("eskalera", "escalate", "eskale")):
        return True
    return len(t) <= 36


def parse_approve_nl(text: str) -> str | None:
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
        return 0.55
    if think in {"low", "min", "off"}:
        return 0.15
    return 0.4


def _max_tokens(session: dict[str, Any]) -> int:
    think = str(session.get("think") or "default").lower()
    if think in {"high", "max", "deep"}:
        return 900
    return 650


def _extract_order_id(text: str) -> str | None:
    from ecom_ops.actions.support import extract_order_id

    found = extract_order_id(text or "")
    if found:
        return found
    m2 = BARE_ORDER_RE.match(text or "")
    return m2.group(1) if m2 else None


def tool_lookup_order(order_id: str) -> str:
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        oid = validate_order_id(order_id)
        woo = client_from_env(use_mock=None)
        order = woo.get_order(oid)
        block = format_order_context_block(order)
        return (
            f"{block}\n"
            "(read-only här — ändra status bara efter bekräftelse: "
            f"«sätt order {oid} till completed»)"
        )
    except Exception as exc:
        return f"Order lookup failed: {exc}"


def tool_list_cases(*, limit: int = 8, suggest_only: bool = False) -> tuple[str, list[str]]:
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
        lines.append("Godkänn utskick: knappen eller /cases approve <id8>.")
        return "\n".join(lines), suggest_ids
    except Exception as exc:
        return f"Cases list failed: {exc}", []


def tool_show_case(id_prefix: str) -> tuple[str, str | None]:
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
        from ecom_ops.budget import budget_status
        from ecom_ops.cases.service import CaseService
        from ecom_ops.config import load_app_config
        from ecom_ops.ops_status import readiness_from_last_poll

        cfg = load_app_config()
        budget = budget_status()
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
        n_cases = 0
        n_suggest = 0
        try:
            cases = CaseService().list_open(limit=50)
            n_cases = len(cases)
            n_suggest = sum(1 for c in cases if getattr(c, "suggest_approve", False))
        except Exception:
            pass
        n_esc = _count_open_escalations()
        ready = readiness_from_last_poll()
        cap_flag = " ⚠ near cap" if budget.get("near_cap") else ""
        return (
            f"Version {__version__} · {'mock' if mock else 'live'}\n"
            f"Customer: {cfg.customer.customer} · domains: {', '.join(cfg.customer.domains)}\n"
            f"OpenRouter: ${budget['used_usd']:.4f} / ${budget['cap_usd']:g}{cap_flag}\n"
            f"Öppna cases: {n_cases} (★ {n_suggest}) · eskaleringar: {n_esc}\n"
            f"Poll readiness: {'ok' if ready.get('ok') else 'stale/issue'}"
            f" (age={ready.get('last_poll_age_sec')})"
        )
    except Exception as exc:
        return f"Ops snapshot failed: {exc}"


def tool_capabilities() -> str:
    return (
        "Jag kan i chatten:\n"
        "• Kolla order (read-only) och följa upp i tråd\n"
        "• Lista/visa ärenden, ★-föreslagna, approve via knapp\n"
        "• Regenerera utkast (/cases regenerate eller «regenerera id8»)\n"
        "• Föreslå orderstatus-ändring (kräver bekräftelse + rätt actor)\n"
        "• Föreslå produktbeskrivning (kräver bekräftelse + PRODUCT_DESC)\n"
        "• Status, budget, brief, eskalera till Oscar\n"
        "Slash: /help /commands /order /cases /status /tools …"
    )


def _make_digest(results: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for name, body in results[:3]:
        first = (body or "").strip().splitlines()[:2]
        snippet = " | ".join(ln.strip() for ln in first if ln.strip())[:180]
        parts.append(f"{name}: {snippet}")
    return "\n".join(parts)


def gather_tool_results(
    user_message: str,
    *,
    sticky_order_id: str | None = None,
    sticky_case_id8: str | None = None,
) -> ToolPrefetch:
    """Tool prefetch with sticky multi-turn context (OpenClaw-like follow-ups)."""
    pref = ToolPrefetch(
        sticky_order_id=sticky_order_id,
        sticky_case_id8=sticky_case_id8,
    )
    text = user_message or ""

    # --- Write rails: propose only (never execute here) ---
    order_intent = parse_order_status_intent(text, fallback_order_id=sticky_order_id)
    if order_intent:
        pref.pending_action = PendingAction(kind="order_status", payload=order_intent)
        pref.sticky_order_id = order_intent["order_id"]
        pref.results.append(
            (
                "lookup_order",
                tool_lookup_order(order_intent["order_id"]),
            )
        )
        pref.results.append(
            (
                "propose_order_status",
                (
                    f"FÖRESLÅ ÄNDRING (ej utförd): order {order_intent['order_id']} → "
                    f"{order_intent['status']}. Vänta på användarens bekräftelse."
                ),
            )
        )
        pref.digest = _make_digest(pref.results)
        return pref

    prod_intent = parse_product_desc_intent(text)
    if prod_intent and prod_intent.get("product_id"):
        pref.pending_action = PendingAction(kind="product_desc", payload=prod_intent)
        pref.results.append(
            (
                "propose_product_desc",
                (
                    f"FÖRESLÅ produktbeskrivning för product_id={prod_intent['product_id']} "
                    f"(publish=false som default). Vänta på bekräftelse."
                ),
            )
        )
        pref.digest = _make_digest(pref.results)
        return pref

    regen_id = parse_regenerate_nl(text)
    if regen_id:
        body, case_id8 = tool_show_case(regen_id)
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.sticky_case_id8 = case_id8 or sticky_case_id8
        if case_id8:
            pref.pending_action = PendingAction(
                kind="case_regenerate",
                payload={"case_id": case_id8},
            )
            pref.results.append(
                (
                    "propose_regenerate",
                    f"FÖRESLÅ regenerera draft för case {case_id8} (skickar inte mail).",
                )
            )
        pref.digest = _make_digest(pref.results)
        return pref

    # NL approve → show case + confirm button only
    approve_id = parse_approve_nl(text)
    if approve_id:
        body, case_id8 = tool_show_case(approve_id)
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.sticky_case_id8 = case_id8 or sticky_case_id8
        pref.approve_confirm_only = True
        pref.digest = _make_digest(pref.results)
        return pref

    # Explicit order id in message
    oid = _extract_order_id(text)
    # Pronoun / short follow-up → re-fetch sticky order
    if not oid and sticky_order_id and (
        wants_order_followup(text) or len(text.strip()) < 48
    ):
        # Only if not clearly cases-only
        if not (CASES_INTENT_RE.search(text) and not wants_order_followup(text)):
            oid = sticky_order_id

    if oid and len(pref.results) < 3:
        try:
            pref.sticky_order_id = validate_order_id(oid)
        except Exception:
            pref.sticky_order_id = oid
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
    if not show_case and sticky_case_id8 and wants_case_followup(text):
        show_case = True
        id_m = re.match(r"([0-9a-f]{8})", sticky_case_id8, re.I) or type(
            "M", (), {"group": lambda _s, _i=1: sticky_case_id8}
        )()

    if show_case and id_m and len(pref.results) < 3:
        cid = id_m.group(1) if hasattr(id_m, "group") else sticky_case_id8
        body, case_id8 = tool_show_case(str(cid))
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.sticky_case_id8 = case_id8 or sticky_case_id8
    elif SUGGEST_INTENT_RE.search(text) and len(pref.results) < 3:
        body, suggest_ids = tool_list_cases(suggest_only=True)
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids
    elif CASES_INTENT_RE.search(text) and len(pref.results) < 3:
        body, suggest_ids = tool_list_cases()
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids

    if OPS_INTENT_RE.search(text) and len(pref.results) < 3:
        if re.search(r"vad kan du|hur funkar|capabilities|verktyg", text, re.I):
            pref.results.append(("capabilities", tool_capabilities()))
        else:
            pref.results.append(("ops_snapshot", tool_ops_snapshot()))

    # Bare short chit-chat with no tools — still ok; LLM handles
    pref.digest = _make_digest(pref.results)
    return pref


def _format_tools_human(
    tool_bits: list[tuple[str, str]],
    *,
    approve_confirm: bool = False,
    pending: PendingAction | None = None,
) -> str:
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
        elif name == "capabilities":
            parts.append(body)
        elif name.startswith("propose_"):
            continue  # handled below
        else:
            parts.append(body)
    if not parts and not pending:
        return FALLBACK_NO_KEY
    text = "\n\n".join(parts) if parts else ""
    if pending and pending.kind == "order_status":
        p = pending.payload
        extra = (
            f"Vill du att jag sätter order {p.get('order_id')} till "
            f"**{p.get('status')}**? Bekräfta med knappen (kräver rätt actor)."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "product_desc":
        p = pending.payload
        extra = (
            f"Vill du att jag genererar produktbeskrivning för "
            f"product {p.get('product_id')}? Bekräfta med knappen."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "case_regenerate":
        extra = (
            f"Vill du regenerera utkastet för case {pending.payload.get('case_id')}? "
            "Bekräfta med knappen (skickar inte mail)."
        )
        text = f"{text}\n\n{extra}".strip()
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
    sticky_order_id: str | None = None,
    sticky_case_id8: str | None = None,
    telemetry: Telemetry | None = None,
    site: str = "azom",
) -> ChatResult:
    """Run one hybrid chat turn with tools, sticky context, optional LLM."""
    session = dict(session or {})
    tel = telemetry or default_telemetry
    hist = clamp_messages(history)
    user_message = (user_message or "").strip()
    if not user_message:
        return ChatResult(text="Skriv något, eller /help.", messages=hist)

    # Prefer session sticky if not passed
    sticky_order_id = sticky_order_id or session.get("last_order_id") or None
    sticky_case_id8 = sticky_case_id8 or session.get("last_case_id8") or None

    def _with_turn(assistant: str) -> list[dict[str, str]]:
        return clamp_messages(
            hist
            + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant},
            ]
        )

    pref = gather_tool_results(
        user_message,
        sticky_order_id=str(sticky_order_id) if sticky_order_id else None,
        sticky_case_id8=str(sticky_case_id8) if sticky_case_id8 else None,
    )
    tool_bits = pref.results
    case_id8 = pref.case_id8
    suggest_ids = pref.suggest_case_ids
    digest = pref.digest or prior_digest
    hard_esc = wants_hard_escalate_confirm(user_message)
    user_esc = wants_escalate(user_message)
    offer_escalate = hard_esc
    soft_nudge = bool(user_esc and not hard_esc)

    def _base(**extra: Any) -> dict[str, Any]:
        return {
            "case_id8": case_id8,
            "suggest_case_ids": suggest_ids,
            "offer_escalate": offer_escalate,
            "soft_escalate_nudge": soft_nudge,
            "tool_digest": digest,
            "approve_confirm_only": pref.approve_confirm_only,
            "pending_action": pref.pending_action,
            "sticky_order_id": pref.sticky_order_id or sticky_order_id,
            "sticky_case_id8": pref.sticky_case_id8 or sticky_case_id8 or case_id8,
            **extra,
        }

    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        if tool_bits or pref.pending_action:
            text = _format_tools_human(
                tool_bits,
                approve_confirm=pref.approve_confirm_only,
                pending=pref.pending_action,
            )
        else:
            text = FALLBACK_NO_KEY
        return ChatResult(text=text, messages=_with_turn(text), **_base())

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
        if tool_bits or pref.pending_action:
            text = _format_tools_human(
                tool_bits,
                approve_confirm=pref.approve_confirm_only,
                pending=pref.pending_action,
            )
            text = text + "\n\n(LLM-budget slut — rå data ovan.)"
        else:
            text = FALLBACK_BUDGET
        return ChatResult(text=text, messages=_with_turn(text), **_base())

    tool_block = ""
    if tool_bits:
        parts = [f"[{name}]\n{body}" for name, body in tool_bits]
        tool_block = "Tool results:\n" + "\n\n".join(parts)
    elif prior_digest:
        tool_block = f"Prior tool digest (follow-up context):\n{prior_digest}"

    context_bits = []
    if sticky_order_id or pref.sticky_order_id:
        context_bits.append(f"sticky_order_id={pref.sticky_order_id or sticky_order_id}")
    if sticky_case_id8 or pref.sticky_case_id8 or case_id8:
        context_bits.append(
            f"sticky_case_id8={pref.sticky_case_id8 or sticky_case_id8 or case_id8}"
        )
    if context_bits:
        ctx_line = "Session context: " + ", ".join(context_bits)
        tool_block = f"{tool_block}\n{ctx_line}".strip() if tool_block else ctx_line

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
        if tool_bits or pref.pending_action:
            text = _format_tools_human(
                tool_bits,
                approve_confirm=pref.approve_confirm_only,
                pending=pref.pending_action,
            )
            text = text + "\n\n(LLM nere — visar verktygsdata.)"
        else:
            text = FALLBACK_ERROR
        return ChatResult(text=text, messages=_with_turn(text), **_base())

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
            "pending": pref.pending_action.kind if pref.pending_action else None,
        },
    )

    verbose = session.get("verbose") in {"on", "full"}
    reply = content
    if pref.approve_confirm_only and "skickar inte" not in content.lower():
        reply = (
            f"{content}\n\n"
            "Jag skickar inte automatiskt — tryck Godkänn & skicka för att bekräfta."
        )
    if pref.pending_action and pref.pending_action.kind == "order_status":
        p = pref.pending_action.payload
        if "bekräft" not in content.lower() and "confirm" not in content.lower():
            reply = (
                f"{reply}\n\n"
                f"Bekräfta knappen för att sätta order {p.get('order_id')} → "
                f"{p.get('status')} (utförs inte förrän du bekräftar)."
            )
    if pref.pending_action and pref.pending_action.kind == "product_desc":
        if "bekräft" not in content.lower():
            reply = (
                f"{reply}\n\n"
                "Bekräfta knappen för att generera produktbeskrivningen."
            )
    if verbose:
        reply = f"[model={model}]\n{reply}"
    usage_mode = str(session.get("usage") or "off").lower()
    if usage_mode in {"cost", "full", "tokens", "on"}:
        reply = f"{reply}\n\n— usage ~${cost:.4f}"

    base = _base(soft_escalate_nudge=soft_nudge or soft_extra)
    return ChatResult(
        text=reply,
        messages=_with_turn(content),
        cost_usd=cost,
        **base,
    )
