"""Pre-change clarification and ambiguity detection for Azom chat bots.

Detects ambiguous change requests (including references to screenshots or
annotations like "ringat in på bilden", vague UI/layout/code requests, or
missing targets/values), parses structured ``<clarify>{...}</clarify>`` blocks,
and renders scannable Swedish verification prompts with clickable options.

Clarification never writes files, executes mutations, or bypasses HITL/RBAC.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_CLARIFY_ROUNDS = 3
MAX_CLARIFY_OPTIONS = 4
CLARIFY_TTL_SECONDS = 12 * 3600
CLARIFY_CALLBACK_PREFIX = "clarify:"

_CLARIFY_RE = re.compile(r"<clarify>\s*(\{.*?\})\s*</clarify>", re.I | re.S)
_CLARIFY_TAG_ANY_RE = re.compile(r"</?clarify>", re.I)
_CLARIFY_BLOCK_RE = re.compile(r"<clarify>(.*?)</clarify>", re.I | re.S)
_FENCE_START_RE = re.compile(r"^\s*```(?:json)?\s*", re.I)
_FENCE_END_RE = re.compile(r"\s*```\s*$")

_VALID_MODES = frozenset({"options", "confirm"})
_VALID_CATEGORIES = frozenset(
    {
        "wp_visual",
        "wp_code",
        "page_content",
        "product",
        "site_settings",
        "order",
        "general_change",
    }
)
_OPTION_ID_RE = re.compile(r"^[a-z0-9_-]{1,12}$")

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.I | re.S)
_TOOL_CALL_TAG_RE = re.compile(r"</?tool_call>", re.I)
_AUTH_SECRET_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_KEY_SECRET_RE = re.compile(
    r"\b(?:"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{6,}|"
    r"xai-[A-Za-z0-9_-]{6,}|"
    r"AIza[0-9A-Za-z_-]{8,}|"
    r"ghp_[A-Za-z0-9]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")\b"
)
_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|access_token|refresh_token|id_token|api_key|apikey|secret|"
    r"password|authorization|code_verifier)\s*[=:]\s*\S+"
)
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CLARIFY_PROTOCOL_GUIDE = (
    "Förtydligande före ändring (<clarify>): När en ändringsbegäran är tvetydig, "
    "hänvisar till en skärmdump/bild eller markering (t.ex. 'ringat in på bilden'), "
    "gäller visuell design/layout utan exakt CSS/fil-diff, saknar mål-ID/fil eller "
    "saknar tydligt målvärde, fråga användaren kort innan du börjar ändra eller öppnar "
    "arbetsytor. Svara då med exakt ett block:\n"
    '<clarify>{"mode":"options","category":"wp_visual","target_summary":"...",'
    '"understanding":"Kort tolkning","question":"Fokuserad motfråga",'
    '"plan_steps":["Steg 1"],"options":[{"id":"1","label":"Kort val",'
    '"summary":"Vad valet innebär","action_hint":"Tekniskt spår","recommended":true}]}'
    "</clarify>\n"
    "Regler: mode='options' kräver 2–4 alternativ (högst 1 recommended=true); "
    "mode='confirm' används med 0–4 alternativ för slutlig bekräftelse. "
    f"Högst {MAX_CLARIFY_ROUNDS} rundor. "
    "Hoppa över <clarify> vid rena läsfrågor, hälsningar, slash-kommandon, "
    "tydliga skrivningar (t.ex. 'sätt order 1001 till completed', "
    "'ändra produkt 501 pris till 999', 'ändra sidan 2 titel till Kontakt', "
    "'ändra sajttitel till Butiken', 'aktivera plugin akismet') samt "
    "förankrade uppföljningsjusteringar, renderingar eller buggfixar på ett aktivt wr-*-förslag "
    "(t.ex. 'gör knappen större', 'byt till mörkblå', 'ta bort ramen', "
    "'rendera och visa screenshots', 'kundvagnsikonen är intryckt — fixa det'). "
    "När användaren redan har angett tydliga sidkrav för en ny sida/tacksida, gå direkt "
    "vidare till att bygga och förhandsvisa i wr-* med riktiga katalogprodukter + "
    "syntetisk zero-PII exempeldata utan onödiga extra rundor. "
    "När <clarify> skickas för en WP-/visuell ändring, behåll alltid kända "
    "site_id, plan_id, path och focus_selector i target_summary och action_hint "
    "så att nästa runda aldrig tappar kontext. "
    "Förtydligande utför aldrig skrivningar och ersätter aldrig bekräftelseknappar "
    "(proposal:yes / wpfile:stage / wpreview:stage) eller RBAC."
)


def _scrub_text(value: Any, *, max_len: int, single_line: bool = False) -> str:
    """Scrub tool-call tags, secret tokens, and control chars, then cap length."""
    raw = str(value or "")
    if not raw:
        return ""
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", raw)
    cleaned = _TOOL_CALL_TAG_RE.sub("", cleaned)
    cleaned = _CLARIFY_BLOCK_RE.sub("", cleaned)
    cleaned = _CLARIFY_TAG_ANY_RE.sub("", cleaned)
    cleaned = _AUTH_SECRET_RE.sub(r"\1 [REDACTED]", cleaned)
    cleaned = _KEY_SECRET_RE.sub("[REDACTED]", cleaned)
    cleaned = _SECRET_KV_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _CTRL_CHARS_RE.sub(" ", cleaned)
    if single_line:
        cleaned = " ".join(cleaned.replace("\r", " ").replace("\n", " ").split())
    else:
        lines = [" ".join(line.split()) for line in cleaned.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        cleaned = "\n".join(lines).strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def _sanitize_option_id(raw_id: Any, fallback_index: int) -> str:
    token = re.sub(r"[^a-z0-9_-]+", "", str(raw_id or "").strip().lower())[:12]
    if token and _OPTION_ID_RE.fullmatch(token):
        return token
    return str(fallback_index)


@dataclass
class ClarificationOption:
    """Single clickable clarification choice."""

    id: str
    label: str
    summary: str
    action_hint: str = ""
    recommended: bool = False

    def __post_init__(self) -> None:
        self.id = _sanitize_option_id(self.id, 1)
        self.label = _scrub_text(self.label, max_len=44, single_line=True)
        self.summary = _scrub_text(self.summary, max_len=240, single_line=True)
        self.action_hint = _scrub_text(self.action_hint, max_len=400, single_line=True)
        self.recommended = bool(self.recommended)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "summary": self.summary,
            "action_hint": self.action_hint,
            "recommended": self.recommended,
        }

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any] | ClarificationOption | None, *, fallback_index: int = 1
    ) -> ClarificationOption | None:
        if isinstance(raw, cls):
            if not raw.label:
                return None
            return cls(
                id=_sanitize_option_id(raw.id, fallback_index),
                label=raw.label,
                summary=raw.summary or raw.label,
                action_hint=raw.action_hint,
                recommended=raw.recommended,
            )
        if not isinstance(raw, dict):
            return None
        label = _scrub_text(raw.get("label") or raw.get("title") or "", max_len=44, single_line=True)
        if not label:
            return None
        summary = _scrub_text(raw.get("summary") or raw.get("description") or label, max_len=240, single_line=True)
        action_hint = _scrub_text(raw.get("action_hint") or raw.get("hint") or "", max_len=400, single_line=True)
        opt_id = _sanitize_option_id(raw.get("id") or "", fallback_index)
        return cls(
            id=opt_id,
            label=label,
            summary=summary or label,
            action_hint=action_hint,
            recommended=bool(raw.get("recommended", False)),
        )


def _normalize_options(raw_options: Any) -> tuple[ClarificationOption, ...]:
    if not raw_options:
        return ()
    parsed: list[ClarificationOption] = []
    for idx, item in enumerate(raw_options, start=1):
        opt = ClarificationOption.from_dict(item, fallback_index=idx)
        if opt is not None:
            parsed.append(opt)
    if not parsed:
        return ()
    # Ensure at most one recommended=True, placed first (preserving relative order).
    seen_rec = False
    deduped_rec: list[ClarificationOption] = []
    for opt in parsed:
        is_rec = bool(opt.recommended) and not seen_rec
        if is_rec:
            seen_rec = True
        deduped_rec.append(
            ClarificationOption(
                id=opt.id,
                label=opt.label,
                summary=opt.summary,
                action_hint=opt.action_hint,
                recommended=is_rec,
            )
        )
    deduped_rec.sort(key=lambda o: not o.recommended)
    capped = deduped_rec[:MAX_CLARIFY_OPTIONS]
    # Ensure option IDs are unique within this proposal.
    used_ids: set[str] = set()
    unique: list[ClarificationOption] = []
    for idx, opt in enumerate(capped, start=1):
        cid = opt.id
        if cid in used_ids:
            cid = str(idx)
            if cid in used_ids:
                for cand in ("1", "2", "3", "4"):
                    if cand not in used_ids:
                        cid = cand
                        break
        used_ids.add(cid)
        unique.append(
            ClarificationOption(
                id=cid,
                label=opt.label,
                summary=opt.summary,
                action_hint=opt.action_hint,
                recommended=opt.recommended,
            )
        )
    return tuple(unique)


@dataclass
class ClarificationProposal:
    """Structured pre-change verification question and selectable options."""

    understanding: str
    question: str
    options: tuple[ClarificationOption, ...] = ()
    plan_steps: tuple[str, ...] = ()
    mode: str = "options"
    category: str = "general_change"
    original_request: str = ""
    target_summary: str = ""
    round_num: int = 1
    source: str = "model"

    def __post_init__(self) -> None:
        self.understanding = _scrub_text(self.understanding, max_len=500, single_line=False)
        self.question = _scrub_text(self.question, max_len=500, single_line=False)
        self.original_request = _scrub_text(self.original_request, max_len=1000, single_line=False)
        self.target_summary = _scrub_text(self.target_summary, max_len=200, single_line=True)
        mode_norm = str(self.mode or "options").strip().lower()
        self.mode = mode_norm if mode_norm in _VALID_MODES else "options"
        cat_norm = str(self.category or "general_change").strip().lower()
        self.category = cat_norm if cat_norm in _VALID_CATEGORIES else "general_change"
        try:
            rnum = int(self.round_num or 1)
        except (TypeError, ValueError):
            rnum = 1
        self.round_num = max(1, rnum)
        self.source = _scrub_text(self.source or "model", max_len=32, single_line=True) or "model"
        steps: list[str] = []
        for s in (self.plan_steps or ())[:5]:
            cleaned = _scrub_text(s, max_len=200, single_line=True)
            if cleaned:
                steps.append(cleaned)
        self.plan_steps = tuple(steps)
        self.options = _normalize_options(self.options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "understanding": self.understanding,
            "question": self.question,
            "options": [opt.to_dict() for opt in self.options],
            "plan_steps": list(self.plan_steps),
            "mode": self.mode,
            "category": self.category,
            "original_request": self.original_request,
            "target_summary": self.target_summary,
            "round_num": self.round_num,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ClarificationProposal | None:
        if not isinstance(raw, dict):
            return None
        understanding = _scrub_text(raw.get("understanding") or "", max_len=500, single_line=False)
        question = _scrub_text(raw.get("question") or "", max_len=500, single_line=False)
        if not understanding or not question:
            return None
        mode = str(raw.get("mode") or "options").strip().lower()
        if mode not in _VALID_MODES:
            mode = "options"
        options = _normalize_options(raw.get("options") or ())
        if mode == "options" and len(options) < 2:
            return None
        steps_raw = raw.get("plan_steps")
        steps = tuple(steps_raw) if isinstance(steps_raw, (list, tuple)) else ()
        return cls(
            understanding=understanding,
            question=question,
            options=options,
            plan_steps=steps,
            mode=mode,
            category=str(raw.get("category") or "general_change"),
            original_request=str(raw.get("original_request") or ""),
            target_summary=str(raw.get("target_summary") or ""),
            round_num=raw.get("round_num", 1),
            source=str(raw.get("source") or "model"),
        )


@dataclass
class ChangeAssessment:
    """Result of heuristic ambiguity assessment on an inbound user message."""

    is_change_request: bool
    is_ambiguous: bool
    category: str
    reasons: tuple[str, ...]
    mentions_screenshot_or_annotation: bool


def has_clarify_tag(text: str) -> bool:
    """Return True if ``text`` contains any ``<clarify>`` or ``</clarify>`` tag."""
    return bool(_CLARIFY_TAG_ANY_RE.search(text or ""))


def strip_clarify_tags(text: str) -> str:
    """Remove ``<clarify>...</clarify>`` blocks and stray clarify tags cleanly."""
    raw = text or ""
    if not raw:
        return ""
    cleaned = _CLARIFY_BLOCK_RE.sub("", raw)
    cleaned = _CLARIFY_TAG_ANY_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def parse_clarify_block(
    content: str, *, original_request: str = "", round_num: int = 1
) -> ClarificationProposal | None:
    """Parse ``<clarify>{...}</clarify>`` into a validated ``ClarificationProposal``."""
    raw = content or ""
    if not raw:
        return None
    match = _CLARIFY_RE.search(raw)
    json_str = match.group(1) if match else ""
    if not json_str:
        block_match = _CLARIFY_BLOCK_RE.search(raw)
        if not block_match:
            return None
        inner = block_match.group(1).strip()
        inner = _FENCE_START_RE.sub("", inner)
        inner = _FENCE_END_RE.sub("", inner).strip()
        rematch = _CLARIFY_RE.search(f"<clarify>{inner}</clarify>")
        if not rematch:
            return None
        json_str = rematch.group(1)

    try:
        payload = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    understanding = _scrub_text(payload.get("understanding") or "", max_len=500, single_line=False)
    question = _scrub_text(payload.get("question") or "", max_len=500, single_line=False)
    if not understanding or not question:
        return None

    mode = str(payload.get("mode") or "options").strip().lower()
    if mode not in _VALID_MODES:
        mode = "options"

    options = _normalize_options(payload.get("options") or ())
    if mode == "options" and not (2 <= len(options) <= MAX_CLARIFY_OPTIONS):
        return None

    steps_raw = payload.get("plan_steps")
    steps = tuple(steps_raw) if isinstance(steps_raw, (list, tuple)) else ()
    orig = original_request or str(payload.get("original_request") or "")
    return ClarificationProposal(
        understanding=understanding,
        question=question,
        options=options,
        plan_steps=steps,
        mode=mode,
        category=str(payload.get("category") or "general_change"),
        original_request=orig,
        target_summary=str(payload.get("target_summary") or ""),
        round_num=round_num,
        source=str(payload.get("source") or "model"),
    )


# ---------------------------------------------------------------------------
# Ambiguity detection heuristics
# ---------------------------------------------------------------------------

_SHORT_ACK_RE = re.compile(
    r"^\s*(?:"
    r"hej+|tjena|hall[åa]|tja|god\s+(?:morgon|dag|kv[äa]ll)|"
    r"tack(?:ar)?(?:\s+s[åa]\s+mycket)?|ok(?:ej)?|fine|yes|no|"
    r"ja+|japp|jo|nej+|n[äa]|status|l[äa]ge(?:t)?|"
    r"hur\s+[äa]r\s+det|bra|perfekt|toppen|kanon|absolut|"
    r"st[äa]mmer|k[öo]r|avbryt|skippa|gl[öo]m\s+det|"
    r"#?\d{1,12}"
    r")\s*[.!?🙏👍👌✅✖️]*\s*$",
    re.I,
)

_SITE_REFUSAL_RE = re.compile(
    r"\b(?:"
    r"(?:installera|avinstallera|radera|ta\s+bort|delete)\s+"
    r"(?:ett\s+|det\s+|nytt\s+|hela\s+)?(?:plugin(?:et|s)?|till[äa]gg(?:et)?|tema(?:t|n)?|theme(?!\.json\b))|"
    r"(?:byt|byta|v[äa]xla)\s+(?:det\s+|ett\s+|nytt\s+|annat\s+|nuvarande\s+|aktiva?\s+)?(?:tema(?:t|n)?|theme(?!\.json\b))|"
    r"(?:radera|ta\s+bort|delete)\s+(?:ett\s+|det\s+)?(?:inl[äa]gg(?:et)?|post)|"
    r"(?:skapa|l[äa]gg\s+till)\s+(?:ett\s+)?nytt\s+(?:blogginl[äa]gg|inl[äa]gg|post)|"
    r"nytt\s+(?:blogginl[äa]gg|inl[äa]gg|post)"
    r")\b",
    re.I,
)

_EXPLICIT_CHANGE_VERB_RE = re.compile(
    r"\b(?:"
    r"[äa]ndra|uppdatera|byt|byta|justera|fixa|g[öo]r\s+om|g[öo]r\s+s[åa]|"
    r"l[äa]gg\s+till|l[äa]gga\s+till|ta\s+bort|flytta|d[öo]lj|g[öo]m|"
    r"designa|styla|f[öo]rb[äa]ttra|f[öo]rstora|f[öo]rminska|centrera|skriv\s+om|"
    r"bygg(?:a)?|skapa|s[äa]tt|markera|aktivera|inaktivera|avaktivera|deaktivera|sl[åa]\s+p[åa]|sl[åa]\s+av|st[äa]ng\s+av|"
    r"redigera|refaktorera|patcha|generera|"
    r"change|update|modify|edit|adjust|fix|redesign|move|hide|remove|add|resize|center|rewrite|build|create|set|enable|disable|style"
    r")\b",
    re.I,
)

_SOFT_CHANGE_INTENT_RE = re.compile(
    r"\b(?:"
    r"g[öo]r\s+(?:den|det|knappen|rubriken|texten|sidan|headern|footern|menyn|layouten)\s+\w+|"
    r"vill\s+(?:ha|att)\s+.{1,60}\b(?:st[öo]rre|mindre|r[öo]d|bl[åa]|gr[öo]n|svart|vit|centrerad|bort|dold|snyggare|b[äa]ttre|tydligare)|"
    r"ska\s+(?:bort|d[öo]ljas|flyttas|[äa]ndras|uppdateras|vara\s+(?:st[öo]rre|mindre|centrerad))|"
    r"beh[öo]ver\s+(?:[äa]ndras|fixas|uppdateras|justeras|f[öo]rb[äa]ttras)"
    r")\b",
    re.I,
)

_READ_ONLY_QUERY_RE = re.compile(
    r"\b(?:"
    r"visa|lista|kolla|h[äa]mta|s[öo]k|leta|vad\s+[äa]r|vad\s+heter|hur\s+m[åa]nga|"
    r"hur\s+g[åa]r|hur\s+m[åa]r|granska|vilket\s+tema|vilka\s+plugins|status\s+p[åa]|"
    r"analysera|kontrollera|inspektera|j[äa]mf[öo]r|"
    r"show|list|check|what\s+is|how\s+many|inspect|review|audit"
    r")\b",
    re.I,
)

_SCREENSHOT_ANNOTATION_RE = re.compile(
    r"\b(?:"
    r"print|printscreen|print\s+screen|sk[äa]rmdump(?:en)?|sk[äa]rmbild(?:en)?|"
    r"screenshot(?:en|et|s)?|bild(?:en)?|p[åa]\s+bilden|"
    r"ringat(?:\s+in)?|inringa(?:t|d|de)?|"
    r"markerat|markerad|markering(?:en)?|"
    r"r[öo]d(?:a)?\s+(?:ring|pil|markering)|pil(?:en)?"
    r")\b",
    re.I,
)

_VAGUE_WORDS_RE = re.compile(
    r"\b(?:"
    r"h[äa]r|d[äa]r|den\s+h[äa]r|det\s+h[äa]r|detta|"
    r"snyggare|b[äa]ttre|tydligare|lite|typ|n[åa]got|"
    r"modernare|st[öo]rre|mindre|fixa|fr[äa]schare|finare|proffsigare"
    r")\b",
    re.I,
)

_VISUAL_DESIGN_RE = re.compile(
    r"\b(?:"
    r"layout(?:en)?|design(?:en)?|utseende(?:t)?|visuell(?:t|a)?|"
    r"f[äa]rg(?:en|er|erna)?|r[öo]d(?:a|t)?|bl[åa](?:tt)?|m[öo]rkbl[åa](?:tt)?|ljusbl[åa](?:tt)?|"
    r"gr[öo]n(?:a|t)?|svart(?:a)?|vit(?:a|t)?|gr[åa](?:tt)?|gul(?:a|t)?|orange|lila|rosa|"
    r"knapp(?:en|ar|arna)?|k[öo]pknapp(?:en|ar|arna)?|meny(?:n)?|navigering(?:en)?|navbar|"
    r"header(?:n)?|footer(?:n)?|hero(?:n)?|banner(?:n)?|sektion(?:en)?|"
    r"kolumn(?:er)?|marginal(?:en|er|erna)?|padding|margin|ram(?:en|ar|arna)?|kant(?:en|er)?|"
    r"hover|rundad(?:e)?|h[öo]rn(?:en)?|skugga(?:n)?|bredd(?:en)?|h[öo]jd(?:en)?|avst[åa]nd(?:et)?|"
    r"kompakt|typsnitt|font(?:en)?|rubrikstorlek|logga(?:n)?|logo(?:n)?|"
    r"ikon(?:en|er|erna)?|kundvagnsikon(?:en)?|[öo]verlapp\w*|intryckt|"
    r"f[öo]rslag(?:et|en)?|sandl[åa]da(?:n)?|sandbox|f[öo]rhandsvisning(?:en)?|"
    r"mobilvy(?:n)?|desktop(?:vy)?|responsiv(?:t|a)?|sidebar|widget|kort(?:en)?|rutn[äa]t|grid|"
    r"centrera|styla|designa|f[öo]rstora|f[öo]rminska|flytta|d[öo]lj"
    r")\b",
    re.I,
)

_VISUAL_DEFECT_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"[öo]verlapp\w*|intryckt|krockar|snett|trasig(?:t|a)?|"
    r"f[öo]r\s+(?:stor|stort|liten|litet|bred|smal|tr[åa]ng|t[äa]t)"
    r")\b",
    re.I,
)

_SWITCH_TARGET_RE = re.compile(
    r"\b(?:"
    r"n[åa]got\s+annat|annan\s+(?:sida|produkt|order)|annat\s+inl[äa]gg|"
    r"helt\s+nytt|nytt\s+[äa]rende"
    r")\b",
    re.I,
)

_ANCHORED_UI_CUE_RE = re.compile(
    r"\b(?:"
    r"knapp(?:en|ar|arna)?|k[öo]pknapp(?:en|ar|arna)?|ikon(?:en|er|erna)?|kundvagnsikon(?:en)?|"
    r"ram(?:en|ar|arna)?|marginal(?:en|er|erna)?|padding|margin|hover|"
    r"f[äa]rg(?:en|er|erna)?|r[öo]d(?:a|t)?|bl[åa](?:tt)?|m[öo]rkbl[åa](?:tt)?|ljusbl[åa](?:tt)?|"
    r"gr[öo]n(?:a|t)?|svart(?:a)?|vit(?:a|t)?|gr[åa](?:tt)?|"
    r"typsnitt|font(?:en)?|rubrik(?:en)?|text(?:en)?|storlek(?:en)?|st[öo]rre|mindre|"
    r"rundad(?:e)?|h[öo]rn(?:en)?|bredd(?:en)?|h[öo]jd(?:en)?|kompakt|"
    r"layout(?:en)?|design(?:en)?|css|style\.css|"
    r"f[öo]rslag(?:et|en)?|f[öo]rhandsvisning(?:en|ar)?|sandl[åa]da(?:n)?|sandbox|"
    r"rendera|preview|screenshot(?:s)?|sk[äa]rmdump(?:ar|en)?|"
    r"[öo]verlapp\w*|intryckt|wr-[a-f0-9]{4,12}"
    r")\b",
    re.I,
)

_CODE_FILE_RE = re.compile(
    r"functions\.php|style\.css|theme\.json|header\.php|footer\.php|single\.php|"
    r"page\.php|front-page\.php|archive\.php|woocommerce/|"
    r"\b(?:"
    r"[a-z0-9_-]+\.(?:php|css|scss|js|json|html)|"
    r"temafil(?:er)?|pluginfil(?:er)?|temakod|pluginkod|k[äa]llkod(?:en)?|"
    r"barntema(?:t|n)?|child[\s_-]?theme|snippet(?:s)?|hook(?:s|ar)?|"
    r"add_action|add_filter|shortcode(?:s)?|template(?:s)?|mall(?:ar|en)?|"
    r"wr-[a-f0-9]{4,12}|fe-[a-f0-9]{4,12}|css|php|scss"
    r")\b",
    re.I,
)

_EXPLICIT_FILE_PATH_RE = re.compile(
    r"\b[a-z0-9_./-]+\.(?:php|css|scss|js|json|html)\b",
    re.I,
)
_EXACT_CSS_OR_DIFF_RE = re.compile(
    r"(?:"
    r"[.#][a-z0-9_-]+\s*\{[^}]+\}|"
    r"\b[a-z-]+\s*:\s*[^;{}]{1,60};|"
    r"\brad\s+\d+\b|"
    r"@@\s*-\d+|"
    r"```"
    r")",
    re.I,
)

_ORDER_WORD_RE = re.compile(r"\b(?:order(?:nr|nummer)?|best[äa]llning(?:en)?)\b", re.I)
_PRODUCT_WORD_RE = re.compile(
    r"\b(?:produkt(?:en|er|beskrivning)?|product|lagerstatus|lagersaldo|lagret|lager|pris(?:et)?|sku)\b",
    re.I,
)
_SITE_SETTINGS_WORD_RE = re.compile(
    r"\b(?:sajttitel|sajtens\s+titel|webbplatsens\s+titel|tagline(?:n)?|undertitel(?:n)?|"
    r"sajtbeskrivning(?:en)?|webbplatsens\s+beskrivning|inst[äa]llning(?:ar)?)\b|"
    r"\b(?:aktivera|inaktivera|avaktivera|deaktivera|sl[åa]\s+p[åa]|sl[åa]\s+av|st[äa]ng\s+av)\s+"
    r"(?:plugin(?:et)?|till[äa]gget?)\b",
    re.I,
)
_SITE_GLOBAL_FIELD_RE = re.compile(
    r"\b(?:sajttitel|sajtens\s+titel|webbplatsens\s+titel|tagline(?:n)?|undertitel(?:n)?|"
    r"sajtbeskrivning(?:en)?|webbplatsens\s+beskrivning)\b",
    re.I,
)
_PLUGIN_ACTION_WITH_ID_RE = re.compile(
    r"\b(?:aktivera|inaktivera|avaktivera|deaktivera|sl[åa]\s+p[åa]|sl[åa]\s+av|st[äa]ng\s+av)\s+"
    r"(?:plugin(?:et)?|till[äa]gget?)\s+[a-z0-9][a-z0-9_./-]{1,80}\b",
    re.I,
)
_PAGE_CONTENT_WORD_RE = re.compile(
    r"\b(?:sid(?:an|a|or|orna)|page|startsida(?:n)?|hemsida(?:n)?|landningssid(?:a|an)|"
    r"kampanjsid(?:a|an)|tacksid(?:a|an)|thank[- ]?you|orderbekr[äa]ftelse|"
    r"kassasida(?:n)?|kontaktsida(?:n)?|inl[äa]gg(?:et|en)?|blogginl[äa]gg(?:et)?|post|"
    r"rubrik(?:en)?|titel|br[öo]dtext|inneh[åa]ll(?:et)?|text(?:en)?)\b",
    re.I,
)

_NUMERIC_ID_RE = re.compile(r"(?:#|\b)\d{1,12}\b")
_TARGET_VALUE_RE = re.compile(r"(?:\b(?:till|to|fr[åa]n)\b\s+\S+|->\s*\S+|→\s*\S+|:\s*\S+)", re.I)
_EXPLICIT_PRODUCT_DESC_RE = re.compile(
    r"\b(?:generera|skriv)\s+(?:en\s+)?(?:ny\s+)?(?:produkt)?beskrivning\b.{0,40}\b(?:produkt|product|#)?\s*\d{1,12}\b",
    re.I,
)


def _classify_category(text: str, *, mentions_screenshot: bool) -> str:
    if _ORDER_WORD_RE.search(text) and not _CODE_FILE_RE.search(text) and not _VISUAL_DESIGN_RE.search(text):
        return "order"
    if _PRODUCT_WORD_RE.search(text) and not _CODE_FILE_RE.search(text) and not _VISUAL_DESIGN_RE.search(text):
        return "product"
    if _SITE_SETTINGS_WORD_RE.search(text) and not _CODE_FILE_RE.search(text):
        return "site_settings"
    if _CODE_FILE_RE.search(text):
        return "wp_code"
    if _VISUAL_DESIGN_RE.search(text) or mentions_screenshot:
        return "wp_visual"
    if _PAGE_CONTENT_WORD_RE.search(text):
        return "page_content"
    return "general_change"


def _prefix_before_target_value(text: str) -> str:
    """Return text before an explicit `till/to/:/->` value clause for vague-word checks."""
    match = re.search(r"\s+(?:till|to)\s+|->|→|:\s+", text, re.I)
    if match:
        return text[: match.start()]
    return text


def detect_change_ambiguity(
    text: str,
    *,
    has_pending_action: bool = False,
    has_tool_results: bool = False,
    has_wp_site_context: bool = False,
    clarification_confirmed: bool = False,
    has_images: bool = False,
    has_active_wp_review: bool = False,
) -> ChangeAssessment:
    """Assess whether ``text`` is an ambiguous change request needing clarification."""
    del has_tool_results, has_wp_site_context  # accepted for caller contract parity
    raw = (text or "").strip()
    mentions_screenshot = bool(has_images) or bool(_SCREENSHOT_ANNOTATION_RE.search(raw))

    if not raw or raw.startswith("/"):
        return ChangeAssessment(False, False, "general_change", (), False)
    if clarification_confirmed or has_pending_action:
        return ChangeAssessment(False, False, "general_change", (), mentions_screenshot)
    if _SHORT_ACK_RE.match(raw):
        return ChangeAssessment(False, False, "general_change", (), False)
    if _SITE_REFUSAL_RE.search(raw):
        return ChangeAssessment(False, False, "general_change", (), mentions_screenshot)

    has_change_verb = bool(_EXPLICIT_CHANGE_VERB_RE.search(raw))
    has_soft_intent = bool(_SOFT_CHANGE_INTENT_RE.search(raw)) or (
        has_active_wp_review and bool(_VISUAL_DEFECT_FOLLOWUP_RE.search(raw))
    )
    if _READ_ONLY_QUERY_RE.search(raw) and not has_change_verb and not has_soft_intent:
        return ChangeAssessment(False, False, "general_change", (), mentions_screenshot)
    if not has_change_verb and not has_soft_intent:
        return ChangeAssessment(False, False, "general_change", (), mentions_screenshot)

    category = _classify_category(raw, mentions_screenshot=mentions_screenshot)
    if (
        has_active_wp_review
        and category in {"wp_visual", "wp_code", "general_change"}
        and not _SWITCH_TARGET_RE.search(raw)
        and bool(_ANCHORED_UI_CUE_RE.search(raw))
    ):
        return ChangeAssessment(
            is_change_request=True,
            is_ambiguous=False,
            category=category,
            reasons=(),
            mentions_screenshot_or_annotation=mentions_screenshot,
        )

    reasons: list[str] = []

    has_file_path = bool(_EXPLICIT_FILE_PATH_RE.search(raw))
    has_exact_diff = bool(_EXACT_CSS_OR_DIFF_RE.search(raw))
    has_numeric_id = bool(_NUMERIC_ID_RE.search(raw))
    has_target_value = bool(_TARGET_VALUE_RE.search(raw))

    if mentions_screenshot and not (has_file_path and has_exact_diff):
        reasons.append(
            "Hänvisar till skärmdump eller markering/inringning utan exakt teknisk specifikation"
        )

    if category == "wp_visual" and not (has_file_path and has_exact_diff):
        reasons.append("Visuell/layoutändring utan exakt CSS-, fil- eller diff-specifikation")

    # Check vague words outside the literal replacement value when an explicit value exists.
    vague_scope = _prefix_before_target_value(raw) if has_target_value else raw
    if _VAGUE_WORDS_RE.search(vague_scope):
        reasons.append("Innehåller vaga uttryck (t.ex. här, snyggare, bättre, fixa) som behöver preciseras")

    if category in {"order", "product", "page_content"}:
        if not has_numeric_id:
            reasons.append("Saknar tydligt numeriskt mål-ID (order-, produkt-, sid- eller inläggsnummer)")
        if not has_target_value and not _EXPLICIT_PRODUCT_DESC_RE.search(raw):
            reasons.append("Saknar tydligt målvärde (vad fältet ska ändras till)")
    elif category == "site_settings":
        is_global = bool(_SITE_GLOBAL_FIELD_RE.search(raw))
        is_plugin_toggle = bool(_PLUGIN_ACTION_WITH_ID_RE.search(raw))
        if not is_global and not is_plugin_toggle:
            reasons.append("Saknar tydligt plugin-namn eller namngiven sajtinställning")
        if is_global and not has_target_value:
            reasons.append("Saknar tydligt målvärde för sajtinställningen")
    elif category == "wp_code":
        if not has_file_path:
            reasons.append("Saknar exakt filnamn eller sökväg i tema/plugin")
        if not has_exact_diff and not has_target_value:
            reasons.append("Saknar exakt kod- eller CSS-specifikation (diff/värde)")
    elif category == "wp_visual":
        if not has_target_value and not has_exact_diff:
            reasons.append("Saknar tydligt målvärde eller exakt visuell specifikation")
        reasons.append("Kan lösas på flera tekniska lager (t.ex. sidinnehåll, tema-CSS eller mallfil)")
    else:
        if not has_numeric_id and not has_file_path:
            reasons.append("Saknar tydligt mål (ID, slug eller filnamn)")
        if not has_target_value:
            reasons.append("Saknar tydligt målvärde (vad som ska ändras och till vad)")

    deduped_reasons = tuple(dict.fromkeys(reasons))
    return ChangeAssessment(
        is_change_request=True,
        is_ambiguous=bool(deduped_reasons),
        category=category,
        reasons=deduped_reasons,
        mentions_screenshot_or_annotation=mentions_screenshot,
    )


def build_clarification_prompt_hint(
    assessment: ChangeAssessment,
    *,
    has_wp_site_context: bool = False,
    site_id: str | None = None,
    round_num: int = 1,
) -> str:
    """Build internal prompt hint instructing the model when/how to ask ``<clarify>``."""
    reasons_txt = "; ".join(assessment.reasons) or "tvetydig ändringsbegäran"
    lines = [
        f"CLARIFY-HINT (runda {max(1, round_num)}/{MAX_CLARIFY_ROUNDS}): "
        f"Begäran bedöms som tvetydig ändring (kategori={assessment.category}).",
        f"Orsaker: {reasons_txt}.",
    ]
    if assessment.mentions_screenshot_or_annotation:
        lines.append(
            "Användaren hänvisar till en bild/skärmdump eller inringning som du inte kan se "
            "direkt i textchatten. Bekräfta vad du tror avses och erbjud 2–4 konkreta tolkningar/spår."
        )
    if has_wp_site_context or site_id or assessment.category in {"wp_visual", "wp_code"}:
        site_note = f" (aktiv sajt: {site_id})" if site_id else ""
        lines.append(
            f"WP-kontext finns{site_note}: du får göra läsningar vid behov, men fråga med "
            "<clarify>{...}</clarify> innan du öppnar arbetsyta eller föreslår ändringar. "
            "Behåll kända site_id, plan_id, path och focus_selector i target_summary och action_hint."
        )
    if round_num >= MAX_CLARIFY_ROUNDS:
        lines.append(
            "Detta är sista förtydliganderundan: använd helst mode='confirm' med tydlig "
            "sammanfattning så användaren kan godkänna eller justera."
        )
    else:
        lines.append(
            "Svara användaren med ett giltigt <clarify>{...}</clarify>-block (utan <tool_call> inuti) "
            "och utför inga skrivningar."
        )
    return "\n".join(lines)


def build_clarified_request_message(
    original_request: str,
    picks: list[dict[str, Any]],
    *,
    extra_note: str = "",
    confirmed: bool = False,
) -> str:
    """Assemble clarified user intent context for subsequent model/handler turns."""
    orig = _scrub_text(original_request, max_len=1000, single_line=False)
    lines = [f"Ursprunglig begäran: {orig or '(saknas)'}"]
    if picks:
        lines.append("Valda förtydliganden:")
        for idx, pick in enumerate(picks, start=1):
            if not isinstance(pick, dict):
                continue
            label = _scrub_text(pick.get("label") or pick.get("id") or f"Val {idx}", max_len=60, single_line=True)
            summary = _scrub_text(pick.get("summary") or "", max_len=240, single_line=True)
            hint = _scrub_text(pick.get("action_hint") or "", max_len=300, single_line=True)
            item = f"- {label}"
            if summary and summary != label:
                item += f": {summary}"
            if hint:
                item += f" [spår: {hint}]"
            lines.append(item)
    clean_note = _scrub_text(extra_note, max_len=600, single_line=False)
    if clean_note:
        lines.append(f"Användarens tillägg: {clean_note}")
    if confirmed:
        lines.append(
            "Status: Användaren har bekräftat inriktningen. Gå vidare enligt vanliga "
            "läs- och bekräftelseregler (skapa förslag/arbetsyta med godkännandeknapp; "
            "utför aldrig tysta skrivningar)."
        )
    else:
        lines.append(
            "Status: Användaren har valt ett steg. Fortsätt med fokuserat förtydligande "
            "om något fortfarande är oklart, eller läs och förbered bekräftelseförslag."
        )
    return "\n".join(lines)


def render_clarification_message(proposal: ClarificationProposal, *, intro: str = "") -> str:
    """Render a scannable Swedish clarification card for Telegram / Messenger."""
    header = (
        "✅ Bekräfta innan jag börjar arbeta"
        if proposal.mode == "confirm"
        else "🔍 Dubbelkoll innan jag börjar arbeta"
    )
    if proposal.round_num > 1:
        header = f"{header} (steg {proposal.round_num}/{MAX_CLARIFY_ROUNDS})"

    parts: list[str] = [header]
    clean_intro = _scrub_text(strip_clarify_tags(intro), max_len=600, single_line=False)
    if clean_intro and clean_intro != proposal.understanding:
        parts.append(clean_intro)
    if proposal.target_summary:
        parts.append(f"Mål: {proposal.target_summary}")
    parts.append(f"Min tolkning: {proposal.understanding}")
    parts.append(f"Fråga: {proposal.question}")

    if proposal.plan_steps:
        step_lines = ["Plan:"]
        for step in proposal.plan_steps:
            step_lines.append(f"• {step}")
        parts.append("\n".join(step_lines))

    if proposal.options:
        opt_lines = ["Välj ett spår:"]
        for idx, opt in enumerate(proposal.options, start=1):
            tag = f"⭐ {opt.label} (rekommenderat)" if opt.recommended else opt.label
            if opt.summary and opt.summary != opt.label:
                opt_lines.append(f"{idx}. {tag} — {opt.summary}")
            else:
                opt_lines.append(f"{idx}. {tag}")
        parts.append("\n".join(opt_lines))

    parts.append("Ingen ändring utförs förrän vi är överens och du bekräftar slutligt förslag.")
    return "\n\n".join(parts)


def build_fallback_clarification(
    text: str,
    assessment: ChangeAssessment,
    *,
    has_wp_site_context: bool = False,
    site_id: str | None = None,
    sticky_page_id: str | None = None,
    sticky_product_id: str | None = None,
) -> ClarificationProposal:
    """Build a tailored 3-option Swedish clarification proposal without an LLM call."""
    del has_wp_site_context
    category = assessment.category if assessment.category in _VALID_CATEGORIES else "general_change"

    target_bits: list[str] = []
    if site_id:
        target_bits.append(f"Sajt: {site_id}")
    if sticky_page_id and category in {"page_content", "wp_visual"}:
        target_bits.append(f"Sida #{sticky_page_id}")
    if sticky_product_id and category == "product":
        target_bits.append(f"Produkt #{sticky_product_id}")
    target_summary = " · ".join(target_bits)

    if category == "wp_visual":
        if assessment.mentions_screenshot_or_annotation:
            understanding = (
                "Du hänvisar till en bild eller markering som jag inte kan se direkt i textchatten, "
                "och vill göra en visuell eller layoutmässig ändring."
            )
            question = "Vilket element eller område gäller det, och vilket spår stämmer bäst?"
        else:
            understanding = "Du vill ändra utseende, placering eller layout på webbplatsen."
            question = "Vilket spår passar bäst för att precisera ändringen?"
        options = (
            ClarificationOption(
                id="css",
                label="Justera styling/CSS",
                summary="Ändra färg, storlek, marginal eller placering via tema-CSS.",
                action_hint="Föreslå säker CSS-justering efter läsning",
                recommended=True,
            ),
            ClarificationOption(
                id="layout",
                label="Ändra sidans block/layout",
                summary="Justera sektion, textblock eller knapp direkt i sidans innehåll eller mall.",
                action_hint="Kontrollera sidans innehåll och blockstruktur",
            ),
            ClarificationOption(
                id="inspect",
                label="Granska sidan först",
                summary="Läs nuvarande tema- och sidstruktur innan vi väljer ändringssätt.",
                action_hint="Läs sajt- och temadata utan att föreslå skrivning",
            ),
        )
    elif category == "wp_code":
        understanding = "Du vill göra en kodändring i WordPress-tema, plugin eller mallfiler."
        question = "Hur vill du att vi förbereder ändringen innan några filer rörs?"
        options = (
            ClarificationOption(
                id="workspace",
                label="Öppna granskningsyta",
                summary="Inspektera tema-/pluginfiler i isolerad arbetsyta och ta fram granskningsbar diff.",
                action_hint="Använd isolerad wr-arbetsyta efter läsning",
                recommended=True,
            ),
            ClarificationOption(
                id="single_diff",
                label="Enfils diff-förslag",
                summary="Ta fram en riktad diffplan för en specifik tema- eller pluginfil.",
                action_hint="Föreslå enfils diffplan med bas-/resultathash",
            ),
            ClarificationOption(
                id="read_first",
                label="Läs aktiva filer först",
                summary="Lista och läs berörda tema-/pluginfiler innan vi bestämmer ändring.",
                action_hint="Endast läsning av tema/plugin-översikt",
            ),
        )
    elif category == "page_content":
        understanding = "Du vill uppdatera innehåll, rubrik eller status på en sida eller ett inlägg."
        question = "Vilken typ av sid- eller inläggsändring vill du göra?"
        options = (
            ClarificationOption(
                id="text",
                label="Ändra text eller rubrik",
                summary="Uppdatera titel eller brödtext på en utpekad sida eller ett inlägg.",
                action_hint="Specificera sid-/inläggs-ID och ny text",
                recommended=True,
            ),
            ClarificationOption(
                id="status",
                label="Ändra publiceringsstatus",
                summary="Sätt sidan eller inlägget till publicerad, utkast eller privat.",
                action_hint="Specificera mål-ID och ny status",
            ),
            ClarificationOption(
                id="list_pages",
                label="Lista sidor/inlägg först",
                summary="Visa aktuella sidor och inlägg så att du kan peka ut rätt ID.",
                action_hint="Kör read-only lista av sidor/inlägg",
            ),
        )
    elif category == "product":
        understanding = "Du vill uppdatera en produkt i WooCommerce, men produkt-ID eller fältvärde saknas."
        question = "Vilken produktändring gäller det?"
        options = (
            ClarificationOption(
                id="price_stock",
                label="Pris eller lagerstatus",
                summary="Ändra ordinarie pris, lagersaldo eller lagerstatus på vald produkt.",
                action_hint="Ange produkt-ID samt nytt pris eller lagervärde",
                recommended=True,
            ),
            ClarificationOption(
                id="prod_text",
                label="Produktnamn/beskrivning",
                summary="Uppdatera titel, kort beskrivning eller generera ny produktbeskrivning.",
                action_hint="Ange produkt-ID och önskad textändring",
            ),
            ClarificationOption(
                id="find_prod",
                label="Sök/lista produkter först",
                summary="Visa produkter i katalogen för att hitta rätt produkt-ID först.",
                action_hint="Läs produktlista utan skrivning",
            ),
        )
    elif category == "site_settings":
        understanding = "Du vill ändra en övergripande sajtinställning eller plugin-status."
        question = "Vilken sajtinställning eller vilket plugin vill du hantera?"
        options = (
            ClarificationOption(
                id="identity",
                label="Sajttitel eller tagline",
                summary="Uppdatera webbplatsens titel eller undertitel (tagline).",
                action_hint="Ange fält (sajttitel/tagline) och nytt värde",
                recommended=True,
            ),
            ClarificationOption(
                id="plugin_stat",
                label="Slå på/av plugin",
                summary="Aktivera eller inaktivera ett befintligt plugin (kräver Oscar-behörighet).",
                action_hint="Ange plugin-slug och önskat läge",
            ),
            ClarificationOption(
                id="show_site",
                label="Visa nuvarande status",
                summary="Kontrollera sajtens titel, tagline och aktiva plugins först.",
                action_hint="Read-only sajt- och pluginstatus",
            ),
        )
    elif category == "order":
        understanding = "Du vill ändra en order i WooCommerce, men ordernummer eller ny status behöver preciseras."
        question = "Vad vill du göra med ordern?"
        options = (
            ClarificationOption(
                id="status",
                label="Ändra orderstatus",
                summary="Föreslå ny status (t.ex. processing, completed, on-hold, cancelled).",
                action_hint="Ange ordernummer och ny status",
                recommended=True,
            ),
            ClarificationOption(
                id="show_order",
                label="Visa ordern först",
                summary="Slå upp orderns nuvarande status och detaljer innan förslag.",
                action_hint="Read-only orderuppslag",
            ),
            ClarificationOption(
                id="list_orders",
                label="Lista senaste ordrar",
                summary="Visa de senaste ordrarna för att välja rätt ordernummer.",
                action_hint="Read-only orderlista",
            ),
        )
    else:
        understanding = "Du vill göra en ändring, men mål och exakt resultat behöver förtydligas innan jag föreslår något."
        question = "Vilket område gäller din ändring?"
        options = (
            ClarificationOption(
                id="site_ui",
                label="Design, sida eller text",
                summary="Ändra utseende, layout, sidtext eller tema-/CSS-detalj på webbplatsen.",
                action_hint="Precisera sida/element och önskat utseende",
                recommended=True,
            ),
            ClarificationOption(
                id="catalog",
                label="Produkt eller order",
                summary="Uppdatera pris, lager, beskrivning eller orderstatus i WooCommerce.",
                action_hint="Precisera order-/produkt-ID och nytt värde",
            ),
            ClarificationOption(
                id="inspect",
                label="Inspektera först",
                summary="Gör en läskontroll av berörd del innan vi tar fram ett ändringsförslag.",
                action_hint="Read-only kontroll",
            ),
        )

    return ClarificationProposal(
        understanding=understanding,
        question=question,
        options=options,
        mode="options",
        category=category,
        original_request=text,
        target_summary=target_summary,
        round_num=1,
        source="fallback",
    )
