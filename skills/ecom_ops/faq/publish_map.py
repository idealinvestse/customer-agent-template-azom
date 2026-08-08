"""SQLite map of FAQ → WordPress page ids under AZOM_DATA_DIR."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_path() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "faq_publish.db"


@dataclass(frozen=True)
class FaqPublishRow:
    market: str
    article_id: str  # "" for parent page
    wp_page_id: int
    status: str
    content_hash: str
    link: str | None
    updated_at: str


class FaqPublishMap:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS faq_publish (
                    market TEXT NOT NULL,
                    article_id TEXT NOT NULL DEFAULT '',
                    wp_page_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    content_hash TEXT NOT NULL DEFAULT '',
                    link TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, article_id)
                )
                """
            )

    def get(self, market: str, article_id: str = "") -> FaqPublishRow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM faq_publish WHERE market = ? AND article_id = ?",
                (market.lower(), article_id),
            ).fetchone()
        if row is None:
            return None
        return FaqPublishRow(
            market=row["market"],
            article_id=row["article_id"],
            wp_page_id=int(row["wp_page_id"]),
            status=row["status"],
            content_hash=row["content_hash"],
            link=row["link"],
            updated_at=row["updated_at"],
        )

    def upsert(
        self,
        *,
        market: str,
        article_id: str,
        wp_page_id: int,
        status: str,
        content_hash: str,
        link: str | None = None,
    ) -> FaqPublishRow:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO faq_publish
                    (market, article_id, wp_page_id, status, content_hash, link, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, article_id) DO UPDATE SET
                    wp_page_id = excluded.wp_page_id,
                    status = excluded.status,
                    content_hash = excluded.content_hash,
                    link = excluded.link,
                    updated_at = excluded.updated_at
                """,
                (
                    market.lower(),
                    article_id,
                    int(wp_page_id),
                    status,
                    content_hash,
                    link,
                    now,
                ),
            )
        return FaqPublishRow(
            market=market.lower(),
            article_id=article_id,
            wp_page_id=int(wp_page_id),
            status=status,
            content_hash=content_hash,
            link=link,
            updated_at=now,
        )

    def list_market(self, market: str) -> list[FaqPublishRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM faq_publish WHERE market = ? ORDER BY article_id",
                (market.lower(),),
            ).fetchall()
        return [
            FaqPublishRow(
                market=r["market"],
                article_id=r["article_id"],
                wp_page_id=int(r["wp_page_id"]),
                status=r["status"],
                content_hash=r["content_hash"],
                link=r["link"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]
