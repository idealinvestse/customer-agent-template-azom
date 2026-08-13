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

# Lightweight synonym expansion (no embeddings) — SE + shared Nordic stems.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "spåra": ("spårning", "spårnings", "tracking", "track"),
    "spårning": ("spåra", "tracking", "track"),
    "tracking": ("spårning", "spåra", "track"),
    "retur": ("returnera", "ånger", "ångerrätt", "return"),
    "returnera": ("retur", "ånger", "return"),
    "ånger": ("ångerrätt", "retur", "returnera"),
    "leverans": ("leverera", "frakt", "delivery", "shipping"),
    "frakt": ("leverans", "shipping", "delivery"),
    "faktura": ("invoice", "kvitto", "betalning"),
    "betalning": ("faktura", "payment", "swish"),
    "paket": ("försändelse", "parcel", "package"),
    "order": ("ordernummer", "beställning", "ordre"),
    "installation": ("montering", "setup", "igångsättning"),
    "trasig": ("defekt", "fel", "reklamation"),
}


def market_from_language(language: str | None, market: str | None = None) -> str:
    if market and str(market).strip():
        return str(market).strip().lower()
    lang = (language or "sv").strip().lower()
    return _LANG_TO_MARKET.get(lang, "se")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1]


def _expand_tokens(tokens: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok not in seen:
            out.append(tok)
            seen.add(tok)
        for syn in _SYNONYMS.get(tok, ()):
            if syn not in seen:
                out.append(syn)
                seen.add(syn)
    return out


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
        score += 0.5 * blob.count(tok)
    if category and article.category == category.strip().lower():
        # Strong boost only when query tokens also matched; else weak category-only.
        if score > 0:
            score += 4.0
        else:
            score += 0.5
    return score


def _snippet(body: str, max_chars: int, query_tokens: list[str] | None = None) -> str:
    text = " ".join((body or "").split())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    # Prefer a window around the first query-token hit.
    lowered = text.lower()
    anchor = 0
    for tok in query_tokens or []:
        idx = lowered.find(tok)
        if idx >= 0:
            anchor = max(0, idx - max_chars // 4)
            break
    chunk = text[anchor : anchor + max_chars]
    if anchor > 0:
        chunk = "…" + chunk.lstrip()
    if anchor + max_chars < len(text):
        chunk = chunk.rstrip() + "…"
    return chunk


def search_faq(
    query: str,
    *,
    market: str | None = None,
    language: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    customer_safe_only: bool = True,
    store: FaqStore | None = None,
    min_score: float | None = None,
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
    tokens = _expand_tokens(_tokenize(query))
    floor = cfg.min_score if min_score is None else float(min_score)
    scored: list[FaqHit] = []
    for art in candidates:
        s = _score(art, tokens, category)
        if s < floor:
            continue
        scored.append(
            FaqHit(
                article=art,
                score=s,
                snippet=_snippet(art.body, cfg.max_chars_per_hit, tokens),
            )
        )
    scored.sort(key=lambda h: (-h.score, h.article.id))
    cap = limit if limit is not None else cfg.max_hits
    return scored[: max(0, cap)]
