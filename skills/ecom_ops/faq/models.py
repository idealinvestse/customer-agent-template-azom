"""FAQ article and search hit models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FaqArticle:
    id: str
    market: str
    language: str
    category: str
    title: str
    body: str
    tags: tuple[str, ...] = ()
    customer_safe: bool = True
    updated_at: str = ""
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "market": self.market,
            "language": self.language,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "customer_safe": self.customer_safe,
            "updated_at": self.updated_at,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class FaqHit:
    article: FaqArticle
    score: float
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.article.id,
            "score": self.score,
            "title": self.article.title,
            "category": self.article.category,
            "market": self.article.market,
            "snippet": self.snippet or self.article.body,
            "customer_safe": self.article.customer_safe,
        }
