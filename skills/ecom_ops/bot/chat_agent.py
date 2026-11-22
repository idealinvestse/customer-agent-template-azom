"""Hybrid OpenClaw-style LLM chat for Telegram (tools + multi-turn + write rails)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops.bot.catalog import catalog_read_turn, catalog_tool_result, catalog_write_turn
from ecom_ops.bot.dialog_actions import (
    PendingAction,
    parse_order_status_intent,
    parse_product_desc_intent,
    parse_regenerate_nl,
    wants_case_followup,
    wants_order_followup,
)
from ecom_ops.bot.site_content import site_content_turn, site_tool_result
from ecom_ops.bot.store import clamp_messages
from ecom_ops.llm import DEFAULT_MODEL, chat_completion, openrouter_cap_usd
from ecom_ops.model_router import get_model_router
from ecom_ops.model_router.errors import ModelRouteError
from ecom_ops.order_context import format_order_context_block
from ecom_ops.prompts import get_prompt
from ecom_ops.security import validate_order_id
from ecom_ops.telemetry import Telemetry, default_telemetry
from ecom_ops.wp_ops.agent_tools import (
    WP_TOOL_GUIDE,
    context_for_storage,
    run_bounded_wp_tool_loop,
)

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
MARKETING_INTENT_RE = re.compile(
    r"\b("
    r"marketing|google\s*ads|ads|ga4|analytics|"
    r"roas|annonsering|kampanj|trafik|"
    r"hur går\s+(ads|annonserna|trafiken)"
    r")\b",
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
CAPABILITIES_RE = re.compile(
    r"\b(vad kan du|hur funkar|vilka verktyg|capabilities|verktyg)\b",
    re.I,
)
RECENT_ORDERS_RE = re.compile(
    r"\b("
    r"senaste\s+ordrar(?:na)?|lista\s+ordrar(?:na)?|nya\s+ordrar|"
    r"orderlista|orderdata|vilka\s+ordrar|hämta\s+ordrar|"
    r"integration(?:en)?|koppling(?:en)?|api-?nyck(?:el|eln)?"
    r")\b",
    re.I,
)
RETRY_WOO_RE = re.compile(
    r"^\s*(?:kolla|försök|testa)(?:\s+(?:nu|igen|en\s+gång|på\s+nytt))?\s*[.!?]*\s*$",
    re.I,
)
MORE_ORDERS_RE = re.compile(
    r"\b("
    r"(?:fler(?:a)?|mer)\s+ordrar(?:na)?|"
    r"plocka\s+fram(?:\s+fler(?:a)?)?(?:\s+ordrar(?:na)?)?|"
    r"visa\s+fler(?:a)?(?:\s+ordrar(?:na)?)?|"
    r"n[äa]sta\s+\d{1,3}(?:\s+ordrar(?:na)?)?|"
    r"n[äa]sta\s+ordrar(?:na)?"
    r")\b",
    re.I,
)
_LIST_COUNT_RE = re.compile(
    r"\b(?:n[äa]sta|senaste|fler|flera|visa|hämta|lista)\s+(\d{1,3})\b"
    r"|\b(\d{1,3})\s+ordrar(?:na)?\b",
    re.I,
)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.I | re.S)
WP_OPS_INTENT_RE = re.compile(
    r"\b(wordpress|wordpress-sajt|wp[- ]?ops|wp-cli|plugin(?:er)?|tema(?:n|t|r)?|"
    r"theme(?:s)?|backup(?:s)?|säkerhets(?:fynd|skanning|koll)|uppdateringsplan|"
    r"wordpressdrift|wordpress-drift|sajt(?:en|er|erna)?|webbplats(?:en|er)?)\b",
    re.I,
)
_LISTED_ORDER_RE = re.compile(r"(?m)^-\s*(\d{4,12})\s*\|")
_CATALOG_WORD_RE = re.compile(r"\b(?:produkt(?:en|er)?|sid(?:an|a|or|orna))\b", re.I)
_DIRECT_CATALOG = frozenset(
    {"list_products", "show_product", "list_pages", "show_page", "catalog"}
)
_DIRECT_SITE = frozenset(
    {
        "list_posts",
        "show_post",
        "show_site",
        "list_themes",
        "list_plugins",
        "site_content",
        "propose_post_update",
        "propose_site_update",
        "propose_plugin_status",
    }
)
_SITE_PENDING = frozenset({"post_update", "site_update", "plugin_status"})
_MARKET_MENTION_RE = re.compile(
    r"\b(?:azom\.(?P<tld>no|se|dk|fi)|"
    r"(?P<name>norge|norway|sverige|sweden|danmark|denmark|finland))\b",
    re.I,
)
_MARKET_FILLER_RE = re.compile(
    r"\b(det|gäller|galler|handlar|om|mot|för|pa|på|är|ar|site|sajten|butiken|azom)\b",
    re.I,
)
_MARKET_NAMES = {
    "norge": "no",
    "norway": "no",
    "sverige": "se",
    "sweden": "se",
    "danmark": "dk",
    "denmark": "dk",
    "finland": "fi",
}
ESCALATE_RE = re.compile(
    r"\b(eskalera|eskale|escalate|critical)\b"
    r"|(?:till\s+oscar)",
    re.I,
)
ASSISTANT_ESCALATE_HINT_RE = re.compile(
    r"\b(eskalera|eskale|oscar|godkänn\s+eskalering)\b",
    re.I,
)

# The editable prompt defines voice and task context. These fixed instructions
# and the typed tool guide are appended after it and cannot be removed in the UI.
CHAT_RUNTIME_POLICY = (
    "H\u00e5rda runtime-regler som alltid g\u00e4ller: tala med Azoms interna personal, "
    "inte butikens kunder. Svenska f\u00f6rst; f\u00f6lj personens spr\u00e5k n\u00e4r det tydligt \u00e4r norska, danska eller engelska. "
    "Anv\u00e4nd verktygsresultat som sanning f\u00f6r order, produkter, sidor och drift; hitta inte p\u00e5 uppslag eller resultat. "
    "N\u00e4r data saknas eller ett resultat \u00e4r mock/unknown, s\u00e4g det tydligt. "
    "Skicka aldrig kundmail fr\u00e5n fri text. Endast befintlig explicit approve-path f\u00e5r skicka. "
    "Order-, produkt-, sid-, inl\u00e4ggs- och sajttext\u00e4ndringar kr\u00e4ver sin befintliga bekr\u00e4ftelseknapp; ett fritt ja r\u00e4cker inte. "
    "Pluginstatus kan bara \u00e4ndras av Oscar efter bekr\u00e4ftelse. P\u00e5st\u00e5 aldrig att en \u00e4ndring redan gjorts utan lyckat verktygsresultat. "
    "WordPress driftverktyg \u00e4r endast de namngivna verktygen i n\u00e4sta instruktion. "
    "Uppdaterings- och restoreplaner \u00e4r l\u00e5sta f\u00f6rhandsgranskningar och saknar exekveringsv\u00e4g; godk\u00e4nnande g\u00f6r dem inte k\u00f6rbara. "
    "Det finns ingen modellskapad shell, PHP, WP-CLI eller egen kod. Visa aldrig credentials, tokens eller interna tool-call-format."
)


def _build_system_prompt() -> tuple[str, str]:
    configured_prompt, version = get_prompt("chat")
    sections = (configured_prompt.strip(), CHAT_RUNTIME_POLICY, WP_TOOL_GUIDE)
    return "\n\n".join(section for section in sections if section), version


SYSTEM_PROMPT, _SYSTEM_PROMPT_VERSION = _build_system_prompt()


class _SafeTextStream:
    """Stream natural text while withholding any model tool-call protocol."""

    _MARKER = "<tool_call>"

    def __init__(
        self,
        on_text_delta: Callable[[str], None] | None,
        on_text_reset: Callable[[], None] | None,
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_text_reset = on_text_reset
        self.pending = ""
        self.emitted = ""
        self.blocked = False

    def _emit(self, text: str) -> None:
        if not text or not self.on_text_delta:
            return
        self.emitted += text
        try:
            self.on_text_delta(text)
        except Exception:
            pass

    def push(self, delta: str) -> None:
        if self.blocked or not delta:
            return
        combined = self.pending + delta
        marker_at = combined.lower().find(self._MARKER)
        if marker_at >= 0:
            self._emit(combined[:marker_at])
            self.pending = ""
            self.blocked = True
            return
        low = combined.lower()
        hold = max(
            (n for n in range(1, len(self._MARKER)) if low.endswith(self._MARKER[:n])),
            default=0,
        )
        if hold:
            self._emit(combined[:-hold])
            self.pending = combined[-hold:]
        else:
            self._emit(combined)
            self.pending = ""

    def finish(self, content: str) -> None:
        if self._MARKER in (content or "").lower():
            self.pending = ""
            self.blocked = True
            return
        if content and content.strip() == (self.emitted + self.pending).strip():
            self.pending = ""
            return
        self._emit(self.pending)
        self.pending = ""
        if content and not self.emitted:
            self._emit(content)
        elif self.emitted and content.startswith(self.emitted):
            self._emit(content[len(self.emitted) :])
        elif content and self.emitted != content:
            self.reset()
            self._emit(content)

    def reset(self) -> None:
        self.pending = ""
        self.emitted = ""
        self.blocked = False
        if self.on_text_reset:
            try:
                self.on_text_reset()
            except Exception:
                pass


def _report_tool(
    callback: Callable[[str], None] | None,
    label: str,
    action: Callable[[], Any],
) -> Any:
    if callback:
        try:
            callback(f"🔧 {label} pågår…")
        except Exception:
            pass
    try:
        result = action()
    except Exception:
        if callback:
            try:
                callback(f"⚠️ {label} misslyckades")
            except Exception:
                pass
        raise
    if callback:
        try:
            callback(f"✓ {label}: resultat mottaget")
        except Exception:
            pass
    return result


_MODEL_TOOL_LABELS = {
    "list_recent_orders": "Orderlista",
    "list_products": "Produktlista",
    "show_product": "Produktuppslag",
    "list_pages": "Sidlista",
    "show_page": "Siduppslag",
    "list_posts": "Inläggslista",
    "show_post": "Inläggsuppslag",
    "show_site": "WordPress-webbplats",
    "list_themes": "Temalista",
    "list_plugins": "Pluginlista",
}

_WP_TOOL_LABELS = {
    "wp_list_sites": "WordPress-sajter",
    "wp_site_status": "WordPress-status",
    "wp_backup_status": "Backupstatus",
    "wp_security_findings": "Säkerhetsfynd",
    "wp_prepare_update_plan": "Uppdateringsplan",
    "wp_prepare_restore_plan": "Återställningsplan",
    "wp_list_open_operations": "Öppna driftärenden",
}


def _report_wp_tool_status(
    callback: Callable[[str], None] | None, name: str, status: str
) -> None:
    if not callback:
        return
    label = _WP_TOOL_LABELS.get(name)
    if not label:
        return
    if status == "started":
        message = f"🔧 {label} pågår…"
    elif status == "result":
        message = f"✓ {label}: resultat mottaget"
    else:
        message = f"⚠️ {label}: resultat kunde inte hämtas"
    try:
        callback(message)
    except Exception:
        pass

FALLBACK_NO_KEY = (
    "LLM är inte kopplad just nu (ingen modellroute är konfigurerad), men jag kan "
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


class ChatCancelled(Exception):
    """Raised when a background Telegram read job is cooperatively stopped."""


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
    sticky_market: str | None = None
    sticky_product_id: str | None = None
    sticky_page_id: str | None = None
    sticky_post_id: str | None = None
    order_list_shown: int | None = None


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
    sticky_market: str | None = None
    sticky_product_id: str | None = None
    sticky_page_id: str | None = None
    sticky_post_id: str | None = None
    order_list_shown: int | None = None
    wp_ops_context: dict[str, Any] | None = None


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


def tool_lookup_order(order_id: str, *, domain: str | None = None) -> str:
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        oid = validate_order_id(order_id)
        woo = client_from_env(use_mock=None, domain=domain)
        order = woo.get_order(oid)
        block = format_order_context_block(order)
        market_note = f" (marknad: {domain})" if domain else ""
        return (
            f"{block}\n"
            f"(read-only här{market_note} — ändra status bara efter bekräftelse: "
            f"«sätt order {oid} till completed»)"
        )
    except Exception as exc:
        from ecom_ops.bot.recovery import FOOTER_ORDER_FAIL, with_recovery

        return with_recovery(f"Order lookup failed: {exc}", footer=FOOTER_ORDER_FAIL)


def market_from_text(text: str) -> str | None:
    """Return se/no/dk/fi when the message names a shop or country."""
    match = _MARKET_MENTION_RE.search(text or "")
    if not match:
        return None
    tld = match.group("tld")
    if tld:
        return tld.lower()
    return _MARKET_NAMES[match.group("name").lower()]


def _market_pointer(text: str, market: str | None) -> bool:
    """True when the message only points at a market, for example 'azom.no'."""
    if not market:
        return False
    rest = _MARKET_MENTION_RE.sub(" ", text or "")
    rest = _MARKET_FILLER_RE.sub(" ", rest)
    return re.sub(r"[\s.,:;!?]+", "", rest) == ""


def _public_woo_failure(exc: Exception) -> str:
    match = re.search(r"API error (\d+)", str(exc))
    if match:
        return f"HTTP {match.group(1)}"
    if "transport error" in str(exc).lower():
        return "nätverksfel"
    return type(exc).__name__


def _requested_list_count(text: str) -> int | None:
    match = _LIST_COUNT_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    count = int(raw)
    if count < 1:
        return None
    return min(count, 40)


def _listed_count_from_history(history: list[dict[str, Any]] | None) -> int:
    """How many orders the latest rendered list already showed."""
    for msg in reversed(history or []):
        if str(msg.get("role") or "") != "assistant":
            continue
        ids = _LISTED_ORDER_RE.findall(str(msg.get("content") or ""))
        if ids:
            return len(ids)
    return 0


def _session_list_offset(session: dict[str, Any], history: list[dict[str, Any]]) -> int:
    try:
        shown = int(session.get("order_list_shown") or 0)
    except (TypeError, ValueError):
        shown = 0
    if shown > 0:
        return shown
    return _listed_count_from_history(history)


def tool_list_recent_orders(
    *,
    domain: str | None = None,
    limit: int = 5,
    offset: int = 0,
) -> tuple[str, int]:
    """Read-only order window. Returns the reply and the next cursor."""
    limit = max(1, min(int(limit), 40))
    offset = max(0, int(offset))
    try:
        from urllib.parse import urlparse

        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=None, domain=domain)
        host = urlparse(woo.base_url).hostname or woo.base_url
        need = offset + limit
        rows: list[Any] = []
        if need <= 100:
            rows = woo.list_orders(per_page=need, page=1)
        else:
            page = 1
            while len(rows) < need and page <= 5:
                batch = woo.list_orders(per_page=100, page=page)
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        orders = rows[offset : offset + limit]
    except Exception as exc:
        market = f" ({domain})" if domain else ""
        return f"Woo-kopplingen{market} svarade inte: {_public_woo_failure(exc)}", offset
    market_note = f" marknad {domain}" if domain else ""
    if not orders:
        if offset:
            return (
                f"Woo {host}{market_note} har inga fler ordrar efter de {offset} senaste.",
                offset,
            )
        return f"Woo {host}{market_note} svarade med en tom orderlista.", 0
    if offset:
        start = offset + 1
        end = offset + len(orders)
        head = (
            f"Woo {host}{market_note}. Nästa {len(orders)} efter de {offset} senaste "
            f"({start}–{end}):"
        )
    else:
        head = f"Woo {host}{market_note} svarade. Senaste {len(orders)} ordrar:"
    lines = [head]
    for order in orders:
        raw = order.raw or {}
        created = str(raw.get("date_created") or "")[:10]
        lines.append(
            f"- {order.id} | {order.status} | {created} | {order.total} {order.currency}".rstrip()
        )
    lines.append("Säg nästa 20, eller ett ordernummer om du vill ha rader och spårning.")
    return "\n".join(lines), offset + len(orders)


def _fulfill_model_tool_call(
    content: str,
    *,
    domain: str | None,
    offset: int,
) -> tuple[str, int | None] | None:
    """Run a model-written read tool. Never return the protocol or a write."""
    match = _TOOL_CALL_RE.search(content or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    if payload.get("name") != "list_recent_orders":
        store = str(args.get("store") or args.get("domain") or "")
        resolved = market_from_text(store) or domain
        catalog = catalog_tool_result(str(payload.get("name") or ""), args, domain=resolved)
        if catalog:
            return catalog, None
        site = site_tool_result(str(payload.get("name") or ""), args, domain=resolved)
        if site:
            return site, None
        return None
    try:
        limit = int(args.get("limit", 20))
        off = int(args.get("offset", offset))
    except (TypeError, ValueError):
        return None
    store = str(args.get("store") or args.get("domain") or "")
    resolved = market_from_text(store) or domain
    return tool_list_recent_orders(domain=resolved, limit=limit, offset=off)


def tool_list_cases(*, limit: int = 8, suggest_only: bool = False) -> tuple[str, list[str]]:
    try:
        from ecom_ops.cases.service import CaseService

        cases = CaseService().list_open(limit=max(limit, 20))
        if suggest_only:
            cases = [c for c in cases if getattr(c, "suggest_approve", False)]
        cases = cases[:limit]
        if not cases:
            from ecom_ops.bot.recovery import empty_queue_text

            return empty_queue_text(suggest_only=suggest_only), []
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


def tool_show_case(id_prefix: str) -> tuple[str, str | None, str | None]:
    try:
        from ecom_ops.bot.openclaw_commands import _format_case_show
        from ecom_ops.cases.service import CaseService
        from ecom_ops.order_context import woo_domain_from_market

        svc = CaseService()
        case = svc.store.resolve_id_prefix(id_prefix) or svc.get(id_prefix)
        if not case:
            from ecom_ops.bot.recovery import case_not_found_text

            return case_not_found_text(id_prefix), None, None
        market = woo_domain_from_market(case.market)
        return _format_case_show(case), case.id[:8], market
    except Exception as exc:
        from ecom_ops.bot.recovery import FOOTER_CASE_NOT_FOUND, with_recovery

        return with_recovery(f"Case show failed: {exc}", footer=FOOTER_CASE_NOT_FOUND), None, None


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


def tool_marketing_snapshot() -> str:
    """Read-only Ads+GA4 snapshot for chat (never mutates)."""
    try:
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=None).snapshot(actor="agent")
        if not result.ok:
            return f"Marketing: {result.message}"
        data = result.data or {}
        dig = data.get("digest") or {}
        ads = dig.get("ads") or {}
        ga = dig.get("ga4") or {}
        health = (data.get("health") or {}).get("labels") or {}
        return (
            "Marketing (märkta vyer — Woo är order-sanning)\n"
            f"Ads kostnad: {ads.get('cost')} · ROAS(Ads): {ads.get('roas_ads_reported')}\n"
            f"GA köp: {ga.get('ecommerce_purchases')} · GA intäkt: {ga.get('purchase_revenue')}\n"
            f"Hälsa: purchase_key={health.get('purchase_key_event')} "
            f"ads_link={health.get('ads_linked')} funnel={health.get('funnel_ok')}\n"
            "Mutates: endast via dashboard/CLI approve — aldrig från fri text."
        )
    except Exception as exc:
        return f"Marketing snapshot failed: {exc}"


def tool_capabilities() -> str:
    return (
        "Jag kan i chatten:\n"
        "• Kolla en order (read-only) och följa upp i tråd\n"
        "• Lista de senaste ordrarna och kolla Woo-kopplingen\n"
        "• Lista/visa ärenden, ★-föreslagna, approve via knapp\n"
        "• Regenerera utkast (/cases regenerate eller «regenerera id8»)\n"
        "• Föreslå orderstatus-ändring (kräver bekräftelse + rätt actor)\n"
        "• Föreslå produktbeskrivning (kräver bekräftelse + PRODUCT_DESC)\n"
        "• Lista och visa produkter (namn, sku, pris, lager, kort text)\n"
        "• Föreslå ändring av produktnamn, pris, lager eller text "
        "(kräver bekräftelse + PRODUCT_DESC)\n"
        "• Lista och visa WordPress-sidor\n"
        "• Föreslå ändring av sidtitel, sidtext eller status "
        "(kräver bekräftelse + WP_OPS_OPERATE)\n"
        "• Lista och visa inlägg, sajttitel och tagline\n"
        "• Föreslå ett fält på ett inlägg, eller sajttitel/tagline "
        "(kräver bekräftelse + WP_OPS_OPERATE)\n"
        "• Lista teman (vilket som är aktivt) och plugins (aktiv/inaktiv)\n"
        "• Föreslå plugin på eller av (kräver bekräftelse + WP_OPS_ADMIN)\n"
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


def _apply_catalog(pref: ToolPrefetch, turn: Any, woo_domain: str | None) -> ToolPrefetch:
    pref.results.extend(turn.results)
    if turn.pending is not None:
        pref.pending_action = turn.pending
    if turn.sticky_product_id:
        pref.sticky_product_id = turn.sticky_product_id
    if turn.sticky_page_id:
        pref.sticky_page_id = turn.sticky_page_id
    sticky_post = getattr(turn, "sticky_post_id", None)
    if sticky_post:
        pref.sticky_post_id = sticky_post
    pref.sticky_market = woo_domain
    pref.digest = _make_digest(pref.results)
    return pref


def gather_tool_results(
    user_message: str,
    *,
    sticky_order_id: str | None = None,
    sticky_case_id8: str | None = None,
    sticky_market: str | None = None,
    sticky_product_id: str | None = None,
    sticky_page_id: str | None = None,
    sticky_post_id: str | None = None,
    listed_offset: int = 0,
    on_progress: Callable[[str], None] | None = None,
) -> ToolPrefetch:
    """Tool prefetch with sticky multi-turn context (OpenClaw-like follow-ups)."""
    pref = ToolPrefetch(
        sticky_order_id=sticky_order_id,
        sticky_case_id8=sticky_case_id8,
        sticky_product_id=sticky_product_id,
        sticky_page_id=sticky_page_id,
        sticky_post_id=sticky_post_id,
    )
    text = user_message or ""
    mentioned = market_from_text(text)
    woo_domain = mentioned or sticky_market
    pref.sticky_market = woo_domain

    site_intent = bool(
        re.search(r"\b(wordpress|wp|plugin|plugins|tema|teman|inlägg|inlägg|tagline|sajttitel)\b", text, re.I)
    )
    def site_action() -> Any:
        return site_content_turn(
            text, domain=woo_domain, sticky_post_id=sticky_post_id
        )
    site_turn = (
        _report_tool(on_progress, "WordPress-innehåll", site_action)
        if site_intent
        else site_action()
    )
    if site_turn.consumed:
        return _apply_catalog(pref, site_turn, woo_domain)

    # --- Write rails: propose only (never execute here) ---
    order_intent = parse_order_status_intent(text, fallback_order_id=sticky_order_id)
    if order_intent:
        pref.pending_action = PendingAction(kind="order_status", payload=order_intent)
        pref.sticky_order_id = order_intent["order_id"]
        pref.results.append(
            (
                "lookup_order",
                _report_tool(
                    on_progress,
                    "Orderuppslag",
                    lambda: tool_lookup_order(order_intent["order_id"], domain=woo_domain),
                ),
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
        pref.sticky_market = woo_domain
        pref.digest = _make_digest(pref.results)
        return pref

    write_turn = catalog_write_turn(
        text,
        domain=woo_domain,
        sticky_product_id=sticky_product_id,
        sticky_page_id=sticky_page_id,
    )
    if write_turn.consumed:
        return _apply_catalog(pref, write_turn, woo_domain)

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
        body, case_id8, market = tool_show_case(regen_id)
        if market:
            woo_domain = market
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
        pref.sticky_market = woo_domain
        pref.digest = _make_digest(pref.results)
        return pref

    # NL approve → show case + confirm button only
    approve_id = parse_approve_nl(text)
    if approve_id:
        body, case_id8, market = tool_show_case(approve_id)
        if market:
            woo_domain = market
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.sticky_case_id8 = case_id8 or sticky_case_id8
        pref.approve_confirm_only = True
        pref.sticky_market = woo_domain
        pref.digest = _make_digest(pref.results)
        return pref

    catalog_intent = bool(
        re.search(r"\b(produkt|produkter|produktdata|sida|sidor|page|pages)\b", text, re.I)
    )
    def catalog_action() -> Any:
        return catalog_read_turn(
            text,
            domain=woo_domain,
            sticky_product_id=sticky_product_id,
            sticky_page_id=sticky_page_id,
        )
    read_turn = (
        _report_tool(on_progress, "Produkt- eller siduppslag", catalog_action)
        if catalog_intent
        else catalog_action()
    )
    if read_turn.consumed:
        return _apply_catalog(pref, read_turn, woo_domain)

    # Explicit order id in message
    oid = _extract_order_id(text)
    # Pronoun / order follow-up → re-fetch sticky order (not bare chit-chat)
    if not oid and sticky_order_id and wants_order_followup(text):
        # Only if not clearly cases-only
        if not (CASES_INTENT_RE.search(text) and not wants_order_followup(text)):
            oid = sticky_order_id

    if oid and len(pref.results) < 3:
        try:
            pref.sticky_order_id = validate_order_id(oid)
        except Exception:
            pref.sticky_order_id = oid
        pref.results.append(
            (
                "lookup_order",
                _report_tool(
                    on_progress,
                    "Orderuppslag",
                    lambda: tool_lookup_order(oid, domain=woo_domain),
                ),
            )
        )

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
        body, case_id8, market = _report_tool(
            on_progress, "Ärendeuppslag", lambda: tool_show_case(str(cid))
        )
        if market:
            woo_domain = market
        pref.results.append(("show_case", body))
        pref.case_id8 = case_id8
        pref.sticky_case_id8 = case_id8 or sticky_case_id8
    elif SUGGEST_INTENT_RE.search(text) and len(pref.results) < 3:
        body, suggest_ids = _report_tool(
            on_progress, "Föreslagna ärenden", lambda: tool_list_cases(suggest_only=True)
        )
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids
    elif CASES_INTENT_RE.search(text) and len(pref.results) < 3:
        body, suggest_ids = _report_tool(
            on_progress, "Ärendelista", lambda: tool_list_cases()
        )
        pref.results.append(("list_cases", body))
        pref.suggest_case_ids = suggest_ids

    if MARKETING_INTENT_RE.search(text) and len(pref.results) < 3:
        pref.results.append(
            (
                "marketing_snapshot",
                _report_tool(on_progress, "Google Ads/GA4-uppslag", tool_marketing_snapshot),
            )
        )

    if CAPABILITIES_RE.search(text) and len(pref.results) < 3:
        pref.results.append(("capabilities", tool_capabilities()))
    elif OPS_INTENT_RE.search(text) and len(pref.results) < 3:
        pref.results.append(
            ("ops_snapshot", _report_tool(on_progress, "Driftstatus", tool_ops_snapshot))
        )

    continuation = bool(MORE_ORDERS_RE.search(text)) and not _CATALOG_WORD_RE.search(text)
    fresh_list = bool(
        RECENT_ORDERS_RE.search(text)
        or RETRY_WOO_RE.search(text)
        or _market_pointer(text, mentioned)
    ) and not continuation
    if (
        (continuation or fresh_list)
        and len(pref.results) < 3
        and not any(name == "lookup_order" for name, _ in pref.results)
        and not any(name == "capabilities" for name, _ in pref.results)
    ):
        limit = _requested_list_count(text)
        if limit is None:
            limit = 20 if continuation else 5
        offset = max(0, int(listed_offset)) if continuation else 0
        body, shown = _report_tool(
            on_progress,
            "Orderlista",
            lambda: tool_list_recent_orders(domain=woo_domain, limit=limit, offset=offset),
        )
        pref.results.append(("list_recent_orders", body))
        pref.order_list_shown = shown

    # Bare short chit-chat with no tools — still ok; LLM handles
    pref.digest = _make_digest(pref.results)
    pref.sticky_market = woo_domain
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
        elif name == "list_recent_orders":
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
    elif pending and pending.kind == "product_update":
        p = pending.payload
        extra = (
            f"Vill du ändra {p.get('label')} på produkt {p.get('product_id')} "
            f"till «{p.get('preview')}»? Inget sparas förrän du trycker Ja."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "page_update":
        p = pending.payload
        extra = (
            f"Vill du ändra {p.get('label')} på sida {p.get('page_id')} "
            f"till «{p.get('preview')}»? Inget sparas förrän du trycker Ja."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "post_update":
        p = pending.payload
        extra = (
            f"Vill du ändra {p.get('label')} på inlägg {p.get('post_id')} "
            f"till «{p.get('preview')}»? Inget sparas förrän du trycker Ja."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "site_update":
        p = pending.payload
        extra = (
            f"Vill du ändra {p.get('label')} till «{p.get('preview')}»? "
            "Inget sparas förrän du trycker Ja."
        )
        text = f"{text}\n\n{extra}".strip()
    elif pending and pending.kind == "plugin_status":
        p = pending.payload
        extra = (
            f"Vill du sätta {p.get('name')} ({p.get('plugin')}) till {p.get('preview')}? "
            "Inget ändras förrän du trycker Ja."
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
    actor: str = "agent",
    channel: str = "telegram",
    conversation_id: str = "",
    on_progress: Callable[[str], None] | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_text_reset: Callable[[], None] | None = None,
    on_checkpoint: Callable[[], list[str]] | None = None,
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
    sticky_market = session.get("last_market") or None
    sticky_product_id = session.get("last_product_id") or None
    sticky_page_id = session.get("last_page_id") or None
    sticky_post_id = session.get("last_post_id") or None
    wp_ops_context = session.get("wp_ops_context")
    listed_offset = _session_list_offset(session, hist)
    turn_user_messages = [user_message]

    def _checkpoint() -> list[str]:
        if not on_checkpoint:
            return []
        incoming = on_checkpoint() or []
        accepted = [str(value).strip()[:8000] for value in incoming if str(value).strip()]
        if accepted:
            turn_user_messages.extend(accepted[:3])
            if on_progress:
                on_progress("Följdfråga infogad vid säker kontrollpunkt.")
        return accepted[:3]

    def _with_turn(assistant: str) -> list[dict[str, str]]:
        messages = list(hist)
        messages.extend({"role": "user", "content": value} for value in turn_user_messages)
        messages.append({"role": "assistant", "content": assistant})
        return clamp_messages(messages)

    pref = gather_tool_results(
        user_message,
        sticky_order_id=str(sticky_order_id) if sticky_order_id else None,
        sticky_case_id8=str(sticky_case_id8) if sticky_case_id8 else None,
        sticky_market=str(sticky_market) if sticky_market else None,
        sticky_product_id=str(sticky_product_id) if sticky_product_id else None,
        sticky_page_id=str(sticky_page_id) if sticky_page_id else None,
        sticky_post_id=str(sticky_post_id) if sticky_post_id else None,
        listed_offset=listed_offset,
        on_progress=on_progress,
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
            "sticky_market": pref.sticky_market or sticky_market,
            "sticky_product_id": pref.sticky_product_id or sticky_product_id,
            "sticky_page_id": pref.sticky_page_id or sticky_page_id,
            "sticky_post_id": pref.sticky_post_id or sticky_post_id,
            "order_list_shown": pref.order_list_shown,
            "wp_ops_context": wp_ops_context,
            **extra,
        }

    direct_names = [name for name, _ in tool_bits]
    catalog_pending = (
        pref.pending_action is not None
        and pref.pending_action.kind in {"product_update", "page_update", *_SITE_PENDING}
    )
    direct_catalog = any(name in _DIRECT_CATALOG or name in _DIRECT_SITE for name in direct_names)
    if catalog_pending or (
        not pref.pending_action
        and direct_names
        and "lookup_order" not in direct_names
        and (
            "list_recent_orders" in direct_names
            or direct_names == ["capabilities"]
            or direct_catalog
        )
    ):
        text = _format_tools_human(
            tool_bits,
            approve_confirm=pref.approve_confirm_only,
            pending=pref.pending_action,
        )
        return ChatResult(text=text, messages=_with_turn(text), cost_usd=0.0, **_base())

    router = get_model_router()
    sticky_for_cont = pref.sticky_order_id or sticky_order_id
    sticky_case_for_cont = pref.sticky_case_id8 or sticky_case_id8 or case_id8

    def _no_llm_text(reason: str, *, tools_suffix: str = "") -> str:
        from ecom_ops.bot.recovery import continuity_fallback

        if tool_bits or pref.pending_action:
            text = _format_tools_human(
                tool_bits,
                approve_confirm=pref.approve_confirm_only,
                pending=pref.pending_action,
            )
            return text + tools_suffix if tools_suffix else text
        cont = continuity_fallback(
            prior_digest=prior_digest,
            sticky_order_id=str(sticky_for_cont) if sticky_for_cont else None,
            sticky_case_id8=str(sticky_case_for_cont) if sticky_case_for_cont else None,
            reason=reason,
        )
        if cont:
            return cont
        if reason == "budget":
            return FALLBACK_BUDGET
        if reason == "error":
            return FALLBACK_ERROR
        return FALLBACK_NO_KEY

    if not router.any_configured():
        text = _no_llm_text("no_key")
        return ChatResult(text=text, messages=_with_turn(text), **_base())

    cap = openrouter_cap_usd()
    if not tel.within_budget(cap) and not router.has_route_outside_openrouter_budget():
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
        text = _no_llm_text(
            "budget",
            tools_suffix="\n\n(LLM-budget slut — rå data ovan.)",
        )
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

    def _complete_stream(
        prompt_messages: list[dict[str, str]], *, timeout: int = 30
    ) -> tuple[str, float]:
        stream = _SafeTextStream(on_text_delta, on_text_reset)
        content, cost = chat_completion(
            prompt_messages,
            model=model,
            route_id=route_id,
            max_tokens=_max_tokens(session),
            temperature=_think_temperature(session),
            timeout=timeout,
            telemetry=tel,
            site=site,
            actor=actor,
            on_text_delta=stream.push if on_text_delta else None,
            on_text_reset=stream.reset if on_text_delta else None,
        )
        stream.finish(content)
        return content, cost

    system_prompt, prompt_version = _build_system_prompt()
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(hist)
    if tool_block:
        messages.append({"role": "system", "content": tool_block})
    _checkpoint()
    messages.extend({"role": "user", "content": value} for value in turn_user_messages)

    # OpenRouter slugs must not be sent to ChatGPT or xAI. Those routes
    # pick OPENAI_MODEL / XAI_MODEL when model is omitted. A session pin
    # applies only when the Telegram route, or the first configured route,
    # is OpenRouter. AZOM_TELEGRAM_MODEL_ROUTE calls that one route.
    telegram_route = (os.environ.get("AZOM_TELEGRAM_MODEL_ROUTE") or "").strip()
    if telegram_route not in {"chatgpt_oauth", "xai_grok_build", "xai_api_key", "openrouter"}:
        telegram_route = ""
    route_id = telegram_route or None
    if telegram_route == "openrouter" or (
        not telegram_route and router.preferred_route_id() == "openrouter"
    ):
        model = _session_model(session) or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    elif telegram_route in {"xai_grok_build", "xai_api_key"}:
        model = (os.environ.get("XAI_MODEL") or "").strip() or None
    elif telegram_route == "chatgpt_oauth":
        model = (os.environ.get("OPENAI_MODEL") or "").strip() or None
    else:
        model = None
    try:
        if on_progress:
            on_progress("💭 Formulerar svar…")
        content, cost = _complete_stream(messages)
    except ModelRouteError as exc:
        tel.record(
            action="llm_telegram_chat_error",
            site=site,
            cost_usd=0.0,
            meta={"error": type(exc).__name__, "error_code": exc.code},
        )
        text = _no_llm_text(
            "error",
            tools_suffix="\n\n(LLM nere — visar verktygsdata.)",
        )
        return ChatResult(text=text, messages=_with_turn(text), **_base())

    # A steer that arrived during the provider call is applied before any
    # model-created WordPress tool request is permitted to run.
    initial_steers = _checkpoint()
    if initial_steers:
        if on_text_reset:
            on_text_reset()
        messages.append({"role": "assistant", "content": content[:4000]})
        messages.extend(
            {"role": "user", "content": value} for value in initial_steers
        )
        try:
            content, added_cost = _complete_stream(messages)
            cost += added_cost
        except ModelRouteError as exc:
            tel.record(
                action="llm_telegram_chat_error",
                site=site,
                cost_usd=0.0,
                meta={"error": type(exc).__name__, "error_code": exc.code},
            )
            text = _no_llm_text("error", tools_suffix="\n\n(LLM nere - visar verktygsdata.)")
            return ChatResult(text=text, messages=_with_turn(text), **_base())

    def _steers_before_tool() -> list[str]:
        values = _checkpoint()
        if values and on_text_reset:
            on_text_reset()
        return values

    wp_loop = run_bounded_wp_tool_loop(
        content,
        messages,
        complete=lambda followup_messages: _complete_stream(
            followup_messages, timeout=30
        ),
        actor=actor,
        channel=channel,
        conversation_id=conversation_id,
        context=wp_ops_context if isinstance(wp_ops_context, dict) else None,
        wp_intent=bool(WP_OPS_INTENT_RE.search(user_message)) or isinstance(wp_ops_context, dict),
        on_tool_status=lambda name, status: _report_wp_tool_status(
            on_progress, name, status
        ),
        before_tool=lambda: _steers_before_tool(),
    )
    if wp_loop.handled:
        content = wp_loop.content
        cost += wp_loop.followup_cost_usd
        stored_context = context_for_storage(
            actor=actor,
            channel=channel,
            conversation_id=conversation_id,
            site_id=wp_loop.site_id,
            operation_ids=wp_loop.operation_ids,
        )
        if stored_context is not None:
            wp_ops_context = stored_context
        else:
            wp_ops_context = None
        if wp_loop.model_error:
            tel.record(
                action="wp_agent_followup_error",
                site=site,
                cost_usd=0.0,
                meta={"tool_calls": wp_loop.calls, "tool_seconds": round(wp_loop.tool_seconds, 3)},
            )
    else:
        tool_match = _TOOL_CALL_RE.search(content or "")
        tool_name = ""
        if tool_match:
            try:
                tool_payload = json.loads(tool_match.group(1))
                if isinstance(tool_payload, dict):
                    tool_name = str(tool_payload.get("name") or "")
            except (json.JSONDecodeError, TypeError):
                tool_name = ""
        tool_label = _MODEL_TOOL_LABELS.get(tool_name)
        if tool_label and on_progress:
            on_progress(f"🔧 {tool_label} pågår…")
        fulfilled = _fulfill_model_tool_call(
            content,
            domain=pref.sticky_market or sticky_market,
            offset=listed_offset,
        )
        if fulfilled:
            if tool_label and on_progress:
                on_progress(f"✓ {tool_label}: resultat mottaget")
            content, shown = fulfilled
            if shown is not None:
                pref.order_list_shown = shown
        elif tool_label and on_progress:
            on_progress(f"⚠️ {tool_label}: resultat kunde inte hämtas")
        elif "<tool_call>" in (content or "").lower():
            content = (
                "Det anropet kördes inte. Säg vad du vill se eller ändra, "
                "till exempel senaste ordrar, en produkt, en sida eller ett inlägg. "
                "Ändringar väntar på bekräftelse."
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
            "prompt_version": prompt_version,
            "tools": [n for n, _ in tool_bits],
            "offer_escalate": offer_escalate,
            "soft_escalate_nudge": soft_nudge or soft_extra,
            "pending": pref.pending_action.kind if pref.pending_action else None,
            "wp_tool_calls": wp_loop.calls if wp_loop.handled else 0,
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

    base = _base(
        soft_escalate_nudge=soft_nudge or soft_extra,
        wp_ops_context=wp_ops_context,
    )
    return ChatResult(
        text=reply,
        messages=_with_turn(content),
        cost_usd=cost,
        **base,
    )
