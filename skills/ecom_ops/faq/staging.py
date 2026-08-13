"""Paths and counts for FAQ staging under AZOM_DATA_DIR."""

from __future__ import annotations

import os
from pathlib import Path


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
