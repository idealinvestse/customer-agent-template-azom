"""Bot Shop Intelligence slash commands (``/shop-intel ...`` and ``/store-learn ...``).

All commands are strictly read-only regarding WordPress/WooCommerce stores:
they trigger or query deterministic, zero-PII ``ShopIntelService`` dossiers
and never mutate store settings, products, themes, plugins, or customer mail.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ecom_ops.bot.actors import ChannelActorDenied, TelegramActorDenied
from ecom_ops.bot.dashboard_links import shop_intel_url
from ecom_ops.bot.progress_events import ProgressCallback, ProgressEvent, emit_progress
from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply, bot_reply
from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
from ecom_ops.shop_intel import (
    SHOP_INTEL_INTENT_RE,
    SHOP_INTEL_RUN_ID_RE,
    VALID_DOMAINS_SET,
    VALID_MODES,
    get_shop_intel_service,
    scrub_text,
)
from ecom_ops.shop_intel.models import VALID_SEVERITIES_SET

_SHOP_INTEL_CMD_RE = re.compile(
    r"^/(?:shop-intel|store-learn)(?:@[a-z0-9_]+)?(?:\s+(.*))?$",
    re.I | re.S,
)
_SITE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class _UnknownSiteError(ValueError):
    """Raised when an explicit site token in ``/shop-intel`` is not registered."""


def _nav_action_markup(links: list[tuple[str, str | None]]) -> ActionMarkup | None:
    """Build strictly read-only URL navigation buttons (max 3)."""
    buttons = [ActionButton(label=label, url=url) for label, url in links if url][:3]
    if not buttons:
        return None
    return ActionMarkup(rows=[[btn] for btn in buttons])


def _known_site_ids() -> list[str]:
    from ecom_ops.wp_ops.registry import load_sites

    try:
        return [s.id for s in load_sites(apply_overlay=False)]
    except Exception:
        return []


def _resolve_site_id(
    handler: Any,
    chat_id: str | int,
    candidate_site: str | None = None,
) -> str:
    """Resolve site_id (explicit > session wp_ops_context > first registered site)."""
    known_ids = _known_site_ids()
    if candidate_site is not None and candidate_site.strip():
        cand = candidate_site.strip().lower()
        if not _SITE_ID_RE.fullmatch(cand):
            raise _UnknownSiteError(cand)
        if known_ids and cand not in known_ids:
            raise _UnknownSiteError(cand)
        return cand

    prev = handler.store.get(chat_id) or {}
    session = dict(prev.get("session") or {})
    wp_ctx = session.get("wp_ops_context")
    if isinstance(wp_ctx, dict) and isinstance(wp_ctx.get("site_id"), str):
        ctx_site = wp_ctx["site_id"].strip().lower()
        if _SITE_ID_RE.fullmatch(ctx_site) and (not known_ids or ctx_site in known_ids):
            return ctx_site

    return known_ids[0] if known_ids else "mock-shop"


def _shop_intel_help_text() -> str:
    url = shop_intel_url()
    link = f"\nÖppna i Shop Intel: {url}" if url else "\nÖppna i Shop Intel: /shop-intel (kräver inloggning)"
    return (
        "Webshop Intelligence (/shop-intel · /store-learn — läsande & zero-PII; muterar aldrig butik):\n"
        "• /shop-intel run [site-id] [full|quick|security] — kör butiksinlärning\n"
        "• /shop-intel status [site-id] — visa senaste körning, schema & gates (mock/live)\n"
        "• /shop-intel query [site-id] <domän|sökord> — sök fakta i senaste dossier\n"
        "• /shop-intel security [site-id] — visa säkerhetspoäng & fynd (sif-*)\n"
        "• /shop-intel diff [site-id] — visa konfigurations- & säkerhetsdrift (sid-*)"
        f"{link}"
    )


def _service_failure_text(res: Any, fallback: str) -> str:
    if isinstance(res, dict):
        err = scrub_text(res.get("message") or res.get("error") or "", max_chars=200).strip()
        err_cls = str(res.get("error_class") or "").strip()
        if err:
            return f"{fallback}: {err}"
        if err_cls:
            return f"{fallback} ({err_cls})."
    return f"{fallback}."


def _compact_value(val: Any, *, max_chars: int = 120) -> str:
    if isinstance(val, (dict, list)):
        raw = json.dumps(val, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        raw = str(val)
    return scrub_text(raw, max_chars=max_chars)


def _format_run_reply(res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    run = dict(res.get("run") or {})
    run_id = str(run.get("run_id") or "")
    site_id = str(run.get("site_id") or "")
    mode = str(run.get("mode") or "full")
    provenance = str(run.get("provenance") or ("live" if res.get("live_calls") else "mock"))
    status = str(run.get("status") or "completed")
    score = res.get("security_score", run.get("security_score", 100))
    facts_count = int(res.get("facts_count") or 0)
    findings = list(res.get("findings") or [])
    diffs = list(res.get("diffs") or [])
    sources = ", ".join(str(s) for s in (run.get("sources") or [])[:6]) or provenance
    warnings = [scrub_text(w, max_chars=120) for w in (run.get("warnings") or []) if w]

    lines = [
        f"Shop Intel-körning {run_id} ({site_id} · läge={mode} · källa={provenance} · status={status})",
        f"Säkerhetspoäng: {score}/100 · fakta: {facts_count} · säkerhetsfynd: {len(findings)} · drift: {len(diffs)}",
        f"Datakällor: {sources}",
    ]
    for finding in findings[:3]:
        if isinstance(finding, dict):
            sev = str(finding.get("severity") or "info").upper()
            title = scrub_text(finding.get("title") or finding.get("code") or "Fynd", max_chars=90)
            lines.append(f"- [{sev}] {title}")
    for diff in diffs[:2]:
        if isinstance(diff, dict):
            sev = str(diff.get("severity") or "info").upper()
            summ = scrub_text(diff.get("summary") or diff.get("category") or "Drift", max_chars=90)
            lines.append(f"- [DRIFT/{sev}] {summ}")
    if warnings:
        lines.append(f"Varningar: {'; '.join(warnings[:3])}")
    lines.append("read-only · zero-PII · inga sajtändringar gjorda.")

    run_url = shop_intel_url(site_id, run_id=run_id)
    hub_url = shop_intel_url(site_id)
    lines.append(
        f"Öppna körning i Shop Intel: {run_url or f'/shop-intel/runs/{run_id}'} (kräver inloggning)"
    )
    actions = _nav_action_markup(
        [
            ("Öppna körning i Shop Intel", run_url),
            ("Öppna Shop Intel", hub_url),
        ]
    )
    return "\n".join(lines), actions


def _format_status_reply(site_id: str | None, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    gates = res.get("gates") if isinstance(res.get("gates"), dict) else {}
    mock_mode = bool(gates.get("mock_mode", True))
    live_enabled = bool(gates.get("live_enabled", False))
    kill_active = bool(gates.get("kill_active", False))
    mode_label = "mock" if mock_mode else ("live" if live_enabled and not kill_active else "live-blockerad")
    lines = [
        f"Shop Intel status ({site_id or 'alla sajter'} · läge={mode_label} · kill={kill_active})"
    ]

    run_url: str | None = None
    if site_id:
        latest = res.get("latest_run") if isinstance(res.get("latest_run"), dict) else None
        sched = res.get("schedule") if isinstance(res.get("schedule"), dict) else None
        recent = list(res.get("recent_runs") or [])
        if latest:
            rid = str(latest.get("run_id") or "")
            prov = str(latest.get("provenance") or "mock")
            lines.append(
                f"Senaste körning: {rid} · källa={prov} · poäng={latest.get('security_score', '-')}/100 · {latest.get('created_at', '-')}"
            )
            lines.append(
                f"Körningar sparade: {len(recent)} · fynd={latest.get('findings_count', 0)} · drift={latest.get('diffs_count', 0)}"
            )
            run_url = shop_intel_url(site_id, run_id=rid)
        else:
            lines.append(f"Ingen Shop Intel-körning sparad för {site_id} ännu. Kör: /shop-intel run {site_id}")
        if sched and sched.get("enabled"):
            lines.append(
                f"Schema: aktivt var {sched.get('interval_hours', 24)}h ({sched.get('mode', 'full')}) · nästa={sched.get('next_run_at') or '-'}"
            )
        else:
            lines.append("Schema: inaktivt (konfigureras av Oscar)")
    else:
        site_rows = list(res.get("sites") or [])
        if site_rows:
            for row in site_rows[:6]:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("site_id") or "")
                rid = str(row.get("latest_run_id") or "ingen")
                score = row.get("security_score")
                score_s = f"{score}/100" if score is not None else "-"
                lines.append(f"- {sid}: {rid} · poäng={score_s}")
        else:
            lines.append("Inga registrerade sajter hittades.")

    hub_url = shop_intel_url(site_id)
    lines.append(
        f"Öppna i Shop Intel: {hub_url or (f'/shop-intel?site_id={site_id}' if site_id else '/shop-intel')} (kräver inloggning)"
    )
    actions = _nav_action_markup(
        [
            ("Öppna Shop Intel", hub_url),
            ("Öppna senaste körning", run_url),
        ]
    )
    return "\n".join(lines), actions


def _format_query_reply(
    res: dict[str, Any],
    *,
    domain: str | None,
    query: str | None,
) -> tuple[str, ActionMarkup | None]:
    site_id = str(res.get("site_id") or "")
    run_id = str(res.get("run_id") or "")
    provenance = str(res.get("provenance") or "mock")
    observed_at = str(res.get("observed_at") or "-")
    facts = list(res.get("facts") or [])
    filt_parts = []
    if domain:
        filt_parts.append(f"domän={domain}")
    if query:
        filt_parts.append(f"sökord={scrub_text(query, max_chars=40)}")
    filt_s = f" · {' · '.join(filt_parts)}" if filt_parts else ""

    lines = [
        f"Shop Intel kunskap ({site_id} · {run_id} · källa={provenance}{filt_s})",
        f"Observerad: {observed_at} · matchade fakta: {len(facts)}",
    ]
    for fact in facts[:6]:
        if not isinstance(fact, dict):
            continue
        dom = str(fact.get("domain") or "")
        key = scrub_text(fact.get("key") or "", max_chars=40)
        val = _compact_value(fact.get("value"), max_chars=110)
        lines.append(f"- [{dom}] {key}: {val}")
    if len(facts) > 6:
        lines.append(f"- ... och {len(facts) - 6} fler fakta")
    if not facts:
        lines.append("Inga fakta matchade filtret i senaste körningen.")

    run_url = shop_intel_url(site_id, run_id=run_id)
    hub_url = shop_intel_url(site_id)
    lines.append(
        f"Öppna i Shop Intel: {run_url or hub_url or f'/shop-intel?site_id={site_id}'} (kräver inloggning)"
    )
    actions = _nav_action_markup(
        [
            ("Öppna körning i Shop Intel", run_url),
            ("Öppna Shop Intel", hub_url),
        ]
    )
    return "\n".join(lines), actions


def _format_security_reply(
    res: dict[str, Any],
    *,
    provenance: str,
) -> tuple[str, ActionMarkup | None]:
    site_id = str(res.get("site_id") or "")
    run_id = str(res.get("run_id") or "")
    score = res.get("security_score", 100)
    findings = list(res.get("findings") or [])

    lines = [
        f"Shop Intel säkerhet ({site_id} · {run_id} · källa={provenance})",
        f"Säkerhetspoäng: {score}/100 · öppna/observerade fynd: {len(findings)}",
    ]
    for finding in findings[:6]:
        if not isinstance(finding, dict):
            continue
        sev = str(finding.get("severity") or "info").upper()
        fid = str(finding.get("finding_id") or "")
        title = scrub_text(finding.get("title") or finding.get("code") or "Fynd", max_chars=85)
        rec = scrub_text(finding.get("recommendation") or "", max_chars=70)
        suffix = f" — {rec}" if rec else ""
        lines.append(f"- [{sev}] {fid} {title}{suffix}".strip())
    if len(findings) > 6:
        lines.append(f"- ... och {len(findings) - 6} fler säkerhetsfynd")
    if not findings:
        lines.append("Inga säkerhetsfynd registrerade i senaste körningen.")

    run_url = shop_intel_url(site_id, run_id=run_id)
    hub_url = shop_intel_url(site_id)
    lines.append(
        f"Öppna i Shop Intel: {run_url or hub_url or f'/shop-intel?site_id={site_id}'} (kräver inloggning)"
    )
    actions = _nav_action_markup(
        [
            ("Öppna körning i Shop Intel", run_url),
            ("Öppna Shop Intel", hub_url),
        ]
    )
    return "\n".join(lines), actions


def _format_diff_reply(
    site_id: str,
    res: dict[str, Any],
    *,
    provenance: str,
) -> tuple[str, ActionMarkup | None]:
    diffs = list(res.get("diffs") or [])
    lines = [
        f"Shop Intel drift ({site_id} · källa={provenance})",
        f"Registrerade driftändringar: {len(diffs)}",
    ]
    for diff in diffs[:6]:
        if not isinstance(diff, dict):
            continue
        did = str(diff.get("diff_id") or "")
        sev = str(diff.get("severity") or "info").upper()
        dom = str(diff.get("domain") or "")
        summ = scrub_text(diff.get("summary") or diff.get("category") or "Drift", max_chars=90)
        lines.append(f"- [{sev}] {did} ({dom}): {summ}")
    if len(diffs) > 6:
        lines.append(f"- ... och {len(diffs) - 6} fler driftposter")
    if not diffs:
        lines.append("Ingen konfigurations- eller säkerhetsdrift noterad ännu.")

    hub_url = shop_intel_url(site_id)
    lines.append(
        f"Öppna i Shop Intel: {hub_url or f'/shop-intel?site_id={site_id}'} (kräver inloggning)"
    )
    actions = _nav_action_markup([("Öppna Shop Intel", hub_url)])
    return "\n".join(lines), actions


def _parse_query_args(
    handler: Any,
    chat_id: str | int,
    rest: str,
) -> tuple[str, str | None, str | None]:
    """Parse ``query [site-id] <domain|keyword>`` without misreading keywords as site IDs."""
    tokens = rest.split()
    known_ids = _known_site_ids()
    known_low = {s.lower() for s in known_ids}
    cand_site: str | None = None
    if tokens:
        first_low = tokens[0].strip().lower()
        if first_low.startswith("site="):
            cand_site = first_low.split("=", 1)[1]
            tokens = tokens[1:]
        elif first_low in known_low or (not known_low and first_low == "mock-shop"):
            cand_site = first_low
            tokens = tokens[1:]

    site_id = _resolve_site_id(handler, chat_id, cand_site)
    domain: str | None = None
    if tokens and tokens[0].strip().lower() in VALID_DOMAINS_SET:
        domain = tokens[0].strip().lower()
        tokens = tokens[1:]
    query = " ".join(tokens).strip() or None
    return site_id, domain, query


def handle_shop_intel_command(
    handler: Any,
    chat_id: str | int,
    raw: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BotReply | None:
    """Handle ``/shop-intel ...`` and ``/store-learn ...`` slash commands."""
    cleaned = (raw or "").strip()
    match = _SHOP_INTEL_CMD_RE.match(cleaned)
    if not match:
        return None

    try:
        actor = handler._resolve_actor(chat_id)
        actor_obj = resolve_actor(actor)
    except (TelegramActorDenied, ChannelActorDenied, AccessDenied):
        return handler._deny_actor()

    if not (actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.READ)):
        return BotReply(text="Åtkomst nekad: /shop-intel kräver WP_OPS_READ eller READ.")

    arg_str = (match.group(1) or "").strip()
    if not arg_str:
        return bot_reply(
            _shop_intel_help_text(),
            actions=_nav_action_markup([("Öppna Shop Intel", shop_intel_url())]),
        )

    parts = arg_str.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in {"help", "?"}:
        return bot_reply(
            _shop_intel_help_text(),
            actions=_nav_action_markup([("Öppna Shop Intel", shop_intel_url())]),
        )

    t0 = time.monotonic()
    step_id = f"cmd:shop_intel:{sub}"
    emit_progress(
        on_progress,
        ProgressEvent(
            step_id=step_id,
            tool=f"shop_intel_{sub}",
            phase="started",
            label="Shop Intel",
            detail=f"/shop-intel {sub}",
        ),
    )

    svc = get_shop_intel_service()

    def _finish(
        reply_text: str,
        *,
        ok: bool = True,
        actions: ActionMarkup | None = None,
    ) -> BotReply:
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=f"shop_intel_{sub}",
                phase="completed" if ok else "failed",
                label="Shop Intel",
                detail=f"/shop-intel {sub}",
                outcome="klar" if ok else "fel",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            ),
        )
        return bot_reply(reply_text, actions=actions)

    try:
        if sub == "run":
            has_run = (
                actor_obj.has(Permission.RESEARCH_RUN)
                or actor_obj.has(Permission.WP_OPS_OPERATE)
                or actor_obj.has(Permission.WP_OPS_ADMIN)
            )
            if not (actor_obj.has(Permission.WP_OPS_READ) and has_run):
                return _finish(
                    "Åtkomst nekad: /shop-intel run kräver WP_OPS_READ samt RESEARCH_RUN eller WP_OPS_OPERATE.",
                    ok=False,
                )
            tokens = rest.split() if rest else []
            cand_site: str | None = None
            mode = "full"
            mode_set = False
            for tok in tokens:
                low = tok.strip().lower()
                if low in VALID_MODES and not mode_set:
                    mode = low
                    mode_set = True
                elif cand_site is None:
                    cand_site = tok
            site_id = _resolve_site_id(handler, chat_id, cand_site)
            res = svc.run_learning(site_id=site_id, actor=actor, mode=mode)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Butiksanalys misslyckades"), ok=False)
            txt, acts = _format_run_reply(res)
            return _finish(txt, actions=acts)

        if sub == "status":
            cand = rest.split()[0] if rest else None
            site_id = _resolve_site_id(handler, chat_id, cand) if cand else None
            res = svc.get_status(site_id=site_id, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Shop Intel-status misslyckades"), ok=False)
            txt, acts = _format_status_reply(site_id, res)
            return _finish(txt, actions=acts)

        if sub == "query":
            if not rest:
                return _finish("Användning: /shop-intel query [site-id] <domän|sökord>", ok=False)
            site_id, domain, query = _parse_query_args(handler, chat_id, rest)
            if not domain and not query:
                return _finish("Användning: /shop-intel query [site-id] <domän|sökord>", ok=False)
            res = svc.query_knowledge(site_id, domain=domain, query=query, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Kunskapssökning misslyckades"), ok=False)
            txt, acts = _format_query_reply(res, domain=domain, query=query)
            return _finish(txt, actions=acts)

        if sub == "security":
            tokens = rest.split() if rest else []
            cand_site = None
            severity: str | None = None
            for tok in tokens:
                low = tok.strip().lower()
                if low in VALID_SEVERITIES_SET and severity is None:
                    severity = low
                elif cand_site is None:
                    cand_site = tok
            site_id = _resolve_site_id(handler, chat_id, cand_site)
            res = svc.get_security_report(site_id, severity=severity, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Säkerhetsrapport misslyckades"), ok=False)
            st = svc.get_status(site_id=site_id, actor=actor)
            latest = st.get("latest_run") if isinstance(st, dict) and isinstance(st.get("latest_run"), dict) else {}
            prov = str(latest.get("provenance") or ("mock" if (st.get("gates") or {}).get("mock_mode", True) else "live"))
            txt, acts = _format_security_reply(res, provenance=prov)
            return _finish(txt, actions=acts)

        if sub == "diff":
            cand = rest.split()[0] if rest else None
            site_id = _resolve_site_id(handler, chat_id, cand)
            res = svc.get_diffs(site_id, limit=20, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Driftrapport misslyckades"), ok=False)
            st = svc.get_status(site_id=site_id, actor=actor)
            latest = st.get("latest_run") if isinstance(st, dict) and isinstance(st.get("latest_run"), dict) else {}
            prov = str(latest.get("provenance") or ("mock" if (st.get("gates") or {}).get("mock_mode", True) else "live"))
            txt, acts = _format_diff_reply(site_id, res, provenance=prov)
            return _finish(txt, actions=acts)

        if SHOP_INTEL_RUN_ID_RE.fullmatch(sub):
            res = svc.get_run(sub, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, f"Kunde inte hämta körning {sub}"), ok=False)
            txt, acts = _format_run_reply(
                {
                    "ok": True,
                    "run": res.get("run"),
                    "facts_count": len(res.get("facts") or []),
                    "findings": res.get("findings") or [],
                    "diffs": res.get("diffs") or [],
                    "security_score": (res.get("run") or {}).get("security_score", 100),
                }
            )
            return _finish(txt, actions=acts)

        return _finish(
            f"Okänt underkommando: /shop-intel {sub}\n\n{_shop_intel_help_text()}",
            ok=False,
        )
    except _UnknownSiteError as exc:
        return _finish(f"Okänd sajt: {exc}", ok=False)
    except AccessDenied as exc:
        return _finish(f"Åtkomst nekad: {exc}", ok=False)
    except ValueError as exc:
        return _finish(f"Ogiltig indata: {scrub_text(exc, max_chars=160)}", ok=False)
    except Exception as exc:
        return _finish(f"Shop Intel-kommando misslyckades ({type(exc).__name__}).", ok=False)


__all__ = [
    "SHOP_INTEL_INTENT_RE",
    "handle_shop_intel_command",
]
