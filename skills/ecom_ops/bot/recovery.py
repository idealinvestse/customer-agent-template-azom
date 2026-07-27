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


def deny_allowlist_reply() -> BotReply:
    return BotReply(
        text=with_recovery(
            "Du är inte behörig att använda denna bot. "
            "Be Oscar lägga till din chat-id i TELEGRAM_ALLOWED_CHAT_IDS.",
            footer=FOOTER_DENY_ALLOWLIST,
        )
    )


def deny_actor_reply() -> BotReply:
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


def approve_fail_reply(case_id8: str, message: str) -> BotReply:
    id8 = str(case_id8 or "")[:8]
    return BotReply(
        text=with_recovery(
            f"Misslyckades: {message}",
            footer=FOOTER_APPROVE_FAIL,
        ),
        reply_markup=approve_fail_keyboard(id8) if id8 else None,
    )


def approve_success_keyboard(
    *,
    next_id8: str | None = None,
) -> dict[str, Any] | None:
    """After successful send: optional Visa nästa / Godkänn & nästa."""
    nid = str(next_id8 or "")[:8]
    if not nid:
        return None
    return inline_keyboard(
        [
            [
                (f"Visa nästa {nid}", f"cases:show:{nid}"),
                (f"Godkänn & nästa {nid}", f"cases:approve:{nid}"),
            ],
            [("Lista ärenden", "cases:list")],
        ]
    )


def approve_success_reply(
    case_id8: str,
    *,
    next_id8: str | None = None,
) -> BotReply:
    id8 = str(case_id8 or "")[:8]
    next_line = ""
    if next_id8:
        next_line = f"\nNästa i kö: {next_id8[:8]} — Visa eller Godkänn & nästa."
    return BotReply(
        text=f"Skickat. Case {id8} → replied.{next_line}",
        reply_markup=approve_success_keyboard(next_id8=next_id8),
    )


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
