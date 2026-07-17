"""Structured Telegram bot replies (text + optional inline keyboard)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BotReply:
    """Reply payload for Telegram sendMessage / callback answers."""

    text: str
    reply_markup: dict[str, Any] | None = None
    # When True, main loop should send typing before slow work (set by caller).
    needs_typing: bool = False

    def __str__(self) -> str:
        return self.text

    def __contains__(self, item: object) -> bool:
        return item in self.text

    def lower(self) -> str:
        return self.text.lower()

    def splitlines(self, keepends: bool = False) -> list[str]:
        return self.text.splitlines(keepends=keepends)


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup from (label, callback_data) rows."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def yes_no_keyboard(*, yes_data: str, no_data: str) -> dict[str, Any]:
    return inline_keyboard([[("Ja", yes_data), ("Nej", no_data)]])


def approve_case_keyboard(case_id8: str) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                (f"Visa {case_id8}", f"cases:show:{case_id8}"),
                (f"Godkänn & skicka {case_id8}", f"cases:approve:{case_id8}"),
            ]
        ]
    )


def triage_cases_keyboard(case_id8s: list[str]) -> dict[str, Any] | None:
    """Quick show/approve rows for up to 3 suggest-approve cases."""
    ids = [c for c in case_id8s if c][:3]
    if not ids:
        return None
    rows: list[list[tuple[str, str]]] = []
    for id8 in ids:
        rows.append(
            [
                (f"Visa {id8}", f"cases:show:{id8}"),
                (f"Godkänn {id8}", f"cases:approve:{id8}"),
            ]
        )
    return inline_keyboard(rows)


def order_status_confirm_keyboard(order_id: str, status: str) -> dict[str, Any]:
    """Confirm Woo order status change (never silent)."""
    oid = str(order_id)[:12]
    st = str(status)[:20]
    return inline_keyboard(
        [
            [
                (f"Bekräfta {oid}→{st}", f"order:set:{oid}:{st}"),
                ("Avbryt", "order:cancel"),
            ]
        ]
    )


def product_desc_confirm_keyboard(product_id: str, *, publish: bool = False) -> dict[str, Any]:
    pid = str(product_id or "0")[:12]
    flag = "1" if publish else "0"
    return inline_keyboard(
        [
            [
                (f"Generera produkt {pid}", f"product:desc:{pid}:{flag}"),
                ("Avbryt", "product:cancel"),
            ]
        ]
    )


def as_reply(value: str | BotReply) -> BotReply:
    if isinstance(value, BotReply):
        return value
    return BotReply(text=str(value or ""))


def chunk_text(text: str, *, limit: int = 4000) -> list[str]:
    """Split text into Telegram-safe chunks (prefer newline boundaries)."""
    raw = text or ""
    if len(raw) <= limit:
        return [raw] if raw else [""]
    chunks: list[str] = []
    rest = raw
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks
