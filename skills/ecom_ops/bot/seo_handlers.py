"""Bot SEO Studio slash commands (``/seo ...``) and natural-language SEO intent patterns.

All ``/seo`` commands are strictly read-only or draft/plan-creating — they never
execute live WordPress/WooCommerce mutations or external link-package purchases
from chat. Mutations require Oscar's hash-pinned HITL approval in ``/seo`` or CLI.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ecom_ops.bot.actors import ChannelActorDenied, TelegramActorDenied
from ecom_ops.bot.dashboard_links import (
    changes_url,
    seo_dashboard_url,
    seo_digest_url,
    seo_parity_url,
    seo_plan_url,
    seo_vendors_url,
    wp_plugins_url,
)
from ecom_ops.bot.progress_events import ProgressCallback, ProgressEvent, emit_progress
from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply, bot_reply
from ecom_ops.rbac import AccessDenied, Permission, resolve_actor

SEO_INTENT_RE = re.compile(
    r"\b(?:"
    r"seo(?:-[a-zåäö0-9]+)?|s[öo]kmotoroptimering|"
    r"meta-?titel|meta-?beskrivning|meta\s+description|seo-?titel|seo-?meta|"
    r"focus\s+keyphrase|fokusfras|nyckelordsanalys|s[öo]kordsanalys|s[öo]kordsstrategi|"
    r"alt-?text\w*|bild-?seo|image\s+seo|"
    r"json-?ld|strukturerad\s+data|schema\.org|schema-?markup|"
    r"hreflang|canonical|sitemap|robots\.txt|"
    r"internl[äa]nk\w*|internal\s+link\w*|orphan\s+pages?|"
    r"kannibaliser\w*|cannibalization|"
    r"l[äa]nkpaket\w*|l[äa]nkbygge|link\s+package|link\s+building|backlinks?|"
    r"l[äa]nkleverant[öo]r\w*|l[äa]nkshop\w*|seo-?butik\w*|seo-?autopilot|autonom\w*\s+seo|"
    r"ankartext\w*|anchor\s+(?:text|mix)|"
    r"yoast|rank\s*math|aioseo|seopress|"
    r"search\s+console|\bgsc\b|nordisk\s+paritet|deep\s*crawl|"
    r"organisk\s+(?:trafik|tillv[äa]xt|synlighet)|"
    r"seoa-[a-f0-9]{12}|seom-[a-f0-9]{12}|seob-[a-f0-9]{12}|"
    r"seos-[a-f0-9]{12}|seoc-[a-f0-9]{12}|seol-[a-f0-9]{12}"
    r")\b",
    re.I,
)

EXPLICIT_ADS_RE = re.compile(
    r"\b(?:google\s*ads|adwords|roas|cpc|ppc|annonser(?:ing|na)?|paid\s+search|shopping\s+ads|ga4)\b",
    re.I,
)

SEO_META_EDIT_RE = re.compile(
    r"\b(?:seo-?titel|meta-?titel|meta-?beskrivning|meta\s+description|focus\s+keyphrase|fokusfras|"
    r"alt-?text\w*|bild-?seo|canonical|hreflang|json-?ld|schema\.org|schema-?markup|strukturerad\s+data|"
    r"yoast|rank\s*math|aioseo|seopress|seo-?meta)\b",
    re.I,
)

_SEO_CMD_RE = re.compile(r"^/seo(?:@[a-z0-9_]+)?(?:\s+(.*))?$", re.I | re.S)
_SITE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SEO_ID_RE = re.compile(r"^(seoa|seom|seob|seos|seoc|seol)-[a-f0-9]{12}$", re.I)
_SEOA_ID_RE = re.compile(r"^seoa-[a-f0-9]{12}$", re.I)
_SEOL_ID_RE = re.compile(r"^seol-[a-f0-9]{12}$", re.I)
_BATCH_SCOPES = frozenset({"media_alt", "seo_meta", "all"})
_EXPLICIT_BUDGET_RE = re.compile(
    r"(?:\bbudget\s*=\s*(\d{1,3}(?:[\s_]\d{3})+|\d{4,6})(?:\s*(?:kr|sek))?|\b(\d{1,3}\s\d{3}|\d{4,6})\s*(?:kr|sek))\b",
    re.I,
)
_BARE_TRAILING_BUDGET_RE = re.compile(
    r"\b(\d{4,6})(?=\s*(?:(?:count\s*=\s*)?\d{1,2}(?:\s*(?:st|l[äa]nkar|links?))?)?\s*$)",
    re.I,
)
_EXPLICIT_COUNT_RE = re.compile(
    r"(?:\bcount\s*=\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:st|l[äa]nkar|links?)\b)",
    re.I,
)
_TRAILING_COUNT_RE = re.compile(r"(?:^|\s)(\d{1,2})\s*$")


class _UnknownSiteError(ValueError):
    """Raised when an explicit site token in ``/seo`` is not registered."""


def _nav_action_markup(links: list[tuple[str, str | None]]) -> ActionMarkup | None:
    """Build strictly read-only URL navigation buttons (max 3 for Telegram + Messenger parity)."""
    buttons = [ActionButton(label=label, url=url) for label, url in links if url][:3]
    if not buttons:
        return None
    return ActionMarkup(rows=[[btn] for btn in buttons])


def _seo_help_text() -> str:
    url = seo_dashboard_url()
    link = f"\nÖppna i SEO Studio: {url}" if url else "\nÖppna i SEO Studio: /seo (kräver inloggning)"
    return (
        "SEO Studio (/seo — läsande & utkast; aldrig direktskrivning eller automatiska länkköp):\n"
        "• /seo scan [site-id] [path] — teknisk, on-page, media, länkar & kannibalisering\n"
        "• /seo product <id|slug> · /seo page <id|slug> · /seo media [site-id]\n"
        "• /seo batch [site-id] <seoa-id> [media_alt|seo_meta|all] — skapa låst batchplan (seob-*)\n"
        "• /seo gsc [site-id] — read-only GSC quick-wins (strike-distance, låg CTR, kannibalisering)\n"
        "• /seo parity [site-id] — nordisk hreflang- & innehållsparitet (SE/NO/DK)\n"
        "• /seo diff [site-id] — jämför senaste SEO-audits (poängtrend, nya/lösta fynd)\n"
        "• /seo digest [site-id] — veckovis SEO- & plugin-driftöversikt\n"
        "• /seo verify <seol-id> — verifiera levererade länkar i länkpaket (läsande)\n"
        "• /seo vendors [se|no|dk] [budget] [antal] — granska kvalitetssäkrade nordiska SEO-butiker (läsande)\n"
        "• /seo autopilot [site-id] <ämne> [budget] [antal] — audit → SERP-research → strategi → kampanj → SEO-butiker → länkpaket-utkast\n"
        "• /seo strategy <ämne> · /seo campaign <namn> · /seo links <mål> [budget/antal]\n"
        "• /seo plans [site-id] · /seo plan <id> — detalj & SHA-256-förhandsvisning (seoa/seom/seob/seos/seoc/seol-*)"
        f"{link}"
    )


def _resolve_site_and_market(
    handler: Any,
    chat_id: str | int,
    candidate_site: str | None = None,
) -> tuple[str, str]:
    """Resolve site_id (explicit > session wp_ops_context > first registered site) and market."""
    from ecom_ops.wp_ops.registry import load_sites

    prev = handler.store.get(chat_id) or {}
    session = dict(prev.get("session") or {})
    known_ids: list[str] = []
    try:
        known_ids = [s.id for s in load_sites()]
    except Exception:
        known_ids = []

    site_id = ""
    if candidate_site is not None and candidate_site.strip():
        cand = candidate_site.strip().lower()
        if not _SITE_ID_RE.fullmatch(cand):
            raise _UnknownSiteError(cand)
        if known_ids and cand not in known_ids:
            raise _UnknownSiteError(cand)
        site_id = cand

    if not site_id:
        wp_ctx = session.get("wp_ops_context")
        if isinstance(wp_ctx, dict) and isinstance(wp_ctx.get("site_id"), str):
            ctx_site = wp_ctx["site_id"].strip().lower()
            if _SITE_ID_RE.fullmatch(ctx_site):
                site_id = ctx_site

    if not site_id:
        site_id = known_ids[0] if known_ids else "mock-shop"

    market = "no"
    low_site = site_id.lower()
    if low_site.endswith(("-se", "_se", ".se")):
        market = "se"
    elif low_site.endswith(("-dk", "_dk", ".dk")):
        market = "dk"
    elif low_site.endswith(("-no", "_no", ".no")):
        market = "no"
    else:
        raw_mkt = str(session.get("last_market") or "").strip().lower()
        if "azom.se" in raw_mkt or raw_mkt == "se":
            market = "se"
        elif "azom.dk" in raw_mkt or raw_mkt == "dk":
            market = "dk"
        elif "azom.no" in raw_mkt or raw_mkt == "no":
            market = "no"
    return site_id, market


def _site_environment(site_id: str) -> str:
    """Resolve site environment fail-closed (unknown defaults to 'production')."""
    from ecom_ops.wp_ops.registry import load_sites, site_by_id

    try:
        site_obj = site_by_id(load_sites(), site_id)
        if site_obj is not None and getattr(site_obj, "environment", None):
            return str(site_obj.environment).strip().lower()
    except Exception:
        pass
    return "production"


def _format_audit_reply(res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    audit_id = str(res.get("audit_id") or "")
    site_id = str(res.get("site_id") or "")
    scope = str(res.get("scope") or "site")
    market = str(res.get("market") or "no").upper()
    score = res.get("score", 0)
    grade = str(res.get("grade") or "-")
    issues = list(res.get("issues") or [])
    sources = ", ".join(str(s) for s in (res.get("data_sources") or [])[:6]) or "mock/bridge"
    gsc_note = str(res.get("gsc_note") or "").strip()

    lines = [
        f"SEO-audit {audit_id} ({site_id} · {market} · scope={scope})",
        f"Poäng: {score}/100 (betyg {grade}) · fynd: {len(issues)}",
        f"Datakällor: {sources}",
    ]
    for issue in issues[:6]:
        if not isinstance(issue, dict):
            continue
        sev = str(issue.get("severity") or "info").upper()
        title = str(issue.get("title") or issue.get("code") or "Fynd")[:90]
        target = str(issue.get("target") or issue.get("url") or "").strip()
        suffix = f" ({target[:48]})" if target else ""
        lines.append(f"- [{sev}] {title}{suffix}")
    if len(issues) > 6:
        lines.append(f"- ... och {len(issues) - 6} fler fynd")
    if gsc_note:
        lines.append(f"Obs: {gsc_note}")

    seo_url = seo_plan_url(audit_id) or seo_dashboard_url(site_id)
    vault_url = changes_url(site_id, category="seo_meta") if site_id else None
    lines.append(
        f"Öppna i SEO Studio: {seo_url or f'/seo/audit/{audit_id}'} (kräver inloggning)"
    )
    if site_id:
        lines.append(
            f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}"
        )
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", seo_url),
            ("Granska i Change Vault", vault_url),
        ]
    )
    return "\n".join(lines), actions


def _format_batch_reply(batch: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    bid = str(batch.get("batch_id") or "")
    site_id = str(batch.get("site_id") or "")
    audit_id = str(batch.get("audit_id") or "")
    scope = str(batch.get("scope") or "all")
    items = batch.get("items") if isinstance(batch.get("items"), list) else []
    sha = str(batch.get("plan_sha256") or batch.get("batch_sha256") or "")
    seo_url = seo_plan_url(bid) or seo_dashboard_url(site_id or None)
    vault_url = changes_url(site_id, batch_id=bid) if site_id and bid else (changes_url(site_id, category="seo_meta") if site_id else None)

    lines = [
        f"SEO-batchplan {bid} (förhandsvisning — ej utförd)",
        f"Sajt: {site_id} · audit: {audit_id or '-'} · scope: {scope} · åtgärder: {len(items)}",
        f"Status: {batch.get('status', 'pending')} · executed=False · approval_required=True",
        f"plan_sha256: {sha[:16]}…" if sha else "plan_sha256: (saknas)",
    ]
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        ft = str(item.get("fix_type") or "seo")
        tt = str(item.get("target_type") or "")
        tid = str(item.get("target_id") or "")
        summ = str(item.get("summary") or "")[:70]
        lines.append(f"- [{ft}] {tt} #{tid}: {summ}")
    if len(items) > 5:
        lines.append(f"- ... och {len(items) - 5} fler ändringar i batchen")
    lines.append("Fri text i chatten godkänner eller utför aldrig ändringar — Oscar godkänner med låst SHA-256 i SEO Studio.")
    lines.append(
        f"Öppna i SEO Studio: {seo_url or f'/seo?batch_id={bid}'} (kräver inloggning)"
    )
    if site_id:
        lines.append(
            f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?batch_id={bid}'}"
        )
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", seo_url),
            ("Granska i Change Vault", vault_url),
        ]
    )
    return "\n".join(lines), actions


def _format_gsc_reply(site_id: str, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    status = res.get("gsc_status") if isinstance(res.get("gsc_status"), dict) else {}
    mode = str(status.get("mode") or ("mock" if status.get("mock") else "read-only"))
    strike = list(res.get("strike_distance") or [])
    low_ctr = list(res.get("low_ctr") or [])
    cannib = list(res.get("cannibalization") or [])

    lines = [
        f"GSC quick-wins ({site_id} · läge={mode} · read-only)",
        f"Strike-distance (pos 4–15): {len(strike)} · låg CTR: {len(low_ctr)} · kannibalisering: {len(cannib)}",
    ]
    for row in strike[:4]:
        if isinstance(row, dict):
            q = str(row.get("query") or "")[:50]
            pos = row.get("position", "-")
            imp = row.get("impressions", 0)
            lines.append(f"- [pos {pos}] {q} ({imp} visningar)")
    for row in low_ctr[:2]:
        if isinstance(row, dict):
            q = str(row.get("query") or "")[:50]
            ctr = row.get("ctr", 0)
            lines.append(f"- [låg CTR {ctr}] {q}")

    seo_url = seo_dashboard_url(site_id)
    vault_url = changes_url(site_id, category="seo_meta")
    lines.append(f"Öppna i SEO Studio: {seo_url or f'/seo?site_id={site_id}'} (kräver inloggning)")
    lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", seo_url),
            ("Granska i Change Vault", vault_url),
        ]
    )
    return "\n".join(lines), actions


def _format_parity_reply(site_id: str, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    markets = [str(m).upper() for m in (res.get("markets") or [])[:6]]
    rows = list(res.get("rows") or [])
    issues = list(res.get("issues") or [])
    lines = [
        f"Nordisk SEO-paritet ({site_id} · marknader: {', '.join(markets) or 'SE/NO/DK'})",
        f"Jämförda sidor/produkter: {len(rows)} · paritetsfynd: {len(issues)}",
    ]
    for issue in issues[:6]:
        if isinstance(issue, dict):
            sev = str(issue.get("severity") or "info").upper()
            title = str(issue.get("title") or issue.get("message") or issue.get("code") or "Fynd")[:90]
            lines.append(f"- [{sev}] {title}")
    if len(issues) > 6:
        lines.append(f"- ... och {len(issues) - 6} fler paritetsfynd")

    p_url = seo_parity_url(site_id)
    vault_url = changes_url(site_id, category="seo_meta")
    lines.append(f"Öppna i SEO Studio: {p_url or f'/seo/parity?site_id={site_id}'} (kräver inloggning)")
    lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", p_url),
            ("Granska i Change Vault", vault_url),
        ]
    )
    return "\n".join(lines), actions


def _format_diff_reply(site_id: str, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    latest = str(res.get("latest_id") or "-")
    prev = str(res.get("previous_id") or "-")
    has_prev = bool(res.get("has_previous"))
    delta = int(res.get("score_delta") or 0)
    delta_s = f"+{delta}" if delta > 0 else str(delta)
    new_issues = list(res.get("new_issues") or [])
    resolved = list(res.get("resolved_issues") or [])

    lines = [f"SEO-audit diff ({site_id})"]
    if not has_prev:
        lines.append(f"Senaste audit: {latest} (ingen tidigare audit att jämföra mot ännu).")
    else:
        lines.append(f"Jämförelse: {prev} → {latest} · poängändring: {delta_s}")
        lines.append(f"Nya fynd: {len(new_issues)} · lösta fynd: {len(resolved)}")
        for item in new_issues[:4]:
            if isinstance(item, dict):
                lines.append(f"- [+NY] {str(item.get('title') or item.get('code') or 'Fynd')[:80]}")
        for item in resolved[:4]:
            if isinstance(item, dict):
                lines.append(f"- [✓LÖST] {str(item.get('title') or item.get('code') or 'Fynd')[:80]}")

    seo_url = seo_plan_url(latest) if _SEOA_ID_RE.fullmatch(latest) else seo_dashboard_url(site_id)
    vault_url = changes_url(site_id, category="seo_meta")
    lines.append(f"Öppna i SEO Studio: {seo_url or f'/seo?site_id={site_id}'} (kräver inloggning)")
    lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", seo_url),
            ("Granska i Change Vault", vault_url),
        ]
    )
    return "\n".join(lines), actions


def _format_digest_reply(site_id: str, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    md = str(res.get("markdown") or "").strip()
    seo_trend = res.get("seo_trend") if isinstance(res.get("seo_trend"), dict) else {}
    plugin_sum = res.get("plugin_summary") if isinstance(res.get("plugin_summary"), dict) else {}
    parity_sum = res.get("parity_summary") if isinstance(res.get("parity_summary"), dict) else {}

    lines = [f"Veckodigest SEO + Plugins ({site_id} · read-only)"]
    if md:
        for raw_line in md.splitlines()[:12]:
            clean = raw_line.strip()
            if clean:
                lines.append(clean[:140])
    else:
        lines.append(
            f"SEO-trend: delta={seo_trend.get('score_delta', 0)} · paritet-fynd={parity_sum.get('issue_count', 0)}"
        )
        lines.append(
            f"Plugins: väntande uppdateringar={plugin_sum.get('updates_available', 0)}"
        )

    dig_url = seo_digest_url(site_id)
    vault_url = changes_url(site_id, category="seo_meta")
    plug_url = wp_plugins_url(site_id)
    lines.append(f"Öppna i SEO Studio: {dig_url or f'/seo/digest?site_id={site_id}'} (kräver inloggning)")
    lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
    lines.append(f"Öppna /wp/{site_id}/plugins: {plug_url or f'/wp/{site_id}/plugins'}")
    actions = _nav_action_markup(
        [
            ("Öppna i SEO Studio", dig_url),
            ("Granska i Change Vault", vault_url),
            (f"Öppna /wp/{site_id}/plugins", plug_url),
        ]
    )
    return "\n".join(lines), actions


def _format_verify_reply(package_id: str, res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    verified = int(res.get("verified_count") or 0)
    issues = int(res.get("issue_count") or 0)
    results = list(res.get("results") or [])
    lines = [
        f"Länkverifiering {package_id} (read-only)",
        f"Verifierade: {verified} · avvikelser: {issues}",
    ]
    for row in results[:6]:
        if isinstance(row, dict):
            st = str(row.get("status") or ("ok" if row.get("ok") else "issue"))
            url = str(row.get("published_url") or row.get("target_url") or "")[:60]
            lines.append(f"- [{st}] {url}")
    if len(results) > 6:
        lines.append(f"- ... och {len(results) - 6} fler rader")

    seo_url = seo_plan_url(package_id) or seo_dashboard_url(section="link-packages")
    lines.append(
        f"Öppna i SEO Studio: {seo_url or f'/seo/link-packages/{package_id}'} (kräver inloggning)"
    )
    actions = _nav_action_markup([("Öppna i SEO Studio", seo_url)])
    return "\n".join(lines), actions


def _format_plan_detail(entity_kind: str, data: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    if entity_kind == "seoa":
        return _format_audit_reply(data)
    if entity_kind == "seob":
        return _format_batch_reply(data)
    if entity_kind == "seom":
        pid = str(data.get("plan_id") or "")
        site_id = str(data.get("site_id") or "")
        url = seo_plan_url(pid)
        vault_url = changes_url(site_id, category="seo_meta") if site_id else None
        link = url if url else "/seo (kräver inloggning)"
        lines = [
            f"SEO-ändringsplan {pid} (förhandsvisning — ej utförd)",
            f"Sajt: {site_id} · typ: {data.get('fix_type')} · mål: {data.get('target_type')} #{data.get('target_id')}",
            f"Status: {data.get('status', 'pending')} · executed={bool(data.get('executed', False))} · approval_required=True",
            f"before_sha256: {str(data.get('before_sha256') or '')[:16]}…",
            f"plan_sha256: {str(data.get('plan_sha256') or '')[:16]}…",
        ]
        if data.get("summary"):
            lines.append(f"Sammanfattning: {str(data['summary'])[:180]}")
        lines.append(f"Godkänn med låst hash i SEO Studio (Oscar): {link}")
        lines.append(f"Öppna i SEO Studio: {link}")
        if site_id:
            lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
        lines.append("Fri text i chatten godkänner eller utför aldrig ändringar.")
        actions = _nav_action_markup(
            [
                ("Öppna i SEO Studio", url),
                ("Granska i Change Vault", vault_url),
            ]
        )
        return "\n".join(lines), actions
    if entity_kind == "seos":
        sid = str(data.get("strategy_id") or "")
        url = seo_plan_url(sid) or seo_dashboard_url(section="strategies")
        lines = [
            f"SEO-strategi {sid} ({data.get('site_id')} · {str(data.get('market') or '').upper()})",
            f"Titel: {data.get('title')}",
        ]
        if data.get("goals"):
            lines.append(f"Mål: {str(data['goals'])[:180]}")
        lines.append(f"Öppna i SEO Studio: {url or '/seo/strategies (kräver inloggning)'}")
        return "\n".join(lines), _nav_action_markup([("Öppna i SEO Studio", url)])
    if entity_kind == "seoc":
        cid = str(data.get("campaign_id") or "")
        url = seo_plan_url(cid) or seo_dashboard_url(section="campaigns")
        kws = ", ".join(str(k) for k in (data.get("target_keywords") or [])[:6])
        lines = [
            f"SEO-kampanj {cid} ({data.get('site_id')} · {str(data.get('market') or '').upper()})",
            f"Namn: {data.get('name')} · typ: {data.get('campaign_type', 'commercial')}",
        ]
        if kws:
            lines.append(f"Målsökord: {kws}")
        lines.append(f"Öppna i SEO Studio: {url or '/seo/campaigns (kräver inloggning)'}")
        return "\n".join(lines), _nav_action_markup([("Öppna i SEO Studio", url)])
    # seol
    lid = str(data.get("package_id") or "")
    url = seo_plan_url(lid) or seo_dashboard_url(section="link-packages")
    warnings = list(data.get("guardrail_warnings") or [])
    blocked = bool(data.get("guardrail_blocked"))
    lines = [
        f"Länkpaket {lid} ({data.get('site_id')} · {str(data.get('market') or '').upper()})",
        f"Namn: {data.get('name')} · status: {data.get('status', 'draft')}",
        f"Budget: {data.get('budget_sek')} SEK · antal länkar: {data.get('link_count')}",
        f"Guardrails: {'BLOCKERAD — kräver justering' if blocked else 'OK'}"
        + (f" ({len(warnings)} varningar)" if warnings else ""),
    ]
    for w in warnings[:4]:
        lines.append(f"- ⚠ {str(w)[:140]}")
    lines.append("Inga externa köp görs automatiskt — Oscar godkänner och exporterar (MD/JSON/CSV) i SEO Studio.")
    lines.append(f"Öppna i SEO Studio: {url or f'/seo/link-packages/{lid} (kräver inloggning)'}")
    return "\n".join(lines), _nav_action_markup([("Öppna i SEO Studio", url)])


def _format_vendor_row(idx: int, cand: dict[str, Any]) -> str:
    name = str(cand.get("name") or cand.get("vendor_id") or "Leverantör")
    domain = str(cand.get("domain") or "").strip()
    rank_score = cand.get("rank_score")
    vet_score = cand.get("vetting_score")
    status = str(cand.get("vetting_status") or cand.get("status") or "vetted")
    price_range = str(cand.get("price_range_sek") or cand.get("typical_price_sek") or "").strip()
    if not price_range:
        lo = cand.get("unit_min_sek") or cand.get("price_min_sek")
        hi = cand.get("unit_max_sek") or cand.get("price_max_sek")
        if lo and hi:
            price_range = f"{int(lo)}–{int(hi)} SEK/länk"
    score_parts: list[str] = []
    if isinstance(rank_score, (int, float)):
        score_parts.append(f"match {rank_score:.0f}/100")
    if isinstance(vet_score, (int, float)):
        score_parts.append(f"vetting {vet_score:.0f}/100")
    score_txt = ", ".join(score_parts) if score_parts else status
    dom_part = f" ({domain})" if domain else ""
    price_part = f" · {price_range}" if price_range else ""
    return f"- {idx}. {name}{dom_part} — {score_txt} [{status}]{price_part}"


def _format_vendors_reply(
    market: str,
    budget: float,
    count: int,
    res: dict[str, Any],
) -> tuple[str, ActionMarkup | None]:
    mkt = str(res.get("market") or market).lower()
    tier = str(res.get("recommended_tier") or "").strip()
    selected_id = str(res.get("selected_vendor_id") or "").strip()
    budget_warning = str(res.get("budget_warning") or "").strip()
    blocked = bool(res.get("blocked"))
    block_reason = str(res.get("block_reason") or "").strip()
    candidates = [c for c in (res.get("candidates") or []) if isinstance(c, dict)]
    notice = str(res.get("no_auto_purchase_notice") or "").strip()

    lines = [
        f"Kvalitetssäkrade nordiska SEO-butiker ({mkt.upper()} · {int(budget)} SEK · {count} länkar · read-only)",
    ]
    if tier:
        lines.append(f"Rekommenderad nivå: {tier}")
    if blocked:
        lines.append(f"⚠ Blockerad: {block_reason or 'Guardrail-avvikelse'}")
    if budget_warning:
        lines.append(f"⚠ Budgetvarning: {budget_warning[:220]}")
    if candidates:
        lines.append("Topp granskade SEO-butiker:")
        for idx, cand in enumerate(candidates[:3], start=1):
            lines.append(_format_vendor_row(idx, cand))
    else:
        lines.append("Inga godkända SEO-butiker hittades för angivna kriterier.")
    if selected_id:
        sel_name = next(
            (str(c.get("name") or selected_id) for c in candidates if str(c.get("vendor_id") or "") == selected_id),
            selected_id,
        )
        lines.append(f"Primärt förslag: {sel_name} ({selected_id})")
    lines.append(
        notice
        or "Inga externa köp görs automatiskt — Oscar godkänner och exporterar beställningsbrief (MD/JSON/CSV) i SEO Studio."
    )

    v_url = seo_vendors_url(mkt)
    lp_url = seo_dashboard_url(section="link-packages")
    lines.append(f"Öppna SEO-butiker: {v_url or f'/seo/vendors?market={mkt}'} (kräver inloggning)")
    actions = _nav_action_markup(
        [
            ("Öppna SEO-butiker", v_url),
            ("Öppna länkpaket", lp_url),
        ]
    )
    return "\n".join(lines), actions


def _format_autopilot_reply(res: dict[str, Any]) -> tuple[str, ActionMarkup | None]:
    site_id = str(res.get("site_id") or "")
    market = str(res.get("market") or "no").lower()
    topic = str(res.get("topic") or "").strip()
    audit = res.get("audit") if isinstance(res.get("audit"), dict) else {}
    serp = res.get("serp_research") if isinstance(res.get("serp_research"), dict) else {}
    strat = res.get("strategy") if isinstance(res.get("strategy"), dict) else {}
    camp = res.get("campaign") if isinstance(res.get("campaign"), dict) else {}
    vendors = res.get("vendor_selection") if isinstance(res.get("vendor_selection"), dict) else {}
    pkg = res.get("link_package") if isinstance(res.get("link_package"), dict) else {}
    batch = res.get("batch_fix") if isinstance(res.get("batch_fix"), dict) else None

    audit_id = str(audit.get("audit_id") or "")
    strategy_id = str(strat.get("strategy_id") or "")
    campaign_id = str(camp.get("campaign_id") or "")
    package_id = str(pkg.get("package_id") or "")
    selected_vid = str(vendors.get("selected_vendor_id") or pkg.get("selected_vendor_id") or "").strip()
    candidates = [c for c in (vendors.get("candidates") or []) if isinstance(c, dict)]
    tier = str(vendors.get("recommended_tier") or "").strip()
    budget_warning = str(vendors.get("budget_warning") or "").strip()

    lines = [
        f"SEO-autopilot klar ({site_id} · {market.upper()}) — utkast & förhandsvisning",
    ]
    if topic:
        lines.append(f"Ämne: {topic[:160]}")
    if audit_id:
        score = audit.get("score")
        grade = str(audit.get("grade") or "")
        score_s = f" · poäng {score}/100 ({grade})" if isinstance(score, int) else ""
        lines.append(f"Audit: {audit_id}{score_s}")
    if serp:
        serp_sources = serp.get("citations") or serp.get("sources") or []
        serp_status = str(serp.get("provenance") or serp.get("status") or "ok")
        lines.append(f"SERP-research: {serp_status} ({len(serp_sources) if isinstance(serp_sources, list) else 0} källor)")
    if strategy_id:
        lines.append(f"Strategi: {strategy_id} — {str(strat.get('title') or '')[:90]}")
    if campaign_id:
        lines.append(f"Kampanj: {campaign_id} — {str(camp.get('name') or '')[:90]}")
    if candidates:
        tier_suffix = f" ({tier})" if tier else ""
        lines.append(f"Granskade nordiska SEO-butiker{tier_suffix}:")
        for idx, cand in enumerate(candidates[:3], start=1):
            lines.append(_format_vendor_row(idx, cand))
    if selected_vid:
        sel_name = next(
            (str(c.get("name") or selected_vid) for c in candidates if str(c.get("vendor_id") or "") == selected_vid),
            selected_vid,
        )
        lines.append(f"Vald SEO-butik: {sel_name} ({selected_vid})")
    if budget_warning:
        lines.append(f"⚠ Budgetvarning: {budget_warning[:200]}")
    if package_id:
        lines.append(
            f"Länkpaket-utkast: {package_id} · status={pkg.get('status', 'draft')} · "
            f"budget={pkg.get('budget_sek')} SEK · {pkg.get('link_count')} länkar"
        )
    if batch and batch.get("batch_id"):
        lines.append(f"Batchplan (förhandsvisning): {batch.get('batch_id')} (ej utförd)")

    lines.append(
        "executed=False · external_order_placed=False · approval_required=True — "
        "inga sajtändringar eller externa köp har utförts; Oscar granskar och godkänner i SEO Studio."
    )

    pkg_url = seo_plan_url(package_id) if package_id else seo_dashboard_url(section="link-packages")
    v_url = seo_vendors_url(market)
    hub_url = seo_dashboard_url(site_id)
    lines.append(
        f"Öppna länkpaket: {pkg_url or (f'/seo/link-packages/{package_id}' if package_id else '/seo/link-packages')} (kräver inloggning)"
    )
    lines.append(f"Öppna SEO-butiker: {v_url or f'/seo/vendors?market={market}'}")
    actions = _nav_action_markup(
        [
            ("Öppna länkpaket", pkg_url),
            ("Öppna SEO-butiker", v_url),
            ("Öppna i SEO Studio", hub_url),
        ]
    )
    return "\n".join(lines), actions


def _parse_vendors_args(rest: str, default_market: str) -> tuple[str, float, int]:
    """Parse ``/seo vendors [se|no|dk] [budget] [count]`` arguments."""
    market = default_market if default_market in {"se", "no", "dk"} else "no"
    market_set = False
    text = rest.strip()
    tokens = text.split()
    kept: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in {"se", "no", "dk"} and not market_set:
            market = low
            market_set = True
            continue
        if low.startswith("market=") and low.split("=", 1)[1] in {"se", "no", "dk"}:
            market = low.split("=", 1)[1]
            market_set = True
            continue
        kept.append(tok)
    rem = " ".join(kept)
    _name, budget, count = _parse_links_args(f"vendors {rem}" if rem else "vendors")
    return market, budget, count


def _parse_autopilot_args(
    rest: str,
    known_site_ids: list[str],
) -> tuple[str | None, str | None, str, float, int]:
    """Parse ``/seo autopilot [site-id] <topic> [budget] [count]`` arguments."""
    text = rest.strip()
    if not text:
        return None, None, "", 15000.0, 6
    tokens = text.split()
    cand_site: str | None = None
    explicit_market: str | None = None
    known_low = {s.lower() for s in known_site_ids}
    if tokens and tokens[0].lower() in known_low:
        cand_site = tokens[0].lower()
        tokens = tokens[1:]
    filtered: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low.startswith("market=") and low.split("=", 1)[1] in {"se", "no", "dk"}:
            explicit_market = low.split("=", 1)[1]
            continue
        filtered.append(tok)
    rem = " ".join(filtered).strip()
    if not rem:
        return cand_site, explicit_market, "", 15000.0, 6
    topic, budget, count = _parse_links_args(rem)
    if topic == "Länkpaket" and not rem:
        topic = ""
    return cand_site, explicit_market, topic, budget, count


def _parse_links_args(rest: str) -> tuple[str, float, int]:
    """Extract target/name, optional budget_sek, and optional link_count from ``/seo links`` args."""
    budget = 15000.0
    count = 6
    text = rest.strip()

    budget_found = False
    bm = _EXPLICIT_BUDGET_RE.search(text)
    if bm:
        raw_num = (bm.group(1) or bm.group(2) or "").replace(" ", "").replace("_", "")
        try:
            val = float(raw_num)
            if 1000.0 <= val <= 500000.0:
                budget = val
                budget_found = True
                text = (text[: bm.start()] + " " + text[bm.end() :]).strip()
        except ValueError:
            pass
    if not budget_found:
        bm2 = _BARE_TRAILING_BUDGET_RE.search(text)
        if bm2:
            try:
                val = float(bm2.group(1))
                int_val = int(val)
                if 1000.0 <= val <= 500000.0 and not (2020 <= int_val <= 2039):
                    budget = val
                    text = (text[: bm2.start()] + " " + text[bm2.end() :]).strip()
            except ValueError:
                pass

    count_found = False
    cm = _EXPLICIT_COUNT_RE.search(text)
    if cm:
        raw_cnt = cm.group(1) or cm.group(2) or ""
        try:
            val_c = int(raw_cnt)
            if 1 <= val_c <= 50:
                count = val_c
                count_found = True
                text = (text[: cm.start()] + " " + text[cm.end() :]).strip()
        except ValueError:
            pass
    if not count_found:
        cm2 = _TRAILING_COUNT_RE.search(text)
        if cm2 and text[: cm2.start()].strip():
            try:
                val_c = int(cm2.group(1))
                if 1 <= val_c <= 50:
                    count = val_c
                    text = text[: cm2.start()].strip()
            except ValueError:
                pass

    name = " ".join(text.split()) or "Länkpaket"
    return name, budget, count


def _service_failure_text(res: Any, fallback: str) -> str:
    if isinstance(res, dict):
        err = str(res.get("error") or res.get("message") or "").strip()
        err_cls = str(res.get("error_class") or "").strip()
        if err:
            return f"{fallback}: {err[:200]}"
        if err_cls:
            return f"{fallback} ({err_cls})."
    return f"{fallback}."


def handle_seo_command(
    handler: Any,
    chat_id: str | int,
    text: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BotReply | None:
    """Handle ``/seo ...`` slash commands synchronously in mock/live safe mode."""
    raw = (text or "").strip()
    match = _SEO_CMD_RE.match(raw)
    if not match:
        return None

    try:
        actor = handler._resolve_actor(chat_id)
        actor_obj = resolve_actor(actor)
    except (TelegramActorDenied, ChannelActorDenied, AccessDenied):
        return handler._deny_actor()

    if not (actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.MARKETING_READ)):
        return BotReply(text="Åtkomst nekad: /seo kräver WP_OPS_READ eller MARKETING_READ.")

    arg_str = (match.group(1) or "").strip()
    if not arg_str:
        return bot_reply(_seo_help_text(), actions=_nav_action_markup([("Öppna i SEO Studio", seo_dashboard_url())]))

    parts = arg_str.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in {"help", "?"}:
        return bot_reply(_seo_help_text(), actions=_nav_action_markup([("Öppna i SEO Studio", seo_dashboard_url())]))

    from ecom_ops.seo.service import SeoStudioService

    t0 = time.monotonic()
    step_id = f"cmd:seo:{sub}"
    emit_progress(
        on_progress,
        ProgressEvent(
            step_id=step_id,
            tool=f"seo_{sub}",
            phase="started",
            label="SEO Studio",
            detail=f"/seo {sub}",
        ),
    )

    svc = SeoStudioService()

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
                tool=f"seo_{sub}",
                phase="completed" if ok else "failed",
                label="SEO Studio",
                detail=f"/seo {sub}",
                outcome="klar" if ok else "fel",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            ),
        )
        return bot_reply(reply_text, actions=actions)

    try:
        if sub in {"scan", "audit"}:
            tokens = rest.split() if rest else []
            cand_site: str | None = None
            path = "/"
            for tok in tokens:
                if tok.startswith("/"):
                    path = tok
                elif cand_site is None:
                    cand_site = tok
            site_id, market = _resolve_site_and_market(handler, chat_id, cand_site)
            res = svc.run_audit(site_id=site_id, actor=actor, scope="site", path=path, market=market)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-audit misslyckades"), ok=False)
            txt, acts = _format_audit_reply(res)
            return _finish(txt, actions=acts)

        if sub == "product":
            if not rest:
                return _finish("Användning: /seo product <id|slug>", ok=False)
            site_id, market = _resolve_site_and_market(handler, chat_id)
            res = svc.run_audit(
                site_id=site_id, actor=actor, scope="product", target_id=rest.split()[0], market=market
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Produkt-SEO-audit misslyckades"), ok=False)
            txt, acts = _format_audit_reply(res)
            return _finish(txt, actions=acts)

        if sub == "page":
            if not rest:
                return _finish("Användning: /seo page <id|slug>", ok=False)
            site_id, market = _resolve_site_and_market(handler, chat_id)
            res = svc.run_audit(
                site_id=site_id, actor=actor, scope="page", target_id=rest.split()[0], market=market
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Sid-SEO-audit misslyckades"), ok=False)
            txt, acts = _format_audit_reply(res)
            return _finish(txt, actions=acts)

        if sub == "media":
            cand = rest.split()[0] if rest else None
            site_id, market = _resolve_site_and_market(handler, chat_id, cand)
            res = svc.run_audit(site_id=site_id, actor=actor, scope="media", market=market)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Bild-SEO-audit misslyckades"), ok=False)
            txt, acts = _format_audit_reply(res)
            return _finish(txt, actions=acts)

        if sub == "batch":
            tokens = rest.split() if rest else []
            audit_id = ""
            batch_scope = "all"
            cand_site = None
            for tok in tokens:
                low = tok.strip().lower()
                if _SEOA_ID_RE.fullmatch(low) and not audit_id:
                    audit_id = low
                elif low in _BATCH_SCOPES:
                    batch_scope = low
                elif cand_site is None:
                    cand_site = tok
            if not audit_id:
                return _finish(
                    "Användning: /seo batch [site-id] <seoa-id> [media_alt|seo_meta|all]",
                    ok=False,
                )
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand_site)
            if not actor_obj.has(Permission.WP_OPS_OPERATE):
                return _finish(
                    "Åtkomst nekad: /seo batch kräver WP_OPS_OPERATE (staging) eller WP_OPS_ADMIN (produktion).",
                    ok=False,
                )
            if _site_environment(site_id) == "production" and not actor_obj.has(Permission.WP_OPS_ADMIN):
                return _finish(
                    "Åtkomst nekad: /seo batch på produktionssajt kräver Oscar (WP_OPS_ADMIN).",
                    ok=False,
                )
            res = svc.propose_batch_from_audit(site_id, audit_id, scope=batch_scope, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-batchplan misslyckades"), ok=False)
            batch = dict(res.get("batch") or {})
            txt, acts = _format_batch_reply(batch)
            return _finish(txt, actions=acts)

        if sub == "gsc":
            cand = rest.split()[0] if rest else None
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand)
            res = svc.get_gsc_quick_wins(site_id, days=28, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "GSC quick-wins misslyckades"), ok=False)
            txt, acts = _format_gsc_reply(site_id, res)
            return _finish(txt, actions=acts)

        if sub == "parity":
            cand = rest.split()[0] if rest else None
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand)
            res = svc.run_nordic_parity(site_id, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Nordisk paritetsanalys misslyckades"), ok=False)
            txt, acts = _format_parity_reply(site_id, res)
            return _finish(txt, actions=acts)

        if sub == "diff":
            cand = rest.split()[0] if rest else None
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand)
            res = svc.diff_audits(site_id, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-audit diff misslyckades"), ok=False)
            txt, acts = _format_diff_reply(site_id, res)
            return _finish(txt, actions=acts)

        if sub == "digest":
            from ecom_ops.seo.digest import build_seo_plugin_drift_digest

            cand = rest.split()[0] if rest else None
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand)
            res = build_seo_plugin_drift_digest(site_id, actor=actor, days=7)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO+plugin-digest misslyckades"), ok=False)
            txt, acts = _format_digest_reply(site_id, res)
            return _finish(txt, actions=acts)

        if sub == "verify":
            pid = rest.split()[0].strip().lower() if rest else ""
            if not _SEOL_ID_RE.fullmatch(pid):
                return _finish("Användning: /seo verify <seol-id>", ok=False)
            res = svc.verify_link_package(pid, actor=actor)
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Länkverifiering misslyckades"), ok=False)
            txt, acts = _format_verify_reply(pid, res)
            return _finish(txt, actions=acts)

        if sub == "vendors":
            _site_id, default_market = _resolve_site_and_market(handler, chat_id)
            market, budget, count = _parse_vendors_args(rest, default_market)
            res = svc.vet_link_vendors(
                market=market,
                budget_sek=budget,
                link_count=count,
                include_web_research=True,
                actor=actor,
            )
            if not isinstance(res, dict) or (res.get("ok") is False and not res.get("blocked")):
                return _finish(_service_failure_text(res, "Granskning av SEO-butiker misslyckades"), ok=False)
            txt, acts = _format_vendors_reply(market, budget, count, res)
            return _finish(txt, actions=acts)

        if sub == "autopilot":
            if not (
                actor_obj.has(Permission.MARKETING_SUGGEST)
                or actor_obj.has(Permission.MARKETING_MUTATE)
            ):
                return _finish(
                    "Åtkomst nekad: /seo autopilot kräver MARKETING_SUGGEST (Jonatan eller Oscar).",
                    ok=False,
                )
            if not rest:
                return _finish(
                    "Användning: /seo autopilot [site-id] <ämne> [budget] [antal]",
                    ok=False,
                )
            from ecom_ops.wp_ops.registry import load_sites

            try:
                known_ids = [s.id for s in load_sites()]
            except Exception:
                known_ids = []
            cand_site, explicit_market, topic, budget, count = _parse_autopilot_args(rest, known_ids)
            if not topic:
                return _finish(
                    "Användning: /seo autopilot [site-id] <ämne> [budget] [antal]",
                    ok=False,
                )
            site_id, inferred_market = _resolve_site_and_market(handler, chat_id, cand_site)
            res = svc.run_seo_autopilot(
                site_id,
                topic=topic[:180],
                market=explicit_market or inferred_market,
                budget_sek=budget,
                link_count=count,
                include_web_research=True,
                include_vendor_vetting=True,
                include_batch_fixes=False,
                actor=actor,
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-autopilot misslyckades"), ok=False)
            txt, acts = _format_autopilot_reply(res)
            return _finish(txt, actions=acts)

        if sub == "strategy":
            if not rest:
                return _finish("Användning: /seo strategy <ämne>", ok=False)
            site_id, market = _resolve_site_and_market(handler, chat_id)
            res = svc.create_strategy(
                site_id=site_id,
                actor=actor,
                title=rest[:180],
                market=market,
                focus_topics=[rest[:80]],
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-strategi misslyckades"), ok=False)
            strat = dict(res.get("strategy") or {})
            txt, acts = _format_plan_detail("seos", strat)
            return _finish(txt, actions=acts)

        if sub == "campaign":
            if not rest:
                return _finish("Användning: /seo campaign <namn>", ok=False)
            site_id, market = _resolve_site_and_market(handler, chat_id)
            res = svc.create_campaign(
                site_id=site_id,
                actor=actor,
                name=rest[:180],
                market=market,
                target_keywords=[rest[:80]],
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "SEO-kampanj misslyckades"), ok=False)
            camp = dict(res.get("campaign") or {})
            txt, acts = _format_plan_detail("seoc", camp)
            return _finish(txt, actions=acts)

        if sub == "links":
            if not actor_obj.has(Permission.MARKETING_SUGGEST):
                return _finish(
                    "Åtkomst nekad: /seo links kräver MARKETING_SUGGEST (Jonatan eller Oscar).",
                    ok=False,
                )
            if not rest:
                return _finish("Användning: /seo links <mål> [budget] [antal]", ok=False)
            name, budget, count = _parse_links_args(rest)
            site_id, market = _resolve_site_and_market(handler, chat_id)
            res = svc.create_link_package(
                site_id=site_id,
                actor=actor,
                name=name[:180],
                market=market,
                budget_sek=budget,
                link_count=count,
            )
            if not isinstance(res, dict) or not res.get("ok"):
                return _finish(_service_failure_text(res, "Länkpaket misslyckades"), ok=False)
            pkg = dict(res.get("package") or {})
            txt, acts = _format_plan_detail("seol", pkg)
            return _finish(txt, actions=acts)

        if sub == "plans":
            cand = rest.split()[0] if rest else None
            site_id, _market = _resolve_site_and_market(handler, chat_id, cand)
            fix_plans = svc.list_fix_plans(site_id=site_id, limit=5)
            fix_batches = svc.list_fix_batches(site_id=site_id, limit=5) if hasattr(svc, "list_fix_batches") else []
            strategies = svc.list_strategies(site_id=site_id, limit=5)
            campaigns = svc.list_campaigns(site_id=site_id, limit=5)
            packages = svc.list_link_packages(site_id=site_id, limit=5)
            lines = [f"SEO Studio-översikt ({site_id}):"]
            lines.append(f"Meta-/alt-planer ({len(fix_plans)}):")
            for p in fix_plans[:5]:
                lines.append(
                    f"- {p.get('plan_id')} | {p.get('fix_type')} | {p.get('status')} | {str(p.get('summary') or p.get('target_id') or '')[:50]}"
                )
            lines.append(f"Batchplaner ({len(fix_batches)}):")
            for b in fix_batches[:5]:
                lines.append(
                    f"- {b.get('batch_id')} | {b.get('scope', 'all')} | {b.get('status')} | {len(b.get('items') or [])} st"
                )
            lines.append(f"Strategier ({len(strategies)}):")
            for s in strategies[:5]:
                lines.append(f"- {s.get('strategy_id')} | {str(s.get('title') or '')[:60]}")
            lines.append(f"Kampanjer ({len(campaigns)}):")
            for c in campaigns[:5]:
                lines.append(f"- {c.get('campaign_id')} | {str(c.get('name') or '')[:60]}")
            lines.append(f"Länkpaket ({len(packages)}):")
            for lpkg in packages[:5]:
                lines.append(
                    f"- {lpkg.get('package_id')} | {lpkg.get('status')} | {str(lpkg.get('name') or '')[:50]}"
                )
            seo_url = seo_dashboard_url(site_id)
            vault_url = changes_url(site_id, category="seo_meta")
            lines.append(f"Öppna i SEO Studio: {seo_url or '/seo (kräver inloggning)'}")
            lines.append(f"Granska i Change Vault: {vault_url or f'/wp/{site_id}/changes?category=seo_meta'}")
            lines.append("Visa detalj: /seo plan <id>")
            actions = _nav_action_markup(
                [
                    ("Öppna i SEO Studio", seo_url),
                    ("Granska i Change Vault", vault_url),
                ]
            )
            return _finish("\n".join(lines), actions=actions)

        if sub == "plan":
            pid = rest.split()[0].strip().lower() if rest else ""
            id_match = _SEO_ID_RE.fullmatch(pid)
            if not id_match:
                return _finish(
                    "Ange giltigt SEO-id: /seo plan <seoa-|seom-|seob-|seos-|seoc-|seol-...>",
                    ok=False,
                )
            prefix = id_match.group(1).lower()
            lookup = {
                "seoa": svc.get_audit,
                "seom": svc.get_fix_plan,
                "seob": svc.get_fix_batch,
                "seos": svc.get_strategy,
                "seoc": svc.get_campaign,
                "seol": svc.get_link_package,
            }[prefix]
            row = lookup(pid)
            if not row:
                return _finish(f"Hittade inte SEO-post {pid}.", ok=False)
            txt, acts = _format_plan_detail(prefix, row)
            return _finish(txt, actions=acts)

        # Bare SEO ID directly: /seo seom-... or /seo seob-...
        direct_match = _SEO_ID_RE.fullmatch(sub)
        if direct_match:
            prefix = direct_match.group(1).lower()
            lookup = {
                "seoa": svc.get_audit,
                "seom": svc.get_fix_plan,
                "seob": svc.get_fix_batch,
                "seos": svc.get_strategy,
                "seoc": svc.get_campaign,
                "seol": svc.get_link_package,
            }[prefix]
            row = lookup(sub)
            if not row:
                return _finish(f"Hittade inte SEO-post {sub}.", ok=False)
            txt, acts = _format_plan_detail(prefix, row)
            return _finish(txt, actions=acts)

        return _finish(f"Okänt underkommando: /seo {sub}\n\n{_seo_help_text()}", ok=False)
    except _UnknownSiteError as exc:
        return _finish(f"Okänd sajt: {exc}", ok=False)
    except AccessDenied as exc:
        return _finish(f"Åtkomst nekad: {exc}", ok=False)
    except Exception as exc:
        return _finish(f"SEO-kommando misslyckades ({type(exc).__name__}).", ok=False)


__all__ = [
    "EXPLICIT_ADS_RE",
    "SEO_INTENT_RE",
    "SEO_META_EDIT_RE",
    "handle_seo_command",
]
