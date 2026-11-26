"""Structured live-progress events and Telegram status-card state.

Provides a typed event contract between tool loops, the model router, and
Telegram's editable live-status card while staying backward-compatible with
plain ``Callable[[str], None]`` listeners in existing callers and tests.

Security invariants (SOUL.md / AGENTS.md):
- Never store or render tokens, OAuth codes, API keys, passwords, raw HTTP
  bodies, chain-of-thought text, source-code excerpts, or customer emails/SSNs.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|access_token|refresh_token|id_token|api_key|apikey|secret|"
    r"password|authorization|code|code_verifier|query|sökfråga)\s*[=:]\s*\S+"
)
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_KEY_PREFIX_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{10,}|xai-[A-Za-z0-9_\-]{10,})\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SSN_RE = re.compile(r"\b(?:19|20)?\d{6}[-+]?\d{4}\b|\b\d{12}\b")


def utf16_length(text: str) -> int:
    """Return Telegram Bot API UTF-16 code unit length."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in (text or ""))


def clip_utf16(text: str, limit: int) -> str:
    """Clip ``text`` to at most ``limit`` UTF-16 code units."""
    if not text or limit <= 0:
        return ""
    if utf16_length(text) <= limit:
        return text
    units = 0
    out: list[str] = []
    budget = max(1, limit - 1)
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > budget:
            break
        out.append(ch)
        units += w
    return "".join(out).rstrip() + "…"


def sanitize_status_fragment(value: object, *, max_len: int = 160) -> str:
    """Redact credentials/PII and collapse whitespace for safe status display."""
    raw = " ".join(str(value or "").split())
    if not raw:
        return ""
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", raw)
    cleaned = _SECRET_KV_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _URL_QUERY_RE.sub(r"\1", cleaned)
    cleaned = _KEY_PREFIX_RE.sub("[REDACTED_KEY]", cleaned)
    cleaned = _EMAIL_RE.sub("[e-post]", cleaned)
    cleaned = _SSN_RE.sub("[id]", cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max(1, max_len - 1)].rstrip() + "…"
    return cleaned


def format_elapsed(seconds: float | None) -> str:
    """Format elapsed seconds compactly in Swedish ops style."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(round(seconds))}s"
    mins, secs = divmod(int(round(seconds)), 60)
    return f"{mins}m {secs:02d}s"


@dataclass(frozen=True)
class ProgressEvent:
    """Structured progress update for a named tool step or model phase."""

    step_id: str
    tool: str
    label: str
    phase: str  # started | running | completed | failed | skipped | steer | route_switch | info
    step_index: int | None = None
    step_total: int | None = None
    elapsed_ms: int | None = None
    provenance: str | None = None  # live | mock | preview | unavailable
    detail: str | None = None
    outcome: str | None = None
    error_class: str | None = None
    legacy_text: str | None = None

    def sanitized(self) -> ProgressEvent:
        return replace(
            self,
            step_id=sanitize_status_fragment(self.step_id, max_len=80) or "step",
            tool=sanitize_status_fragment(self.tool, max_len=60) or "tool",
            label=sanitize_status_fragment(self.label, max_len=90) or "Steg",
            phase=sanitize_status_fragment(self.phase, max_len=24) or "info",
            provenance=(
                sanitize_status_fragment(self.provenance, max_len=24)
                if self.provenance
                else None
            ),
            detail=(
                sanitize_status_fragment(self.detail, max_len=100)
                if self.detail
                else None
            ),
            outcome=(
                sanitize_status_fragment(self.outcome, max_len=140)
                if self.outcome
                else None
            ),
            error_class=(
                sanitize_status_fragment(self.error_class, max_len=60)
                if self.error_class
                else None
            ),
            legacy_text=(
                sanitize_status_fragment(self.legacy_text, max_len=380)
                if self.legacy_text
                else None
            ),
        )


ProgressCallback = Callable[[Any], None]

_PHASE_ICONS = {
    "started": "🔧",
    "running": "⏳",
    "completed": "✓",
    "failed": "⚠️",
    "skipped": "⏭",
    "steer": "↪",
    "route_switch": "↻",
    "info": "•",
    "research:planning": "🧭",
    "research:searching": "🔎",
    "research:fetching": "🌐",
    "research:evaluating": "⚖️",
    "research:synthesizing": "📝",
}

RESEARCH_PHASE_LABELS: dict[str, str] = {
    "research:planning": "Planerar sökfrågor",
    "research:searching": "Söker externa källor",
    "research:fetching": "Hämtar källsidor",
    "research:evaluating": "Utvärderar källor",
    "research:synthesizing": "Sammanställer brief",
    "planning": "Planerar sökfrågor",
    "searching": "Söker externa källor",
    "fetching": "Hämtar källsidor",
    "evaluating": "Utvärderar källor",
    "synthesizing": "Sammanställer brief",
}


def progress_event_from_research_event(event: dict[str, Any]) -> ProgressEvent:
    """Convert a ``research_events`` row into a sanitized ``ProgressEvent``."""
    raw_phase = str(event.get("phase") or "info").strip().lower()
    round_num = int(event.get("round_num") or 0)
    provenance_raw = str(event.get("provenance") or "").strip().lower()
    provenance = provenance_raw if provenance_raw in {"live", "mock", "mock_fixture", "system"} else None
    if provenance == "mock_fixture":
        provenance = "mock"
    elif provenance == "system":
        provenance = None

    research_key = (
        raw_phase
        if raw_phase.startswith("research:")
        else (f"research:{raw_phase}" if f"research:{raw_phase}" in RESEARCH_PHASE_LABELS else "")
    )
    if research_key in RESEARCH_PHASE_LABELS:
        label = RESEARCH_PHASE_LABELS[research_key]
        phase = research_key
    elif raw_phase in {"completed", "partial"}:
        label = "Research klar" if raw_phase == "completed" else "Delvis research klar"
        phase = "completed"
    elif raw_phase in {"failed", "cancelled"}:
        label = "Research avbruten" if raw_phase == "cancelled" else "Research misslyckades"
        phase = "failed"
    elif raw_phase in {"steer_queued", "steer_applied"}:
        label = "Styrning till research"
        phase = "steer"
    else:
        label = "Djupresearch"
        phase = "info"

    detail = f"runda {round_num}" if round_num > 0 else None
    raw_msg = str(event.get("message") or "").strip()
    # Keep only safe phase/count status messages; never forward raw search query strings.
    safe_outcome: str | None = None
    if raw_msg and not re.search(r"(?i)\b(?:query|queries|sökfråga|url)\s*[:=]", raw_msg):
        safe_outcome = sanitize_status_fragment(raw_msg, max_len=100)
    if not safe_outcome and research_key in RESEARCH_PHASE_LABELS:
        safe_outcome = "pågår…"

    return ProgressEvent(
        step_id=f"research:{raw_phase}:{round_num}",
        tool="research",
        label=label,
        phase=phase,
        provenance=provenance,
        detail=detail,
        outcome=safe_outcome,
    )


def format_progress_line(event: ProgressEvent) -> str:
    """Format one ``ProgressEvent`` into a rich, concise Swedish status line."""
    ev = event.sanitized()
    icon = _PHASE_ICONS.get(ev.phase, "•")
    label = ev.label
    if ev.phase in RESEARCH_PHASE_LABELS and (
        not label or label == "Steg" or label == ev.phase or label == ev.phase.removeprefix("research:")
    ):
        label = RESEARCH_PHASE_LABELS[ev.phase]
    step_prefix = ""
    if ev.step_index is not None and ev.step_total is not None and ev.step_total > 0:
        step_prefix = f"[{ev.step_index}/{ev.step_total}] "
    elif ev.step_index is not None:
        step_prefix = f"[#{ev.step_index}] "

    meta_parts: list[str] = []
    if ev.detail:
        meta_parts.append(ev.detail)
    if ev.provenance:
        meta_parts.append(ev.provenance)
    meta = f" ({' · '.join(meta_parts)})" if meta_parts else ""

    dur = ""
    if ev.elapsed_ms is not None and ev.elapsed_ms >= 0:
        dur = f" · {format_elapsed(ev.elapsed_ms / 1000.0)}"

    if ev.phase in {"started", "running"} or ev.phase in RESEARCH_PHASE_LABELS:
        tail = ev.outcome or "pågår…"
        return f"{icon} {step_prefix}{label}{meta} — {tail}{dur}"
    if ev.phase == "failed":
        err = f" ({ev.error_class})" if ev.error_class else ""
        tail = ev.outcome or f"misslyckades{err}"
        if err and ev.error_class not in tail:
            tail = f"{tail}{err}"
        return f"{icon} {step_prefix}{label}{meta} — {tail}{dur}"
    if ev.phase == "skipped":
        tail = ev.outcome or "ej körd"
        return f"{icon} {step_prefix}{label}{meta} — {tail}{dur}"
    if ev.phase in {"steer", "route_switch", "info"}:
        tail = f": {ev.outcome}" if ev.outcome else ""
        return f"{icon} {step_prefix}{label}{meta}{tail}{dur}"
    tail = ev.outcome or "resultat mottaget"
    return f"{icon} {step_prefix}{label}{meta} — {tail}{dur}"


def mark_event_consumer(fn: Callable[[Any], None]) -> Callable[[Any], None]:
    """Mark a callback as accepting structured ``ProgressEvent`` instances."""
    fn.accepts_progress_event = True  # type: ignore[attr-defined]
    return fn


def emit_progress(
    callback: ProgressCallback | None,
    event: ProgressEvent | str,
) -> None:
    """Deliver a ``ProgressEvent`` to rich consumers or formatted string to legacy ones."""
    if callback is None:
        return
    try:
        if isinstance(event, ProgressEvent):
            safe_ev = event.sanitized()
            if getattr(callback, "accepts_progress_event", False):
                callback(safe_ev)
            else:
                callback(safe_ev.legacy_text or format_progress_line(safe_ev))
        else:
            text = " ".join(str(event or "").split())[:420]
            if text:
                callback(text)
    except Exception:
        pass


_MODEL_PHASE_LABELS = {
    "starting": "Kontaktar modell",
    "waiting": "Väntar på modellsvar",
    "reasoning": "Analyserar och resonerar",
    "streaming": "Skriver svar",
    "oauth_refresh": "Förnyar OAuth-session",
}


class LiveStatusBoard:
    """In-place live status card model for one Telegram task."""

    MAX_UTF16 = 3800
    MAX_VISIBLE_STEPS = 12

    def __init__(
        self,
        *,
        job_id: str | None = None,
        task_id: str | None = None,
        kind: str = "read_turn",
        title: str | None = None,
        started_at: float | None = None,
    ) -> None:
        raw_jid = str(job_id or "")
        raw_tid = str(task_id or "")
        self.job_id = raw_jid[:16] if raw_jid.startswith("res-") else raw_jid[:8]
        self.task_id = raw_tid[:16] if raw_tid.startswith("res-") else raw_tid[:8]
        self.kind = kind or "read_turn"
        self.title = sanitize_status_fragment(title or "", max_len=72)
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.step_order: list[str] = []
        self.steps_by_id: dict[str, str] = {}
        self.raw_lines: list[str] = ["⏳ Tar hand om din fråga…"]
        self.model_phase: str = ""
        self.model_route: str = ""
        self.model_name: str = ""
        self.model_phase_started_at: float | None = None
        self.hard_timeout_s: int | None = None
        self.steer_count: int = 0
        self.terminal_line: str = ""

    def record(self, item: ProgressEvent | str, *, now: float | None = None) -> str:
        """Record a structured event (in-place by step_id) or plain progress string."""
        ts = now if now is not None else time.monotonic()
        if isinstance(item, ProgressEvent):
            ev = item.sanitized()
            if ev.phase == "steer":
                self.steer_count += 1
            line = format_progress_line(ev)
            if ev.step_id not in self.steps_by_id:
                self.step_order.append(ev.step_id)
            self.steps_by_id[ev.step_id] = line
            if not self.raw_lines or self.raw_lines[-1] != line:
                self.raw_lines.append(line)
            # Clear active model phase banner once a concrete tool step starts after it
            if (
                ev.phase in {"started", "running"} or ev.phase in RESEARCH_PHASE_LABELS
            ) and ev.tool != "model_router":
                self.model_phase = ""
                self.model_phase_started_at = None
            del ts
            return line

        clean = " ".join(str(item or "").split())[:420]
        if not clean:
            return ""
        # Skip boilerplate line in structured step view while preserving raw_lines
        if clean not in {"Arbetet körs i bakgrunden."}:
            synthetic_id = f"line-{len(self.step_order) + 1}"
            # Deduplicate consecutive identical raw lines
            if not self.step_order or self.steps_by_id.get(self.step_order[-1]) != clean:
                self.step_order.append(synthetic_id)
                self.steps_by_id[synthetic_id] = clean
        if not self.raw_lines or self.raw_lines[-1] != clean:
            self.raw_lines.append(clean)
        return clean

    def set_model_phase(self, info: dict[str, Any], *, now: float | None = None) -> str | None:
        """Update live model phase/route state without exposing CoT."""
        ts = now if now is not None else time.monotonic()
        phase = str(info.get("phase") or "").strip().lower()
        route_id = sanitize_status_fragment(info.get("route_id") or "", max_len=40)
        model = sanitize_status_fragment(info.get("model") or "", max_len=60)
        hard_timeout = info.get("hard_timeout")
        if isinstance(hard_timeout, (int, float)) and hard_timeout > 0:
            self.hard_timeout_s = int(hard_timeout)

        if phase == "route_switch":
            from_route = sanitize_status_fragment(info.get("from_route") or route_id, max_len=40)
            err_cls = sanitize_status_fragment(info.get("error_class") or "fel", max_len=40)
            to_route = sanitize_status_fragment(info.get("to_route") or "", max_len=40)
            arrow = f" → {to_route}" if to_route else " → nästa rutt"
            ev = ProgressEvent(
                step_id=f"route-switch-{len(self.step_order) + 1}",
                tool="model_router",
                label="Byter modellrutt",
                phase="route_switch",
                outcome=f"{from_route} ({err_cls}){arrow}",
            )
            self.model_phase = ""
            self.model_phase_started_at = None
            return self.record(ev, now=ts)

        if phase == "model_fallback":
            from_m = sanitize_status_fragment(info.get("from_model") or "", max_len=40)
            to_m = sanitize_status_fragment(info.get("to_model") or "", max_len=40)
            ev = ProgressEvent(
                step_id="model-fast-fallback",
                tool="model_router",
                label="Modell-fallback",
                phase="route_switch",
                outcome=f"{from_m} → {to_m}",
            )
            return self.record(ev, now=ts)

        if phase == "profile_switch":
            from_p = sanitize_status_fragment(info.get("from_profile") or "", max_len=40)
            to_p = sanitize_status_fragment(info.get("to_profile") or "", max_len=40)
            ev = ProgressEvent(
                step_id=f"profile-switch-{len(self.step_order) + 1}",
                tool="model_router",
                label="Byter modellprofil",
                phase="route_switch",
                outcome=f"{from_p} → {to_p}",
            )
            return self.record(ev, now=ts)

        if phase == "circuit_skipped":
            br_state = sanitize_status_fragment(info.get("breaker_state") or "open", max_len=24)
            ev = ProgressEvent(
                step_id=f"circuit-skip-{route_id or 'route'}",
                tool="model_router",
                label=f"Hoppade över {route_id or 'rutt'}",
                phase="skipped",
                outcome=f"viloläge ({br_state})",
            )
            return self.record(ev, now=ts)

        if phase == "completed":
            self.model_phase = ""
            self.model_phase_started_at = None
            if route_id:
                self.model_route = route_id
            if model:
                self.model_name = model
            return None

        if phase in _MODEL_PHASE_LABELS:
            if phase != self.model_phase:
                self.model_phase_started_at = ts
            self.model_phase = phase
            if route_id:
                self.model_route = route_id
            if model:
                self.model_name = model
            return _MODEL_PHASE_LABELS[phase]
        return None

    def mark_finished(self, *, ok: bool = True, cancelled: bool = False, error_class: str | None = None, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        total_dur = format_elapsed(max(0.0, ts - self.started_at))
        self.model_phase = ""
        self.model_phase_started_at = None
        if cancelled:
            self.terminal_line = f"🛑 Avbrutet efter {total_dur}"
            self.raw_lines.append("🛑 Arbetet avbröts")
        elif not ok:
            err = f" ({sanitize_status_fragment(error_class, max_len=50)})" if error_class else ""
            self.terminal_line = f"⚠️ Arbetet avbröts av ett fel{err} · {total_dur}"
            self.raw_lines.append("⚠️ Arbetet avbröts av ett tekniskt fel")
        else:
            route_note = f" · {self.model_route}" if self.model_route else ""
            self.terminal_line = f"✓ Svar klart · {total_dur}{route_note}"
            self.raw_lines.append("✓ Svar klart")

    def render(self, *, now: float | None = None) -> str:
        """Render the complete live status card within Telegram's UTF-16 budget."""
        ts = now if now is not None else time.monotonic()
        elapsed = max(0.0, ts - self.started_at)
        lines: list[str] = []

        # Header with job badge, task title, and live elapsed time
        kind_labels = {
            "read_turn": "Analys & verktyg",
            "page_review": "Startsidegranskning",
            "action": "Godkänd åtgärd",
            "callback": "Snabbval",
            "research": "Djupresearch",
        }
        kind_label = kind_labels.get(self.kind, self.kind)
        badge = f"#{self.job_id}" if self.job_id else ""
        header_parts = [p for p in [f"⚡ Azom Live {badge}".strip(), kind_label, format_elapsed(elapsed)] if p]
        lines.append(" · ".join(header_parts))
        if self.title:
            lines.append(f"Uppgift: {self.title}")

        # Steps (keep most recent MAX_VISIBLE_STEPS, preserving earliest context indicator if truncated)
        visible_ids = self.step_order[-self.MAX_VISIBLE_STEPS :]
        hidden = len(self.step_order) - len(visible_ids)
        if hidden > 0:
            lines.append(f"… ({hidden} tidigare steg klara)")
        if visible_ids:
            for sid in visible_ids:
                lines.append(self.steps_by_id[sid])
        elif not self.terminal_line:
            lines.append("⏳ Tar hand om din fråga…")

        # Active model phase / long-running heartbeat indicator
        if not self.terminal_line and self.model_phase:
            phase_label = _MODEL_PHASE_LABELS.get(self.model_phase, "Bearbetar")
            phase_elapsed = (
                max(0.0, ts - self.model_phase_started_at)
                if self.model_phase_started_at is not None
                else elapsed
            )
            route_bits = [b for b in (self.model_route, self.model_name) if b]
            route_str = f" ({' · '.join(route_bits)})" if route_bits else ""
            cap_str = (
                f" / tak {format_elapsed(self.hard_timeout_s)}"
                if self.hard_timeout_s and phase_elapsed >= 10.0
                else ""
            )
            lines.append(
                f"💭 {phase_label}{route_str} — {format_elapsed(phase_elapsed)}{cap_str}"
            )
        elif not self.terminal_line and elapsed >= 12.0:
            lines.append(f"⏳ Körning aktiv ({format_elapsed(elapsed)}) — arbetar fortfarande…")

        # Helpful hint during longer runs (> 15s) while still running
        if not self.terminal_line and elapsed >= 15.0 and self.job_id:
            lines.append(
                f"💡 Svara (reply) på kortet för att styra · /cancel {self.job_id} för att avbryta"
            )

        if self.terminal_line:
            lines.append(self.terminal_line)

        return clip_utf16("\n".join(lines), self.MAX_UTF16)
