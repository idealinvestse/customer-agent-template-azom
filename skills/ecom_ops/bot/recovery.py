"""Recovery footers and keyboards — never leave the operator without a next step."""

from __future__ import annotations

from typing import Any

from ecom_ops.bot.reply import BotReply, inline_keyboard


# --- Standard footers -------------------------------------------------------

FOOTER_EMPTY_QUEUE = (
    "Nästa steg: vänta på nästa poll (~5 min), "
    "öppna dashboard /cases, eller /help."
)
FOOTER_CASE_NOT_FOUND = (
    "Nästa steg: /cases · /cases show <id8> · /help"
)
FOOTER_ORDER_FAIL = (
    "Nästa steg: /order <nummer> · /cases · /help"
)
FOOTER_DENY_ALLOWLIST = (
    "Nästa steg: be Oscar uppdatera TELEGRAM_ALLOWED_CHAT_IDS."
)
FOOTER_DENY_ACTOR = (
    "Nästa steg: be Oscar lägga till dig i TELEGRAM_ACTOR_MAP "
    "(t.ex. <chat_id>:jonatan)."
)
FOOTER_APPROVE_FAIL = (
    "Nästa steg: regenerera utkast, visa ärendet, eller /cases."
)
FOOTER_UNKNOWN_CALLBACK = "Nästa steg: /cases · /order · /help"
FOOTER_GENERIC = "Nästa steg: /cases · /order · /help"


def with_recovery(text: str, *, footer: str = FOOTER_GENERIC) -> str:
    """Append a recovery footer if not already present."""
    body = (text or "").rstrip()
    foot = (footer or "").strip()
    if not foot:
        return body
    if foot in body:
        return body
    if not body:
        return foot
    return f"{body}\n\n{foot}"


def deny_allowlist_reply(*, channel: str = "telegram") -> BotReply:
    if channel == "messenger":
        return BotReply(
            text=with_recovery(
                "Du är inte behörig att använda denna Messenger-bot. "
                "Be Oscar lägga till din PSID i MESSENGER_ALLOWED_PSIDS.",
                footer=(
                    "Nästa steg: be Oscar uppdatera MESSENGER_ALLOWED_PSIDS."
                ),
            )
        )
    return BotReply(
        text=with_recovery(
            "Du är inte behörig att använda denna bot. "
            "Be Oscar lägga till din chat-id i TELEGRAM_ALLOWED_CHAT_IDS.",
            footer=FOOTER_DENY_ALLOWLIST,
        )
    )


def deny_actor_reply(*, channel: str = "telegram") -> BotReply:
    if channel == "messenger":
        return BotReply(
            text=with_recovery(
                "Din Messenger-chat saknar actor-mapping. "
                "Be Oscar lägga till dig i MESSENGER_ACTOR_MAP "
                "(t.ex. <psid>:jonatan).",
                footer=(
                    "Nästa steg: be Oscar lägga till dig i MESSENGER_ACTOR_MAP."
                ),
            )
        )
    return BotReply(
        text=with_recovery(
            "Din chat saknar actor-mapping. "
            "Be Oscar lägga till dig i TELEGRAM_ACTOR_MAP "
            "(t.ex. <chat_id>:jonatan).",
            footer=FOOTER_DENY_ACTOR,
        )
    )


def empty_queue_text(*, suggest_only: bool = False) -> str:
    if suggest_only:
        head = "Inga ★-föreslagna ärenden just nu."
    else:
        head = "Inga öppna/eskalerade ärenden."
    return with_recovery(head, footer=FOOTER_EMPTY_QUEUE)


def case_not_found_text(id_hint: str) -> str:
    return with_recovery(
        f"Hittade inte case {id_hint!r}",
        footer=FOOTER_CASE_NOT_FOUND,
    )


def approve_fail_keyboard(case_id8: str) -> dict[str, Any]:
    """Regenerera / Visa / Lista after approve failure."""
    id8 = str(case_id8 or "")[:8]
    rows: list[list[tuple[str, str]]] = [
        [
            (f"Regenerera {id8}", f"cases:regen:{id8}"),
            (f"Visa {id8}", f"cases:show:{id8}"),
        ],
        [("Lista ärenden", "cases:list")],
    ]
    return inline_keyboard(rows)


def approve_fail_reply(case_id: str, message: str) -> BotReply:
    """Approve failure with recovery actions (max 3 for Messenger: regen, show, dashboard)."""
    from ecom_ops.bot.dashboard_links import case_detail_url, link_footer
    from ecom_ops.bot.reply import ActionButton, ActionMarkup, actions_to_telegram_markup

    full = str(case_id or "").strip()
    id8 = full[:8]
    rows: list[list[ActionButton]] = [
        [
            ActionButton(label="Regenerera", payload=f"cases:regen:{id8}"),
            ActionButton(label=f"Visa {id8}", payload=f"cases:show:{id8}"),
        ],
    ]
    dash = case_detail_url(full) if full else None
    if dash:
        rows.append([ActionButton(label="Redigera i dashboard", url=dash)])
    else:
        rows.append([ActionButton(label="Lista ärenden", payload="cases:list")])
    actions = ActionMarkup(rows=rows) if id8 else None
    text = with_recovery(
        f"Misslyckades: {message}",
        footer=FOOTER_APPROVE_FAIL,
    )
    if dash:
        # Ensure text footer if URL button is truncated by Messenger
        foot = link_footer(dash, label="Redigera i dashboard")
        if foot and foot not in text:
            text = f"{text}\n\n{foot}"
    return BotReply(
        text=text,
        actions=actions,
        reply_markup=actions_to_telegram_markup(actions) if actions else None,
    )


def approve_success_reply(
    case_id: str,
    *,
    next_case_id: str | None = None,
) -> BotReply:
    """After send: next approve/show + dashboard URL (prioritized for Messenger 3-cap)."""
    from ecom_ops.bot.dashboard_links import case_detail_url, link_footer
    from ecom_ops.bot.reply import ActionButton, ActionMarkup, actions_to_telegram_markup

    full = str(case_id or "").strip()
    id8 = full[:8]
    nxt_full = str(next_case_id or "").strip()
    nid = nxt_full[:8] if nxt_full else ""
    next_line = ""
    if nid:
        next_line = f"\nNästa i kö: {nid} — Visa eller Godkänn & nästa."
    text = f"Skickat. Case {id8} → replied.{next_line}"
    if nid:
        rows: list[list[ActionButton]] = [
            [
                ActionButton(label=f"Visa {nid}", payload=f"cases:show:{nid}"),
                ActionButton(label="Godkänn & nästa", payload=f"cases:approve:{nid}"),
            ],
        ]
        dash = case_detail_url(nxt_full)
        if dash:
            rows.append([ActionButton(label="Öppna i dashboard", url=dash)])
        actions = ActionMarkup(rows=rows)
        return BotReply(
            text=text,
            actions=actions,
            reply_markup=actions_to_telegram_markup(actions),
        )
    dash = case_detail_url(full)
    foot = link_footer(dash)
    if foot:
        text = f"{text}\n\n{foot}"
    return BotReply(text=text)


def continuity_fallback(
    *,
    prior_digest: str = "",
    sticky_order_id: str | None = None,
    sticky_case_id8: str | None = None,
    reason: str = "no_key",
) -> str | None:
    """Build a continuity reply when LLM is unavailable but context exists.

    Returns None when there is nothing sticky/digest to continue from
    (caller should use the generic FALLBACK_* string).
    """
    digest = (prior_digest or "").strip()
    sticky_bits: list[str] = []
    if sticky_order_id:
        sticky_bits.append(f"order {sticky_order_id}")
    if sticky_case_id8:
        sticky_bits.append(f"ärende {sticky_case_id8}")
    if not digest and not sticky_bits:
        return None

    if reason == "budget":
        head = (
            "OpenRouter-budgeten är slut just nu — "
            "fortsätter med sparad kontext (utan LLM)."
        )
    elif reason == "error":
        head = (
            "Kunde inte nå LLM just nu — "
            "fortsätter med sparad kontext."
        )
    else:
        head = (
            "LLM är inte kopplad just nu — "
            "fortsätter med sparad kontext."
        )

    parts = [head]
    if sticky_bits:
        parts.append("Sticky: " + ", ".join(sticky_bits))
    if digest:
        parts.append(f"Senaste tool-digest:\n{digest[:800]}")
    parts.append(
        "Fråga vidare om order/ärende, eller /order · /cases · /help."
    )
    return "\n\n".join(parts)
