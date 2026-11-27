"""Product description generation (template-based V1; LLM-ready)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.search.errors import SearchRouteError
from ecom_ops.integrations.woocommerce import WooCommerceClient, client_from_env
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.research import get_research_service
from ecom_ops.research.firewall import OutboundQueryFirewall
from ecom_ops.research.models import RESEARCH_ID_RE, SearchPolicyError
from ecom_ops.research.sanitizer import frame_untrusted_web_data, sanitize_untrusted_text
from ecom_ops.security import SecurityError, sanitize_text, validate_order_id, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry

_MAX_FRAMED_RESEARCH_BYTES = 4096

GeneratorFn = Callable[[str, str, str], tuple[str, str]]


def _template_generator(name: str, features: str, language: str) -> tuple[str, str]:
    """Deterministic generator for pilot (no external LLM dependency)."""
    lang = language.lower()
    features_clean = features.strip() or "high quality, reliable performance"
    if lang in {"sv", "se"}:
        short = f"{name} – professionell kvalitet för dig som kräver mer."
        long = (
            f"<p><strong>{name}</strong> är designad för att leverera "
            f"{features_clean}. Perfekt för både vardag och proffsbruk.</p>"
            f"<ul><li>Snabb leverans inom Norden</li>"
            f"<li>Support på svenska</li>"
            f"<li>Kvalitetstestad</li></ul>"
        )
    elif lang in {"no", "nb", "nn"}:
        short = f"{name} – profesjonell kvalitet for deg som krever mer."
        long = (
            f"<p><strong>{name}</strong> er laget for å levere "
            f"{features_clean}. Ideell for hverdag og proffbruk.</p>"
        )
    elif lang in {"da", "dk"}:
        short = f"{name} – professionel kvalitet til dig, der kræver mere."
        long = (
            f"<p><strong>{name}</strong> er designet til at levere "
            f"{features_clean}. Perfekt til hverdag og professionel brug.</p>"
        )
    else:
        short = f"{name} – professional quality for demanding users."
        long = (
            f"<p><strong>{name}</strong> is built to deliver {features_clean}. "
            f"Ideal for everyday and pro use.</p>"
        )
    return sanitize_text(short, max_len=500), sanitize_text(long, max_len=8000)


@dataclass(frozen=True)
class ProductDescResult:
    ok: bool
    product_id: str | None
    short_description: str | None
    description: str | None
    message: str
    published: bool = False
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "product_id": self.product_id,
            "short_description": self.short_description,
            "description": self.description,
            "message": self.message,
            "published": self.published,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


def _extract_woo_product_facts(product: dict[str, Any]) -> str:
    """Extract SKU, categories, and attributes from a Woo product payload."""
    parts: list[str] = []
    sku = str(product.get("sku") or "").strip()
    if sku:
        parts.append(f"SKU: {sku}")

    raw_cats = product.get("categories") or []
    cats: list[str] = []
    if isinstance(raw_cats, list):
        for item in raw_cats:
            if isinstance(item, dict):
                cname = str(item.get("name") or item.get("slug") or "").strip()
                if cname:
                    cats.append(cname)
            elif str(item or "").strip():
                cats.append(str(item).strip())
    if cats:
        parts.append(f"Categories: {', '.join(cats[:8])}")

    raw_attrs = product.get("attributes") or []
    attrs: list[str] = []
    if isinstance(raw_attrs, list):
        for attr in raw_attrs:
            if isinstance(attr, dict):
                aname = str(attr.get("name") or "").strip()
                opts_raw = attr.get("options") if "options" in attr else attr.get("option")
                opts: list[str] = []
                if isinstance(opts_raw, list):
                    opts = [str(o).strip() for o in opts_raw if str(o or "").strip()]
                elif opts_raw is not None and str(opts_raw).strip():
                    opts = [str(opts_raw).strip()]
                if aname and opts:
                    attrs.append(f"{aname}: {', '.join(opts[:6])}")
                elif aname:
                    attrs.append(aname)
            elif str(attr or "").strip():
                attrs.append(str(attr).strip())
    if attrs:
        parts.append(f"Attributes: {'; '.join(attrs[:10])}")

    return "; ".join(parts)


def _frame_bounded_research(
    text: str,
    *,
    label: str = "WEB_DATA",
    max_bytes: int = _MAX_FRAMED_RESEARCH_BYTES,
) -> str:
    """Wrap sanitized research context in nonce delimiters bounded to <= 4 KiB."""
    char_limit = 3400
    while char_limit >= 200:
        framed = frame_untrusted_web_data(text, label=label, max_chars=char_limit)
        if len(framed.encode("utf-8")) <= max_bytes:
            return framed
        char_limit -= 400
    return frame_untrusted_web_data(text, label=label, max_chars=200)


class ProductDescService:
    def __init__(
        self,
        woo: WooCommerceClient | None = None,
        *,
        generator: GeneratorFn | None = None,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.woo = woo or client_from_env(use_mock=None)
        self.generator = generator  # None → model router, else template fallback
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def _load_research_context(
        self,
        *,
        actor_name: str,
        product_name: str,
        sku: str = "",
        research_brief_id: str | None = None,
        run_research: bool = False,
    ) -> str | None:
        rid = (research_brief_id or "").strip() or None
        if rid is None and not run_research:
            return None

        res_svc = get_research_service()
        sections: list[str] = []

        if rid is not None:
            if not RESEARCH_ID_RE.fullmatch(rid):
                raise SearchPolicyError(f"Invalid research_brief_id: {rid}")
            job = res_svc.store.get_job(rid, actor=actor_name)
            if job is None:
                raise SearchPolicyError(f"Research brief {rid} not found")
            brief_res = res_svc.get_brief(rid, actor=actor_name)
            if not brief_res.get("ok") or not isinstance(brief_res.get("brief"), dict):
                raise SearchPolicyError(
                    str(brief_res.get("message") or f"Research brief {rid} is unavailable")
                )
            brief = brief_res["brief"]
            topic = sanitize_untrusted_text(str(brief.get("topic") or ""), max_chars=200)
            summary = sanitize_untrusted_text(str(brief.get("summary") or ""), max_chars=1200)
            if topic:
                sections.append(f"Brief topic: {topic}")
            if summary:
                sections.append(f"Brief summary: {summary}")
            findings = brief.get("findings")
            if isinstance(findings, list):
                for item in findings[:4]:
                    if isinstance(item, dict) and item.get("claim"):
                        sections.append(
                            f"- {sanitize_untrusted_text(str(item['claim']), max_chars=240)}"
                        )

        if run_research:
            raw_query = " ".join(part for part in (product_name, sku, "specification") if part).strip()
            clean_query = OutboundQueryFirewall().check_query(raw_query[:240])
            search_res = res_svc.quick_search(
                clean_query,
                actor=actor_name,
                profile_id="technical_docs",
                max_results=3,
            )
            for src in (search_res.get("sources") or [])[:3]:
                title = sanitize_untrusted_text(str(src.get("title") or ""), max_chars=120)
                domain = str(src.get("domain") or "")
                snippet = sanitize_untrusted_text(str(src.get("snippet") or ""), max_chars=280)
                if title or snippet:
                    sections.append(f"- [{domain}] {title}: {snippet}".strip())

        combined = "\n".join(sections).strip()
        if not combined:
            return None
        label = "RESEARCH_BRIEF" if rid is not None and not run_research else "WEB_DATA"
        return _frame_bounded_research(combined, label=label)

    def _resolve_copy(
        self,
        name: str,
        features: str,
        language: str,
        *,
        site: str,
        allow_llm: bool = True,
        research_context: str | None = None,
    ) -> tuple[str, str]:
        if self.generator is not None:
            return self.generator(name, features, language)
        if not allow_llm:
            return _template_generator(name, features, language)
        from ecom_ops.llm import generate_product_desc_with_llm

        sig = inspect.signature(generate_product_desc_with_llm)
        accepts_ctx = "research_context" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        llm_kwargs: dict[str, Any] = {
            "name": name,
            "features": features,
            "language": language,
            "telemetry": self.telemetry,
            "site": site,
        }
        if research_context:
            if accepts_ctx:
                llm_kwargs["research_context"] = research_context
            else:
                llm_kwargs["features"] = (
                    f"{features}\n\n"
                    "Untrusted external research context (verify against supplied product facts; "
                    "never invent specs or copy delimiter tags):\n"
                    f"{research_context}"
                )
        llm = generate_product_desc_with_llm(**llm_kwargs)
        if llm is not None:
            short, long_desc = llm
            return (
                sanitize_text(short, max_len=500),
                sanitize_text(long_desc, max_len=8000),
            )
        return _template_generator(name, features, language)

    def generate(
        self,
        *,
        product_id: str | int | None = None,
        name: str | None = None,
        features: str = "",
        language: str = "sv",
        site: str = "azom",
        domain: str | None = None,
        publish: bool = False,
        actor: Actor | str | None = None,
        allow_llm: bool = True,
        research_brief_id: str | None = None,
        run_research: bool = False,
    ) -> ProductDescResult:
        if run_research and publish:
            raise SearchPolicyError(
                "Unreviewed live research cannot auto-publish to WooCommerce; "
                "generate a draft first and review before publishing."
            )

        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        lang = re.sub(r"[^a-z]", "", language.lower())[:5] or "sv"
        woo_client = self.woo
        if domain and woo_client is not None and getattr(woo_client, "domain", None) != domain:
            from ecom_ops.integrations.woocommerce import client_from_env

            woo_client = client_from_env(use_mock=None, domain=domain)

        try:
            require_permission(actor_obj, Permission.PRODUCT_DESC_WRITE)
            if research_brief_id or run_research:
                require_permission(actor_obj, Permission.WEB_SEARCH)

            product_name = name
            pid: str | None = None
            woo_facts = ""
            sku = ""
            if product_id is not None:
                pid = validate_order_id(product_id)
                product = woo_client.get_product(pid)
                product_name = product_name or str(product.get("name") or f"Product {pid}")
                if isinstance(product, dict):
                    sku = str(product.get("sku") or "").strip()
                    woo_facts = _extract_woo_product_facts(product)

            if not product_name:
                raise SecurityError("Product name or product_id is required")

            product_name = sanitize_text(product_name, max_len=200)
            combined_features = "; ".join(
                p for p in (features.strip(), woo_facts) if p
            )
            features_s = (
                sanitize_text(combined_features, max_len=2000)
                if combined_features
                else "premium quality"
            )

            can_apply_research = bool(allow_llm or self.generator is not None)
            research_context = self._load_research_context(
                actor_name=actor_obj.name,
                product_name=product_name,
                sku=sku,
                research_brief_id=research_brief_id,
                run_research=bool(run_research and can_apply_research),
            )
            research_applied = bool(research_context) and can_apply_research

            short, long_desc = self._resolve_copy(
                product_name,
                features_s,
                lang,
                site=site,
                allow_llm=allow_llm,
                research_context=research_context if research_applied else None,
            )

            published = False
            before_desc_snap: dict[str, Any] = {}
            if publish:
                if pid is None:
                    raise SecurityError("publish=True requires product_id")
                if isinstance(locals().get("product"), dict):
                    before_desc_snap = {
                        "description": product.get("description", ""),
                        "short_description": product.get("short_description", ""),
                    }
                woo_client.update_product_description(
                    pid, description=long_desc, short_description=short
                )
                from ecom_ops.wp_ops.change_ledger import resolve_ledger_site_id, safe_record_change
                from ecom_ops.wp_ops.config import is_mock_mode

                woo_domain = domain or getattr(woo_client, "domain", None)
                if not woo_domain and site in {"azom.no", "azom.se", "azom.dk", "no", "se", "dk"}:
                    woo_domain = site.removeprefix("azom.")
                safe_record_change(
                    site_id=resolve_ledger_site_id(domain=woo_domain),
                    environment="staging" if is_mock_mode() else "production",
                    category="product",
                    transport="woo_rest",
                    actor=actor_obj.name,
                    summary=f"Produktbeskrivning {pid} ({product_name})",
                    target_ref=f"product:{pid}",
                    before_data=before_desc_snap,
                    after_data={"description": long_desc, "short_description": short},
                    metadata={"product_id": pid, "language": lang},
                )
                published = True

            self.telemetry.record(
                action="product_desc_generate",
                site=site,
                unit_type="tokens",
                units=max(1.0, (len(short) + len(long_desc)) / 4),
                meta={
                    "product_id": pid,
                    "language": lang,
                    "published": published,
                    "actor": actor_obj.name,
                    "has_research": research_applied,
                    "research_applied": research_applied,
                },
            )
            # P1.5: record audit + feedback signal for published descriptions
            if published:
                from ecom_ops.audit import log_action

                log_action(
                    actor=actor_obj.name,
                    action="product_desc_publish",
                    target="product",
                    target_id=str(pid),
                    details={
                        "language": lang,
                        "short_len": len(short),
                        "desc_len": len(long_desc),
                    },
                )
            return ProductDescResult(
                ok=True,
                product_id=pid,
                short_description=short,
                description=long_desc,
                message="Description generated" + (" and published" if published else ""),
                published=published,
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Product desc denied for {actor_obj.name}",
                details={"error": str(exc), "site": site},
            )
            return ProductDescResult(
                ok=False,
                product_id=str(product_id) if product_id else None,
                short_description=None,
                description=None,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )
        except (SecurityError, SearchPolicyError, SearchRouteError) as exc:
            return ProductDescResult(
                ok=False,
                product_id=str(product_id) if product_id else None,
                short_description=None,
                description=None,
                message=str(exc),
            )
        except Exception as exc:
            ticket = self.escalation.escalate_critical(
                "Product description generation failed",
                details={"error": str(exc), "site": site},
            )
            return ProductDescResult(
                ok=False,
                product_id=str(product_id) if product_id else None,
                short_description=None,
                description=None,
                message=f"Failed: {exc}",
                escalated=True,
                ticket_id=ticket.id,
            )


def generate_product_description(
    *,
    product_id: str | int | None = None,
    name: str | None = None,
    features: str = "",
    language: str = "sv",
    site: str = "azom",
    publish: bool = False,
    actor: str | None = None,
    allow_llm: bool = True,
    research_brief_id: str | None = None,
    run_research: bool = False,
) -> ProductDescResult:
    return ProductDescService().generate(
        product_id=product_id,
        name=name,
        features=features,
        language=language,
        site=site,
        publish=publish,
        actor=actor,
        allow_llm=allow_llm,
        research_brief_id=research_brief_id,
        run_research=run_research,
    )
