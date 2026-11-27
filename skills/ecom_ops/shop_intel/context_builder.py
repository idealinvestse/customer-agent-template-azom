"""Purpose-tailored, budget-bounded, nonce-framed context builder for Shop Intelligence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.research.sanitizer import frame_untrusted_web_data
from ecom_ops.shop_intel.config import (
    load_shop_intel_config,
    shop_intel_inject_enabled,
)
from ecom_ops.shop_intel.models import VALID_PURPOSES
from ecom_ops.shop_intel.store import ShopIntelStore, scrub_text

_FRAMING_OVERHEAD_CHARS = 120


def _format_support_draft(dossier: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    pr = dossier.get("platform_rules") or {}
    if pr:
        lines.append(
            f"Currency: {pr.get('currency')} | Country: {pr.get('default_country')} | "
            f"Carriers: {', '.join(pr.get('carriers') or []) or '-'}"
        )
        thresholds = pr.get("free_shipping_thresholds") or []
        if thresholds:
            t_str = "; ".join(f"{t.get('zone')}: {t.get('min_amount')}" for t in thresholds[:4])
            lines.append(f"Free shipping thresholds: {t_str}")
        gws = pr.get("enabled_gateways") or []
        if gws:
            lines.append(f"Payment gateways: {', '.join(gws[:8])}")
    pl = dossier.get("policies_legal") or {}
    if pl:
        rules = pl.get("extracted_rules") or {}
        if rules:
            lines.append(f"Grounded policy rules: {rules}")
        slugs = [p.get("slug") for p in (pl.get("policy_pages") or []) if p.get("slug")]
        if slugs:
            lines.append(f"Policy pages: {', '.join(slugs[:10])}")
    return lines


def _format_product_copy(dossier: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bv = dossier.get("brand_voice_copy") or {}
    if bv:
        lines.append(
            f"Brand: {bv.get('site_title')} — {bv.get('site_tagline')} | "
            f"Languages: {', '.join(bv.get('languages') or []) or '-'} | Formality: {bv.get('formality')}"
        )
        if bv.get("usp_phrases"):
            lines.append(f"USPs: {', '.join(bv['usp_phrases'][:8])}")
        if bv.get("cta_vocabulary"):
            lines.append(f"CTAs: {', '.join(bv['cta_vocabulary'][:8])}")
    cp = dossier.get("catalog_pricing") or {}
    if cp:
        cats = [c.get("name") for c in (cp.get("categories") or []) if c.get("name")]
        attrs = [a.get("name") for a in (cp.get("attributes") or []) if a.get("name")]
        pricing = cp.get("pricing") or {}
        lines.append(
            f"Catalog: {cp.get('product_count')} products | Price range: {pricing.get('min_price')}–{pricing.get('max_price')}"
        )
        if cats:
            lines.append(f"Categories: {', '.join(cats[:10])}")
        if attrs:
            lines.append(f"Attributes: {', '.join(attrs[:10])}")
    pl = dossier.get("policies_legal") or {}
    if pl and pl.get("extracted_rules"):
        lines.append(f"Policy promises: {pl['extracted_rules']}")
    return lines


def _format_wp_coder(dossier: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    pr = dossier.get("platform_rules") or {}
    vers = pr.get("versions") or {}
    if vers:
        lines.append(
            f"Versions: WP={vers.get('wordpress') or '?'} Woo={vers.get('woocommerce') or '?'} PHP={vers.get('php') or '?'}"
        )
    ta = dossier.get("theme_architecture") or {}
    if ta:
        lines.append(
            f"Theme: {ta.get('active_theme')} (kind={ta.get('theme_kind')}, parent={ta.get('parent_theme')}, "
            f"sftp={ta.get('sftp_source')})"
        )
        tj = ta.get("theme_json") or {}
        if tj.get("present"):
            palette = [p.get("slug") or p.get("color") for p in (tj.get("palette") or [])[:8]]
            fonts = [f.get("slug") or f.get("name") for f in (tj.get("font_families") or [])[:6]]
            lines.append(f"theme.json v{tj.get('version')}: palette={palette} fonts={fonts}")
        woo_ov = ta.get("woo_overrides") or {}
        lines.append(
            f"Woo overrides: {woo_ov.get('total_overrides', 0)} (outdated={woo_ov.get('outdated', 0)})"
        )
        if ta.get("mu_plugins"):
            lines.append(
                f"MU-plugins: {', '.join(m.get('slug', '') for m in ta['mu_plugins'][:8])}"
            )
    pe = dossier.get("plugins_ecosystem") or {}
    if pe:
        active_slugs = [p.get("slug") for p in (pe.get("plugins") or []) if p.get("active")]
        lines.append(f"Active plugins: {', '.join(active_slugs[:20])}")
        if pe.get("rest_namespaces"):
            lines.append(f"REST namespaces: {', '.join(pe['rest_namespaces'][:12])}")
    return lines


def _format_seo(dossier: dict[str, Any]) -> list[str]:
    lines = _format_product_copy(dossier)
    pe = dossier.get("plugins_ecosystem") or {}
    seo_plugins = (pe.get("groups") or {}).get("seo") or []
    if seo_plugins:
        lines.append(f"SEO plugins: {', '.join(seo_plugins)}")
    cp = dossier.get("catalog_pricing") or {}
    if cp.get("catalog_gaps"):
        lines.append(f"Catalog gaps: {cp['catalog_gaps']}")
    return lines


def _format_security_audit(dossier: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    sp = dossier.get("security_posture") or {}
    if sp:
        lines.append(
            f"Security score: {sp.get('security_score')}/100 | "
            f"Findings: {sp.get('findings_count')} | Diffs: {sp.get('diffs_count')} | "
            f"Severities: {sp.get('severity_counts')}"
        )
    pe = dossier.get("plugins_ecosystem") or {}
    if pe:
        inactive = [p.get("slug") for p in (pe.get("plugins") or []) if not p.get("active")]
        lines.append(f"Inactive plugins ({len(inactive)}): {', '.join(inactive[:12]) or 'none'}")
        wh_hosts = [w.get("delivery_host") for w in (pe.get("webhooks") or []) if w.get("delivery_host")]
        if wh_hosts:
            lines.append(f"Webhook hosts: {', '.join(wh_hosts[:8])}")
    ta = dossier.get("theme_architecture") or {}
    if ta:
        woo_ov = ta.get("woo_overrides") or {}
        lines.append(
            f"Theme: {ta.get('active_theme')} | Outdated Woo overrides: {woo_ov.get('outdated', 0)} | "
            f"sftp={ta.get('sftp_source')}"
        )
    pl = dossier.get("policies_legal") or {}
    if pl and pl.get("missing_policies"):
        lines.append(f"Missing legal pages: {', '.join(pl['missing_policies'])}")
    return lines


def _format_chat_general(dossier: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bv = dossier.get("brand_voice_copy") or {}
    pr = dossier.get("platform_rules") or {}
    cp = dossier.get("catalog_pricing") or {}
    ta = dossier.get("theme_architecture") or {}
    pe = dossier.get("plugins_ecosystem") or {}
    pl = dossier.get("policies_legal") or {}
    sp = dossier.get("security_posture") or {}

    if bv.get("site_title"):
        lines.append(f"Store: {bv.get('site_title')} ({bv.get('site_tagline')})")
    if pr:
        lines.append(
            f"Platform: currency={pr.get('currency')} carriers={pr.get('carriers')} "
            f"gateways={pr.get('enabled_gateways')}"
        )
    if cp:
        lines.append(f"Catalog: {cp.get('product_count')} products, {len(cp.get('categories') or [])} categories")
    if ta:
        lines.append(f"Theme: {ta.get('active_theme')} ({ta.get('theme_kind')})")
    if pe:
        lines.append(f"Plugins: {pe.get('active_count')} active / {pe.get('total_plugins')} total")
    if pl and pl.get("extracted_rules"):
        lines.append(f"Policies: {pl['extracted_rules']}")
    if sp:
        lines.append(f"Security score: {sp.get('security_score')}/100")
    return lines


def build_tailored_context(
    site_id: str,
    *,
    purpose: str,
    data_dir: Path | None = None,
    max_chars: int | None = None,
) -> str | None:
    """Build a bounded, nonce-framed ``SHOP_INTEL_CONTEXT`` block or return ``None``."""
    clean_purpose = str(purpose or "").strip().lower()
    if clean_purpose not in VALID_PURPOSES:
        raise ValueError(f"Invalid Shop Intel purpose {purpose!r}. Allowed: {sorted(VALID_PURPOSES)}")
    if not shop_intel_inject_enabled():
        return None

    store = ShopIntelStore(data_dir=data_dir)
    run = store.get_latest_run(site_id)
    if not run:
        return None

    dossier = run.get("dossier") if isinstance(run.get("dossier"), dict) else {}
    formatter_map = {
        "support_draft": _format_support_draft,
        "product_copy": _format_product_copy,
        "wp_coder": _format_wp_coder,
        "chat_general": _format_chat_general,
        "seo": _format_seo,
        "security_audit": _format_security_audit,
    }
    body_lines = formatter_map[clean_purpose](dossier)
    header = (
        f"[site_id={run.get('site_id')} purpose={clean_purpose} "
        f"provenance={run.get('provenance')} run_id={run.get('run_id')} "
        f"observed_at={run.get('created_at')}]"
    )
    raw_text = "\n".join([header, *body_lines])

    cfg = load_shop_intel_config()
    budget = cfg.context_budgets.get(clean_purpose, 2200)
    if max_chars is not None:
        limit = max(64, int(max_chars))
        inner_limit = max(32, limit - _FRAMING_OVERHEAD_CHARS) if limit > _FRAMING_OVERHEAD_CHARS + 32 else limit
    else:
        inner_limit = budget

    safe_inner = scrub_text(raw_text, max_chars=inner_limit)
    return frame_untrusted_web_data(safe_inner, label="SHOP_INTEL_CONTEXT", max_chars=inner_limit)


__all__ = ["build_tailored_context"]
