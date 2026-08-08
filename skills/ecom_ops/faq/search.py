"""Lexical FAQ search (category + market + keyword scoring)."""

from __future__ import annotations

import re
from typing import Iterable

from ecom_ops.faq.config import load_faq_config
from ecom_ops.faq.models import FaqArticle, FaqHit
from ecom_ops.faq.store import FaqStore, default_faq_store

_TOKEN_RE = re.compile(r"[a-z0-9åäöæøéèü]+", re.IGNORECASE)

_LANG_TO_MARKET = {
    "sv": "se",
    "se": "se",
    "nb": "no",
    "nn": "no",
    "no": "no",
    "da": "dk",
    "dk": "dk",
    "en": "se",
}


def market_from_language(language: str | None, market: str | None = None) -> str:
    if market and str(market).strip():
        return str(market).strip().lower()
    lang = (language or "sv").strip().lower()
    return _LANG_TO_MARKET.get(lang, "se")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1]


def _score(article: FaqArticle, query_tokens: Iterable[str], category: str | None) -> float:
    q = list(query_tokens)
    if not q and not category:
        return 0.0
    blob = " ".join(
        [
            article.title.lower(),
            article.body.lower(),
            " ".join(article.tags).lower(),
            article.category.lower(),
            article.id.lower(),
        ]
    )
    score = 0.0
    for tok in q:
        if tok in article.title.lower():
            score += 3.0
        if tok in article.tags:
            score += 2.5
        if tok in article.category:
            score += 1.5
        # term frequency-ish
        score += 0.5 * blob.count(tok)
    if category and article.category == category.strip().lower():
        score += 4.0
    return score


def _snippet(body: str, max_chars: int) -> str:
    text = " ".join((body or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def search_faq(
    query: str,
    *,
    market: str | None = None,
    language: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    customer_safe_only: bool = True,
    store: FaqStore | None = None,
) -> list[FaqHit]:
    cfg = load_faq_config()
    if not cfg.enabled:
        return []
    faq_store = store or default_faq_store()
    mkt = market_from_language(language, market)
    candidates = faq_store.list(
        market=mkt,
        customer_safe_only=customer_safe_only,
    )
    tokens = _tokenize(query)
    scored: list[FaqHit] = []
    for art in candidates:
        s = _score(art, tokens, category)
        if s <= 0 and not (category and art.category == (category or "").lower()):
            continue
        if s <= 0:
            # category-only fallback: mild score so empty query + category still works
            s = 1.0 if category and art.category == category.lower() else 0.0
        if s <= 0:
            continue
        scored.append(
            FaqHit(
                article=art,
                score=s,
                snippet=_snippet(art.body, cfg.max_chars_per_hit),
            )
        )
    scored.sort(key=lambda h: (-h.score, h.article.id))
    cap = limit if limit is not None else cfg.max_hits
    return scored[: max(0, cap)]
