"""Structured bot replies — channel-neutral actions + Telegram markup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ecom_ops.bot.dashboard_links import case_detail_url, cases_list_url, link_footer


@dataclass
class ActionButton:
    """Postback button (payload) or URL handoff (url). Exactly one of payload/url."""

    label: str
    payload: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        has_p = bool(self.payload)
        has_u = bool(self.url)
        if has_p == has_u:
            raise ValueError("ActionButton requires exactly one of payload or url")


@dataclass
class ActionMarkup:
    rows: list[list[ActionButton]] = field(default_factory=list)


@dataclass
class BotReply:
    """Reply payload for chat transports (Telegram / Messenger)."""

    text: str
    reply_markup: dict[str, Any] | None = None
    actions: ActionMarkup | None = None
    needs_typing: bool = False

    def __str__(self) -> str:
        return self.text

    def __contains__(self, item: object) -> bool:
        return item in self.text

    def lower(self) -> str:
        return self.text.lower()

    def splitlines(self, keepends: bool = False) -> list[str]:
        return self.text.splitlines(keepends=keepends)


def actions_to_telegram_markup(actions: ActionMarkup | None) -> dict[str, Any] | None:
    """Render ActionMarkup as Telegram InlineKeyboardMarkup (postbacks only + URL as url buttons)."""
    if not actions or not actions.rows:
        return None
    rows: list[list[dict[str, str]]] = []
    for row in actions.rows:
        tg_row: list[dict[str, str]] = []
        for btn in row:
            if btn.url:
                tg_row.append({"text": btn.label, "url": btn.url})
            elif btn.payload:
                tg_row.append({"text": btn.label, "callback_data": btn.payload})
        if tg_row:
            rows.append(tg_row)
    if not rows:
        return None
    return {"inline_keyboard": rows}


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup from (label, callback_data) rows."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def markup_from_postback_rows(rows: list[list[tuple[str, str]]]) -> tuple[ActionMarkup, dict[str, Any]]:
    """Convenience: postback-only rows → actions + telegram markup."""
    actions = ActionMarkup(
        rows=[
            [ActionButton(label=lab, payload=data) for lab, data in row]
            for row in rows
        ]
    )
    return actions, inline_keyboard(rows)


def yes_no_keyboard(*, yes_data: str, no_data: str) -> dict[str, Any]:
    return inline_keyboard([[("Ja", yes_data), ("Nej", no_data)]])


def approve_case_actions(case_id8: str) -> ActionMarkup:
    id8 = str(case_id8)[:8]
    rows: list[list[ActionButton]] = [
        [
            ActionButton(label=f"Visa {id8}", payload=f"cases:show:{id8}"),
            ActionButton(label=f"Godkänn & skicka {id8}", payload=f"cases:approve:{id8}"),
        ]
    ]
    dash = case_detail_url(id8)
    if dash:
        rows.append([ActionButton(label="Öppna i dashboard", url=dash)])
    return ActionMarkup(rows=rows)


def approve_case_keyboard(case_id8: str) -> dict[str, Any]:
    return actions_to_telegram_markup(approve_case_actions(case_id8)) or inline_keyboard(
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
    markup = inline_keyboard(rows)
    dash = cases_list_url(suggest=True)
    if dash:
        markup["inline_keyboard"].append([{"text": "Öppna ★ i dashboard", "url": dash}])
    return markup


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
        # Ensure telegram markup from actions when missing
        if value.actions and not value.reply_markup:
            value.reply_markup = actions_to_telegram_markup(value.actions)
        return value
    return BotReply(text=str(value or ""))


def with_dashboard_footer(text: str, url: str | None) -> str:
    foot = link_footer(url)
    if not foot:
        return text
    if foot in text:
        return text
    return f"{text.rstrip()}\n\n{foot}"


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
