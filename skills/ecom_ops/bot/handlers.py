"""Telegram message handlers — OpenClaw-style multi-turn + write rails."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ecom_ops.bot.actors import (
    ChannelActorDenied,
    TelegramActorDenied,
    channel_peer_allowed,
    messenger_mutations_allowed,
    resolve_channel_actor,
)
from ecom_ops.bot.catalog import execute_page_update, execute_product_update
from ecom_ops.bot.chat_agent import (
    SOFT_ESCALATE_NUDGE,
    ChatResult,
    run_chat,
    tool_lookup_order,
    wants_escalate,
    wants_hard_escalate_confirm,
)
from ecom_ops.bot.dashboard_links import review_plan_url
from ecom_ops.bot.dialog_actions import (
    PendingAction,
    execute_case_regenerate,
    execute_order_status,
    execute_product_desc,
)
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.recovery import (
    FOOTER_UNKNOWN_CALLBACK,
    deny_actor_reply,
    deny_allowlist_reply,
    with_recovery,
)
from ecom_ops.bot.reply import (
    ActionButton,
    ActionMarkup,
    BotReply,
    approve_case_actions,
    as_reply,
    bot_reply,
    order_status_confirm_actions,
    product_desc_confirm_actions,
    safe_workspace_attachments,
    triage_cases_actions,
    yes_no_actions,
)
from ecom_ops.bot.site_content import (
    execute_plugin_status,
    execute_post_update,
    execute_site_update,
)
from ecom_ops.bot.store import ConversationStore, clamp_messages
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.rbac import Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import validate_order_id
from ecom_ops.wp_ops.agent_tools import WpAgentTools, context_for_storage, context_matches

ORDER_FAST_RE = re.compile(r"^\s*(?:order|ordernr|#)\s*(\d{4,12})\s*$", re.I)
_FILE_PLAN_ID = re.compile(r"fe-[a-f0-9]{12}")
_REVIEW_PLAN_ID = re.compile(r"wr-[a-f0-9]{12}")
_MAX_REVIEW_HASH_CACHE = 8


def _wpreview_stage_actions(
    plan_id: str, *, review_url: str | None = None
) -> ActionMarkup:
    rows: list[list[ActionButton]] = [
        [
            ActionButton(
                label="Stagea ändringar till staging",
                payload=f"wpreview:stage:{plan_id}",
            ),
            ActionButton(label="Avbryt", payload=f"wpreview:no:{plan_id}"),
        ]
    ]
    if review_url:
        rows.append([ActionButton(label="Öppna granskning", url=review_url)])
    return ActionMarkup(rows=rows)


def _review_plan_ready(
    plans: Any,
    plan: dict[str, Any],
    *,
    changed_count: int | None = None,
) -> tuple[bool, str]:
    status = str(plan.get("status") or "")
    if status == "staged":
        return False, "redan i staging"
    if status in {
        "expired",
        "rejected",
        "denied",
        "fork_not_approved",
        "not_resumable",
    }:
        return False, status
    if plan.get("superseded_by"):
        return False, "ersatt av nyare plan"
    if plan.get("risk") == "high":
        return False, "hög risk blockerad"
    if plan.get("environment") != "staging":
        return False, "endast staging kan stageas"
    if status != "tests_passed":
        return False, f"kontroller ej godkända ({status})"
    evidence = plans._require_current_evidence(plan)
    if evidence is not None:
        reason = str((evidence.data or {}).get("error_class") or "EvidenceMissing")
        return False, f"bevis saknas ({reason})"
    if changed_count is not None and changed_count == 0:
        return False, "inga ändringar"
    return True, "redo för staging"


def _format_changed_files(
    changed_files: list[dict[str, Any]], *, limit: int = 15
) -> list[str]:
    lines: list[str] = []
    for item in changed_files[:limit]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        op = str(item.get("status") or item.get("op") or "changed").strip()
        lines.append(f"- [{op}] {path}")
    extra = len(changed_files) - limit
    if extra > 0:
        lines.append(f"- ... och {extra} fler")
    return lines


def _format_check_rows(checks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in checks[:12]:
        if not isinstance(row, dict):
            continue
        job = str(row.get("job") or "").strip()
        status = str(row.get("status") or "unknown").strip()
        if not job:
            continue
        reason = str(row.get("reason") or "").strip()
        suffix = f" ({reason})" if reason and status != "passed" else ""
        lines.append(f"- {job}: {status}{suffix}")
    return lines


def _with_review_hashes(
    session: dict[str, Any], *, actor: str, plan: dict[str, Any]
) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    site_id = str(plan.get("site_id") or "")
    base_sha = str(plan.get("base_sha256") or "")
    work_sha = str(plan.get("work_sha256") or "")
    if not (_REVIEW_PLAN_ID.fullmatch(plan_id) and site_id and base_sha and work_sha):
        return session
    existing = dict(session.get("wp_review_hashes") or {})
    existing.pop(plan_id, None)
    existing[plan_id] = {
        "actor": actor,
        "site_id": site_id,
        "base_sha256": base_sha,
        "work_sha256": work_sha,
    }
    while len(existing) > _MAX_REVIEW_HASH_CACHE:
        oldest = next(iter(existing))
        existing.pop(oldest, None)
    session["wp_review_hashes"] = existing
    return session


def _drop_review_hash(session: dict[str, Any], plan_id: str) -> bool:
    cached = session.get("wp_review_hashes")
    if not isinstance(cached, dict) or plan_id not in cached:
        return False
    updated = dict(cached)
    updated.pop(plan_id, None)
    if updated:
        session["wp_review_hashes"] = updated
    else:
        session.pop("wp_review_hashes", None)
    return True


def _stored_tool_digest(result: ChatResult, prior_digest: str) -> str:
    """Keep an explicit empty digest when a page review clears the order summary."""
    if result.clear_tool_digest:
        return ""
    return result.tool_digest or prior_digest


def telegram_chat_allowed(chat_id: str | int) -> bool:
    return channel_peer_allowed("telegram", chat_id)


class BotHandler:
    """OpenClaw-compatible commands + hybrid chat + site write confirms."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        escalation: EscalationService | None = None,
        *,
        channel: str = "telegram",
    ) -> None:
        self.store = store or ConversationStore()
        self.escalation = escalation or default_escalation
        self.channel = (channel or "telegram").strip().lower()

    def _peer_allowed(self, peer_id: str | int) -> bool:
        return channel_peer_allowed(self.channel, peer_id)

    def _resolve_actor(self, peer_id: str | int) -> str:
        return resolve_channel_actor(self.channel, peer_id)

    def _deny_allowlist(self) -> BotReply:
        return deny_allowlist_reply(channel=self.channel)

    def _deny_actor(self) -> BotReply:
        return deny_actor_reply(channel=self.channel)

    def _mutations_blocked_reply(self) -> BotReply | None:
        """Block Messenger writes when page token missing outside mock."""
        if self.channel != "messenger":
            return None
        if messenger_mutations_allowed():
            return None
        return BotReply(
            text=(
                "Muterande Messenger-åtgärder är blockerade: "
                "MESSENGER_PAGE_ACCESS_TOKEN saknas (prod). "
                "Be Oscar sätta page token — läsning (/cases list|show, /order) fungerar."
            )
        )

    def _session_market(self, chat_id: str | int) -> str | None:
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        raw = session.get("last_market")
        return str(raw).strip() if raw else None

    def _actor_obj(self, chat_id: str | int) -> Actor:
        return resolve_actor(self._resolve_actor(chat_id))

    def _pending_allowed(self, actor: Actor, pending: PendingAction) -> bool:
        if pending.kind == "order_status":
            return actor.has(Permission.ORDER_STATUS_UPDATE)
        if pending.kind in {"product_desc", "product_update"}:
            return actor.has(Permission.PRODUCT_DESC_WRITE)
        if pending.kind == "page_update":
            return actor.has(Permission.WP_OPS_OPERATE)
        if pending.kind in {"post_update", "site_update"}:
            return actor.has(Permission.WP_OPS_OPERATE)
        if pending.kind == "plugin_status":
            return actor.has(Permission.WP_OPS_ADMIN)
        if pending.kind == "case_regenerate":
            return actor.has(Permission.CASE_REPLY)
        return True

    def _deny_pending_reply(self, pending: PendingAction) -> str:
        if pending.kind == "order_status":
            return (
                "Orderstatus-ändring kräver operator eller Oscar "
                "(Jonatan har CASE_REPLY men inte order write)."
            )
        if pending.kind in {"product_desc", "product_update"}:
            return (
                "Produktändring kräver operator eller Oscar "
                "(Jonatan har inte PRODUCT_DESC_WRITE)."
            )
        if pending.kind == "page_update":
            return (
                "Sidändring kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "post_update":
            return (
                "Inläggsändring kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "site_update":
            return (
                "Sajttitel och tagline kräver operator eller Oscar "
                "(Jonatan har inte WP_OPS_OPERATE)."
            )
        if pending.kind == "plugin_status":
            return "Plugin på eller av kräver Oscar (WP_OPS_ADMIN)."
        return "Du saknar behörighet för den här åtgärden."

    def _merge_state(self, chat_id: str | int, **updates: Any) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(updates.get("session", prev.get("session") or {}))
        if "messages" in updates:
            messages = clamp_messages(updates.get("messages"))
        else:
            messages = clamp_messages(prev.get("messages"))
        if "tool_digest" in updates:
            tool_digest = str(updates.get("tool_digest") or "")
        else:
            tool_digest = str(prev.get("tool_digest") or "")
        slots = updates.get("slots", prev.get("slots") or {})
        state = {
            "flow": updates.get("flow", prev.get("flow")),
            "step": updates.get("step", prev.get("step")),
            "slots": slots,
            "session": session,
            "messages": messages,
            "tool_digest": tool_digest,
        }
        if (
            not state.get("flow")
            and not state.get("session")
            and not state.get("messages")
            and not state.get("tool_digest")
            and not state.get("slots")
        ):
            self.store.clear(chat_id)
            return
        self.store.set(chat_id, state)

    def handle(
        self,
        chat_id: str | int,
        text: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
    ) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (text or "").strip()
        if not raw:
            return BotReply(text="Skriv /help eller /commands — eller bara fråga.")

        wp_command = self._wp_context_command(chat_id, raw)
        if wp_command is not None:
            return wp_command

        # OpenClaw slash commands first
        oc = dispatch_openclaw_command(
            chat_id, raw, self.store, channel=self.channel
        )
        if oc is not None:
            if oc == "__ORDER_PROMPT__":
                self._merge_state(
                    chat_id, flow="order_lookup", step="await_id", slots={}
                )
                return BotReply(text="Ange ordernummer (t.ex. 1001):")
            if isinstance(oc, str) and oc.startswith("__ORDER__:"):
                oid = oc.split(":", 1)[1]
                self._touch_session_order(chat_id, oid)
                return BotReply(
                    text=tool_lookup_order(oid, domain=self._session_market(chat_id))
                )
            return as_reply(oc)

        # Continue multi-turn flows (confirm writes, escalate, order id prompt)
        state = self.store.get(chat_id)
        if state and state.get("flow"):
            return as_reply(self._continue_flow(chat_id, raw, state))

        # Ultra-short order shortcut
        fast = ORDER_FAST_RE.match(raw)
        if fast:
            self._touch_session_order(chat_id, fast.group(1))
            return BotReply(
                text=tool_lookup_order(
                    fast.group(1), domain=self._session_market(chat_id)
                )
            )

        if wants_hard_escalate_confirm(raw):
            return self._start_escalate_confirm(chat_id, raw)

        return self._run_llm_chat(
            chat_id,
            raw,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_checkpoint=on_checkpoint,
        )

    def handle_callback(self, chat_id: str | int, data: str) -> BotReply:
        if not self._peer_allowed(chat_id):
            return self._deny_allowlist()
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        self._clear_mismatched_wp_context(chat_id, actor)
        raw = (data or "").strip()
        if raw == "escalate:yes":
            return as_reply(self._confirm_escalate(chat_id, yes=True))
        if raw == "escalate:no":
            return as_reply(self._confirm_escalate(chat_id, yes=False))
        if raw == "cases:list":
            return as_reply(
                dispatch_openclaw_command(
                    chat_id, "/cases list", self.store, channel=self.channel
                )
                or with_recovery("Kunde inte lista ärenden.")
            )
        if raw.startswith("cases:show:"):
            id8 = raw.split(":", 2)[2].strip()
            self._touch_session_case(chat_id, id8)
            return as_reply(
                dispatch_openclaw_command(
                    chat_id, f"/cases show {id8}", self.store, channel=self.channel
                )
                or with_recovery(f"Hittade inte {id8}.")
            )
        if raw.startswith("cases:approve:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            id8 = raw.split(":", 2)[2].strip()
            return as_reply(
                dispatch_openclaw_command(
                    chat_id,
                    f"/cases approve {id8}",
                    self.store,
                    channel=self.channel,
                )
                or with_recovery("Kunde inte godkänna.")
            )
        if raw.startswith("cases:regen:"):
            id8 = raw.split(":", 2)[2].strip()
            return self._exec_pending(
                chat_id,
                PendingAction(kind="case_regenerate", payload={"case_id": id8}),
            )
        if raw.startswith("order:set:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            # order:set:{oid}:{status}
            parts = raw.split(":")
            if len(parts) >= 4:
                oid, status = parts[2], parts[3]
                return self._exec_pending(
                    chat_id,
                    PendingAction(
                        kind="order_status",
                        payload={
                            "order_id": oid,
                            "status": status,
                            "domain": self._session_market(chat_id),
                        },
                    ),
                )
        if raw == "order:cancel":
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen orderstatus ändrad.")
        if raw.startswith("product:desc:"):
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
            parts = raw.split(":")
            # product:desc:{pid}:{publish_flag}
            if len(parts) >= 4:
                pid, flag = parts[2], parts[3]
                return self._exec_pending(
                    chat_id,
                    PendingAction(
                        kind="product_desc",
                        payload={
                            "product_id": pid if pid != "0" else "",
                            "publish": flag == "1",
                            "language": "sv",
                        },
                    ),
                )
        if raw == "product:cancel":
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen produktbeskrivning genererad.")
        if raw == "wpfile:no" or raw.startswith("wpfile:no:"):
            plan_id = raw.split(":", 2)[2] if raw.startswith("wpfile:no:") else ""
            if plan_id:
                if not _FILE_PLAN_ID.fullmatch(plan_id):
                    return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
                from ecom_ops.wp_ops import WpOpsService

                WpOpsService().deny_file_plan(plan_id, actor=actor)
            return BotReply(text="Avbrutet. Ingen fil ändrades.")
        if raw.startswith("wpfile:stage:"):
            plan_id = raw.split(":", 2)[2]
            if not _FILE_PLAN_ID.fullmatch(plan_id):
                return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
            from ecom_ops.wp_ops import WpOpsService

            staged = WpOpsService().stage_file_plan(plan_id, actor=actor)
            if staged.ok and (staged.data or {}).get("executed") is True:
                return BotReply(text="Filen är skriven i staging. Produktionen är oförändrad.")
            error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
            return BotReply(text=f"Staging skrevs inte ({error}). Ingen fil ändrades.")
        if raw == "wpreview:no" or raw.startswith(("wpreview:no:", "wpreview:stage:")):
            return self._handle_wpreview_callback(chat_id, actor, raw)
        if raw == "action:yes":
            blocked = self._mutations_blocked_reply()
            if blocked:
                state = self.store.get(chat_id) or {}
                slots = dict(state.get("slots") or {})
                pending = PendingAction.from_dict(slots.get("pending"))
                if pending and pending.kind in {
                    "order_status",
                    "product_desc",
                    "product_update",
                    "page_update",
                    "post_update",
                    "site_update",
                    "plugin_status",
                }:
                    return blocked
            return self._confirm_pending_text(chat_id, yes=True)
        if raw == "action:no":
            return self._confirm_pending_text(chat_id, yes=False)
        return BotReply(
            text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
        )

    def _touch_session_order(self, chat_id: str | int, order_id: str) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        session["last_order_id"] = str(order_id)
        self._merge_state(chat_id, session=session)

    def _clear_mismatched_wp_context(self, chat_id: str | int, actor: str) -> None:
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        context = session.get("wp_ops_context")
        changed = False
        if context is not None and not context_matches(
            context,
            actor=actor,
            channel=self.channel,
            conversation_id=f"{self.channel}:{chat_id}",
        ):
            session.pop("wp_ops_context", None)
            changed = True
        hashes = session.get("wp_review_hashes")
        if isinstance(hashes, dict):
            kept = {
                pid: row
                for pid, row in hashes.items()
                if isinstance(row, dict) and row.get("actor") == actor
            }
            if len(kept) != len(hashes):
                if kept:
                    session["wp_review_hashes"] = kept
                else:
                    session.pop("wp_review_hashes", None)
                changed = True
        if not changed:
            return
        self._merge_state(
            chat_id,
            session=session,
            messages=state.get("messages"),
            tool_digest=state.get("tool_digest"),
        )

    def _touch_session_case(self, chat_id: str | int, case_id8: str) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        session["last_case_id8"] = str(case_id8)
        self._merge_state(chat_id, session=session)

    def _clear_pending(self, chat_id: str | int) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        self._merge_state(
            chat_id,
            flow=None,
            step=None,
            slots={},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )

    def _continue_flow(
        self, chat_id: str | int, text: str, state: dict[str, Any]
    ) -> str | BotReply:
        flow = state.get("flow")
        step = state.get("step")
        session = dict(state.get("session") or {})
        slots = dict(state.get("slots") or {})

        if flow == "order_lookup" and step == "await_id":
            try:
                oid = validate_order_id(text.strip())
            except Exception:
                return BotReply(
                    text="Ogiltigt ordernummer. Försök igen eller /stop."
                )
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            self._touch_session_order(chat_id, oid)
            return tool_lookup_order(oid, domain=self._session_market(chat_id))

        if flow == "escalate_confirm" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j"}:
                return self._confirm_escalate(chat_id, yes=True)
            if answer in {"nej", "no", "n"}:
                return self._confirm_escalate(chat_id, yes=False)
            if not wants_escalate(text) and len(text) > 12:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(chat_id, text)
            return bot_reply(
                "Svara ja eller nej (eller tryck knappen), eller /stop.",
                actions=yes_no_actions(
                    yes_data="escalate:yes", no_data="escalate:no"
                ),
            )

        if flow == "pending_action" and step == "confirm":
            answer = text.strip().lower()
            if answer in {"ja", "yes", "y", "j", "ok", "kör", "bekrafta", "bekräfta"}:
                return self._confirm_pending_text(chat_id, yes=True)
            if answer in {"nej", "no", "n", "avbryt", "cancel"}:
                return self._confirm_pending_text(chat_id, yes=False)
            # Free text mid-confirm → drop pending and chat
            if len(text) > 8:
                self._merge_state(
                    chat_id, flow=None, step=None, slots={}, session=session
                )
                return self._run_llm_chat(chat_id, text)
            pending = PendingAction.from_dict(slots.get("pending"))
            try:
                actor_obj = self._actor_obj(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return self._deny_actor()
            return bot_reply(
                "Svara ja/nej eller tryck knappen för att bekräfta ändringen.",
                actions=self._actions_for_pending(pending, actor=actor_obj),
            )

        self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
        return self._run_llm_chat(chat_id, text)

    def _start_escalate_confirm(self, chat_id: str | int, message: str) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        self._merge_state(
            chat_id,
            flow="escalate_confirm",
            step="confirm",
            slots={"message": message[:500]},
            session=session,
        )
        return bot_reply(
            (
                "Eskalera till Oscar?\n\n"
                f"Meddelande: {message[:300]}\n\n"
                "Bekräfta med Ja/Nej."
            ),
            actions=yes_no_actions(yes_data="escalate:yes", no_data="escalate:no"),
        )

    def _confirm_escalate(self, chat_id: str | int, *, yes: bool) -> str:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        session = dict(state.get("session") or {})
        if yes:
            try:
                actor = self._resolve_actor(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return (
                    "Din chat saknar actor-mapping. "
                    "Be Oscar uppdatera actor-map för kanalen."
                )
            ticket = self.escalation.escalate_critical(
                f"{self.channel} escalation by {actor}",
                details={
                    "channel": self.channel,
                    "chat_id": str(chat_id),
                    "actor": actor,
                    "message": slots.get("message", "")[:500],
                    "draft": (slots.get("reply") or "")[:500],
                },
            )
            self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
            return f"Eskalerat till Oscar. Ticket: {ticket.id}"
        self._merge_state(chat_id, flow=None, step=None, slots={}, session=session)
        return "Inte eskalerad. Fortsätt chatta — eller /cases · /help"

    def _actions_for_pending(self, pending: PendingAction | None, *, actor: Actor | None = None):
        if not pending:
            return yes_no_actions(yes_data="action:yes", no_data="action:no")
        if actor is not None and not self._pending_allowed(actor, pending):
            return None
        if pending.kind == "order_status":
            return order_status_confirm_actions(
                str(pending.payload.get("order_id") or ""),
                str(pending.payload.get("status") or ""),
            )
        if pending.kind == "product_desc":
            return product_desc_confirm_actions(
                str(pending.payload.get("product_id") or "0"),
                publish=bool(pending.payload.get("publish")),
            )
        if pending.kind == "case_regenerate":
            id8 = str(pending.payload.get("case_id") or "")[:8]
            return yes_no_actions(yes_data=f"cases:regen:{id8}", no_data="action:no")
        return yes_no_actions(yes_data="action:yes", no_data="action:no")

    def _start_pending(
        self,
        chat_id: str | int,
        pending: PendingAction,
        text: str,
        *,
        actor: Actor,
        attachments: tuple[str, ...] = (),
    ) -> BotReply:
        if not self._pending_allowed(actor, pending):
            return BotReply(text=f"{text}\n\n{self._deny_pending_reply(pending)}")
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        if pending.kind == "order_status" and pending.payload.get("order_id"):
            session["last_order_id"] = str(pending.payload["order_id"])
        if pending.kind == "case_regenerate" and pending.payload.get("case_id"):
            session["last_case_id8"] = str(pending.payload["case_id"])[:8]
        if pending.kind == "product_update" and pending.payload.get("product_id"):
            session["last_product_id"] = str(pending.payload["product_id"])
        if pending.kind == "page_update" and pending.payload.get("page_id"):
            session["last_page_id"] = str(pending.payload["page_id"])
        if pending.kind == "post_update" and pending.payload.get("post_id"):
            session["last_post_id"] = str(pending.payload["post_id"])
        self._merge_state(
            chat_id,
            flow="pending_action",
            step="confirm",
            slots={"pending": pending.to_dict(), "prompt": text[:500]},
            session=session,
            messages=prev.get("messages"),
            tool_digest=prev.get("tool_digest"),
        )
        attachment_kwargs = {"attachments": attachments} if attachments else {}
        return bot_reply(
            text,
            actions=self._actions_for_pending(pending, actor=actor),
            needs_typing=True,
            **attachment_kwargs,
        )

    def _confirm_pending_text(self, chat_id: str | int, *, yes: bool) -> BotReply:
        state = self.store.get(chat_id) or {}
        slots = dict(state.get("slots") or {})
        pending = PendingAction.from_dict(slots.get("pending"))
        if not yes or not pending:
            self._clear_pending(chat_id)
            return BotReply(text="Avbrutet — ingen ändring utförd.")
        return self._exec_pending(chat_id, pending)

    def _exec_pending(self, chat_id: str | int, pending: PendingAction) -> BotReply:
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            self._clear_pending(chat_id)
            return self._deny_actor()
        try:
            actor_obj = resolve_actor(actor)
        except Exception:
            self._clear_pending(chat_id)
            return self._deny_actor()
        if not self._pending_allowed(actor_obj, pending):
            self._clear_pending(chat_id)
            return BotReply(text=self._deny_pending_reply(pending))
        if pending.kind in {
            "order_status",
            "product_desc",
            "product_update",
            "page_update",
            "post_update",
            "site_update",
            "plugin_status",
        }:
            blocked = self._mutations_blocked_reply()
            if blocked:
                return blocked
        ok = False
        msg = "Okänd action"
        if pending.kind == "order_status":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_order_status(
                order_id=str(pending.payload.get("order_id") or ""),
                status=str(pending.payload.get("status") or ""),
                actor=actor,
                domain=str(domain) if domain else None,
            )
            if ok and pending.payload.get("order_id"):
                self._touch_session_order(chat_id, str(pending.payload["order_id"]))
        elif pending.kind == "product_desc":
            ok, msg = execute_product_desc(
                product_id=str(pending.payload.get("product_id") or ""),
                language=str(pending.payload.get("language") or "sv"),
                publish=bool(pending.payload.get("publish")),
                actor=actor,
            )
        elif pending.kind == "product_update":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_product_update(
                product_id=str(pending.payload.get("product_id") or ""),
                fields=dict(pending.payload.get("fields") or {}),
                actor=actor,
                domain=str(domain) if domain else None,
            )
        elif pending.kind == "page_update":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_page_update(
                page_id=str(pending.payload.get("page_id") or ""),
                fields=dict(pending.payload.get("fields") or {}),
                actor=actor,
                domain=str(domain) if domain else None,
            )
        elif pending.kind == "post_update":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_post_update(
                post_id=str(pending.payload.get("post_id") or ""),
                fields=dict(pending.payload.get("fields") or {}),
                actor=actor,
                domain=str(domain) if domain else None,
            )
        elif pending.kind == "site_update":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_site_update(
                fields=dict(pending.payload.get("fields") or {}),
                actor=actor,
                domain=str(domain) if domain else None,
            )
        elif pending.kind == "plugin_status":
            domain = pending.payload.get("domain") or self._session_market(chat_id)
            ok, msg = execute_plugin_status(
                plugin=str(pending.payload.get("plugin") or ""),
                status=str(pending.payload.get("status") or ""),
                actor=actor,
                domain=str(domain) if domain else None,
            )
        elif pending.kind == "case_regenerate":
            # Prefer full id via store if only id8
            case_key = str(pending.payload.get("case_id") or "")
            case = None
            try:
                from ecom_ops.cases.service import CaseService

                svc = CaseService()
                case = svc.store.resolve_id_prefix(case_key) or svc.get(case_key)
                if case:
                    case_key = case.id
            except Exception:
                pass
            ok, msg = execute_case_regenerate(case_id=case_key, actor=actor)
            if ok:
                self._touch_session_case(chat_id, case_key[:8])
                try:
                    from ecom_ops.order_context import woo_domain_from_market

                    if case and getattr(case, "market", None):
                        market = woo_domain_from_market(case.market)
                        if market:
                            prev = self.store.get(chat_id) or {}
                            session = dict(prev.get("session") or {})
                            session["last_market"] = market
                            self._merge_state(chat_id, session=session)
                except Exception:
                    pass
        self._clear_pending(chat_id)
        prefix = "Klart." if ok else "Misslyckades."
        # RBAC denies are common for jonatan on order update
        if not ok and ("lacks permission" in msg.lower() or "access" in msg.lower()):
            map_hint = (
                "MESSENGER_ACTOR_MAP"
                if self.channel == "messenger"
                else "TELEGRAM_ACTOR_MAP"
            )
            msg = (
                f"{msg}\n\n"
                f"Tips: mappa din chat till en operator/Oscar via {map_hint} "
                "(Jonatan har CASE_REPLY men inte order/product write)."
            )
        return BotReply(text=f"{prefix}\n{msg}")

    def _run_llm_chat(
        self,
        chat_id: str | int,
        message: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_reset: Callable[[], None] | None = None,
        on_checkpoint: Callable[[], list[str]] | None = None,
    ) -> BotReply:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        actor = self._resolve_actor(chat_id)
        conversation_id = f"{self.channel}:{chat_id}"
        wp_context = session.get("wp_ops_context")
        if not context_matches(
            wp_context, actor=actor, channel=self.channel, conversation_id=conversation_id
        ):
            session.pop("wp_ops_context", None)
        history = list(prev.get("messages") or [])
        prior_digest = str(prev.get("tool_digest") or "")
        result = run_chat(
            message,
            history=history,
            session=session,
            prior_digest=prior_digest,
            sticky_order_id=session.get("last_order_id"),
            sticky_case_id8=session.get("last_case_id8"),
            actor=actor,
            channel=self.channel,
            conversation_id=conversation_id,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
            on_text_reset=on_text_reset,
            on_checkpoint=on_checkpoint,
        )
        if result.sticky_order_id:
            session["last_order_id"] = str(result.sticky_order_id)
        if result.sticky_case_id8:
            session["last_case_id8"] = str(result.sticky_case_id8)
        if result.sticky_market:
            session["last_market"] = str(result.sticky_market)
        if result.order_list_shown is not None:
            session["order_list_shown"] = int(result.order_list_shown)
        if result.sticky_product_id:
            session["last_product_id"] = str(result.sticky_product_id)
        if result.sticky_page_id:
            session["last_page_id"] = str(result.sticky_page_id)
        if result.sticky_post_id:
            session["last_post_id"] = str(result.sticky_post_id)
        if result.wp_ops_context is not None:
            session["wp_ops_context"] = result.wp_ops_context

        review_plan_id = getattr(result, "review_stage_plan_id", None)
        review_plan_row: dict[str, Any] | None = None
        if review_plan_id and _REVIEW_PLAN_ID.fullmatch(review_plan_id):
            try:
                from ecom_ops.wp_ops.review_plan import ReviewPlans

                review_plan_row = ReviewPlans().get(review_plan_id)
                if isinstance(review_plan_row, dict):
                    _with_review_hashes(session, actor=actor, plan=review_plan_row)
            except Exception:
                review_plan_row = None

        self._merge_state(
            chat_id,
            flow=None,
            step=None,
            slots={},
            session=session,
            messages=result.messages,
            tool_digest=_stored_tool_digest(result, prior_digest),
        )

        text = result.text
        actions = None
        safe_attachments = (
            safe_workspace_attachments(result.attachments)
            if result.attachments
            else ()
        )
        attachment_kwargs = (
            {"attachments": safe_attachments} if safe_attachments else {}
        )

        if result.offer_escalate:
            self._merge_state(
                chat_id,
                flow="escalate_confirm",
                step="confirm",
                slots={"message": message[:500]},
                session=session,
                messages=result.messages,
                tool_digest=_stored_tool_digest(result, prior_digest),
            )
            if "eskalera" not in text.lower():
                text = f"{text}\n\nVill du eskalera till Oscar?"
            return bot_reply(
                text,
                actions=yes_no_actions(
                    yes_data="escalate:yes", no_data="escalate:no"
                ),
                needs_typing=True,
                **attachment_kwargs,
            )

        if result.file_stage_plan_id and _FILE_PLAN_ID.fullmatch(result.file_stage_plan_id):
            plan_id = result.file_stage_plan_id
            return bot_reply(
                text,
                actions=yes_no_actions(
                    yes_data=f"wpfile:stage:{plan_id}",
                    no_data=f"wpfile:no:{plan_id}",
                ),
                needs_typing=True,
                **attachment_kwargs,
            )

        if review_plan_id and _REVIEW_PLAN_ID.fullmatch(review_plan_id):
            site_id = (
                str(review_plan_row.get("site_id") or "")
                if isinstance(review_plan_row, dict)
                else ""
            )
            url = review_plan_url(site_id, review_plan_id) if site_id else None
            return bot_reply(
                text,
                actions=_wpreview_stage_actions(review_plan_id, review_url=url),
                needs_typing=True,
                **attachment_kwargs,
            )

        if result.pending_action is not None:
            try:
                actor_obj = self._actor_obj(chat_id)
            except (TelegramActorDenied, ChannelActorDenied):
                return self._deny_actor()
            return self._start_pending(
                chat_id,
                result.pending_action,
                text,
                actor=actor_obj,
                **attachment_kwargs,
            )

        if result.soft_escalate_nudge and SOFT_ESCALATE_NUDGE not in text:
            text = f"{text}\n\n{SOFT_ESCALATE_NUDGE}"

        if result.case_id8:
            # Prefer sticky full id from session when prefix matches
            sticky = str(session.get("last_case_id8") or "")
            case_key = (
                sticky
                if sticky.startswith(str(result.case_id8)[:8])
                else str(result.case_id8)
            )
            actions = approve_case_actions(case_key)
        elif result.suggest_case_ids:
            actions = triage_cases_actions(result.suggest_case_ids)

        return bot_reply(
            text, actions=actions, needs_typing=True, attachments=safe_attachments
        )

    def _handle_wpreview_callback(
        self, chat_id: str | int, actor: str, raw: str
    ) -> BotReply:
        if raw == "wpreview:no" or raw.startswith("wpreview:no:"):
            plan_id = raw.split(":", 2)[2] if raw.startswith("wpreview:no:") else ""
            if plan_id:
                if not _REVIEW_PLAN_ID.fullmatch(plan_id):
                    return BotReply(
                        text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
                    )
                prev = self.store.get(chat_id) or {}
                session = dict(prev.get("session") or {})
                if _drop_review_hash(session, plan_id):
                    self._merge_state(
                        chat_id,
                        session=session,
                        messages=prev.get("messages"),
                        tool_digest=prev.get("tool_digest"),
                    )
                return BotReply(
                    text=f"Avbrutet ({plan_id}). Ingen ändring skrevs till staging eller produktion."
                )
            return BotReply(
                text="Avbrutet. Ingen ändring skrevs till staging eller produktion."
            )

        plan_id = raw.split(":", 2)[2]
        if not _REVIEW_PLAN_ID.fullmatch(plan_id):
            return BotReply(
                text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK)
            )
        blocked = self._mutations_blocked_reply()
        if blocked:
            return blocked
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_ADMIN)
            require_permission(actor_obj, Permission.CODE_EDIT)
        except Exception:
            return BotReply(
                text="Staging av granskningsplan kräver Oscar (WP_OPS_ADMIN och CODE_EDIT). Ingen ändring utfördes."
            )

        from ecom_ops.wp_ops.review_plan import ReviewPlans

        plans = ReviewPlans()
        plan = plans.get(plan_id)
        if plan is None:
            return BotReply(
                text=f"Staging skrevs inte (UnknownPlan). Granskningsplanen {plan_id} hittades inte."
            )
        site_id = str(plan.get("site_id") or "")
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        cached = (session.get("wp_review_hashes") or {}).get(plan_id)
        if (
            isinstance(cached, dict)
            and cached.get("actor") == actor
            and cached.get("site_id") == site_id
        ):
            base_sha = str(cached.get("base_sha256") or "")
            work_sha = str(cached.get("work_sha256") or "")
        else:
            base_sha = str(plan.get("base_sha256") or "")
            work_sha = str(plan.get("work_sha256") or "")

        ws_status = plans.workspace_status(site_id, plan_id, actor=actor)
        changed_files = (
            list((ws_status.data or {}).get("changed_files") or [])
            if ws_status.ok
            else []
        )
        staged = plans.approve_staging(
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha,
            work_sha256=work_sha,
        )
        if staged.ok and (staged.data or {}).get("executed") is True:
            if _drop_review_hash(session, plan_id):
                self._merge_state(
                    chat_id,
                    session=session,
                    messages=prev.get("messages"),
                    tool_digest=prev.get("tool_digest"),
                )
            lines = [
                f"Ändringarna för {plan_id} är skrivna i staging ({site_id}, mock). Produktionen är oförändrad."
            ]
            file_lines = _format_changed_files(changed_files)
            if file_lines:
                lines.append("Ändrade filer:")
                lines.extend(file_lines)
            url = review_plan_url(site_id, plan_id)
            if url:
                lines.append(f"Granskning: {url} (kräver inloggning)")
            else:
                lines.append(f"Granskningssida: /wp/{site_id}/reviews/{plan_id} (kräver inloggning)")
            return BotReply(text="\n".join(lines))

        error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
        detail = str(staged.message or "").strip()
        msg = f"Staging skrevs inte ({error}). Ingen ändring utfördes."
        if detail and detail != error:
            msg = f"{msg}\n{detail}"
        return BotReply(text=msg)

    def _wp_plans_command(self, actor: str, parts: list[str]) -> BotReply:
        from ecom_ops.wp_ops.review_plan import ReviewPlans

        site_id = parts[1] if len(parts) == 2 else None
        plans = ReviewPlans()
        res = plans.list_plans(site_id, actor=actor)
        if not res.ok:
            err = str((res.data or {}).get("error_class") or "")
            if err == "AccessDenied":
                return BotReply(text="Du saknar rättighet att läsa granskningsplaner.")
            if err == "UnknownSite":
                return BotReply(text=f"Okänd WordPress-sajt: {site_id}.")
            return BotReply(text=f"Kunde inte lista granskningsplaner ({err or 'okänt fel'}).")

        rows = list((res.data or {}).get("plans") or [])
        if not rows:
            scope = f" för {site_id}" if site_id else ""
            return BotReply(text=f"Inga öppna granskningsarbetsytor (wr-*){scope}.")

        can_see_summary = False
        try:
            actor_obj = resolve_actor(actor)
            can_see_summary = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(
                Permission.CODE_EDIT
            )
        except Exception:
            can_see_summary = False

        header = f"Granskningsarbetsytor ({site_id}):" if site_id else "Granskningsarbetsytor (senaste):"
        lines = [header]
        for item in rows[:10]:
            pid = str(item.get("plan_id") or "")
            psite = str(item.get("site_id") or "")
            target = f"{item.get('kind')}:{item.get('slug')}"
            status = str(item.get("status") or "unknown")
            _ready, ready_label = _review_plan_ready(plans, item)
            title = ""
            if can_see_summary:
                raw_row = plans.get(pid, site_id=psite)
                raw_summary = str((raw_row or {}).get("summary") or "").strip()
                if raw_summary:
                    title = f" · {raw_summary[:40]}"
            lines.append(f"- {pid} | {psite} | {target} | {status} | {ready_label}{title}")
        if len(rows) > 10:
            lines.append(f"... och {len(rows) - 10} fler.")
        lines.append("Detaljer: /wp plan <plan-id>")
        return BotReply(text="\n".join(lines))

    def _wp_plan_command(
        self, chat_id: str | int, actor: str, parts: list[str]
    ) -> BotReply:
        plan_id = parts[1].strip()
        if not _REVIEW_PLAN_ID.fullmatch(plan_id):
            return BotReply(text="Ange ett giltigt plan-id (wr-xxxxxxxxxxxx).")
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_READ)
        except Exception:
            return BotReply(text="Du saknar rättighet att läsa granskningsplaner.")

        from ecom_ops.wp_ops.review_plan import ReviewPlans

        plans = ReviewPlans()
        raw_plan = plans.get(plan_id)
        if raw_plan is None:
            return BotReply(text=f"Okänd granskningsplan: {plan_id}.")
        site_id = str(raw_plan.get("site_id") or "")
        shown = plans.show(site_id, plan_id, actor=actor, include_diff=True)
        if not shown.ok:
            err = str((shown.data or {}).get("error_class") or "")
            if err == "AccessDenied":
                return BotReply(text="Du saknar rättighet att läsa granskningsplanen.")
            if err == "Expired":
                return BotReply(text=f"Granskningsplan {plan_id} har gått ut.")
            return BotReply(text=f"Kunde inte visa plan {plan_id} ({err or 'okänt fel'}).")

        view = dict((shown.data or {}).get("plan") or {})
        ws_status = plans.workspace_status(site_id, plan_id, actor=actor)
        changed_files = (
            list((ws_status.data or {}).get("changed_files") or [])
            if ws_status.ok
            else []
        )
        ready, ready_label = _review_plan_ready(
            plans, raw_plan, changed_count=len(changed_files)
        )
        can_stage = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(
            Permission.CODE_EDIT
        )
        artifacts = (
            plans.list_artifacts(site_id, plan_id, actor=actor) if can_stage else []
        )
        url = review_plan_url(site_id, plan_id)

        lines = [
            f"Granskningsplan {plan_id}",
            f"Sajt: {site_id} ({view.get('environment')}) · {view.get('kind')}: {view.get('slug')}",
            f"Status: {view.get('status')} · Risk: {view.get('risk')} · Staging: {ready_label}",
        ]
        summary = str(view.get("summary") or "").strip()
        if summary:
            lines.append(f"Sammanfattning: {summary[:200]}")

        if changed_files:
            lines.append(f"Ändrade filer ({len(changed_files)}):")
            lines.extend(_format_changed_files(changed_files))
        else:
            lines.append("Ändrade filer: inga ännu.")

        check_lines = _format_check_rows(list(view.get("checks") or []))
        if check_lines:
            lines.append("Kontroller:")
            lines.extend(check_lines)
        check_run = view.get("check_run")
        if isinstance(check_run, dict) and check_run.get("run_id"):
            pub = "ja" if check_run.get("published") is True else "nej"
            lines.append(f"Check-run: {check_run.get('run_id')} (publicerad: {pub})")

        sandbox = view.get("sandbox") if isinstance(view.get("sandbox"), dict) else {}
        if sandbox:
            lines.append(
                f"Sandbox: {sandbox.get('state', 'stopped')} ({sandbox.get('engine', 'unavailable')})"
            )
        if artifacts:
            lines.append(f"Artifakter: {', '.join(artifacts[:6])}")
        if url:
            lines.append(f"Granskningslänk: {url} (kräver inloggning)")
        else:
            lines.append(f"Granskningssida: /wp/{site_id}/reviews/{plan_id} (kräver inloggning)")
        lines.append("Produktion deployas aldrig från chatten.")

        if ready and can_stage:
            prev = self.store.get(chat_id) or {}
            session = dict(prev.get("session") or {})
            _with_review_hashes(session, actor=actor, plan=raw_plan)
            self._merge_state(
                chat_id,
                session=session,
                messages=prev.get("messages"),
                tool_digest=prev.get("tool_digest"),
            )
            return bot_reply(
                "\n".join(lines),
                actions=_wpreview_stage_actions(plan_id, review_url=url),
            )
        if ready and not can_stage:
            lines.append("Staging kräver Oscar (WP_OPS_ADMIN + CODE_EDIT).")
        return BotReply(text="\n".join(lines))

    def _wp_context_command(self, chat_id: str | int, text: str) -> BotReply | None:
        """Minimal user controls for the sticky, actor-scoped WordPress site."""
        match = re.fullmatch(r"/wp(?:@\w+)?(?:\s+(.*))?", text.strip(), re.I)
        if not match:
            return None
        parts = (match.group(1) or "").strip().split()
        action = parts[0].lower() if parts else "help"
        state = self.store.get(chat_id) or {}
        session = dict(state.get("session") or {})
        if action == "clear" and len(parts) == 1:
            session.pop("wp_ops_context", None)
            session.pop("wp_review_hashes", None)
            self._merge_state(
                chat_id,
                session=session,
                messages=state.get("messages"),
                tool_digest=state.get("tool_digest"),
            )
            return BotReply(text="WordPress-sajtkontexten är rensad.")
        if action == "help" and len(parts) == 1:
            return BotReply(
                text="/wp sites · /wp use <site-id> · /wp plans [site-id] · /wp plan <plan-id> · /wp clear"
            )
        try:
            actor = self._resolve_actor(chat_id)
        except (TelegramActorDenied, ChannelActorDenied):
            return self._deny_actor()
        if action == "plans" and len(parts) in {1, 2}:
            return self._wp_plans_command(actor, parts)
        if action == "plan" and len(parts) == 2:
            return self._wp_plan_command(chat_id, actor, parts)
        tools = WpAgentTools()
        if action == "sites" and len(parts) == 1:
            output = tools.execute(
                "wp_list_sites",
                {},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            if output.data.get("ok") is not True:
                return BotReply(text="Du saknar rättighet att läsa WordPress-sajter.")
            sites = output.data.get("sites", [])
            if not sites:
                return BotReply(text="Inga registrerade WordPress-sajter.")
            lines = ["Registrerade WordPress-sajter:"]
            for row in sites[:20]:
                lines.append(
                    f"- {row.get('site_id')} · {row.get('name')} · "
                    f"{row.get('environment')} · status {row.get('status')}"
                )
            return BotReply(text="\n".join(lines))
        if action == "use" and len(parts) == 2:
            site_id = parts[1]
            output = tools.execute(
                "wp_site_status",
                {"site_id": site_id},
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
            )
            if output.data.get("ok") is not True:
                return BotReply(text="Sajten är okänd eller du saknar rättighet att läsa den.")
            context = context_for_storage(
                actor=actor,
                channel=self.channel,
                conversation_id=f"{self.channel}:{chat_id}",
                site_id=output.site_id,
                operation_ids=output.operation_ids,
            )
            if context is None:
                return BotReply(text="WordPress-sajten kunde inte väljas.")
            session["wp_ops_context"] = context
            self._merge_state(
                chat_id,
                session=session,
                messages=state.get("messages"),
                tool_digest=state.get("tool_digest"),
            )
            return BotReply(text=f"Aktiv WordPress-sajt: {site_id}. Fråga om status, backup eller uppdateringar.")
        return BotReply(
            text="Använd /wp sites, /wp use <site-id>, /wp plans [site-id], /wp plan <plan-id> eller /wp clear."
        )
