"""Paths and counts for FAQ staging under AZOM_DATA_DIR."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def staging_root() -> Path:
    path = data_dir() / "faq_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def site_staging_dir(domain: str) -> Path:
    path = staging_root() / "site" / domain.strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def products_staging_dir(market: str) -> Path:
    path = staging_root() / "products" / market.strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def guides_staging_dir(market: str) -> Path:
    path = staging_root() / "guides" / market.strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def articles_staging_dir(market: str) -> Path:
    path = staging_root() / "articles" / market.strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_counts(market: str | None = None) -> dict[str, int]:
    """Count staged files for dashboard / CLI."""
    root = staging_root()
    markets = [market.strip().lower()] if market else ["se", "no", "dk"]
    site = 0
    for dom in markets:
        d = root / "site" / dom
        if d.is_dir():
            site += sum(
                1
                for p in d.glob("*.json")
                if p.is_file() and not p.name.startswith("_")
            )
    products = 0
    guides = 0
    articles = 0
    for mkt in markets:
        pd = root / "products" / mkt
        if pd.is_dir():
            products += sum(1 for p in pd.glob("*.json") if p.is_file())
        gd = root / "guides" / mkt
        if gd.is_dir():
            guides += sum(1 for p in gd.glob("*.json") if p.is_file())
        ad = root / "articles" / mkt
        if ad.is_dir():
            articles += sum(1 for p in ad.glob("*.yaml") if p.is_file())
            articles += sum(1 for p in ad.glob("*.yml") if p.is_file())
    dataset_dir = data_dir() / "faq_dataset"
    datasets = 0
    if dataset_dir.is_dir():
        datasets = sum(
            1
            for p in dataset_dir.glob("*.jsonl")
            if p.is_file() and not p.name.endswith(".raw.jsonl")
        )
    return {
        "site_docs": site,
        "product_docs": products,
        "guide_docs": guides,
        "article_drafts": articles,
        "dataset_files": datasets,
    }


def list_article_drafts(market: str) -> list[dict[str, Any]]:
    """Staging YAML drafts for Oscar HITL (id, title, source, preview)."""
    out: list[dict[str, Any]] = []
    d = articles_staging_dir(market)
    paths = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if not isinstance(raw, dict):
            continue
        sources = raw.get("sources") or []
        src_type = ""
        if isinstance(sources, list) and sources and isinstance(sources[0], dict):
            src_type = str(sources[0].get("type") or "")
        body = str(raw.get("body") or "")
        out.append(
            {
                "id": str(raw.get("id") or path.stem),
                "title": str(raw.get("title") or path.stem),
                "category": str(raw.get("category") or ""),
                "source": src_type,
                "path": str(path),
                "customer_safe": bool(raw.get("customer_safe", False)),
                "needs_review": bool(raw.get("needs_review", True)),
                "body_preview": body[:240],
                "body": body,
            }
        )
    return out
