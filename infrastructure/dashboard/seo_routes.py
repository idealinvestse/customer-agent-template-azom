"""Dashboard routes for dedicated SEO Studio (/seo)."""

from __future__ import annotations

import inspect
import json
import math
import re
from typing import Any

from flask import (
    Blueprint,
    Flask,
    Response,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import _auth_required, _dashboard_actor
from ecom_ops.rbac import AccessDenied, Permission
from ecom_ops.runtime_env import env_flag
from support import _dashboard_context, _data_dir, _flash, _fmt_dt, _is_mock

seo_bp = Blueprint("seo", __name__)

SITE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SEO_AUDIT_ID_RE = re.compile(r"^seoa-[a-f0-9]{12}$")
SEO_PLAN_ID_RE = re.compile(r"^seom-[a-f0-9]{12}$")
SEO_BATCH_ID_RE = re.compile(r"^seob-[a-f0-9]{12}$")
SEO_STRATEGY_ID_RE = re.compile(r"^seos-[a-f0-9]{12}$")
SEO_CAMPAIGN_ID_RE = re.compile(r"^seoc-[a-f0-9]{12}$")
SEO_LINK_ID_RE = re.compile(r"^seol-[a-f0-9]{12}$")
VENDOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
_VALID_VENDOR_GOALS = frozenset(
    {
        "organic_growth",
        "brand_launch_pr",
        "category_support",
        "product_launch",
        "authority_pr",
    }
)

_VALID_SCOPES = frozenset(
    {
        "site",
        "technical",
        "page",
        "product",
        "media",
        "links",
        "cannibalization",
        "gsc",
        "parity",
        "deep_crawl",
    }
)
_VALID_BATCH_SCOPES = frozenset({"all", "media_alt", "seo_meta"})
_VALID_MARKETS = frozenset({"no", "se", "dk"})
_VALID_FIX_TYPES = frozenset(
    {"seo_meta", "media_alt", "product_seo", "page_seo", "schema_jsonld"}
)
_VALID_TARGET_TYPES = frozenset({"product", "page", "post", "media", "category", "site"})
try:
    from ecom_ops.seo.models import VALID_CAMPAIGN_TYPES as _CANONICAL_CAMPAIGN_TYPES
except ImportError:
    _CANONICAL_CAMPAIGN_TYPES = (
        "commercial",
        "seasonal",
        "product_launch",
        "content_hub",
        "technical_sprint",
        "category",
        "informational",
        "technical",
    )
_VALID_CAMPAIGN_TYPES = frozenset(_CANONICAL_CAMPAIGN_TYPES)
_VALID_LINK_STATUSES = frozenset(
    {
        "draft",
        "ready_for_review",
        "approved_for_manual_order",
        "ordered_external",
        "delivered",
        "cancelled",
    }
)
_OSCAR_ONLY_LINK_STATUSES = frozenset(
    {"approved_for_manual_order", "ordered_external", "delivered", "cancelled"}
)
_VALID_EXPORT_FMTS = frozenset({"md", "json", "csv"})


def _seo_service():
    from ecom_ops.seo.service import SeoStudioService

    return SeoStudioService(data_dir=_data_dir())


def _can_read(actor_obj: Any) -> bool:
    return bool(
        actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.MARKETING_READ)
    )


def _can_suggest(actor_obj: Any) -> bool:
    return bool(actor_obj.has(Permission.MARKETING_SUGGEST))


def _can_propose_fix(actor_obj: Any) -> bool:
    return bool(
        actor_obj.has(Permission.MARKETING_SUGGEST)
        or actor_obj.has(Permission.WP_OPS_OPERATE)
        or actor_obj.has(Permission.WP_OPS_ADMIN)
    )


def _can_apply_fix(actor_obj: Any) -> bool:
    return bool(g.actor.get("is_oscar")) and bool(actor_obj.has(Permission.WP_OPS_ADMIN))


def _can_rollback_batch(actor_obj: Any) -> bool:
    return (
        bool(g.actor.get("is_oscar"))
        and bool(actor_obj.has(Permission.WP_OPS_ADMIN))
        and bool(actor_obj.has(Permission.WP_OPS_RESTORE))
    )


def _can_approve_links(actor_obj: Any) -> bool:
    return bool(g.actor.get("is_oscar")) and bool(actor_obj.has(Permission.MARKETING_MUTATE))


def _can_run_autopilot(actor_obj: Any) -> bool:
    return bool(
        actor_obj.has(Permission.WP_OPS_READ)
        and (
            actor_obj.has(Permission.MARKETING_SUGGEST)
            or actor_obj.has(Permission.MARKETING_MUTATE)
        )
    )


def _parse_bool_flag(raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_budget_and_links(
    src: Any,
    *,
    default_budget: float = 15000.0,
    default_links: int = 6,
) -> tuple[float | None, int | None, str | None]:
    raw_budget = str(src.get("budget_sek") or src.get("budget") or "").strip()
    try:
        budget_sek = float(raw_budget) if raw_budget else float(default_budget)
    except ValueError:
        return None, None, "Ogiltig budget (ange ett belopp mellan 1 000 och 500 000 SEK)."
    if not math.isfinite(budget_sek) or budget_sek < 1000.0 or budget_sek > 500000.0:
        return None, None, "Budget måste vara ett ändligt belopp mellan 1 000 och 500 000 SEK."

    raw_links = str(src.get("link_count") or src.get("links") or "").strip()
    try:
        link_count = int(raw_links) if raw_links else int(default_links)
    except ValueError:
        return None, None, "Antal länkar måste vara ett heltal mellan 1 och 50."
    if link_count < 1 or link_count > 50:
        return None, None, "Antal länkar måste vara mellan 1 och 50."
    return budget_sek, link_count, None


def _normalize_vendor_selection(raw: Any, pkg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize vendor vetting payload across fresh service responses and persisted link packages."""
    base: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    pkg_map = pkg if isinstance(pkg, dict) else {}
    persisted_summary = (
        pkg_map.get("vendor_vetting_summary")
        if isinstance(pkg_map.get("vendor_vetting_summary"), dict)
        else {}
    )
    persisted_candidates = (
        pkg_map.get("recommended_vendors")
        if isinstance(pkg_map.get("recommended_vendors"), list)
        else []
    )
    candidates = base.get("candidates") if isinstance(base.get("candidates"), list) else []
    if persisted_candidates and not candidates:
        candidates = persisted_candidates
    elif persisted_candidates and candidates:
        # Prefer persisted candidates if they carry vetted scores, otherwise keep fresh
        if all(isinstance(c, dict) and c.get("vendor_id") for c in persisted_candidates):
            candidates = persisted_candidates

    selected_vendor_id = str(
        pkg_map.get("selected_vendor_id")
        or base.get("selected_vendor_id")
        or persisted_summary.get("selected_vendor_id")
        or ""
    ).strip() or None

    recommended_tier = str(
        base.get("recommended_tier")
        or persisted_summary.get("recommended_tier")
        or ""
    ).strip()
    budget_warning = str(
        base.get("budget_warning")
        or persisted_summary.get("budget_warning")
        or ""
    ).strip()
    disclosure_checklist = (
        base.get("disclosure_checklist")
        if isinstance(base.get("disclosure_checklist"), list) and base.get("disclosure_checklist")
        else (
            persisted_summary.get("disclosure_checklist")
            if isinstance(persisted_summary.get("disclosure_checklist"), list)
            else []
        )
    )
    citations = (
        base.get("citations")
        if isinstance(base.get("citations"), list) and base.get("citations")
        else (
            persisted_summary.get("citations")
            if isinstance(persisted_summary.get("citations"), list)
            else []
        )
    )
    no_auto_purchase_notice = str(
        base.get("no_auto_purchase_notice")
        or persisted_summary.get("no_auto_purchase_notice")
        or "Strikt HITL: Azom köper aldrig länkar automatiskt. Oscar (MARKETING_MUTATE) godkänner underlaget för manuell beställning."
    ).strip()

    selected_vendor_obj: dict[str, Any] | None = None
    if selected_vendor_id:
        for cand in candidates:
            if isinstance(cand, dict) and str(cand.get("vendor_id") or "") == selected_vendor_id:
                selected_vendor_obj = cand
                break

    return {
        "ok": bool(base.get("ok", bool(candidates))),
        "blocked": bool(base.get("blocked") or persisted_summary.get("blocked")),
        "block_reason": str(base.get("block_reason") or persisted_summary.get("block_reason") or ""),
        "budget_warning": budget_warning,
        "recommended_tier": recommended_tier,
        "selected_vendor_id": selected_vendor_id,
        "selected_vendor": selected_vendor_obj,
        "candidates": [c for c in candidates if isinstance(c, dict)],
        "disclosure_checklist": [str(item) for item in disclosure_checklist if str(item).strip()],
        "no_auto_purchase_notice": no_auto_purchase_notice,
        "citations": [c for c in citations if isinstance(c, dict)],
    }


def _safe_seo_next(raw: str | None, fallback: str) -> str:
    text = str(raw or "").strip()
    if not text or any(ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in text):
        return fallback
    text = text.split("#", 1)[0].strip()
    if text.startswith("/seo") and not text.startswith("//"):
        return text
    return fallback


def _load_sites_list() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        from ecom_ops.wp_ops.registry import load_sites

        for site in load_sites(data_dir=_data_dir()):
            out.append(
                {
                    "id": str(site.id),
                    "display_name": str(getattr(site, "display_name", "") or site.id),
                    "environment": str(getattr(site, "environment", "") or "staging"),
                    "domain": str(getattr(site, "domain", "") or ""),
                }
            )
    except Exception:
        pass
    if not out:
        out.append(
            {
                "id": "mock-staging",
                "display_name": "Mock Staging",
                "environment": "staging",
                "domain": "staging.azom.example",
            }
        )
    return out


def _resolve_site_id(sites: list[dict[str, str]], requested: str | None) -> str:
    cand = str(requested or "").strip().lower()
    known = {s["id"] for s in sites}
    if cand and cand in known:
        return cand
    if cand and SITE_ID_RE.fullmatch(cand):
        return cand
    return sites[0]["id"] if sites else "mock-staging"


def _seo_posture() -> dict[str, Any]:
    mock_mode = _is_mock()
    write_enabled = env_flag("AZOM_SEO_WRITE_ENABLED", default=False)
    seo_kill = env_flag("AZOM_SEO_KILL", default=False)
    deploy_kill = env_flag("AZOM_WP_DEPLOY_KILL", default=False)
    return {
        "mock_mode": mock_mode,
        "seo_write_enabled": write_enabled,
        "seo_kill_active": seo_kill,
        "deploy_kill_active": deploy_kill,
        "kill_switch_active": seo_kill or deploy_kill,
        "live_writes_open": (not mock_mode) and write_enabled and (not seo_kill) and (not deploy_kill),
    }


def _split_csv_or_lines(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\r\n,]+", str(raw))
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _parse_link_targets(raw_text: str | None, raw_json: str | None) -> list[dict[str, Any]]:
    if raw_json and raw_json.strip():
        parsed = json.loads(raw_json)
        if not isinstance(parsed, list):
            raise ValueError("targets_json måste vara en JSON-lista.")
        return [dict(item) for item in parsed if isinstance(item, dict)]

    targets: list[dict[str, Any]] = []
    for line in str(raw_text or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        cols = [c.strip() for c in clean.split("|")]
        url = cols[0] if len(cols) >= 1 else ""
        keyword = cols[1] if len(cols) >= 2 else ""
        anchor_type = cols[2].lower() if len(cols) >= 3 and cols[2] else ""
        anchor_text = cols[3] if len(cols) >= 4 else ""
        if not url:
            continue
        entry: dict[str, Any] = {"target_url": url}
        if keyword:
            entry["keyword"] = keyword
            entry["primary_keyword"] = keyword
        if anchor_type:
            norm_at = {
                "partial": "partial_match",
                "exact": "exact_match",
                "url": "naked_url",
                "naked": "naked_url",
            }.get(anchor_type, anchor_type)
            entry["anchor_type"] = norm_at
        if anchor_text:
            entry["anchor_text"] = anchor_text
        targets.append(entry)
    return targets


def _load_recent_research_briefs(actor: str) -> list[dict[str, Any]]:
    try:
        from research_routes import _research_store

        store = _research_store()
        jobs = store.list_jobs(actor=actor, limit=20)
        out: list[dict[str, Any]] = []
        for job in jobs:
            spec = getattr(job, "spec", None)
            topic = getattr(spec, "topic", "") if spec is not None else ""
            out.append(
                {
                    "id": str(job.research_id),
                    "topic": str(topic or job.research_id),
                    "status": str(job.status),
                }
            )
        return out
    except Exception:
        return []


def _load_seo_research_jobs(actor: str, target_ref: str | None = None) -> list[dict[str, Any]]:
    try:
        from research_routes import load_linked_research_jobs

        if target_ref:
            return load_linked_research_jobs(actor=actor, target_ref=target_ref, limit=8)
        return load_linked_research_jobs(actor=actor, target_ref_prefix="seo:", limit=8)
    except Exception:
        return []


def _safe_budget_int(raw: Any) -> int:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(val):
        return 0
    return int(max(0.0, min(val, 10_000_000.0)))


def _decorate_timestamps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["created_at_fmt"] = _fmt_dt(item.get("created_at"))
        if item.get("updated_at"):
            item["updated_at_fmt"] = _fmt_dt(item.get("updated_at"))
        if item.get("expires_at"):
            item["expires_at_fmt"] = _fmt_dt(item.get("expires_at"))
        if "budget_sek" in item:
            item["budget_sek_int"] = _safe_budget_int(item.get("budget_sek"))
        out.append(item)
    return out


def _build_diff_rows(before: Any, proposed: Any) -> list[dict[str, str]]:
    """Build structured Before vs. Proposed diff rows for template rendering."""
    b_map = before if isinstance(before, dict) else {}
    p_map = proposed if isinstance(proposed, dict) else {}
    rows: list[dict[str, str]] = []
    for key, p_val in p_map.items():
        b_val = b_map.get(key)
        b_str = (
            json.dumps(b_val, ensure_ascii=False)
            if isinstance(b_val, (dict, list))
            else ("" if b_val is None else str(b_val))
        )
        p_str = (
            json.dumps(p_val, ensure_ascii=False)
            if isinstance(p_val, (dict, list))
            else ("" if p_val is None else str(p_val))
        )
        rows.append(
            {
                "field": str(key),
                "before": b_str.strip() or "—",
                "after": p_str.strip() or "—",
            }
        )
    return rows


def _decorate_fix_plans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = _decorate_timestamps(rows)
    for item in out:
        item["diff_rows"] = _build_diff_rows(item.get("before"), item.get("proposed"))
    return out


def _decorate_fix_batches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = _decorate_timestamps(rows)
    for batch in out:
        raw_items = batch.get("items") if isinstance(batch.get("items"), list) else []
        dec_items: list[dict[str, Any]] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            copy = dict(it)
            copy["diff_rows"] = _build_diff_rows(
                copy.get("before") or copy.get("before_data"),
                copy.get("proposed") or copy.get("proposed_data"),
            )
            dec_items.append(copy)
        batch["items"] = dec_items
        batch["item_count"] = int(batch.get("item_count") or len(dec_items))
    return out


def _seo_panel_cache_path():
    return _data_dir() / "seo_panel_cache.json"


def _load_seo_panel_cache(site_id: str, key: str) -> dict[str, Any]:
    path = _seo_panel_cache_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        site_map = raw.get(site_id) if isinstance(raw, dict) else None
        entry = site_map.get(key) if isinstance(site_map, dict) else None
        if not isinstance(entry, dict):
            return {}
        if bool(entry.get("mock_mode")) != bool(_is_mock()):
            return {}
        payload = entry.get("payload")
        return dict(payload) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_seo_panel_cache(site_id: str, key: str, payload: dict[str, Any]) -> None:
    path = _seo_panel_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        site_map = existing.get(site_id)
        if not isinstance(site_map, dict):
            site_map = {}
        site_map[key] = {"mock_mode": bool(_is_mock()), "payload": payload}
        existing[site_id] = site_map
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _load_recent_seo_changes(site_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    try:
        from ecom_ops.wp_ops.change_ledger import ChangeLedger

        rows = ChangeLedger(data_dir=_data_dir()).list_changes(site_id, limit=40)
        seo_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("category") or "") in {"seo_meta", "media_alt"}:
                copy = dict(row)
                copy["created_at_fmt"] = _fmt_dt(copy.get("created_at"))
                seo_rows.append(copy)
            if len(seo_rows) >= limit:
                break
        return seo_rows
    except Exception:
        return []


def _format_targets_raw_prefill(targets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        url = str(t.get("target_url") or t.get("url") or "").strip()
        kw = str(t.get("primary_keyword") or t.get("keyword") or "").strip()
        at = str(t.get("anchor_type") or "brand").strip()
        txt = str(t.get("anchor_text") or "").strip()
        if url:
            lines.append(f"{url} | {kw} | {at} | {txt}")
    return "\n".join(lines)


def _safe_service_call(svc: Any, method_name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    fn = getattr(svc, method_name, None)
    if not callable(fn):
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _load_drift_digest(site_id: str, actor: str, *, days: int = 7) -> dict[str, Any]:
    try:
        from ecom_ops.seo.digest import build_seo_plugin_drift_digest

        res = build_seo_plugin_drift_digest(
            site_id, actor=actor, days=days, data_dir=_data_dir()
        )
        return dict(res) if isinstance(res, dict) else {}
    except Exception:
        return {}


def _build_gsc_note(audits: list[dict[str, Any]], gsc_data: dict[str, Any]) -> str:
    gsc_status = gsc_data.get("gsc_status") if isinstance(gsc_data.get("gsc_status"), dict) else {}
    mode = str(gsc_status.get("mode") or ("mock" if _is_mock() else "")).lower()
    if gsc_status.get("connected") or mode == "live":
        return (
            "Google Search Console (GSC) är anslutet i skrivskyddat läge (webmasters.readonly). "
            "Strike-distance quick-wins och låg-CTR-signaler kombineras med WordPress/WooCommerce, Bridge, GA4 och Ads."
        )
    if mode == "mock" or gsc_data.get("ok"):
        return (
            "Google Search Console (GSC) körs i mock-läge (AZOM_USE_MOCK=1). "
            "Signaler för strike-distance quick-wins bygger på offline-fixtures + WordPress/WooCommerce + Azom Ops Bridge + GA4 + Google Ads."
        )
    if audits and audits[0].get("gsc_note"):
        return str(audits[0].get("gsc_note"))
    return (
        "Google Search Console (GSC) är inte anslutet ännu. "
        "Signaler och rekommendationer bygger på WordPress/WooCommerce + Azom Ops Bridge + GA4 + Google Ads + Web Research."
    )


def _seo_common_ctx(
    *,
    active_section: str,
    sites: list[dict[str, str]],
    selected_site: str,
    actor_obj: Any,
    **extra: Any,
) -> dict[str, Any]:
    known_site_ids = {s["id"] for s in sites}
    return _dashboard_context(
        active_seo_section=active_section,
        sites=sites,
        selected_site=selected_site,
        selected_site_registered=selected_site in known_site_ids,
        posture=_seo_posture(),
        can_suggest=_can_suggest(actor_obj),
        can_propose_fix=_can_propose_fix(actor_obj),
        can_apply_fix=_can_apply_fix(actor_obj),
        can_rollback_batch=_can_rollback_batch(actor_obj),
        can_approve_links=_can_approve_links(actor_obj),
        can_run_autopilot=_can_run_autopilot(actor_obj),
        **extra,
    )


def _compute_hub_kpis(
    audits: list[dict[str, Any]],
    fix_plans: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    link_packages: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = audits[0] if audits else None
    latest_score: int | None = None
    latest_grade = ""
    critical_findings = 0
    missing_alt = 0

    if isinstance(latest, dict):
        raw_score = latest.get("score")
        if isinstance(raw_score, (int, float)):
            latest_score = int(raw_score)
        latest_grade = str(latest.get("grade") or "")
        summary = latest.get("summary") if isinstance(latest.get("summary"), dict) else {}
        issues = latest.get("issues") if isinstance(latest.get("issues"), list) else []
        if "critical" in summary or "high" in summary:
            critical_findings = int(summary.get("critical") or 0) + int(summary.get("high") or 0)
        else:
            critical_findings = sum(
                1
                for iss in issues
                if isinstance(iss, dict)
                and str(iss.get("severity") or "").lower() in {"critical", "high", "error"}
            )
        media = latest.get("media") if isinstance(latest.get("media"), dict) else {}
        if "missing_alt_count" in media:
            missing_alt = int(media.get("missing_alt_count") or 0)
        elif "missing_alt" in summary:
            missing_alt = int(summary.get("missing_alt") or 0)
        elif isinstance(media.get("missing_alt"), list):
            missing_alt = len(media["missing_alt"])
        elif isinstance(media.get("items"), list):
            missing_alt = sum(
                1
                for m in media["items"]
                if isinstance(m, dict) and not str(m.get("alt_text") or "").strip()
            )

    pending_fixes = sum(
        1
        for fp in fix_plans
        if isinstance(fp, dict) and str(fp.get("status") or "pending").lower() == "pending"
    )
    ready_links = sum(
        1
        for lp in link_packages
        if isinstance(lp, dict) and str(lp.get("status") or "").lower() == "ready_for_review"
    )
    approved_links = sum(
        1
        for lp in link_packages
        if isinstance(lp, dict)
        and str(lp.get("status") or "").lower()
        in {"approved_for_manual_order", "ordered_external", "delivered"}
    )
    return {
        "latest_audit_id": str(latest.get("audit_id") or "") if isinstance(latest, dict) else "",
        "latest_score": latest_score,
        "latest_grade": latest_grade,
        "critical_findings": critical_findings,
        "missing_alt": missing_alt,
        "strategies_count": len(strategies),
        "campaigns_count": len(campaigns),
        "link_packages_count": len(link_packages),
        "ready_links_count": ready_links,
        "approved_links_count": approved_links,
        "pending_fixes_count": pending_fixes,
    }


def _normalize_anchor_mix(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    raw_mix = pkg.get("anchor_mix") if isinstance(pkg.get("anchor_mix"), dict) else {}
    buckets = (
        ("brand", "Varumärke (Brand)", "az-badge-ok"),
        ("naked_url", "Naken URL", "az-badge-ok"),
        ("generic", "Generisk (t.ex. läs mer)", "az-badge-info"),
        ("partial", "Partial-match", "az-badge-warn"),
        ("exact", "Exact-match", "az-badge-danger"),
    )
    pct_map = raw_mix.get("percentages") if isinstance(raw_mix.get("percentages"), dict) else raw_mix
    cnt_map = raw_mix.get("counts") if isinstance(raw_mix.get("counts"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key, label, badge_cls in buckets:
        alt_key = f"{key}_pct"
        raw_pct = pct_map.get(key)
        if raw_pct is None:
            raw_pct = pct_map.get(alt_key)
        if raw_pct is None and key == "partial":
            raw_pct = pct_map.get("partial_match") or pct_map.get("partial_match_pct")
        if raw_pct is None and key == "exact":
            raw_pct = pct_map.get("exact_match") or pct_map.get("exact_match_pct")
        try:
            pct_val = float(raw_pct or 0.0)
        except (TypeError, ValueError):
            pct_val = 0.0
        if 0 < pct_val <= 1.0 and raw_mix.get("as_ratios"):
            pct_val *= 100.0
        pct_val = max(0.0, min(100.0, round(pct_val, 1)))
        cnt = cnt_map.get(key)
        if cnt is None and key == "partial":
            cnt = cnt_map.get("partial_match")
        if cnt is None and key == "exact":
            cnt = cnt_map.get("exact_match")
        rows.append(
            {
                "key": key,
                "label": label,
                "pct": pct_val,
                "count": cnt,
                "badge_cls": badge_cls,
            }
        )
    return rows


def _render_seo_hub(
    *,
    actor_obj: Any,
    active_section: str = "hub",
    digest_focus: bool = False,
):
    sites = _load_sites_list()
    requested_site = (request.args.get("site") or "").strip().lower()
    selected_site = _resolve_site_id(sites, requested_site)
    site_filter = requested_site if requested_site and requested_site in {s["id"] for s in sites} else None

    svc = _seo_service()
    actor_name = g.actor["name"]
    audits = _decorate_timestamps(svc.list_audits(site_id=site_filter, limit=20))
    fix_plans = _decorate_fix_plans(svc.list_fix_plans(site_id=site_filter, limit=30))
    raw_batches = _safe_service_call(
        svc, "list_fix_batches", site_id=site_filter, limit=20, default=[]
    )
    fix_batches = _decorate_fix_batches(raw_batches if isinstance(raw_batches, list) else [])
    strategies = _decorate_timestamps(svc.list_strategies(site_id=site_filter, limit=20))
    campaigns = _decorate_timestamps(svc.list_campaigns(site_id=site_filter, limit=20))
    link_packages = _decorate_timestamps(svc.list_link_packages(site_id=site_filter, limit=20))
    kpis = _compute_hub_kpis(audits, fix_plans, strategies, campaigns, link_packages)
    kpis["pending_batches_count"] = sum(
        1
        for fb in fix_batches
        if isinstance(fb, dict) and str(fb.get("status") or "pending").lower() == "pending"
    )
    research_jobs = _load_seo_research_jobs(actor_name)

    gsc_data = _safe_service_call(
        svc, "get_gsc_quick_wins", selected_site, days=28, actor=actor_name, default={}
    )
    if not isinstance(gsc_data, dict):
        gsc_data = {}

    audit_diff = _safe_service_call(
        svc, "diff_audits", selected_site, actor=actor_name, default={}
    )
    if not isinstance(audit_diff, dict):
        audit_diff = {}

    parity_data = _load_seo_panel_cache(selected_site, "parity")
    if not parity_data and _is_mock():
        p_res = _safe_service_call(
            svc, "run_nordic_parity", selected_site, actor=actor_name, default={}
        )
        if isinstance(p_res, dict) and p_res.get("ok"):
            parity_data = p_res

    deep_crawl_data = _load_seo_panel_cache(selected_site, "deep_crawl")

    raw_days = request.args.get("days")
    try:
        digest_days = max(1, min(int(raw_days), 90)) if raw_days else 7
    except ValueError:
        digest_days = 7
    is_registered_site = selected_site in {s["id"] for s in sites}
    digest_data = (
        _load_drift_digest(selected_site, actor_name, days=digest_days)
        if is_registered_site
        else {}
    )
    recent_seo_changes = (
        _load_recent_seo_changes(selected_site, limit=8) if is_registered_site else []
    )
    gsc_note = _build_gsc_note(audits, gsc_data)

    return render_template(
        "seo/hub.html",
        **_seo_common_ctx(
            active_section=active_section,
            sites=sites,
            selected_site=selected_site,
            actor_obj=actor_obj,
            audits=audits,
            fix_plans=fix_plans,
            fix_batches=fix_batches,
            strategies=strategies,
            campaigns=campaigns,
            link_packages=link_packages,
            kpis=kpis,
            gsc_note=gsc_note,
            gsc_data=gsc_data,
            audit_diff=audit_diff,
            parity_data=parity_data,
            deep_crawl_data=deep_crawl_data,
            digest_data=digest_data,
            digest_focus=digest_focus,
            recent_seo_changes=recent_seo_changes,
            seo_research_jobs=research_jobs,
        ),
    )


@seo_bp.route("/seo", methods=["GET"])
@_auth_required
def seo_hub():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    return _render_seo_hub(actor_obj=actor_obj, active_section="hub", digest_focus=False)


@seo_bp.route("/seo/digest", methods=["GET"])
@_auth_required
def seo_digest():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    fmt = (request.args.get("format") or "").strip().lower()
    if fmt == "json" or (
        request.accept_mimetypes.best == "application/json"
        and "text/html" not in str(request.headers.get("Accept") or "")
    ):
        sites = _load_sites_list()
        selected_site = _resolve_site_id(sites, request.args.get("site"))
        raw_days = request.args.get("days")
        try:
            days = max(1, min(int(raw_days), 90)) if raw_days else 7
        except ValueError:
            days = 7
        payload = _load_drift_digest(selected_site, g.actor["name"], days=days)
        if not payload:
            payload = {
                "ok": False,
                "site_id": selected_site,
                "message": "Drift-digest är inte tillgänglig ännu.",
            }
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return _render_seo_hub(actor_obj=actor_obj, active_section="digest", digest_focus=True)


@seo_bp.route("/seo/parity", methods=["GET", "POST"])
@_auth_required
def seo_run_parity():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    if request.method == "GET":
        return _render_seo_hub(actor_obj=actor_obj, active_section="hub", digest_focus=False)

    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    fallback = url_for("seo.seo_hub", site=site_id)
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    svc = _seo_service()
    fn = getattr(svc, "run_nordic_parity", None)
    if not callable(fn):
        _flash("Nordisk SE/NO/DK-paritetskontroll är inte tillgänglig.", "error")
        return redirect(next_url)
    try:
        res = fn(site_id=site_id, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Paritetskontroll misslyckades: {exc}", "error")
        return redirect(next_url)
    if not isinstance(res, dict) or not res.get("ok"):
        if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(str((res or {}).get("message") or "Paritetskontroll misslyckades."), "error")
        return redirect(next_url)
    _save_seo_panel_cache(site_id, "parity", res)
    issue_n = len(res.get("issues") or [])
    markets = ", ".join(str(m).upper() for m in (res.get("markets") or [])) or "SE/NO/DK"
    _flash(f"Nordisk paritetskontroll ({markets}) klar — {issue_n} avvikelser identifierade.", "ok")
    return redirect(next_url)


@seo_bp.route("/seo/deep-crawl", methods=["POST"])
@_auth_required
def seo_run_deep_crawl():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    fallback = url_for("seo.seo_hub", site=site_id)
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    try:
        max_pages = max(1, min(int(request.form.get("max_pages") or 10), 25))
    except ValueError:
        max_pages = 10
    svc = _seo_service()
    fn = getattr(svc, "run_deep_crawl", None)
    if not callable(fn):
        _flash("Djup flersidig crawl är inte tillgänglig.", "error")
        return redirect(next_url)
    try:
        res = fn(site_id=site_id, max_pages=max_pages, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Djup crawl misslyckades: {exc}", "error")
        return redirect(next_url)
    if not isinstance(res, dict) or not res.get("ok"):
        if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(str((res or {}).get("message") or "Djup crawl misslyckades."), "error")
        return redirect(next_url)
    _save_seo_panel_cache(site_id, "deep_crawl", res)
    crawled_n = len(res.get("crawled") or [])
    issues_n = len(res.get("issues") or [])
    _flash(f"Djup crawl klar — {crawled_n} sidor kontrollerade, {issues_n} tekniska fynd.", "ok")
    return redirect(next_url)


@seo_bp.route("/seo/audit", methods=["POST"])
@_auth_required
def seo_run_audit():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    scope = (request.form.get("scope") or "site").strip().lower()
    if scope not in _VALID_SCOPES:
        scope = "site"
    market = (request.form.get("market") or "no").strip().lower()
    if market not in _VALID_MARKETS:
        market = "no"
    path = (request.form.get("path") or "/").strip() or "/"
    target_id = (request.form.get("target_id") or "").strip() or None

    try:
        res = _seo_service().run_audit(
            site_id=site_id,
            actor=g.actor["name"],
            scope=scope,
            target_id=target_id,
            path=path,
            market=market,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"SEO-audit misslyckades: {exc}", "error")
        return redirect(url_for("seo.seo_hub"))

    if not res.get("ok"):
        if res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(str(res.get("message") or "SEO-audit misslyckades."), "error")
        return redirect(url_for("seo.seo_hub"))

    audit_id = str(res.get("audit_id") or "")
    score = res.get("score")
    grade = res.get("grade") or ""
    _flash(f"SEO-audit {audit_id} klar — poäng {score}/100 ({grade}).", "ok")
    if SEO_AUDIT_ID_RE.fullmatch(audit_id):
        return redirect(url_for("seo.seo_audit_detail", audit_id=audit_id))
    return redirect(url_for("seo.seo_hub"))


@seo_bp.route("/seo/audit/<audit_id>", methods=["GET"])
@_auth_required
def seo_audit_detail(audit_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_AUDIT_ID_RE.fullmatch(str(audit_id or "")):
        return Response("Okänd SEO-audit", 404)

    svc = _seo_service()
    audit = svc.get_audit(audit_id)
    if not audit:
        return Response("SEO-audit hittades inte", 404)

    audit_view = dict(audit)
    audit_view["created_at_fmt"] = _fmt_dt(audit_view.get("created_at"))
    sites = _load_sites_list()
    site_id = str(audit_view.get("site_id") or _resolve_site_id(sites, None))
    site_meta = next((s for s in sites if s["id"] == site_id), {})
    fix_plans = _decorate_fix_plans(svc.list_fix_plans(site_id=site_id, limit=20))
    raw_batches = _safe_service_call(
        svc, "list_fix_batches", site_id=site_id, limit=10, default=[]
    )
    fix_batches = _decorate_fix_batches(raw_batches if isinstance(raw_batches, list) else [])
    audit_diff = _safe_service_call(
        svc,
        "diff_audits",
        site_id,
        latest_audit_id=audit_id,
        actor=g.actor["name"],
        default={},
    )
    if not isinstance(audit_diff, dict):
        audit_diff = {}
    research_jobs = _load_seo_research_jobs(g.actor["name"], target_ref=f"seo:audit:{audit_id}")

    return render_template(
        "seo/audit_detail.html",
        **_seo_common_ctx(
            active_section="hub",
            sites=sites,
            selected_site=site_id,
            actor_obj=actor_obj,
            audit=audit_view,
            site_domain=str(site_meta.get("domain") or f"{site_id}.example"),
            audit_diff=audit_diff,
            fix_plans=fix_plans,
            fix_batches=fix_batches,
            seo_research_jobs=research_jobs,
        ),
    )


@seo_bp.route("/seo/batches/from-audit", methods=["POST"])
@_auth_required
def seo_batch_from_audit():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_propose_fix(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    audit_id = (request.form.get("audit_id") or "").strip()
    fallback = (
        url_for("seo.seo_audit_detail", audit_id=audit_id)
        if SEO_AUDIT_ID_RE.fullmatch(audit_id)
        else url_for("seo.seo_hub", site=site_id)
    )
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    if not SEO_AUDIT_ID_RE.fullmatch(audit_id):
        _flash("Ogiltigt audit-ID för batch-plan.", "error")
        return redirect(next_url)

    scope = (request.form.get("scope") or "all").strip().lower()
    if scope not in _VALID_BATCH_SCOPES:
        scope = "all"
    try:
        limit = max(1, min(int(request.form.get("limit") or 20), 50))
    except ValueError:
        limit = 20

    svc = _seo_service()
    fn = getattr(svc, "propose_batch_from_audit", None)
    if not callable(fn):
        _flash("Batch-fixplaner är inte tillgängliga i denna version.", "error")
        return redirect(next_url)

    try:
        res = fn(
            site_id=site_id,
            audit_id=audit_id,
            scope=scope,
            limit=limit,
            actor=g.actor["name"],
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Kunde inte skapa batch-fixplan: {exc}", "error")
        return redirect(next_url)

    if not isinstance(res, dict) or not res.get("ok"):
        if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(str((res or {}).get("message") or "Kunde inte skapa batch-fixplan."), "error")
        return redirect(next_url)

    batch = res.get("batch") or {}
    batch_id = str(batch.get("batch_id") or "")
    item_count = len(batch.get("items") or [])
    _flash(
        f"Batch-fixplan {batch_id} skapad ({item_count} objekt, scope={scope}) — väntar på Oscar-godkännande.",
        "ok",
    )
    return redirect(next_url)


@seo_bp.route("/seo/batches/<batch_id>/apply", methods=["POST"])
@_auth_required
def seo_batch_apply(batch_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_apply_fix(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_BATCH_ID_RE.fullmatch(str(batch_id or "")):
        return Response("Okänd batch-plan", 404)

    fallback = url_for("seo.seo_hub")
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    svc = _seo_service()
    get_fn = getattr(svc, "get_fix_batch", None)
    apply_fn = getattr(svc, "apply_batch", None)
    if not callable(get_fn) or not callable(apply_fn):
        _flash("Batch-applicering är inte tillgänglig.", "error")
        return redirect(next_url)

    batch = get_fn(batch_id)
    if not batch:
        return Response("Batch-plan hittades inte", 404)

    site_id = (request.form.get("site_id") or str(batch.get("site_id") or "")).strip()
    expected_sha256 = (request.form.get("expected_sha256") or "").strip()

    try:
        res = apply_fn(
            site_id=site_id,
            batch_id=batch_id,
            expected_sha256=expected_sha256,
            actor=g.actor["name"],
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Batch-applicering misslyckades: {exc}", "error")
        return redirect(next_url)

    if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)

    if isinstance(res, dict) and res.get("ok"):
        applied_n = int(res.get("applied_count") or 0)
        failed_n = int(res.get("failed_count") or 0)
        _flash(
            f"Batch-plan {batch_id} applicerad ({applied_n} lyckades, {failed_n} misslyckades). Change Vault batch_id={batch_id}.",
            "ok",
        )
    else:
        _flash(
            str((res or {}).get("message") or "Batch-plan kunde inte appliceras."),
            "error",
        )
    return redirect(next_url)


@seo_bp.route("/seo/batches/<batch_id>/rollback", methods=["POST"])
@_auth_required
def seo_batch_rollback(batch_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_rollback_batch(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_BATCH_ID_RE.fullmatch(str(batch_id or "")):
        return Response("Okänd batch-plan", 404)

    fallback = url_for("seo.seo_hub")
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    svc = _seo_service()
    batch = _safe_service_call(svc, "get_fix_batch", batch_id, default=None)
    site_id = (
        request.form.get("site_id")
        or (str(batch.get("site_id") or "") if isinstance(batch, dict) else "")
    ).strip()
    if not site_id or not SITE_ID_RE.fullmatch(site_id):
        return Response("Okänd webbplats för batch", 404)

    confirm_batch_id = (request.form.get("confirm_batch_id") or batch_id).strip()
    from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

    rsvc = ChangeRollbackService(data_dir=_data_dir())
    fn = getattr(rsvc, "execute_batch_rollback", None)
    if not callable(fn):
        _flash("Batch-återställning är inte tillgänglig i denna version.", "error")
        return redirect(next_url)

    try:
        res = fn(
            site_id,
            batch_id,
            actor=g.actor["name"],
            confirm_batch_id=confirm_batch_id,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Batch-återställning misslyckades: {exc}", "error")
        return redirect(next_url)

    if isinstance(res, dict):
        if res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        if res.get("ok"):
            rolled = res.get("rolled_back_count")
            suffix = f" ({rolled} ändringar)" if rolled is not None else ""
            _flash(f"Batch {batch_id} återställd via Change Vault{suffix}.", "ok")
        else:
            _flash(str(res.get("message") or "Batch-återställning misslyckades."), "error")
    else:
        data = getattr(res, "data", None) or {}
        if data.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(
            str(getattr(res, "message", "") or f"Batch {batch_id} återställd."),
            "ok" if getattr(res, "ok", False) else "error",
        )
    return redirect(next_url)


@seo_bp.route("/seo/fixes/propose", methods=["POST"])
@_auth_required
def seo_fix_propose():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_propose_fix(actor_obj):
        return Response("Saknar behörighet", 403)

    fallback = url_for("seo.seo_hub")
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    fix_type = (request.form.get("fix_type") or "seo_meta").strip().lower()
    if fix_type not in _VALID_FIX_TYPES:
        _flash("Ogiltig fix_type.", "error")
        return redirect(next_url)

    target_type = (request.form.get("target_type") or "product").strip().lower()
    if target_type not in _VALID_TARGET_TYPES:
        _flash("Ogiltig target_type.", "error")
        return redirect(next_url)

    target_id = (request.form.get("target_id") or "").strip()
    if not target_id:
        _flash("Ange target_id (t.ex. produkt-, sid- eller media-ID).", "error")
        return redirect(next_url)

    market = (request.form.get("market") or "no").strip().lower()
    if market not in _VALID_MARKETS:
        market = "no"
    summary = (request.form.get("summary") or "").strip()

    proposed: dict[str, Any] = {}
    raw_json = (request.form.get("proposed_json") or "").strip()
    if raw_json:
        try:
            loaded = json.loads(raw_json)
            if not isinstance(loaded, dict):
                raise ValueError("JSON måste vara ett objekt.")
            proposed.update(loaded)
        except Exception as exc:
            _flash(f"Ogiltig JSON i föreslagna fält: {exc}", "error")
            return redirect(next_url)
    else:
        for field_name in (
            "meta_title",
            "meta_description",
            "focus_keyphrase",
            "canonical",
            "robots",
            "slug",
            "alt_text",
            "title",
            "caption",
            "description",
            "schema_type",
        ):
            val = (request.form.get(field_name) or "").strip()
            if val:
                proposed[field_name] = val

    if not proposed:
        _flash("Ange minst ett föreslaget SEO-fält för fixplanen.", "error")
        return redirect(next_url)

    try:
        res = _seo_service().propose_fix(
            site_id=site_id,
            actor=g.actor["name"],
            fix_type=fix_type,
            target_type=target_type,
            target_id=target_id,
            proposed=proposed,
            summary=summary,
            market=market,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Kunde inte skapa fixplan: {exc}", "error")
        return redirect(next_url)

    if not res.get("ok"):
        if res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(str(res.get("message") or "Kunde inte skapa fixplan."), "error")
        return redirect(next_url)

    plan = res.get("plan") or {}
    plan_id = str(plan.get("plan_id") or "")
    _flash(
        f"Granskningsbar SEO-fixplan {plan_id} skapad (väntar på Oscar-godkännande).",
        "ok",
    )
    return redirect(next_url)


@seo_bp.route("/seo/fixes/<plan_id>/apply", methods=["POST"])
@_auth_required
def seo_fix_apply(plan_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_apply_fix(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_PLAN_ID_RE.fullmatch(str(plan_id or "")):
        return Response("Okänd fixplan", 404)

    fallback = url_for("seo.seo_hub")
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)
    svc = _seo_service()
    plan = svc.get_fix_plan(plan_id)
    if not plan:
        return Response("Fixplan hittades inte", 404)

    site_id = (request.form.get("site_id") or str(plan.get("site_id") or "")).strip()
    expected_sha256 = (request.form.get("expected_sha256") or "").strip()

    try:
        res = svc.apply_fix(
            site_id=site_id,
            plan_id=plan_id,
            actor=g.actor["name"],
            expected_sha256=expected_sha256,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Applicering misslyckades: {exc}", "error")
        return redirect(next_url)

    if res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)

    if res.get("ok"):
        chg = str(res.get("change_id") or "")
        detail = f" (Change Vault: {chg})" if chg else ""
        _flash(f"SEO-fixplan {plan_id} applicerad{detail}.", "ok")
    else:
        _flash(str(res.get("message") or "SEO-fixplan kunde inte appliceras."), "error")
    return redirect(next_url)


@seo_bp.route("/seo/strategies", methods=["GET", "POST"])
@_auth_required
def seo_strategies():
    actor_obj = _dashboard_actor(g.actor)
    if request.method == "POST":
        if not _can_suggest(actor_obj):
            return Response("Saknar behörighet", 403)
        sites = _load_sites_list()
        site_id = _resolve_site_id(sites, request.form.get("site_id"))
        title = (request.form.get("title") or "").strip()
        if not title:
            _flash("Ange en titel för SEO-strategin.", "error")
            return redirect(url_for("seo.seo_strategies"))
        market = (request.form.get("market") or "no").strip().lower()
        if market not in _VALID_MARKETS:
            market = "no"
        focus_topics = _split_csv_or_lines(request.form.get("focus_topics"))
        goals = (request.form.get("goals") or "").strip()
        research_brief_id = (request.form.get("research_brief_id") or "").strip() or None
        try:
            res = _seo_service().create_strategy(
                site_id=site_id,
                actor=g.actor["name"],
                title=title,
                market=market,
                focus_topics=focus_topics or None,
                goals=goals,
                research_brief_id=research_brief_id,
            )
        except AccessDenied:
            return Response("Saknar behörighet", 403)
        except Exception as exc:
            _flash(f"Kunde inte skapa strategi: {exc}", "error")
            return redirect(url_for("seo.seo_strategies"))

        if not res.get("ok"):
            _flash(str(res.get("message") or "Kunde inte skapa strategi."), "error")
        else:
            sid = str((res.get("strategy") or {}).get("strategy_id") or "")
            _flash(f"SEO-strategi {sid} skapad.", "ok")
        return redirect(url_for("seo.seo_strategies"))

    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    selected_site = _resolve_site_id(sites, request.args.get("site"))
    svc = _seo_service()
    strategies = _decorate_timestamps(svc.list_strategies(limit=30))
    campaigns = _decorate_timestamps(svc.list_campaigns(limit=30))
    briefs = _load_recent_research_briefs(g.actor["name"])
    return render_template(
        "seo/strategy_campaign.html",
        **_seo_common_ctx(
            active_section="strategies",
            sites=sites,
            selected_site=selected_site,
            actor_obj=actor_obj,
            active_tab="strategies",
            strategies=strategies,
            campaigns=campaigns,
            research_briefs=briefs,
        ),
    )


@seo_bp.route("/seo/campaigns", methods=["GET", "POST"])
@_auth_required
def seo_campaigns():
    actor_obj = _dashboard_actor(g.actor)
    if request.method == "POST":
        if not _can_suggest(actor_obj):
            return Response("Saknar behörighet", 403)
        sites = _load_sites_list()
        site_id = _resolve_site_id(sites, request.form.get("site_id"))
        name = (request.form.get("name") or "").strip()
        if not name:
            _flash("Ange ett namn för SEO-kampanjen.", "error")
            return redirect(url_for("seo.seo_campaigns"))
        market = (request.form.get("market") or "no").strip().lower()
        if market not in _VALID_MARKETS:
            market = "no"
        strategy_id = (request.form.get("strategy_id") or "").strip() or None
        campaign_type = (request.form.get("campaign_type") or "commercial").strip().lower()
        if campaign_type not in _VALID_CAMPAIGN_TYPES:
            campaign_type = "commercial"
        target_keywords = _split_csv_or_lines(request.form.get("target_keywords"))
        target_urls = _split_csv_or_lines(request.form.get("target_urls"))
        objective = (request.form.get("objective") or "").strip()
        research_brief_id = (request.form.get("research_brief_id") or "").strip() or None

        try:
            res = _seo_service().create_campaign(
                site_id=site_id,
                actor=g.actor["name"],
                name=name,
                market=market,
                strategy_id=strategy_id,
                target_keywords=target_keywords or None,
                target_urls=target_urls or None,
                campaign_type=campaign_type,
                objective=objective,
                research_brief_id=research_brief_id,
            )
        except AccessDenied:
            return Response("Saknar behörighet", 403)
        except Exception as exc:
            _flash(f"Kunde inte skapa kampanj: {exc}", "error")
            return redirect(url_for("seo.seo_campaigns"))

        if not res.get("ok"):
            _flash(str(res.get("message") or "Kunde inte skapa kampanj."), "error")
        else:
            cid = str((res.get("campaign") or {}).get("campaign_id") or "")
            _flash(f"SEO-kampanj {cid} skapad.", "ok")
        return redirect(url_for("seo.seo_campaigns"))

    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    selected_site = _resolve_site_id(sites, request.args.get("site"))
    svc = _seo_service()
    strategies = _decorate_timestamps(svc.list_strategies(limit=30))
    campaigns = _decorate_timestamps(svc.list_campaigns(limit=30))
    briefs = _load_recent_research_briefs(g.actor["name"])
    return render_template(
        "seo/strategy_campaign.html",
        **_seo_common_ctx(
            active_section="campaigns",
            sites=sites,
            selected_site=selected_site,
            actor_obj=actor_obj,
            active_tab="campaigns",
            strategies=strategies,
            campaigns=campaigns,
            research_briefs=briefs,
        ),
    )


@seo_bp.route("/seo/autopilot", methods=["POST"])
@_auth_required
def seo_run_autopilot():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_run_autopilot(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    fallback = url_for("seo.seo_hub", site=site_id)
    next_url = _safe_seo_next(request.form.get("next_url"), fallback)

    topic = (request.form.get("topic") or "").strip()[:240]
    market = (request.form.get("market") or "no").strip().lower()
    if market not in _VALID_MARKETS:
        market = "no"

    budget_sek, link_count, err_msg = _parse_budget_and_links(request.form)
    if err_msg or budget_sek is None or link_count is None:
        _flash(err_msg or "Ogiltig budget eller antal länkar.", "error")
        return redirect(next_url)

    if "include_web_research" in request.form:
        include_web_research = _parse_bool_flag(
            request.form.get("include_web_research"), default=True
        )
    elif request.form.get("include_web_research_present"):
        include_web_research = False
    else:
        include_web_research = True

    include_batch_fixes = _parse_bool_flag(
        request.form.get("include_batch_fixes"), default=False
    )
    requested_vendor_id = (
        request.form.get("selected_vendor_id") or request.form.get("vendor_id") or ""
    ).strip().lower()
    if requested_vendor_id and not VENDOR_ID_RE.fullmatch(requested_vendor_id):
        _flash("Ogiltigt leverantörs-ID för SEO-butik.", "error")
        return redirect(next_url)

    svc = _seo_service()
    fn = getattr(svc, "run_seo_autopilot", None)
    if not callable(fn):
        _flash("Autonom SEO-pipeline är inte tillgänglig i denna version.", "error")
        return redirect(next_url)

    try:
        res = fn(
            site_id,
            topic=topic,
            market=market,
            budget_sek=budget_sek,
            link_count=link_count,
            include_web_research=include_web_research,
            include_vendor_vetting=True,
            include_batch_fixes=include_batch_fixes,
            actor=g.actor["name"],
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Autonom SEO-pipeline misslyckades: {exc}", "error")
        return redirect(next_url)

    if not isinstance(res, dict) or not res.get("ok"):
        if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
            return Response("Saknar behörighet", 403)
        _flash(
            str((res or {}).get("message") or "Autonom SEO-pipeline misslyckades."),
            "error",
        )
        return redirect(next_url)

    audit_id = str((res.get("audit") or {}).get("audit_id") or "")
    strategy_id = str((res.get("strategy") or {}).get("strategy_id") or "")
    campaign_id = str((res.get("campaign") or {}).get("campaign_id") or "")
    pkg = res.get("link_package") or {}
    pkg_id = str(pkg.get("package_id") or "")
    vsel = res.get("vendor_selection") or {}
    selected_vendor = str(
        pkg.get("selected_vendor_id") or vsel.get("selected_vendor_id") or ""
    ).strip()

    if requested_vendor_id and SEO_LINK_ID_RE.fullmatch(pkg_id):
        sel_fn = getattr(svc, "select_link_package_vendor", None)
        if callable(sel_fn):
            try:
                sel_res = sel_fn(pkg_id, vendor_id=requested_vendor_id, actor=g.actor["name"])
                if isinstance(sel_res, dict) and sel_res.get("ok"):
                    selected_vendor = requested_vendor_id
            except Exception:
                pass

    batch_id = str((res.get("batch_fix") or {}).get("batch_id") or "")
    parts = [
        f"SEO Autopilot ({market.upper()}) klar:",
        f"Audit {audit_id}" if audit_id else "",
        f"Strategi {strategy_id}" if strategy_id else "",
        f"Kampanj {campaign_id}" if campaign_id else "",
        f"Länkpaket {pkg_id}" if pkg_id else "",
        f"Vald SEO-butik: {selected_vendor}" if selected_vendor else "",
        f"Batch-förhandsvisning: {batch_id}" if batch_id else "",
    ]
    summary_text = " · ".join(p for p in parts if p)
    _flash(f"{summary_text} (strikt HITL-utkast — inga externa köp gjorda).", "ok")
    if SEO_LINK_ID_RE.fullmatch(pkg_id):
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id, autopilot="1"))
    return redirect(url_for("seo.seo_hub", site=site_id))


@seo_bp.route("/seo/vendors", methods=["GET", "POST"])
@_auth_required
def seo_vendors():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    src = request.form if request.method == "POST" else request.args
    sites = _load_sites_list()
    selected_site = _resolve_site_id(sites, src.get("site") or src.get("site_id"))
    market = (src.get("market") or "no").strip().lower()
    if market not in _VALID_MARKETS:
        market = "no"
    goal = (src.get("goal") or "organic_growth").strip().lower()
    if goal not in _VALID_VENDOR_GOALS:
        goal = "organic_growth"

    budget_sek, link_count, err_msg = _parse_budget_and_links(src)
    if err_msg or budget_sek is None or link_count is None:
        if request.method == "POST" or src.get("budget_sek") or src.get("budget") or src.get("link_count") or src.get("links"):
            _flash(err_msg or "Ogiltig budget eller antal länkar.", "error")
        budget_sek = 15000.0
        link_count = 6

    if request.method == "POST":
        if "include_web_research" in request.form:
            include_web_research = _parse_bool_flag(
                request.form.get("include_web_research"), default=True
            )
        elif request.form.get("include_web_research_present"):
            include_web_research = False
        else:
            include_web_research = True
    else:
        raw_web = src.get("include_web_research")
        if raw_web is None:
            raw_web = src.get("web_research")
        include_web_research = _parse_bool_flag(raw_web, default=True)

    svc = _seo_service()
    raw_res = _safe_service_call(
        svc,
        "vet_link_vendors",
        market=market,
        budget_sek=budget_sek,
        link_count=link_count,
        goal=goal,
        include_web_research=include_web_research,
        actor=g.actor["name"],
        default={},
    )
    if isinstance(raw_res, dict) and raw_res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)

    vendor_selection = _normalize_vendor_selection(raw_res)
    unit_sek = int(round(budget_sek / max(link_count, 1)))
    return render_template(
        "seo/vendors.html",
        **_seo_common_ctx(
            active_section="vendors",
            sites=sites,
            selected_site=selected_site,
            actor_obj=actor_obj,
            market=market,
            budget_sek=int(budget_sek),
            link_count=link_count,
            unit_sek=unit_sek,
            goal=goal,
            include_web_research=include_web_research,
            vendor_selection=vendor_selection,
        ),
    )


@seo_bp.route("/seo/link-packages", methods=["GET", "POST"])
@_auth_required
def seo_link_packages():
    actor_obj = _dashboard_actor(g.actor)
    if request.method == "POST":
        if not _can_suggest(actor_obj):
            return Response("Saknar behörighet", 403)
        sites = _load_sites_list()
        site_id = _resolve_site_id(sites, request.form.get("site_id"))
        name = (request.form.get("name") or "").strip()
        if not name:
            _flash("Ange ett namn för länkpaketet.", "error")
            return redirect(url_for("seo.seo_link_packages"))
        market = (request.form.get("market") or "no").strip().lower()
        if market not in _VALID_MARKETS:
            market = "no"
        campaign_id = (request.form.get("campaign_id") or "").strip() or None
        budget_sek, link_count, err_msg = _parse_budget_and_links(request.form)
        if err_msg or budget_sek is None or link_count is None:
            _flash(err_msg or "Ogiltig budget eller antal länkar.", "error")
            return redirect(url_for("seo.seo_link_packages"))
        notes = (request.form.get("notes") or "").strip()
        selected_vendor_id = (
            request.form.get("selected_vendor_id") or request.form.get("vendor_id") or ""
        ).strip().lower() or None
        if selected_vendor_id and not VENDOR_ID_RE.fullmatch(selected_vendor_id):
            _flash("Ogiltigt leverantörs-ID för SEO-butik.", "error")
            return redirect(url_for("seo.seo_link_packages"))

        try:
            targets = _parse_link_targets(
                request.form.get("targets_raw"),
                request.form.get("targets_json"),
            )
        except Exception as exc:
            _flash(f"Ogiltiga mål-rader för länkpaketet: {exc}", "error")
            return redirect(url_for("seo.seo_link_packages"))

        svc = _seo_service()
        create_kwargs: dict[str, Any] = {
            "site_id": site_id,
            "actor": g.actor["name"],
            "name": name,
            "market": market,
            "campaign_id": campaign_id,
            "budget_sek": budget_sek,
            "link_count": link_count,
            "targets": targets or None,
            "notes": notes,
        }
        try:
            sig = inspect.signature(svc.create_link_package)
            if selected_vendor_id and "selected_vendor_id" in sig.parameters:
                create_kwargs["selected_vendor_id"] = selected_vendor_id
        except (TypeError, ValueError):
            pass

        try:
            res = svc.create_link_package(**create_kwargs)
        except AccessDenied:
            return Response("Saknar behörighet", 403)
        except Exception as exc:
            _flash(f"Kunde inte skapa länkpaket: {exc}", "error")
            return redirect(url_for("seo.seo_link_packages"))

        if not res.get("ok"):
            _flash(str(res.get("message") or "Kunde inte skapa länkpaket."), "error")
            return redirect(url_for("seo.seo_link_packages"))

        pkg = res.get("package") or {}
        pkg_id = str(pkg.get("package_id") or "")
        if (
            selected_vendor_id
            and SEO_LINK_ID_RE.fullmatch(pkg_id)
            and str(pkg.get("selected_vendor_id") or "") != selected_vendor_id
        ):
            sel_fn = getattr(svc, "select_link_package_vendor", None)
            if callable(sel_fn):
                try:
                    sel_res = sel_fn(pkg_id, vendor_id=selected_vendor_id, actor=g.actor["name"])
                    if isinstance(sel_res, dict) and not sel_res.get("ok"):
                        _flash(
                            str(
                                sel_res.get("message")
                                or f"Vald SEO-butik '{selected_vendor_id}' kunde inte kopplas."
                            ),
                            "warn",
                        )
                except Exception:
                    pass

        if pkg.get("guardrail_blocked"):
            _flash(
                f"Länkpaket {pkg_id} skapat som utkast — men anti-spam-guardrails flaggade risker som måste åtgärdas före granskning.",
                "warn",
            )
        else:
            _flash(f"Länkpaket {pkg_id} skapat som utkast (inga externa köp görs automatiskt).", "ok")
        if SEO_LINK_ID_RE.fullmatch(pkg_id):
            return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))
        return redirect(url_for("seo.seo_link_packages"))

    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    sites = _load_sites_list()
    selected_site = _resolve_site_id(sites, request.args.get("site"))
    prefill_market = (request.args.get("market") or "no").strip().lower()
    if prefill_market not in _VALID_MARKETS:
        prefill_market = "no"
    prefill_budget, prefill_links, _ = _parse_budget_and_links(request.args)
    prefill_budget_val = float(prefill_budget) if prefill_budget is not None else 15000.0
    prefill_links_val = int(prefill_links) if prefill_links is not None else 6
    prefill_vendor = (request.args.get("vendor") or "").strip().lower()
    if prefill_vendor and not VENDOR_ID_RE.fullmatch(prefill_vendor):
        prefill_vendor = ""

    svc = _seo_service()
    packages = _decorate_timestamps(svc.list_link_packages(limit=30))
    campaigns = _decorate_timestamps(svc.list_campaigns(limit=30))
    cumulative_anchor_mix = _safe_service_call(
        svc,
        "evaluate_site_cumulative_anchor_mix",
        selected_site,
        actor=g.actor["name"],
        default={},
    )
    if not isinstance(cumulative_anchor_mix, dict):
        cumulative_anchor_mix = {}

    raw_vendors = _safe_service_call(
        svc,
        "vet_link_vendors",
        market=prefill_market,
        budget_sek=prefill_budget_val,
        link_count=prefill_links_val,
        include_web_research=False,
        actor=g.actor["name"],
        default={},
    )
    vendor_selection = _normalize_vendor_selection(raw_vendors)

    return render_template(
        "seo/link_packages.html",
        **_seo_common_ctx(
            active_section="link_packages",
            sites=sites,
            selected_site=selected_site,
            actor_obj=actor_obj,
            packages=packages,
            campaigns=campaigns,
            cumulative_anchor_mix=cumulative_anchor_mix,
            vendor_selection=vendor_selection,
            prefill_market=prefill_market,
            prefill_budget=int(prefill_budget_val),
            prefill_links=prefill_links_val,
            prefill_vendor=prefill_vendor,
        ),
    )


@seo_bp.route("/seo/link-packages/<pkg_id>", methods=["GET"])
@_auth_required
def seo_link_package_detail(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    svc = _seo_service()
    pkg = svc.get_link_package(pkg_id)
    if not pkg:
        return Response("Länkpaket hittades inte", 404)

    pkg_view = dict(pkg)
    pkg_view["created_at_fmt"] = _fmt_dt(pkg_view.get("created_at"))
    pkg_view["updated_at_fmt"] = _fmt_dt(pkg_view.get("updated_at"))
    pkg_view["budget_sek_int"] = _safe_budget_int(pkg_view.get("budget_sek"))
    anchor_rows = _normalize_anchor_mix(pkg_view)
    sites = _load_sites_list()
    site_id = str(pkg_view.get("site_id") or _resolve_site_id(sites, None))

    cumulative_anchor_mix = pkg_view.get("cumulative_anchor_mix")
    if not isinstance(cumulative_anchor_mix, dict) or not cumulative_anchor_mix:
        cumulative_anchor_mix = _safe_service_call(
            svc,
            "evaluate_site_cumulative_anchor_mix",
            site_id,
            include_package_id=pkg_id,
            actor=g.actor["name"],
            default={},
        )
    if not isinstance(cumulative_anchor_mix, dict):
        cumulative_anchor_mix = {}

    pkg_market = str(pkg_view.get("market") or "no").strip().lower()
    if pkg_market not in _VALID_MARKETS:
        pkg_market = "no"
    pkg_budget = float(pkg_view["budget_sek_int"]) if pkg_view["budget_sek_int"] >= 1000 else 15000.0
    pkg_links = int(pkg_view.get("link_count") or 6)
    pkg_links = max(1, min(pkg_links, 50))
    fresh_vendors = _safe_service_call(
        svc,
        "vet_link_vendors",
        market=pkg_market,
        budget_sek=pkg_budget,
        link_count=pkg_links,
        include_web_research=False,
        actor=g.actor["name"],
        default={},
    )
    vendor_selection = _normalize_vendor_selection(fresh_vendors, pkg=pkg_view)

    raw_targets = pkg_view.get("targets") if isinstance(pkg_view.get("targets"), list) else []
    targets_raw_prefill = _format_targets_raw_prefill(raw_targets)

    # Index verification results by target_index and published_url for template lookup
    ver_by_idx: dict[int, dict[str, Any]] = {}
    ver_by_url: dict[str, dict[str, Any]] = {}
    ver_report = pkg_view.get("verification_report") or pkg_view.get("verification")
    ver_list: list[Any] = []
    if isinstance(ver_report, dict) and isinstance(ver_report.get("results"), list):
        ver_list = ver_report["results"]
    elif isinstance(pkg_view.get("verification_results"), list):
        ver_list = pkg_view["verification_results"]
    for idx_i, entry in enumerate(ver_list):
        if not isinstance(entry, dict):
            continue
        t_idx = entry.get("target_index")
        if isinstance(t_idx, int):
            ver_by_idx[t_idx] = entry
        else:
            ver_by_idx[idx_i] = entry
        p_url = str(entry.get("published_url") or "").strip()
        if p_url:
            ver_by_url[p_url] = entry

    warnings = pkg_view.get("guardrail_warnings") or []
    blocked = bool(pkg_view.get("guardrail_blocked"))
    guardrail_tone = "danger" if blocked else ("warn" if warnings else "ok")
    guardrail_label = (
        "Blockerad av anti-spam-guardrail"
        if blocked
        else ("Varningar att granska" if warnings else "Naturlig ankarprofil (OK)")
    )

    return render_template(
        "seo/link_package_detail.html",
        **_seo_common_ctx(
            active_section="link_packages",
            sites=sites,
            selected_site=site_id,
            actor_obj=actor_obj,
            pkg=pkg_view,
            anchor_rows=anchor_rows,
            cumulative_anchor_mix=cumulative_anchor_mix,
            vendor_selection=vendor_selection,
            autopilot_banner=request.args.get("autopilot") == "1",
            targets_raw_prefill=targets_raw_prefill,
            ver_by_idx=ver_by_idx,
            ver_by_url=ver_by_url,
            guardrail_tone=guardrail_tone,
            guardrail_label=guardrail_label,
        ),
    )


@seo_bp.route("/seo/link-packages/<pkg_id>/vendor", methods=["POST"])
@_auth_required
def seo_link_package_select_vendor(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_suggest(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    svc = _seo_service()
    pkg = svc.get_link_package(pkg_id)
    if pkg is None:
        return Response("Länkpaket hittades inte", 404)

    vendor_id = (
        request.form.get("vendor_id") or request.form.get("selected_vendor_id") or ""
    ).strip().lower()
    if not vendor_id or not VENDOR_ID_RE.fullmatch(vendor_id):
        _flash("Ange ett giltigt leverantörs-ID för kvalitetssäkrad SEO-butik.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if str(pkg.get("status") or "").lower() != "draft":
        _flash(
            "Vald SEO-butik kan endast ändras medan länkpaketet har status 'draft'.",
            "error",
        )
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    fn = getattr(svc, "select_link_package_vendor", None)
    if not callable(fn):
        _flash("Byte av SEO-butik är inte tillgängligt i denna version.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    try:
        res = fn(pkg_id, vendor_id=vendor_id, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Kunde inte uppdatera vald SEO-butik: {exc}", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)
    if isinstance(res, dict) and res.get("ok"):
        _flash(f"Vald SEO-butik för {pkg_id} uppdaterad till '{vendor_id}'.", "ok")
    else:
        _flash(
            str((res or {}).get("message") or "Kunde inte uppdatera vald SEO-butik."),
            "error",
        )
    return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))


@seo_bp.route("/seo/link-packages/<pkg_id>/edit", methods=["POST"])
@_auth_required
def seo_link_package_edit(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_suggest(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    svc = _seo_service()
    if svc.get_link_package(pkg_id) is None:
        return Response("Länkpaket hittades inte", 404)

    fn = getattr(svc, "update_link_package_draft", None)
    if not callable(fn):
        _flash("Utkastredigering är inte tillgänglig i denna version.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    name = (request.form.get("name") or "").strip() or None
    notes = request.form.get("notes")
    if notes is not None:
        notes = notes.strip()
    raw_budget = (request.form.get("budget_sek") or "").strip()
    budget_sek: float | None = None
    if raw_budget:
        try:
            budget_sek = float(raw_budget)
        except ValueError:
            _flash("Ogiltig budget (ange 1 000–500 000 SEK).", "error")
            return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))
        if not math.isfinite(budget_sek) or budget_sek < 1000.0 or budget_sek > 500000.0:
            _flash("Budget måste vara mellan 1 000 och 500 000 SEK.", "error")
            return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    targets: list[dict[str, Any]] | None = None
    raw_targets = request.form.get("targets_raw")
    raw_json = request.form.get("targets_json")
    if (raw_targets and raw_targets.strip()) or (raw_json and raw_json.strip()):
        try:
            targets = _parse_link_targets(raw_targets, raw_json)
        except Exception as exc:
            _flash(f"Ogiltiga mål-rader: {exc}", "error")
            return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    try:
        res = fn(
            pkg_id,
            name=name,
            budget_sek=budget_sek,
            targets=targets,
            notes=notes,
            actor=g.actor["name"],
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Kunde inte uppdatera utkast: {exc}", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)
    if isinstance(res, dict) and res.get("ok"):
        _flash(f"Utkast {pkg_id} uppdaterat och omräknat mot guardrails.", "ok")
    else:
        _flash(str((res or {}).get("message") or "Kunde inte uppdatera utkast."), "error")
    return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))


@seo_bp.route("/seo/link-packages/<pkg_id>/deliver", methods=["POST"])
@_auth_required
def seo_link_package_deliver(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_approve_links(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    svc = _seo_service()
    if svc.get_link_package(pkg_id) is None:
        return Response("Länkpaket hittades inte", 404)

    fn = getattr(svc, "record_link_delivery", None)
    if not callable(fn):
        _flash("Leveransregistrering är inte tillgänglig i denna version.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    try:
        target_index = int(request.form.get("target_index") or "1")
    except ValueError:
        _flash("Ogiltigt target_index.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))
    published_url = (request.form.get("published_url") or "").strip()
    published_at = (request.form.get("published_at") or "").strip()
    if not published_url or not published_url.startswith(("https://", "http://")):
        _flash("Ange en giltig publicerad URL (https://...).", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    try:
        res = fn(
            pkg_id,
            target_index=target_index,
            published_url=published_url,
            published_at=published_at,
            actor=g.actor["name"],
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Leveransregistrering misslyckades: {exc}", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)
    if isinstance(res, dict) and res.get("ok"):
        _flash(f"Levererad URL registrerad för mål #{target_index}.", "ok")
    else:
        _flash(str((res or {}).get("message") or "Kunde inte registrera levererad URL."), "error")
    return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))


@seo_bp.route("/seo/link-packages/<pkg_id>/verify", methods=["POST"])
@_auth_required
def seo_link_package_verify(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    svc = _seo_service()
    if svc.get_link_package(pkg_id) is None:
        return Response("Länkpaket hittades inte", 404)

    fn = getattr(svc, "verify_link_package", None)
    if not callable(fn):
        _flash("Länkverifiering är inte tillgänglig i denna version.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    try:
        res = fn(pkg_id, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Placeringverifiering misslyckades: {exc}", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if isinstance(res, dict) and res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)
    if isinstance(res, dict) and res.get("ok"):
        verified_n = int(res.get("verified_count") or 0)
        issue_n = int(res.get("issue_count") or 0)
        tone = "ok" if issue_n == 0 else "warn"
        _flash(
            f"Länkverifiering klar för {pkg_id}: {verified_n} verifierade placeringar, {issue_n} avvikelser.",
            tone,
        )
    else:
        _flash(str((res or {}).get("message") or "Länkverifiering misslyckades."), "error")
    return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))


@seo_bp.route("/seo/link-packages/<pkg_id>/status", methods=["POST"])
@_auth_required
def seo_link_package_status(pkg_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)

    new_status = (request.form.get("new_status") or request.form.get("status") or "").strip().lower()
    note = (request.form.get("note") or "").strip()

    if new_status not in _VALID_LINK_STATUSES:
        _flash("Ogiltig målstatus för länkpaket.", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if new_status in _OSCAR_ONLY_LINK_STATUSES:
        if not _can_approve_links(actor_obj):
            return Response("Saknar behörighet", 403)
    else:
        if not _can_suggest(actor_obj):
            return Response("Saknar behörighet", 403)

    svc = _seo_service()
    if svc.get_link_package(pkg_id) is None:
        return Response("Länkpaket hittades inte", 404)

    try:
        res = svc.transition_link_package(
            package_id=pkg_id,
            actor=g.actor["name"],
            new_status=new_status,
            note=note,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except Exception as exc:
        _flash(f"Statusändring misslyckades: {exc}", "error")
        return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))

    if res.get("error_class") == "AccessDenied":
        return Response("Saknar behörighet", 403)

    if res.get("ok"):
        _flash(f"Länkpaket {pkg_id} uppdaterat till status '{new_status}'.", "ok")
    else:
        _flash(str(res.get("message") or "Statusändring blockerades."), "error")
    return redirect(url_for("seo.seo_link_package_detail", pkg_id=pkg_id))


@seo_bp.route("/seo/link-packages/<pkg_id>/export.<fmt>", methods=["GET"])
@_auth_required
def seo_link_package_export(pkg_id: str, fmt: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)
    if not SEO_LINK_ID_RE.fullmatch(str(pkg_id or "")):
        return Response("Okänt länkpaket", 404)
    clean_fmt = str(fmt or "").strip().lower()
    if clean_fmt not in _VALID_EXPORT_FMTS:
        return Response("Okänt exportformat", 404)

    svc = _seo_service()
    if svc.get_link_package(pkg_id) is None:
        return Response("Länkpaket hittades inte", 404)

    try:
        filename, mimetype, body = svc.export_link_package(pkg_id, fmt=clean_fmt)
    except KeyError:
        return Response("Länkpaket hittades inte", 404)
    except ValueError:
        return Response("Okänt exportformat", 404)

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename or f"{pkg_id}.{clean_fmt}"))
    return Response(
        body,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def register_seo_routes(app: Flask) -> None:
    app.register_blueprint(seo_bp)


__all__ = ["register_seo_routes", "seo_bp"]
