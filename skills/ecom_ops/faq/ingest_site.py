"""Ingest WordPress pages/posts into FAQ staging (HITL, no auto-merge)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ecom_ops.faq.ingest_config import (
    effective_use_mock,
    faq_ingest_killed,
    load_faq_ingest_config,
)
from ecom_ops.faq.rbac_ingest import require_faq_ingest
from ecom_ops.faq.staging import site_staging_dir
from ecom_ops.integrations.wordpress import (
    WordPressClient,
    extract_plain_text,
    wp_client_from_env,
)
from ecom_ops.rbac import AccessDenied, Actor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class SiteIngestResult:
    ok: bool
    message: str
    written: int = 0
    skipped: int = 0
    candidates: int = 0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "written": self.written,
            "skipped": self.skipped,
            "candidates": self.candidates,
            "details": self.details or {},
        }


def _stub_candidate(title: str, text: str, *, url: str | None) -> dict[str, Any]:
    summary = text[:280].strip()
    if len(text) > 280:
        summary += "…"
    return {
        "title": title,
        "summary": summary,
        "source_url": url,
        "needs_review": True,
        "customer_safe": False,
    }


def ingest_site(
    *,
    market: str = "se",
    pages: bool | None = None,
    posts: bool | None = None,
    actor: Actor | str | None = None,
    use_mock: bool | None = False,
    client: WordPressClient | None = None,
) -> SiteIngestResult:
    """Fetch publish pages/posts for domain=market into staging JSON."""
    mock = effective_use_mock(use_mock)
    try:
        actor_obj = require_faq_ingest(actor, use_mock=mock)
    except AccessDenied as exc:
        return SiteIngestResult(ok=False, message=str(exc))

    if faq_ingest_killed():
        return SiteIngestResult(ok=False, message="FAQ ingest kill-switch active")

    cfg = load_faq_ingest_config()
    do_pages = cfg.ingest_pages if pages is None else pages
    do_posts = cfg.ingest_posts if posts is None else posts
    domain = market.strip().lower()
    if domain not in {"se", "no", "dk"}:
        return SiteIngestResult(ok=False, message=f"Unsupported market: {market}")

    wp = client or wp_client_from_env(use_mock=mock, domain=domain)
    out_dir = site_staging_dir(domain)
    written = 0
    skipped = 0
    candidates: list[dict[str, Any]] = []
    fetched_at = _now()

    def _save(kind: str, item: Any) -> None:
        nonlocal written, skipped
        raw = item.raw or {}
        content = ""
        c = raw.get("content")
        if isinstance(c, dict):
            content = str(c.get("rendered") or "")
        elif isinstance(c, str):
            content = c
        text = extract_plain_text(content)
        if not text and not item.title:
            skipped += 1
            return
        ch = _content_hash(f"{item.title}\n{text}")
        path = out_dir / f"{kind}_{item.id}.json"
        if path.is_file():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
                if prev.get("content_hash") == ch:
                    skipped += 1
                    return
            except (OSError, json.JSONDecodeError):
                pass
        doc = {
            "id": item.id,
            "type": kind,
            "title": item.title,
            "url": item.link,
            "text": text,
            "status": item.status,
            "fetched_at": fetched_at,
            "content_hash": ch,
            "market": domain,
            "actor": actor_obj.name,
        }
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written += 1
        candidates.append(_stub_candidate(item.title, text, url=item.link))

    if do_pages:
        for page in wp.list_all_pages(
            max_pages=cfg.max_pages_per_site, status="publish"
        ):
            _save("page", page)
    if do_posts:
        for post in wp.list_all_posts(
            max_pages=cfg.max_posts_per_site, status="publish"
        ):
            _save("post", post)

    cand_path = out_dir / "_candidates.json"
    cand_path.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "count": len(candidates),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return SiteIngestResult(
        ok=True,
        message=f"Site ingest {domain}: wrote {written}, skipped {skipped}",
        written=written,
        skipped=skipped,
        candidates=len(candidates),
        details={"dir": str(out_dir), "actor": actor_obj.name},
    )
