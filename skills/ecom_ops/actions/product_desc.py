"""Product description generation (template-based V1; LLM-ready)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.woocommerce import WooCommerceClient, client_from_env
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, sanitize_text, validate_order_id, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry

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
        self.generator = generator  # None → LLM when key set, else template
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def _resolve_copy(
        self, name: str, features: str, language: str, *, site: str
    ) -> tuple[str, str]:
        if self.generator is not None:
            return self.generator(name, features, language)
        if os.environ.get("OPENROUTER_API_KEY"):
            from ecom_ops.llm import generate_product_desc_with_llm

            llm = generate_product_desc_with_llm(
                name=name,
                features=features,
                language=language,
                telemetry=self.telemetry,
                site=site,
            )
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
        publish: bool = False,
        actor: Actor | str | None = None,
    ) -> ProductDescResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        lang = re.sub(r"[^a-z]", "", language.lower())[:5] or "sv"

        try:
            require_permission(actor_obj, Permission.PRODUCT_DESC_WRITE)

            product_name = name
            pid: str | None = None
            if product_id is not None:
                pid = validate_order_id(product_id)
                product = self.woo.get_product(pid)
                product_name = product_name or str(product.get("name") or f"Product {pid}")

            if not product_name:
                raise SecurityError("Product name or product_id is required")

            product_name = sanitize_text(product_name, max_len=200)
            features_s = sanitize_text(features, max_len=2000) if features.strip() else "premium quality"

            short, long_desc = self._resolve_copy(
                product_name, features_s, lang, site=site
            )

            published = False
            if publish:
                if pid is None:
                    raise SecurityError("publish=True requires product_id")
                self.woo.update_product_description(
                    pid, description=long_desc, short_description=short
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
        except SecurityError as exc:
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
) -> ProductDescResult:
    return ProductDescService().generate(
        product_id=product_id,
        name=name,
        features=features,
        language=language,
        site=site,
        publish=publish,
        actor=actor,
    )
