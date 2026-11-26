"""Progress reporting and Swedish operator summary formatting for page reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ecom_ops.wp_ops.review.page.context import not_run_rendered_dom

if TYPE_CHECKING:
    from ecom_ops.bot.progress_events import ProgressCallback

_STEP_LABELS = {
    "wp_page_review_home": "Startsida & mallhierarki",
    "wp_page_review_content": "Editor- & databasinnehåll",
    "wp_page_review_sources": "Källkodsanalys (tema/plugins)",
    "wp_page_review_preview": "Browser-preview",
}
_FAILED_REASONS = frozenset(
    {"actor_denied", "unknown_site", "rest_failed", "transport_failed", "io_timeout"}
)
_REASON_LABELS = {
    "not_bound": "ej kopplad till REST (not_bound)",
    "browser_unavailable": "ej tillgänglig (not_run/unavailable)",
    "call_budget": "anropsgräns nådd",
    "time_budget": "tidsgräns nådd",
    "io_timeout": "tidsgräns nådd (timeout)",
    "actor_denied": "behörighet saknas",
    "unknown_site": "okänd sajt",
    "site_unselected": "ingen sajt vald",
    "settings_incomplete": "startsideinställning saknas",
}


def _step_outcome(row: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """Return ``(phase, outcome, provenance, error_class)`` for a completed review step."""
    tool = str(row.get("tool") or "")
    status = str(row.get("status") or "")
    availability = str(row.get("availability") or "")
    reason = str(row.get("reason") or "")
    if status == "read":
        if tool == "wp_page_review_home":
            mode = str(row.get("homepage_mode") or "")
            if mode == "static_page":
                return "completed", "statisk startsida läst", None, None
            if mode == "latest_posts":
                return "completed", "senaste inlägg läst", None, None
            return "completed", "startsida & mallhierarki läst", None, None
        if tool == "wp_page_review_content":
            db = row.get("database")
            if isinstance(db, dict) and db.get("editor_raw_present"):
                return "completed", "editorinnehåll läst", None, None
            return "completed", "databasmetadata läst", None, None
        if tool == "wp_page_review_sources":
            inv = row.get("source_inventory")
            findings = row.get("findings")
            if isinstance(inv, list):
                n_findings = len(findings) if isinstance(findings, list) else 0
                return "completed", f"{len(inv)} källfiler, {n_findings} fynd", None, None
            return "completed", "källfiler lästa", None, None
        return "completed", "renderad DOM läst", "preview", None

    if status == "not_run":
        phase = "failed" if reason in _FAILED_REASONS else "skipped"
        err_cls = reason if phase == "failed" else None
        prov = "unavailable" if availability in {"", "unavailable", "not_bound"} else None
        outcome = _REASON_LABELS.get(
            reason,
            "ej tillgänglig (not_run/unavailable)"
            if tool == "wp_page_review_preview"
            else f"ej körd ({reason or availability or 'unavailable'})",
        )
        return phase, outcome, prov, err_cls

    err_cls = reason or status or "step_failed"
    return "failed", f"misslyckades ({err_cls})", None, err_cls


def _report(
    on_progress: ProgressCallback | None,
    row: dict[str, Any],
    *,
    step_id: str | None = None,
    step_index: int | None = None,
    step_total: int | None = None,
    elapsed_ms: int | None = None,
) -> None:
    if on_progress is None:
        return
    tool = str(row.get("tool") or "")
    site_id = str(row.get("site_id") or "")
    status = str(row.get("status") or "")
    availability = str(row.get("availability") or "")
    if status == "not_run":
        tail = "not_run/unavailable" if availability in {"", "unavailable"} else f"not_run/{availability}"
    else:
        tail = str(row.get("detail") or status)
    legacy_line = " ".join(part for part in (tool, site_id, tail) if part)[:180]
    phase, outcome, provenance, error_class = _step_outcome(row)
    from ecom_ops.bot.progress_events import ProgressEvent, emit_progress

    emit_progress(
        on_progress,
        ProgressEvent(
            step_id=step_id or f"page-review-{tool}",
            tool=tool or "page_review",
            label=_STEP_LABELS.get(tool, tool or "Sidgranskning"),
            phase=phase,
            step_index=step_index,
            step_total=step_total,
            elapsed_ms=elapsed_ms,
            provenance=provenance,
            detail=site_id or None,
            outcome=outcome,
            error_class=error_class,
            legacy_text=legacy_line,
        ),
    )


def _layers(steps: list[dict[str, Any]]) -> dict[str, Any]:
    database: dict[str, Any] = {"status": "not_run", "availability": "unavailable"}
    source: dict[str, Any] = {"status": "not_run", "availability": "unavailable"}
    rendered: dict[str, Any] = not_run_rendered_dom(include_verification=False)
    for row in steps:
        if row.get("tool") == "wp_page_review_content" and isinstance(row.get("database"), dict):
            database = dict(row["database"])
            database["status"] = row.get("status")
        elif row.get("tool") == "wp_page_review_content":
            database = {"status": row.get("status"), "availability": row.get("availability")}
        if row.get("tool") == "wp_page_review_sources":
            source = {
                "status": row.get("status"),
                "availability": row.get("availability"),
                "detail": row.get("detail"),
            }
        if isinstance(row.get("rendered_dom"), dict):
            rendered = dict(row["rendered_dom"])
    return {"database": database, "source_file": source, "rendered_dom": rendered}


def _source_line(steps: list[dict[str, Any]]) -> str:
    for row in steps:
        if row.get("tool") != "wp_page_review_sources":
            continue
        if row.get("status") == "read":
            return str(row.get("detail") or "läst")
        return "not_run/unavailable"
    return "not_run/unavailable"


def _finding_line(finding: dict[str, Any]) -> str:
    layer = str(finding.get("layer") or "")
    path = finding.get("path") or "saknar sökväg"
    line = finding.get("line")
    where = f"{path}:{line}" if isinstance(line, int) else str(path)
    excerpt = finding.get("excerpt")
    tail = f" — {excerpt}" if isinstance(excerpt, str) and excerpt else ""
    return (
        f"- {layer} {where} risk={finding.get('risk')} "
        f"security={finding.get('security')} verification={finding.get('verification')}{tail}"
    )


def _format(site_id: str, steps: list[dict[str, Any]]) -> str:
    lines = [
        f"Sidgranskning för {site_id or 'ingen sajt'}.",
        "Orderverktyg kördes inte.",
        "",
        "Lager: databas, källfil, renderad DOM.",
    ]
    for row in steps:
        status = row.get("status")
        if status == "not_run":
            availability = row.get("availability") or "unavailable"
            label = "not_run/unavailable" if availability == "unavailable" else f"not_run/{availability}"
        else:
            label = str(status)
        reason = str(row.get("reason") or "")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"{row.get('tool')} — {row.get('site_id')} — {label}{suffix}")
    layers = _layers(steps)
    database = layers["database"]
    lines.append("")
    lines.append(
        "Databas: editorinnehåll "
        + ("lästes" if database.get("editor_raw_present") else "saknas eller lästes inte")
        + ". Lagrat rendered-HTML är inte DOM."
    )
    lines.append("Källfil: " + _source_line(steps))
    rendered = layers["rendered_dom"]
    if rendered.get("status") != "read":
        lines.append(
            "Renderad DOM: not_run/unavailable. Desktop-bild: saknas. Mobilbild: saknas. "
            "Konsol: not_run/unavailable. Nätverk: not_run/unavailable."
        )
    else:
        d_label = "bifogad" if rendered.get("desktop_screenshot") else "saknas"
        m_label = "bifogad" if rendered.get("mobile_screenshot") else "saknas"
        n_console = len(rendered.get("console_errors") or [])
        n_net = len(rendered.get("network_errors") or [])
        lines.append(
            f"Renderad DOM: läst (live read-only). Desktop-bild: {d_label}. Mobilbild: {m_label}. "
            f"Konsolfel: {n_console}. Nätverksfel: {n_net}."
        )
    lines.append("")
    lines.append("Fynd:")
    any_finding = False
    for row in steps:
        for finding in row.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            any_finding = True
            lines.append(_finding_line(finding))
    if not any_finding:
        lines.append("Inga fil- eller DOM-rader. Kontroller som inte kördes är not_run/unavailable.")
    text = "\n".join(lines)
    return text[:6000]


__all__ = [
    "_FAILED_REASONS",
    "_STEP_LABELS",
    "_finding_line",
    "_format",
    "_layers",
    "_report",
    "_source_line",
    "_step_outcome",
]
