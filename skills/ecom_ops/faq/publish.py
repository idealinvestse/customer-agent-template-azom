"""Sync FAQ corpus to WordPress pages (draft-first HITL)."""

from __future__ import annotations

import hashlib
import html
import os
from dataclasses import dataclass
from typing import Any

from ecom_ops.faq.config import load_faq_config
from ecom_ops.faq.kill_switch import faq_publish_killed
from ecom_ops.faq.models import FaqArticle
from ecom_ops.faq.publish_map import FaqPublishMap
from ecom_ops.faq.store import FaqStore, default_faq_store
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").strip() in {"1", "true", "yes", "on"}


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def article_to_html(article: FaqArticle) -> str:
    title = html.escape(article.title)
    paras = [
        f"<p>{html.escape(p.strip())}</p>"
        for p in article.body.split("\n\n")
        if p.strip()
    ]
    if not paras:
        paras = [f"<p>{html.escape(article.body)}</p>"]
    return f"<h2 id=\"{html.escape(article.id)}\">{title}</h2>\n" + "\n".join(paras)


def build_parent_html(articles: list[FaqArticle], *, market: str) -> str:
    cfg = load_faq_config()
    title = cfg.wp_parent_title.get(market, "FAQ")
    parts = [
        f"<h1>{html.escape(title)}</h1>",
        "<p>Vanliga frågor om order, leverans och produkter.</p>",
        "<ul>",
    ]
    for art in articles:
        parts.append(
            f'<li><a href="#{html.escape(art.id)}">{html.escape(art.title)}</a></li>'
        )
    parts.append("</ul>")
    for art in articles:
        parts.append(article_to_html(art))
    return "\n".join(parts)


@dataclass
class FaqPublishResult:
    ok: bool
    message: str
    market: str = ""
    wp_page_id: int | None = None
    status: str | None = None
    link: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "market": self.market,
            "wp_page_id": self.wp_page_id,
            "status": self.status,
            "link": self.link,
            "details": self.details or {},
        }


# Shared mock transports so sync-draft + publish see the same pages in-process.
_MOCK_WP: dict[str, Any] = {}


def _wp_client(domain: str, *, use_mock: bool):
    from ecom_ops.integrations.wordpress import (
        InMemoryWpTransport,
        WordPressClient,
        wp_client_from_env,
    )

    if use_mock:
        key = domain.strip().lower()
        transport = _MOCK_WP.get(key)
        if transport is None:
            transport = InMemoryWpTransport()
            _MOCK_WP[key] = transport
        return WordPressClient(
            base_url=f"https://azom.{key}",
            transport=transport,
        )
    return wp_client_from_env(domain=domain)


def clear_mock_wp_clients() -> None:
    """Test helper — drop shared mock WP transports."""
    _MOCK_WP.clear()


def _live_market_allowed(market: str, *, use_mock: bool) -> bool:
    if use_mock:
        return True
    cfg = load_faq_config()
    return market.lower() in {m.lower() for m in cfg.live_markets_allowed}


def sync_draft(
    market: str,
    *,
    actor: Actor | str | None = None,
    use_mock: bool | None = None,
    store: FaqStore | None = None,
    publish_map: FaqPublishMap | None = None,
) -> FaqPublishResult:
    """Upsert parent FAQ page as WordPress draft for market."""
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    try:
        require_permission(actor_obj, Permission.FAQ_PUBLISH)
    except AccessDenied as exc:
        return FaqPublishResult(ok=False, message=str(exc), market=market)

    if faq_publish_killed():
        return FaqPublishResult(
            ok=False,
            message="FAQ publish kill-switch active",
            market=market,
        )

    mkt = market.strip().lower()
    mock = _use_mock(use_mock)
    if not _live_market_allowed(mkt, use_mock=mock):
        return FaqPublishResult(
            ok=False,
            message=f"Live FAQ publish not allowed for market={mkt}",
            market=mkt,
        )

    faq_store = store or default_faq_store()
    articles = faq_store.list(market=mkt, customer_safe_only=True)
    if not articles:
        return FaqPublishResult(
            ok=False, message=f"No customer_safe FAQ articles for {mkt}", market=mkt
        )

    cfg = load_faq_config()
    content = build_parent_html(articles, market=mkt)
    chash = _content_hash(content)
    title = cfg.wp_parent_title.get(mkt, "FAQ")
    pmap = publish_map or FaqPublishMap()
    existing = pmap.get(mkt, "")

    try:
        client = _wp_client(mkt, use_mock=mock)
        page = None
        if existing:
            try:
                page = client.update_page(
                    existing.wp_page_id,
                    title=title,
                    content=content,
                    status="draft",
                    slug=cfg.wp_parent_slug,
                )
            except SecurityError as exc:
                # Mock transports are process-local; recreate after CLI restart.
                if not (mock and "404" in str(exc)):
                    raise
                page = None
        if page is None:
            # Try find by slug search first
            found = client.list_pages(search=cfg.wp_parent_slug, per_page=20)
            match = next(
                (
                    p
                    for p in found
                    if (p.raw or {}).get("slug") == cfg.wp_parent_slug
                    or p.title.lower() == title.lower()
                ),
                None,
            )
            if match:
                page = client.update_page(
                    match.id,
                    title=title,
                    content=content,
                    status="draft",
                    slug=cfg.wp_parent_slug,
                )
            else:
                page = client.create_page(
                    title=title,
                    content=content,
                    status="draft",
                    slug=cfg.wp_parent_slug,
                )
        row = pmap.upsert(
            market=mkt,
            article_id="",
            wp_page_id=page.id,
            status=page.status,
            content_hash=chash,
            link=page.link,
        )
    except SecurityError as exc:
        return FaqPublishResult(ok=False, message=str(exc), market=mkt)
    except Exception as exc:  # noqa: BLE001 — surface to CLI/dashboard
        return FaqPublishResult(
            ok=False, message=f"WP sync failed: {exc}", market=mkt
        )

    return FaqPublishResult(
        ok=True,
        message=f"Synced FAQ draft page for {mkt}",
        market=mkt,
        wp_page_id=row.wp_page_id,
        status=row.status,
        link=row.link,
        details={"content_hash": chash, "articles": len(articles)},
    )


def publish_page(
    market: str,
    *,
    status: str = "publish",
    actor: Actor | str | None = None,
    use_mock: bool | None = None,
    publish_map: FaqPublishMap | None = None,
) -> FaqPublishResult:
    """Set WP FAQ page status (publish requires Oscar + FAQ_PUBLISH)."""
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    try:
        require_permission(actor_obj, Permission.FAQ_PUBLISH)
    except AccessDenied as exc:
        return FaqPublishResult(ok=False, message=str(exc), market=market)

    if faq_publish_killed():
        return FaqPublishResult(
            ok=False,
            message="FAQ publish kill-switch active",
            market=market,
        )

    mkt = market.strip().lower()
    want = (status or "publish").strip().lower()
    if want not in {"publish", "draft", "private"}:
        return FaqPublishResult(
            ok=False, message=f"Invalid status: {want}", market=mkt
        )

    mock = _use_mock(use_mock)
    if want == "publish" and not _live_market_allowed(mkt, use_mock=mock):
        return FaqPublishResult(
            ok=False,
            message=f"Live FAQ publish not allowed for market={mkt}",
            market=mkt,
        )

    pmap = publish_map or FaqPublishMap()
    existing = pmap.get(mkt, "")
    if existing is None:
        return FaqPublishResult(
            ok=False,
            message="No synced FAQ page — run faq sync-draft first",
            market=mkt,
        )

    try:
        client = _wp_client(mkt, use_mock=mock)
        try:
            page = client.update_page(existing.wp_page_id, status=want)
        except SecurityError as exc:
            if not (mock and "404" in str(exc)):
                raise
            # Recreate stub page in fresh mock process, then set status.
            cfg = load_faq_config()
            title = cfg.wp_parent_title.get(mkt, "FAQ")
            page = client.create_page(
                title=title,
                content="<p>FAQ</p>",
                status=want,
                slug=cfg.wp_parent_slug,
            )
        row = pmap.upsert(
            market=mkt,
            article_id="",
            wp_page_id=page.id,
            status=page.status,
            content_hash=existing.content_hash,
            link=page.link,
        )
    except SecurityError as exc:
        return FaqPublishResult(ok=False, message=str(exc), market=mkt)
    except Exception as exc:  # noqa: BLE001
        return FaqPublishResult(
            ok=False, message=f"WP publish failed: {exc}", market=mkt
        )

    return FaqPublishResult(
        ok=True,
        message=f"FAQ page status={row.status} for {mkt}",
        market=mkt,
        wp_page_id=row.wp_page_id,
        status=row.status,
        link=row.link,
    )
