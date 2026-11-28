"""Weekly SEO & Plugin Drift Digest generator (Proposal 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.seo.policy import require_seo_read
from ecom_ops.seo.service import SeoStudioService
from ecom_ops.wp_ops.config import is_mock_mode


def build_seo_plugin_drift_digest(
    site_id: str,
    *,
    actor: str = "jonatan",
    days: int = 7,
    data_dir: Path | None = None,
    seo_service: SeoStudioService | None = None,
    plugin_service: Any | None = None,
    gsc_result: dict[str, Any] | None = None,
    parity_result: dict[str, Any] | None = None,
    include_live_probes: bool = True,
) -> dict[str, Any]:
    """Combine SEO audit trend diff, GSC quick wins, Nordic parity, orphan/cannibalization counts,
    and WordPress plugin update drift into structured JSON + concise Swedish Messenger/Telegram markdown.
    """
    require_seo_read(actor)
    clean_site = str(site_id or "").strip().lower()
    if not clean_site:
        raise ValueError("site_id must not be empty")

    svc = seo_service or SeoStudioService(data_dir=data_dir)
    days_int = max(1, min(int(days or 7), 90))

    # 1. SEO audit trend + latest audit orphan/cannibalization counts
    diff_res = svc.diff_audits(clean_site, actor=actor)
    recent_audits = svc.list_audits(site_id=clean_site, limit=1)
    latest_audit = recent_audits[0] if recent_audits else None
    latest_summary = (latest_audit or {}).get("summary") or {}
    orphan_count = int(
        latest_summary.get("orphan_pages_count")
        or ((latest_audit or {}).get("links") or {}).get("orphan_count", 0)
    )
    cannibalization_count = int(
        latest_summary.get("cannibalization_count")
        or len((latest_audit or {}).get("cannibalization") or [])
    )
    seo_trend = {
        "has_previous": bool(diff_res.get("has_previous")),
        "latest_id": str(diff_res.get("latest_id") or ""),
        "previous_id": diff_res.get("previous_id"),
        "latest_score": (diff_res.get("summary") or {}).get("latest_score"),
        "latest_grade": (diff_res.get("summary") or {}).get("latest_grade", ""),
        "score_delta": int(diff_res.get("score_delta") or 0),
        "new_issues_count": len(diff_res.get("new_issues") or []),
        "resolved_issues_count": len(diff_res.get("resolved_issues") or []),
        "orphan_pages_count": orphan_count,
        "cannibalization_count": cannibalization_count,
    }

    # 2. GSC quick wins
    gsc_res = (
        gsc_result
        if isinstance(gsc_result, dict)
        else svc.get_gsc_quick_wins(clean_site, days=max(days_int, 28), actor=actor)
    )
    gsc_sum = dict(gsc_res.get("summary") or {})
    top_strike = [
        {
            "query": str(r.get("query") or ""),
            "position": r.get("position"),
            "impressions": r.get("impressions"),
            "estimated_click_upside": r.get("estimated_click_upside", 0),
        }
        for r in (gsc_res.get("strike_distance") or [])[:3]
    ]
    gsc_summary = {
        "mode": str((gsc_res.get("gsc_status") or {}).get("mode") or "mock"),
        "connected": bool((gsc_res.get("gsc_status") or {}).get("connected")),
        "strike_distance_count": int(gsc_sum.get("strike_distance_count", 0)),
        "low_ctr_count": int(gsc_sum.get("low_ctr_count", 0)),
        "cannibalization_count": int(gsc_sum.get("cannibalization_count", 0)),
        "estimated_click_upside": int(gsc_sum.get("estimated_click_upside", 0)),
        "top_strike_distance": top_strike,
    }

    run_live = bool(include_live_probes or is_mock_mode())

    # 3. Nordic SE/NO/DK parity
    if isinstance(parity_result, dict) and parity_result:
        parity_res = parity_result
    elif run_live:
        parity_res = svc.run_nordic_parity(clean_site, actor=actor)
    else:
        parity_res = {"markets": [], "summary": {}}
    parity_sum = dict(parity_res.get("summary") or {})
    parity_summary = {
        "markets": list(parity_res.get("markets") or []),
        "total_pages": int(parity_sum.get("total_pages", 0)),
        "missing_translations": int(parity_sum.get("missing_translations", 0)),
        "swedish_leaks": int(parity_sum.get("swedish_leaks", 0)),
        "wrong_currency": int(parity_sum.get("wrong_currency", 0)),
        "hreflang_issues": int(parity_sum.get("hreflang_issues", 0)),
        "total_issues": int(parity_sum.get("total_issues", 0)),
    }

    # 4. WordPress plugin update drift (fail-soft if bridge/site unavailable)
    plugin_summary: dict[str, Any] = {
        "available": False,
        "total_plugins": 0,
        "updates_available": 0,
        "critical_updates": 0,
        "pending_plugins": [],
        "error": "" if run_live else "Klicka på Veckodigest för live-kontroll",
    }
    if run_live or plugin_service is not None:
        try:
            psvc = plugin_service
            if psvc is None:
                from ecom_ops.wp_ops.plugin_service import PluginOpsService

                psvc = PluginOpsService(data_dir=data_dir)
            inv = psvc.plugin_inventory(clean_site, actor=actor, include_updates=True)
            inv_ok = bool(getattr(inv, "ok", False) if not isinstance(inv, dict) else inv.get("ok"))
            inv_data = (
                dict(getattr(inv, "data", {}) or {})
                if not isinstance(inv, dict)
                else dict(inv.get("data") or inv)
            )
            if inv_ok:
                ps = dict(inv_data.get("summary") or {})
                pending_list = [
                    {
                        "slug": str(p.get("slug") or ""),
                        "version": str(p.get("version") or ""),
                        "new_version": str(p.get("new_version") or ""),
                        "is_critical": bool(p.get("is_critical")),
                        "risk_class": str(p.get("risk_class") or ""),
                    }
                    for p in (inv_data.get("plugins") or [])
                    if isinstance(p, dict) and p.get("update_available")
                ]
                plugin_summary = {
                    "available": True,
                    "total_plugins": int(ps.get("total", len(inv_data.get("plugins") or []))),
                    "updates_available": int(ps.get("updates_available", len(pending_list))),
                    "critical_updates": int(
                        ps.get(
                            "critical_updates",
                            sum(1 for p in pending_list if p.get("is_critical")),
                        )
                    ),
                    "pending_plugins": pending_list[:10],
                    "error": "",
                }
            else:
                err_msg = (
                    str(getattr(inv, "message", "") or getattr(inv, "error_class", ""))
                    if not isinstance(inv, dict)
                    else str(inv.get("message") or inv.get("error_class") or "")
                )
                plugin_summary["error"] = err_msg[:160]
        except Exception as exc:
            plugin_summary["error"] = str(exc)[:160]

    # 5. Direct dashboard links
    dashboard_links = {
        "seo": f"/seo?site={clean_site}",
        "plugins": f"/wp/{clean_site}/plugins",
        "changes": f"/wp/{clean_site}/changes?category=seo_meta",
    }

    # 6. Concise Swedish Messenger/Telegram markdown
    score_txt = (
        f"{seo_trend['latest_score']}/100 ({seo_trend['latest_grade']})"
        if seo_trend["latest_score"] is not None
        else "Ingen audit ännu"
    )
    delta = seo_trend["score_delta"]
    delta_txt = f"+{delta}" if delta > 0 else str(delta)
    trend_line = (
        f"Trend: {delta_txt} p (+{seo_trend['new_issues_count']} nya / -{seo_trend['resolved_issues_count']} lösta)"
        if seo_trend["has_previous"]
        else "Första baslinje-audit (ingen tidigare jämförelse)"
    )

    lines: list[str] = [
        f"**Veckorapport SEO & Plugin-drift – `{clean_site}` ({days_int}d)**",
        "",
        f"- **SEO-hälsa:** {score_txt} · {trend_line}",
        f"- **Struktur:** {orphan_count} föräldralösa sidor · {cannibalization_count} kannibaliseringsfynd",
        (
            f"- **GSC Quick Wins ({gsc_summary['mode']}):** "
            f"{gsc_summary['strike_distance_count']} i slagläge (pos 4–15), "
            f"{gsc_summary['low_ctr_count']} låg CTR (+{gsc_summary['estimated_click_upside']} est. klick)"
        ),
    ]
    if top_strike:
        top_q = ", ".join(f"\"{q['query']}\" (pos {q['position']})" for q in top_strike[:2])
        lines.append(f"  - Topp slagläge: {top_q}")
    lines.append(
        f"- **Nordisk paritet (SE/NO/DK):** {parity_summary['total_issues']} fynd "
        f"(saknad översättning: {parity_summary['missing_translations']}, "
        f"svenskläckage: {parity_summary['swedish_leaks']}, "
        f"fel valuta: {parity_summary['wrong_currency']}, "
        f"hreflang: {parity_summary['hreflang_issues']})"
    )
    if plugin_summary["available"]:
        lines.append(
            f"- **Plugins:** {plugin_summary['updates_available']} väntande uppdateringar "
            f"({plugin_summary['critical_updates']} kritiska) av {plugin_summary['total_plugins']} installerade"
        )
    else:
        lines.append(
            f"- **Plugins:** Ej tillgänglig ({plugin_summary['error'] or 'bridge ej ansluten'})"
        )
    lines.extend(
        [
            "",
            "**Snabblänkar:**",
            f"- SEO Studio: `{dashboard_links['seo']}`",
            f"- Plugins: `{dashboard_links['plugins']}`",
            f"- Change Vault (SEO): `{dashboard_links['changes']}`",
        ]
    )
    markdown = "\n".join(lines)

    return {
        "ok": True,
        "site_id": clean_site,
        "days": days_int,
        "seo_trend": seo_trend,
        "gsc_summary": gsc_summary,
        "parity_summary": parity_summary,
        "plugin_summary": plugin_summary,
        "markdown": markdown,
        "dashboard_links": dashboard_links,
    }


__all__ = ["build_seo_plugin_drift_digest"]
