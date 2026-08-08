"""Load FAQ articles from config/faq/{market}/*.yaml (+ optional data overlay)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from ecom_ops.faq.models import FaqArticle


def _config_dir() -> Path:
    override = os.environ.get("AZOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"


def _data_faq_dir() -> Path | None:
    base = os.environ.get("AZOM_DATA_DIR")
    if not base:
        return None
    path = Path(base) / "faq"
    return path if path.is_dir() else None


def _parse_article(raw: dict[str, Any], *, source_path: str) -> FaqArticle | None:
    aid = str(raw.get("id") or "").strip()
    if not aid:
        return None
    tags_raw = raw.get("tags") or []
    if isinstance(tags_raw, str):
        tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        tags = ()
    return FaqArticle(
        id=aid,
        market=str(raw.get("market") or "se").strip().lower(),
        language=str(raw.get("language") or "sv").strip().lower(),
        category=str(raw.get("category") or "other").strip().lower(),
        title=str(raw.get("title") or aid).strip(),
        body=str(raw.get("body") or "").strip(),
        tags=tags,
        customer_safe=bool(raw.get("customer_safe", True)),
        updated_at=str(raw.get("updated_at") or ""),
        source_path=source_path,
    )


def _load_yaml_file(path: Path) -> list[FaqArticle]:
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except OSError:
        return []
    rows: list[Any]
    if isinstance(loaded, list):
        rows = loaded
    elif isinstance(loaded, dict) and isinstance(loaded.get("articles"), list):
        rows = loaded["articles"]
    elif isinstance(loaded, dict) and loaded.get("id"):
        rows = [loaded]
    else:
        return []
    out: list[FaqArticle] = []
    for row in rows:
        if isinstance(row, dict):
            art = _parse_article(row, source_path=str(path))
            if art:
                out.append(art)
    return out


def _iter_yaml_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))


class FaqStore:
    """In-memory article index loaded from config (+ optional overlay)."""

    def __init__(self, articles: list[FaqArticle] | None = None) -> None:
        self._by_id: dict[str, FaqArticle] = {}
        if articles is not None:
            for art in articles:
                self._by_id[art.id] = art
        else:
            self.reload()

    def reload(self) -> None:
        by_id: dict[str, FaqArticle] = {}
        roots = [_config_dir() / "faq"]
        overlay = _data_faq_dir()
        if overlay is not None:
            roots.append(overlay)
        for root in roots:
            for path in _iter_yaml_files(root):
                for art in _load_yaml_file(path):
                    by_id[art.id] = art  # overlay wins on same id
        self._by_id = by_id

    def list(
        self,
        *,
        market: str | None = None,
        category: str | None = None,
        customer_safe_only: bool = False,
    ) -> list[FaqArticle]:
        rows = list(self._by_id.values())
        if market:
            m = market.strip().lower()
            rows = [a for a in rows if a.market == m]
        if category:
            c = category.strip().lower()
            rows = [a for a in rows if a.category == c]
        if customer_safe_only:
            rows = [a for a in rows if a.customer_safe]
        rows.sort(key=lambda a: (a.market, a.category, a.id))
        return rows

    def get(self, article_id: str) -> FaqArticle | None:
        return self._by_id.get(article_id)


@lru_cache(maxsize=1)
def default_faq_store() -> FaqStore:
    return FaqStore()


def clear_faq_store_cache() -> None:
    default_faq_store.cache_clear()
