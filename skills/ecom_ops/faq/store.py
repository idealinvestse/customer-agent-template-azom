"""Load FAQ articles from config/faq/{market}/*.yaml (+ optional data overlay)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from ecom_ops.faq.models import FaqArticle

_ID_RE = re.compile(r"^(se|no|dk)-[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class FaqValidationIssue:
    level: str  # error | warning
    message: str
    path: str = ""
    article_id: str = ""


@dataclass
class FaqLoadReport:
    articles: list[FaqArticle] = field(default_factory=list)
    issues: list[FaqValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[FaqValidationIssue]:
        return [i for i in self.issues if i.level == "error"]


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


def _market_from_path(path: Path) -> str | None:
    # .../faq/{market}/file.yaml
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "faq" and i + 1 < len(parts):
            cand = parts[i + 1].lower()
            if cand in {"se", "no", "dk"}:
                return cand
    return None


def _parse_article(
    raw: dict[str, Any],
    *,
    source_path: str,
    path_market: str | None,
) -> tuple[FaqArticle | None, list[FaqValidationIssue]]:
    issues: list[FaqValidationIssue] = []
    aid = str(raw.get("id") or "").strip()
    if not aid:
        issues.append(
            FaqValidationIssue("error", "missing id", path=source_path)
        )
        return None, issues
    if not _ID_RE.match(aid):
        issues.append(
            FaqValidationIssue(
                "warning",
                f"id {aid!r} should match {{se|no|dk}}-slug",
                path=source_path,
                article_id=aid,
            )
        )
    tags_raw = raw.get("tags") or []
    if isinstance(tags_raw, str):
        tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        tags = ()
    market = str(raw.get("market") or path_market or "se").strip().lower()
    body = str(raw.get("body") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not body:
        issues.append(
            FaqValidationIssue(
                "error", "empty body", path=source_path, article_id=aid
            )
        )
    if not title:
        issues.append(
            FaqValidationIssue(
                "error", "empty title", path=source_path, article_id=aid
            )
        )
        title = aid
    if path_market and market != path_market:
        issues.append(
            FaqValidationIssue(
                "error",
                f"market={market} does not match path market={path_market}",
                path=source_path,
                article_id=aid,
            )
        )
    if aid.startswith(("se-", "no-", "dk-")) and not aid.startswith(f"{market}-"):
        issues.append(
            FaqValidationIssue(
                "warning",
                f"id prefix does not match market={market}",
                path=source_path,
                article_id=aid,
            )
        )
    if issues and any(i.level == "error" for i in issues):
        return None, issues
    art = FaqArticle(
        id=aid,
        market=market,
        language=str(raw.get("language") or "sv").strip().lower(),
        category=str(raw.get("category") or "other").strip().lower(),
        title=title,
        body=body,
        tags=tags,
        customer_safe=bool(raw.get("customer_safe", True)),
        updated_at=str(raw.get("updated_at") or ""),
        source_path=source_path,
    )
    return art, issues


def _load_yaml_file(path: Path) -> tuple[list[FaqArticle], list[FaqValidationIssue]]:
    issues: list[FaqValidationIssue] = []
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except OSError as exc:
        issues.append(
            FaqValidationIssue("error", f"read failed: {exc}", path=str(path))
        )
        return [], issues
    rows: list[Any]
    if isinstance(loaded, list):
        rows = loaded
    elif isinstance(loaded, dict) and isinstance(loaded.get("articles"), list):
        rows = loaded["articles"]
    elif isinstance(loaded, dict) and loaded.get("id"):
        rows = [loaded]
    else:
        issues.append(
            FaqValidationIssue(
                "warning", "no articles found", path=str(path)
            )
        )
        return [], issues
    path_market = _market_from_path(path)
    out: list[FaqArticle] = []
    for row in rows:
        if isinstance(row, dict):
            art, art_issues = _parse_article(
                row, source_path=str(path), path_market=path_market
            )
            issues.extend(art_issues)
            if art:
                out.append(art)
    return out, issues


def _iter_yaml_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))


def load_faq_corpus() -> FaqLoadReport:
    """Load + validate corpus; reject error articles; warn on id collisions."""
    report = FaqLoadReport()
    by_id: dict[str, FaqArticle] = {}
    roots = [_config_dir() / "faq"]
    overlay = _data_faq_dir()
    if overlay is not None:
        roots.append(overlay)
    for root in roots:
        for path in _iter_yaml_files(root):
            arts, issues = _load_yaml_file(path)
            report.issues.extend(issues)
            for art in arts:
                if art.id in by_id and by_id[art.id].source_path != art.source_path:
                    # Overlay may intentionally replace — warn only when same root class
                    prev = by_id[art.id]
                    if Path(prev.source_path).parent == Path(art.source_path).parent:
                        report.issues.append(
                            FaqValidationIssue(
                                "error",
                                f"duplicate id {art.id!r} in same market tree "
                                f"({prev.source_path} vs {art.source_path})",
                                path=art.source_path,
                                article_id=art.id,
                            )
                        )
                        continue
                    report.issues.append(
                        FaqValidationIssue(
                            "warning",
                            f"id {art.id!r} overwritten by overlay/later file",
                            path=art.source_path,
                            article_id=art.id,
                        )
                    )
                by_id[art.id] = art
    report.articles = list(by_id.values())
    return report


class FaqStore:
    """In-memory article index loaded from config (+ optional overlay)."""

    def __init__(self, articles: list[FaqArticle] | None = None) -> None:
        self._by_id: dict[str, FaqArticle] = {}
        self.last_issues: list[FaqValidationIssue] = []
        if articles is not None:
            for art in articles:
                self._by_id[art.id] = art
        else:
            self.reload()

    def reload(self) -> FaqLoadReport:
        report = load_faq_corpus()
        self._by_id = {a.id: a for a in report.articles}
        self.last_issues = list(report.issues)
        return report

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

    def coverage(self) -> dict[str, Any]:
        """Article counts per market/category for ops reporting."""
        out: dict[str, Any] = {"markets": {}, "total": len(self._by_id)}
        for art in self._by_id.values():
            m = out["markets"].setdefault(
                art.market, {"total": 0, "categories": {}, "stubs": 0}
            )
            m["total"] += 1
            m["categories"][art.category] = m["categories"].get(art.category, 0) + 1
            if "stub" in art.body.lower() or "stub" in art.title.lower():
                m["stubs"] += 1
        return out


@lru_cache(maxsize=1)
def default_faq_store() -> FaqStore:
    return FaqStore()


def clear_faq_store_cache() -> None:
    default_faq_store.cache_clear()


def reload_faq_store() -> FaqStore:
    """Force reload corpus for long-lived dashboard/poll workers."""
    from ecom_ops.faq.config import clear_faq_config_cache

    clear_faq_config_cache()
    clear_faq_store_cache()
    store = default_faq_store()
    store.reload()
    return store
