"""Lexical FAQ search (category + market + keyword scoring)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from ecom_ops.faq.config import load_faq_config
from ecom_ops.faq.models import FaqArticle, FaqHit
from ecom_ops.faq.store import FaqStore, default_faq_store

_TOKEN_RE = re.compile(r"[a-z0-9åäöæøéèü]+", re.IGNORECASE)
_MIN_PREFIX_LEN = 4
_TITLE_PHRASE_BONUS = 5.0

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

# Fallback if config/faq_synonyms.yaml is missing (keep SE + shared Nordic stems).
_FALLBACK_SYNONYMS: dict[str, tuple[str, ...]] = {
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
    "sporing": ("spor", "tracking", "track", "spårning"),
    "fragt": ("levering", "shipping", "delivery", "frakt"),
}


def market_from_language(language: str | None, market: str | None = None) -> str:
    if market and str(market).strip():
        return str(market).strip().lower()
    lang = (language or "sv").strip().lower()
    return _LANG_TO_MARKET.get(lang, "se")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1]


def _normalize_phrase(text: str) -> str:
    return " ".join(_tokenize(text))


def _token_prefix_related(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < _MIN_PREFIX_LEN or len(b) < _MIN_PREFIX_LEN:
        return False
    return a.startswith(b) or b.startswith(a)


def _config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def _as_syn_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return tuple(parts)
    if isinstance(value, (list, tuple)):
        return tuple(str(x).strip().lower() for x in value if str(x).strip())
    return ()


@lru_cache(maxsize=1)
def _load_synonym_maps() -> dict[str, dict[str, tuple[str, ...]]]:
    path = _config_dir() / "faq_synonyms.yaml"
    maps: dict[str, dict[str, tuple[str, ...]]] = {
        "shared": {},
        "se": {},
        "no": {},
        "dk": {},
    }
    if not path.is_file():
        maps["shared"] = dict(_FALLBACK_SYNONYMS)
        return maps
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        maps["shared"] = dict(_FALLBACK_SYNONYMS)
        return maps
    if not isinstance(loaded, dict):
        maps["shared"] = dict(_FALLBACK_SYNONYMS)
        return maps
    for section in ("shared", "se", "no", "dk"):
        raw = loaded.get(section) or {}
        if not isinstance(raw, dict):
            continue
        maps[section] = {
            str(k).strip().lower(): _as_syn_tuple(v)
            for k, v in raw.items()
            if str(k).strip()
        }
    if not any(maps.values()):
        maps["shared"] = dict(_FALLBACK_SYNONYMS)
    return maps


def clear_faq_synonym_cache() -> None:
    _load_synonym_maps.cache_clear()


def _synonym_lookup(market: str) -> dict[str, tuple[str, ...]]:
    maps = _load_synonym_maps()
    out: dict[str, tuple[str, ...]] = {}
    sections = ["shared", "se"]
    if market in {"no", "dk"} and market not in sections:
        sections.append(market)
    for section in sections:
        for key, vals in maps.get(section, {}).items():
            if key in out:
                merged = list(out[key])
                for v in vals:
                    if v not in merged:
                        merged.append(v)
                out[key] = tuple(merged)
            else:
                out[key] = vals
    return out


def _expand_tokens(tokens: Iterable[str], *, market: str = "se") -> list[str]:
    table = _synonym_lookup(market)
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if tok and tok not in seen:
            out.append(tok)
            seen.add(tok)

    raw = [t for t in tokens if t]
    for tok in raw:
        _add(tok)
        for syn in table.get(tok, ()):
            _add(syn)
        if len(tok) < _MIN_PREFIX_LEN:
            continue
        for key, syns in table.items():
            if _token_prefix_related(tok, key):
                _add(key)
                for syn in syns:
                    _add(syn)
    return out


def _tok_in_text(tok: str, text: str) -> bool:
    lowered = (text or "").lower()
    if not tok or not lowered:
        return False
    if tok in lowered:
        return True
    if len(tok) < _MIN_PREFIX_LEN:
        return False
    return any(_token_prefix_related(tok, word) for word in _tokenize(lowered))


def _tok_in_tags(tok: str, tags: tuple[str, ...]) -> bool:
    for tag in tags:
        t = (tag or "").lower()
        if not t:
            continue
        if tok == t or tok in t:
            return True
        if _token_prefix_related(tok, t):
            return True
    return False


def _score(
    article: FaqArticle,
    query_tokens: Iterable[str],
    category: str | None,
    *,
    query_phrase: str = "",
) -> float:
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
        if _tok_in_text(tok, article.title):
            score += 3.0
        if _tok_in_tags(tok, article.tags):
            score += 2.5
        if tok in article.category:
            score += 1.5
        score += 0.5 * blob.count(tok)
        if tok not in blob and _tok_in_text(tok, blob):
            score += 0.5
    if query_phrase and query_phrase in _normalize_phrase(article.title):
        score += _TITLE_PHRASE_BONUS
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
        if idx < 0 and len(tok) >= _MIN_PREFIX_LEN:
            for word in _tokenize(lowered):
                if _token_prefix_related(tok, word):
                    idx = lowered.find(word)
                    break
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
    tokens = _expand_tokens(_tokenize(query), market=mkt)
    phrase = _normalize_phrase(query)
    floor = cfg.min_score if min_score is None else float(min_score)
    scored: list[FaqHit] = []
    for art in candidates:
        s = _score(art, tokens, category, query_phrase=phrase)
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
