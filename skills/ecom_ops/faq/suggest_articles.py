"""Suggest FAQ article drafts into staging YAML (template-first, HITL)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from ecom_ops.faq.ingest_config import (
    effective_use_mock,
    faq_ingest_killed,
    load_faq_ingest_config,
)
from ecom_ops.faq.pii import redact_pii
from ecom_ops.faq.rbac_ingest import require_faq_ingest
from ecom_ops.faq.staging import (
    articles_staging_dir,
    data_dir,
    products_staging_dir,
    site_staging_dir,
)
from ecom_ops.rbac import AccessDenied, Actor

SourceKind = Literal["staging", "dataset", "products"]

_FORBIDDEN = re.compile(
    r"(?i)\b("
    r"vi\s+återbetalar|"
    r"we\s+refund|"
    r"full\s+refund|"
    r"pengarna\s+tillbaka|"
    r"garanti\s+på\s+pengarna|"
    r"vi\s+garanterar|"
    r"money[- ]back|"
    r"full\s+refund\s+guaranteed"
    r")\b"
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, *, max_len: int = 48) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s or "draft")[:max_len].strip("-")


def _sanitize_body(text: str) -> str:
    body = (text or "").strip()
    if _FORBIDDEN.search(body):
        body = _FORBIDDEN.sub("[REDACTED_PROMISE]", body)
        body += (
            "\n\nObs: Genererat utkast — lova aldrig återbetalning eller garanti "
            "utan manuell granskning."
        )
    return body


def _write_draft(path: Path, article: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump([article], allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@dataclass
class SuggestResult:
    ok: bool
    message: str
    written: int = 0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "written": self.written,
            "details": self.details or {},
        }


def _from_products(market: str, out_dir: Path, *, limit: int) -> int:
    src = products_staging_dir(market)
    written = 0
    for path in sorted(src.glob("product_*.json")):
        if written >= limit:
            break
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(doc.get("name") or "Produkt").strip()
        cat = str(doc.get("faq_category") or "product")
        body_parts = [
            str(doc.get("short_description") or "").strip(),
            str(doc.get("description") or "").strip(),
        ]
        body = _sanitize_body("\n\n".join(p for p in body_parts if p))
        if len(body) < 40:
            continue
        aid = f"{market}-{_slug(name)}"
        article = {
            "id": aid,
            "market": market,
            "language": {"se": "sv", "no": "nb", "dk": "da"}.get(market, "sv"),
            "category": cat,
            "title": name[:120],
            "tags": [cat, "ingest", "product"],
            "customer_safe": False,
            "needs_review": True,
            "updated_at": _now()[:10],
            "sources": [
                {
                    "type": "woo_product",
                    "id": doc.get("id"),
                    "sku": doc.get("sku"),
                    "permalink": doc.get("permalink"),
                }
            ],
            "body": body,
        }
        _write_draft(out_dir / f"{aid}.yaml", article)
        written += 1
    return written


def _from_site(market: str, out_dir: Path, *, limit: int) -> int:
    src = site_staging_dir(market)
    written = 0
    for path in sorted(src.glob("*.json")):
        if path.name.startswith("_"):
            continue
        if written >= limit:
            break
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        title = str(doc.get("title") or "Sida").strip()
        text = _sanitize_body(str(doc.get("text") or ""))
        if len(text) < 40:
            continue
        aid = f"{market}-site-{_slug(title)}"
        article = {
            "id": aid,
            "market": market,
            "language": {"se": "sv", "no": "nb", "dk": "da"}.get(market, "sv"),
            "category": "other",
            "title": title[:120],
            "tags": ["ingest", "site", str(doc.get("type") or "page")],
            "customer_safe": False,
            "needs_review": True,
            "updated_at": _now()[:10],
            "sources": [
                {
                    "type": "wp_" + str(doc.get("type") or "page"),
                    "id": doc.get("id"),
                    "url": doc.get("url"),
                }
            ],
            "body": text[:4000],
        }
        _write_draft(out_dir / f"{aid}.yaml", article)
        written += 1
    return written


def _from_dataset(market: str, out_dir: Path, *, limit: int) -> int:
    """Build article stubs from non-stale Q&A pairs (redacted exports)."""
    cfg = load_faq_ingest_config()
    ds_dir = data_dir() / "faq_dataset"
    if not ds_dir.is_dir():
        return 0
    written = 0
    files = [
        p
        for p in (
            list(ds_dir.glob(f"qa_{market}_*.jsonl"))
            + list(ds_dir.glob("qa_all_*.jsonl"))
        )
        if p.is_file() and not p.name.endswith(".raw.jsonl")
    ]
    seen_q: set[str] = set()
    for fpath in files:
        if written >= limit:
            break
        man = fpath.with_name(fpath.name.replace(".jsonl", ".manifest.json"))
        if man.is_file():
            try:
                meta = json.loads(man.read_text(encoding="utf-8"))
                if meta.get("redacted") is False:
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        try:
            lines = fpath.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if written >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("stale"):
                continue
            row_m = (row.get("market") or "").lower()
            if row_m and row_m != market:
                continue
            q = redact_pii(str(row.get("question") or "").strip())
            a = redact_pii(str(row.get("answer") or "").strip())
            if len(a) < cfg.dataset_min_reply_chars or len(q) < 10:
                continue
            qkey = q.lower()[:120]
            if qkey in seen_q:
                continue
            seen_q.add(qkey)
            cat = str(row.get("category") or "other")
            aid = f"{market}-qa-{_slug(q[:40])}"
            body = _sanitize_body(
                f"**Fråga (redigerad):** {q[:500]}\n\n**Svar (utkast):** {a[:3000]}\n\n"
                "Obs: Baserat på historiskt case — granska policy innan publish."
            )
            article = {
                "id": aid,
                "market": market,
                "language": str(row.get("language") or "sv"),
                "category": cat,
                "title": q[:120],
                "tags": ["ingest", "dataset", cat],
                "customer_safe": False,
                "needs_review": True,
                "updated_at": _now()[:10],
                "sources": [
                    {
                        "type": "case_qa",
                        "case_id": row.get("case_id"),
                        "answered_at": row.get("answered_at"),
                    }
                ],
                "body": body,
            }
            _write_draft(out_dir / f"{aid}.yaml", article)
            written += 1
    return written


def suggest_articles(
    *,
    market: str = "se",
    source: SourceKind = "products",
    limit: int = 50,
    actor: Actor | str | None = None,
    use_mock: bool | None = False,
) -> SuggestResult:
    mock = effective_use_mock(use_mock)
    try:
        actor_obj = require_faq_ingest(actor, use_mock=mock)
    except AccessDenied as exc:
        return SuggestResult(ok=False, message=str(exc))

    if faq_ingest_killed():
        return SuggestResult(ok=False, message="FAQ ingest kill-switch active")

    cfg = load_faq_ingest_config()
    if cfg.llm_suggest_enabled:
        # Template-first path remains; LLM polish is opt-in later.
        pass

    domain = market.strip().lower()
    if domain not in {"se", "no", "dk"}:
        return SuggestResult(ok=False, message=f"Unsupported market: {market}")

    out_dir = articles_staging_dir(domain)
    if source == "products":
        n = _from_products(domain, out_dir, limit=limit)
    elif source == "staging":
        n = _from_site(domain, out_dir, limit=limit)
    elif source == "dataset":
        n = _from_dataset(domain, out_dir, limit=limit)
    else:
        return SuggestResult(ok=False, message=f"Unknown source: {source}")

    return SuggestResult(
        ok=True,
        message=f"Suggested {n} article drafts from {source} → {out_dir}",
        written=n,
        details={
            "dir": str(out_dir),
            "source": source,
            "actor": actor_obj.name,
            "llm_suggest_enabled": cfg.llm_suggest_enabled,
        },
    )
