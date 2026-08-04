"""Durable marketing suggest queue (JSONL under AZOM_DATA_DIR)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_path() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "marketing_suggests.jsonl"


@dataclass
class MarketingSuggest:
    id: str
    kind: str  # negative | pause | budget | recommendation | mp | merchant
    status: str  # open | approved | denied | applied | failed
    title: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    decided_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketingSuggestStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()

    def _read_all(self) -> list[MarketingSuggest]:
        if not self.path.is_file():
            return []
        out: list[MarketingSuggest] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and raw.get("id"):
                out.append(
                    MarketingSuggest(
                        id=str(raw["id"]),
                        kind=str(raw.get("kind") or ""),
                        status=str(raw.get("status") or "open"),
                        title=str(raw.get("title") or ""),
                        reason=str(raw.get("reason") or ""),
                        evidence=dict(raw.get("evidence") or {}),
                        payload=dict(raw.get("payload") or {}),
                        created_at=str(raw.get("created_at") or ""),
                        updated_at=str(raw.get("updated_at") or ""),
                        decided_by=(
                            str(raw["decided_by"])
                            if raw.get("decided_by")
                            else None
                        ),
                    )
                )
        return out

    def _write_all(self, rows: list[MarketingSuggest]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def add(
        self,
        *,
        kind: str,
        title: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> MarketingSuggest:
        now = _now()
        item = MarketingSuggest(
            id=str(uuid.uuid4()),
            kind=kind,
            status="open",
            title=title,
            reason=reason,
            evidence=evidence or {},
            payload=payload or {},
            created_at=now,
            updated_at=now,
        )
        rows = self._read_all()
        rows.append(item)
        self._write_all(rows)
        return item

    def list(
        self, *, status: str | None = "open", limit: int = 50
    ) -> list[MarketingSuggest]:
        rows = self._read_all()
        if status and status != "all":
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def get(self, suggest_id: str) -> MarketingSuggest | None:
        for row in self._read_all():
            if row.id == suggest_id:
                return row
        return None

    def set_status(
        self,
        suggest_id: str,
        status: str,
        *,
        actor: str | None = None,
    ) -> MarketingSuggest | None:
        rows = self._read_all()
        found: MarketingSuggest | None = None
        for i, row in enumerate(rows):
            if row.id == suggest_id:
                row.status = status
                row.updated_at = _now()
                row.decided_by = actor
                rows[i] = row
                found = row
                break
        if found:
            self._write_all(rows)
        return found
