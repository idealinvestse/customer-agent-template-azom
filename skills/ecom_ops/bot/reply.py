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


def telegram_markup_to_actions(markup: dict[str, Any] | None) -> ActionMarkup | None:
    """Reverse-parse Telegram InlineKeyboardMarkup into ActionMarkup."""
    if not markup or not isinstance(markup, dict):
        return None
    rows_out: list[list[ActionButton]] = []
    for row in markup.get("inline_keyboard") or []:
        if not isinstance(row, list):
            continue
        btns: list[ActionButton] = []
        for cell in row:
            if not isinstance(cell, dict):
                continue
            label = str(cell.get("text") or "…")[:64]
            if cell.get("url"):
                btns.append(ActionButton(label=label, url=str(cell["url"])))
            elif cell.get("callback_data"):
                btns.append(ActionButton(label=label, payload=str(cell["callback_data"])))
        if btns:
            rows_out.append(btns)
    return ActionMarkup(rows=rows_out) if rows_out else None


def yes_no_actions(*, yes_data: str, no_data: str) -> ActionMarkup:
    return ActionMarkup(
        rows=[
            [
                ActionButton(label="Ja", payload=yes_data),
                ActionButton(label="Nej", payload=no_data),
            ]
        ]
    )


def yes_no_keyboard(*, yes_data: str, no_data: str) -> dict[str, Any]:
    return actions_to_telegram_markup(
        yes_no_actions(yes_data=yes_data, no_data=no_data)
    ) or inline_keyboard([[("Ja", yes_data), ("Nej", no_data)]])


def approve_case_actions(case_id: str) -> ActionMarkup:
    """Postbacks use id8; dashboard URL prefers full case id when provided."""
    full = str(case_id or "").strip()
    id8 = full[:8]
    rows: list[list[ActionButton]] = [
        [
            ActionButton(label=f"Visa {id8}", payload=f"cases:show:{id8}"),
            ActionButton(label="Godkänn & skicka", payload=f"cases:approve:{id8}"),
        ]
    ]
    dash = case_detail_url(full)
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


def triage_cases_actions(case_id8s: list[str]) -> ActionMarkup | None:
    """Up to 3 suggest cases — one show+approve pair max for Messenger (3-btn cap)."""
    ids = [c for c in case_id8s if c][:3]
    if not ids:
        return None
    rows: list[list[ActionButton]] = []
    # First case gets show+approve; rest show-only to leave room for dashboard URL
    first = ids[0]
    rows.append(
        [
            ActionButton(label=f"Visa {first[:8]}", payload=f"cases:show:{first[:8]}"),
            ActionButton(label="Godkänn", payload=f"cases:approve:{first[:8]}"),
        ]
    )
    for id8 in ids[1:2]:
        rows.append(
            [ActionButton(label=f"Visa {id8[:8]}", payload=f"cases:show:{id8[:8]}")]
        )
    dash = cases_list_url(suggest=True)
    if dash:
        rows.append([ActionButton(label="★ i dashboard", url=dash)])
    return ActionMarkup(rows=rows)


def triage_cases_keyboard(case_id8s: list[str]) -> dict[str, Any] | None:
    actions = triage_cases_actions(case_id8s)
    return actions_to_telegram_markup(actions) if actions else None


def order_status_confirm_actions(order_id: str, status: str) -> ActionMarkup:
    oid = str(order_id)[:12]
    st = str(status)[:20]
    return ActionMarkup(
        rows=[
            [
                ActionButton(label=f"Bekräfta {oid}→{st}"[:20], payload=f"order:set:{oid}:{st}"),
                ActionButton(label="Avbryt", payload="order:cancel"),
            ]
        ]
    )


def order_status_confirm_keyboard(order_id: str, status: str) -> dict[str, Any]:
    return (
        actions_to_telegram_markup(order_status_confirm_actions(order_id, status))
        or inline_keyboard(
            [
                [
                    (f"Bekräfta {order_id[:12]}→{status[:20]}", f"order:set:{order_id[:12]}:{status[:20]}"),
                    ("Avbryt", "order:cancel"),
                ]
            ]
        )
    )


def product_desc_confirm_actions(product_id: str, *, publish: bool = False) -> ActionMarkup:
    pid = str(product_id or "0")[:12]
    flag = "1" if publish else "0"
    return ActionMarkup(
        rows=[
            [
                ActionButton(label=f"Generera {pid}", payload=f"product:desc:{pid}:{flag}"),
                ActionButton(label="Avbryt", payload="product:cancel"),
            ]
        ]
    )


def product_desc_confirm_keyboard(product_id: str, *, publish: bool = False) -> dict[str, Any]:
    return (
        actions_to_telegram_markup(product_desc_confirm_actions(product_id, publish=publish))
        or inline_keyboard(
            [
                [
                    (
                        f"Generera produkt {str(product_id or '0')[:12]}",
                        f"product:desc:{str(product_id or '0')[:12]}:{1 if publish else 0}",
                    ),
                    ("Avbryt", "product:cancel"),
                ]
            ]
        )
    )


def as_reply(value: str | BotReply) -> BotReply:
    if isinstance(value, BotReply):
        if value.actions and not value.reply_markup:
            value.reply_markup = actions_to_telegram_markup(value.actions)
        elif value.reply_markup and not value.actions:
            value.actions = telegram_markup_to_actions(value.reply_markup)
        return value
    return BotReply(text=str(value or ""))


def bot_reply(
    text: str,
    *,
    actions: ActionMarkup | None = None,
    reply_markup: dict[str, Any] | None = None,
    needs_typing: bool = False,
) -> BotReply:
    """Build BotReply with actions and telegram markup kept in sync."""
    if actions and not reply_markup:
        reply_markup = actions_to_telegram_markup(actions)
    elif reply_markup and not actions:
        actions = telegram_markup_to_actions(reply_markup)
    return BotReply(
        text=text,
        actions=actions,
        reply_markup=reply_markup,
        needs_typing=needs_typing,
    )


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
