"""Ingest Woo products + allowlisted guide URLs into FAQ staging."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from ecom_ops.faq.ingest_config import (
    effective_use_mock,
    faq_ingest_killed,
    load_faq_ingest_config,
)
from ecom_ops.faq.rbac_ingest import require_faq_ingest
from ecom_ops.faq.staging import guides_staging_dir, products_staging_dir
from ecom_ops.integrations.woocommerce import WooCommerceClient, client_from_env
from ecom_ops.integrations.wordpress import extract_plain_text
from ecom_ops.rbac import AccessDenied, Actor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _host_allowed(url: str, allowlist: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in {h.lower() for h in allowlist}


def _fetch_allowlisted_guide(
    url: str,
    *,
    allowlist: tuple[str, ...],
    max_bytes: int,
) -> str | None:
    """GET an allowlisted HTTPS URL. No redirects (SSRF)."""
    if not _host_allowed(url, allowlist):
        return None
    if url.lower().endswith(".pdf"):
        return None
    resp = requests.get(
        url,
        timeout=20,
        allow_redirects=False,
        stream=True,
    )
    if 300 <= int(resp.status_code) < 400:
        resp.close()
        return None
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype:
        resp.close()
        return None
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            resp.close()
            return None
        chunks.append(chunk)
    resp.close()
    return extract_plain_text(b"".join(chunks).decode("utf-8", errors="replace"))


@dataclass
class ProductIngestResult:
    ok: bool
    message: str
    products_written: int = 0
    guides_written: int = 0
    skipped: int = 0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "products_written": self.products_written,
            "guides_written": self.guides_written,
            "skipped": self.skipped,
            "details": self.details or {},
        }


def _product_category(product: dict[str, Any]) -> str:
    attrs = product.get("attributes") or []
    attr_names = " ".join(
        str(a.get("name") or "") for a in attrs if isinstance(a, dict)
    ).lower()
    cats = product.get("categories") or []
    cat_names = " ".join(
        str(c.get("name") or "") for c in cats if isinstance(c, dict)
    ).lower()
    blob = f"{attr_names} {cat_names}"
    if any(k in blob for k in ("frakt", "shipping", "leverans", "porto")):
        return "shipping"
    if any(k in blob for k in ("tech", "teknisk", "spec", "manual")):
        return "technical"
    return "product"


def ingest_products(
    *,
    market: str = "se",
    actor: Actor | str | None = None,
    use_mock: bool | None = False,
    client: WooCommerceClient | None = None,
    fetch_guides: bool = True,
) -> ProductIngestResult:
    mock = effective_use_mock(use_mock)
    try:
        actor_obj = require_faq_ingest(actor, use_mock=mock)
    except AccessDenied as exc:
        return ProductIngestResult(ok=False, message=str(exc))

    if faq_ingest_killed():
        return ProductIngestResult(
            ok=False, message="FAQ ingest kill-switch active"
        )

    cfg = load_faq_ingest_config()
    domain = market.strip().lower()
    if domain not in {"se", "no", "dk"}:
        return ProductIngestResult(ok=False, message=f"Unsupported market: {market}")

    woo = client or client_from_env(use_mock=mock, domain=domain)
    out_dir = products_staging_dir(domain)
    written = 0
    skipped = 0
    fetched_at = _now()
    count = 0
    for product in woo.list_all_products(max_pages=20):
        count += 1
        if count > cfg.max_products_per_market:
            break
        if not isinstance(product, dict):
            skipped += 1
            continue
        pid = product.get("id")
        name = str(product.get("name") or "").strip()
        short = extract_plain_text(str(product.get("short_description") or ""))
        desc = extract_plain_text(str(product.get("description") or ""))
        text = "\n\n".join(p for p in (short, desc) if p).strip()
        ch = _hash(f"{name}\n{text}")
        path = out_dir / f"product_{pid}.json"
        if path.is_file():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
                if prev.get("content_hash") == ch:
                    skipped += 1
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        doc = {
            "id": pid,
            "sku": product.get("sku"),
            "name": name,
            "short_description": short,
            "description": desc,
            "attributes": product.get("attributes") or [],
            "categories": product.get("categories") or [],
            "faq_category": _product_category(product),
            "permalink": product.get("permalink"),
            "fetched_at": fetched_at,
            "content_hash": ch,
            "market": domain,
            "actor": actor_obj.name,
        }
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written += 1

    guides_written = 0
    guide_skips = 0
    if fetch_guides and cfg.guide_urls:
        gdir = guides_staging_dir(domain)
        for url in cfg.guide_urls:
            if mock:
                # Mock never hits the network, even if guide_urls is set.
                guide_skips += 1
                continue
            try:
                text = _fetch_allowlisted_guide(
                    url,
                    allowlist=cfg.guide_allowlist_hosts,
                    max_bytes=cfg.max_guide_bytes,
                )
            except Exception:  # noqa: BLE001
                guide_skips += 1
                continue
            if text is None:
                guide_skips += 1
                continue
            slug = _hash(url)
            path = gdir / f"guide_{slug}.json"
            path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "text": text,
                        "fetched_at": fetched_at,
                        "content_hash": _hash(text),
                        "market": domain,
                        "actor": actor_obj.name,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            guides_written += 1

    return ProductIngestResult(
        ok=True,
        message=(
            f"Product ingest {domain}: products={written}, "
            f"guides={guides_written}, skipped={skipped + guide_skips}"
        ),
        products_written=written,
        guides_written=guides_written,
        skipped=skipped + guide_skips,
        details={"dir": str(out_dir), "actor": actor_obj.name},
    )
