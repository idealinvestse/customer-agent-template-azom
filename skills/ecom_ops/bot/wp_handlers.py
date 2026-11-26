"""WordPress ``/wp`` slash commands and ``wpfile:*`` / ``wpreview:*`` callback handlers.

Uses ``ReviewPlans.staging_readiness`` (public read-model API) and ``bot.wp_session``
for hash-pinning and actor/channel-scoped session tracking.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ecom_ops.bot.actors import ChannelActorDenied, TelegramActorDenied
from ecom_ops.bot.chat_agent import _provenance
from ecom_ops.bot.dashboard_links import review_plan_url
from ecom_ops.bot.progress_events import ProgressCallback, ProgressEvent, emit_progress
from ecom_ops.bot.recovery import FOOTER_UNKNOWN_CALLBACK, with_recovery
from ecom_ops.bot.reply import ActionMarkup, BotReply, bot_reply, wp_review_actions
from ecom_ops.bot.wp_session import (
    FILE_PLAN_ID_RE,
    REVIEW_PLAN_ID_RE,
    resolve_pinned_hashes,
)
from ecom_ops.rbac import Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.agent_tools import WpAgentTools, context_for_storage


def wpreview_stage_actions(
    plan_id: str,
    *,
    review_url: str | None = None,
    include_push_draft: bool = False,
) -> ActionMarkup:
    """Build channel-neutral WP review actions via ``reply.wp_review_actions``."""
    return wp_review_actions(
        plan_id,
        review_url=review_url,
        include_push_draft=include_push_draft,
    )


def review_plan_ready(
    plans: Any,
    plan: dict[str, Any],
    *,
    changed_count: int | None = None,
) -> tuple[bool, str]:
    """Delegate staging readiness to ``ReviewPlans.staging_readiness``."""
    if hasattr(plans, "staging_readiness") and callable(plans.staging_readiness):
        return plans.staging_readiness(plan, changed_count=changed_count)
    status = str(plan.get("status") or "")
    if status != "tests_passed":
        return False, f"kontroller ej godkända ({status})"
    return True, "redo för staging"


def format_changed_files(
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


def format_check_rows(checks: list[dict[str, Any]]) -> list[str]:
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


class _ProgressTracker:
    """Emit matched started/completed/failed ``ProgressEvent`` pairs with elapsed timing."""

    def __init__(
        self,
        on_progress: ProgressCallback | None,
        *,
        step_id: str,
        tool: str,
        label: str,
        detail: str,
        legacy_start: str,
    ) -> None:
        self.on_progress = on_progress
        self.step_id = step_id
        self.tool = tool
        self.label = label
        self.prov = _provenance()
        self._t0 = time.monotonic()
        emit_progress(
            on_progress,
            ProgressEvent(
                step_id=step_id,
                tool=tool,
                phase="started",
                label=label,
                detail=detail,
                provenance=self.prov,
                legacy_text=legacy_start,
            ),
        )

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def complete(self, outcome: str, legacy_text: str) -> None:
        emit_progress(
            self.on_progress,
            ProgressEvent(
                step_id=self.step_id,
                tool=self.tool,
                phase="completed",
                label=self.label,
                outcome=outcome,
                elapsed_ms=self._elapsed_ms(),
                provenance=self.prov,
                legacy_text=legacy_text,
            ),
        )

    def fail(self, outcome: str, legacy_text: str) -> None:
        emit_progress(
            self.on_progress,
            ProgressEvent(
                step_id=self.step_id,
                tool=self.tool,
                phase="failed",
                label=self.label,
                outcome=outcome,
                elapsed_ms=self._elapsed_ms(),
                provenance=self.prov,
                legacy_text=legacy_text,
            ),
        )


def handle_wpfile_callback(
    actor: str,
    raw: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BotReply:
    """Handle ``wpfile:no:*`` and ``wpfile:stage:*`` approval callbacks."""
    if raw == "wpfile:no" or raw.startswith("wpfile:no:"):
        plan_id = raw.split(":", 2)[2] if raw.startswith("wpfile:no:") else ""
        if plan_id:
            if not FILE_PLAN_ID_RE.fullmatch(plan_id):
                return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
            from ecom_ops.wp_ops import WpOpsService

            WpOpsService().deny_file_plan(plan_id, actor=actor)
        return BotReply(text="Avbrutet. Ingen fil ändrades.")

    plan_id = raw.split(":", 2)[2]
    if not FILE_PLAN_ID_RE.fullmatch(plan_id):
        return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
    from ecom_ops.wp_ops import WpOpsService

    tracker = _ProgressTracker(
        on_progress,
        step_id=f"wpfile:{plan_id}",
        tool="wpfile_stage",
        label="Staging-filändring",
        detail="Skriver godkänd filändring i staging",
        legacy_start="Skriver godkänd filändring i staging...",
    )
    staged = WpOpsService().stage_file_plan(plan_id, actor=actor)
    if staged.ok and (staged.data or {}).get("executed") is True:
        tracker.complete("fil skriven i staging", "Staging-filändring: klar.")
        return BotReply(text="Filen är skriven i staging. Produktionen är oförändrad.")
    error = str((staged.data or {}).get("error_class") or "OperationUnavailable")
    tracker.fail(f"skrevs inte ({error})", f"Staging-filändring: misslyckades ({error}).")
    return BotReply(text=f"Staging skrevs inte ({error}). Ingen fil ändrades.")


def _handle_wpreview_push_draft(
    site_id: str,
    plan_id: str,
    *,
    actor: str,
    base_sha: str,
    work_sha: str,
    on_progress: ProgressCallback | None,
) -> BotReply:
    from ecom_ops.wp_ops.service import WpOpsService

    tracker = _ProgressTracker(
        on_progress,
        step_id=f"wpreview_draft:{plan_id}",
        tool="wpreview_push_draft",
        label="WP-sidutkast",
        detail="Skapar WordPress-utkast (draft) från verifierad sandlåda",
        legacy_start="Skapar WordPress-utkast (draft)...",
    )
    try:
        pushed = WpOpsService().push_page_draft(
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha,
            work_sha256=work_sha,
        )
    except Exception:
        tracker.fail("misslyckades", "WP-sidutkast: misslyckades.")
        raise

    if isinstance(pushed, dict):
        push_ok = bool(pushed.get("ok"))
        push_data = dict(pushed)
        push_msg = str(pushed.get("message") or "")
    else:
        push_ok = bool(getattr(pushed, "ok", False))
        push_data = dict(getattr(pushed, "data", None) or {})
        push_msg = str(getattr(pushed, "message", "") or "")

    if push_ok:
        draft_page = dict(push_data.get("draft_page") or {})
        status_val = str(draft_page.get("status") or "draft")
        if status_val != "draft":
            status_val = "draft"
        tracker.complete(f"utkast skapat (status={status_val})", "WP-sidutkast: klart.")
        lines = [
            f"WordPress-utkast skapat för {plan_id} ({site_id}) [status={status_val}] — aldrig autopublicerat."
        ]
        if draft_page.get("id"):
            lines.append(f"Sid-ID: {draft_page['id']} · slug: {draft_page.get('slug') or '-'}")
        if draft_page.get("edit_url"):
            lines.append(f"Redigera i WP Admin: {draft_page['edit_url']}")
        if draft_page.get("link"):
            lines.append(f"Förhandsvisningslänk (draft): {draft_page['link']}")
        return BotReply(text="\n".join(lines))

    error = str(push_data.get("error_class") or "OperationUnavailable")
    tracker.fail(f"skapades inte ({error})", f"WP-sidutkast: misslyckades ({error}).")
    msg = f"WP-utkast skapades inte ({error}). Ingen sida skapades i WordPress."
    if push_msg and push_msg != error:
        msg = f"{msg}\n{push_msg}"
    return BotReply(text=msg)


def handle_wpreview_callback(
    handler: Any,
    chat_id: str | int,
    actor: str,
    raw: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BotReply:
    """Handle ``wpreview:no:*``, ``wpreview:stage:*``, and ``wpreview:push_draft:*`` callbacks."""
    if raw == "wpreview:no" or raw.startswith("wpreview:no:"):
        plan_id = raw.split(":", 2)[2] if raw.startswith("wpreview:no:") else ""
        if plan_id:
            if not REVIEW_PLAN_ID_RE.fullmatch(plan_id):
                return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
            prev = handler.store.get(chat_id) or {}
            session = dict(prev.get("session") or {})
            handler._clear_review_tracking(chat_id, session, plan_id)
            return BotReply(
                text=f"Avbrutet ({plan_id}). Ingen ändring skrevs till staging eller produktion."
            )
        prev = handler.store.get(chat_id) or {}
        if isinstance(prev.get("session"), dict) and "active_wp_review" in prev["session"]:
            handler._update_session_atomic(chat_id, pop_session_keys=("active_wp_review",))
        return BotReply(text="Avbrutet. Ingen ändring skrevs till staging eller produktion.")

    is_push_draft = raw.startswith("wpreview:push_draft:")
    plan_id = raw.split(":", 2)[2]
    if not REVIEW_PLAN_ID_RE.fullmatch(plan_id):
        return BotReply(text=with_recovery("Okänd knapp.", footer=FOOTER_UNKNOWN_CALLBACK))
    blocked = handler._mutations_blocked_reply()
    if blocked:
        return blocked
    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WP_OPS_ADMIN)
        require_permission(actor_obj, Permission.CODE_EDIT)
    except Exception:
        action_name = "Skicka WP-utkast" if is_push_draft else "Staging av granskningsplan"
        return BotReply(
            text=f"{action_name} kräver Oscar (WP_OPS_ADMIN och CODE_EDIT). Ingen ändring utfördes."
        )

    from ecom_ops.wp_ops.review_plan import ReviewPlans

    plans = ReviewPlans()
    plan = plans.get(plan_id)
    if plan is None:
        return BotReply(
            text=f"Staging skrevs inte (UnknownPlan). Granskningsplanen {plan_id} hittades inte."
        )
    site_id = str(plan.get("site_id") or "")
    prev = handler.store.get(chat_id) or {}
    session = dict(prev.get("session") or {})
    pinned = resolve_pinned_hashes(
        session,
        handler.proposal_store,
        chat_id,
        actor=actor,
        site_id=site_id,
        plan_id=plan_id,
    )
    if pinned is None:
        return BotReply(
            text=(
                f"Staging skrevs inte (MissingApprovalHash). Låst godkännandehash för {plan_id} "
                "saknas eller har gått ut — kör en ny förhandsvisning i chatten eller godkänn via dashboarden."
            )
        )
    base_sha, work_sha = pinned

    if is_push_draft:
        return _handle_wpreview_push_draft(
            site_id,
            plan_id,
            actor=actor,
            base_sha=base_sha,
            work_sha=work_sha,
            on_progress=on_progress,
        )

    tracker = _ProgressTracker(
        on_progress,
        step_id=f"wpreview:{plan_id}",
        tool="wpreview_stage",
        label="Staging-granskningsplan",
        detail="Skriver godkänd granskningsplan i staging",
        legacy_start="Skriver godkänd granskningsplan i staging...",
    )
    try:
        ws_status = plans.workspace_status(site_id, plan_id, actor=actor)
        changed_files = (
            list((ws_status.data or {}).get("changed_files") or []) if ws_status.ok else []
        )
        staged = plans.approve_staging(
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha,
            work_sha256=work_sha,
        )
    except Exception:
        tracker.fail("misslyckades", "Staging-granskningsplan: misslyckades.")
        raise

    if staged.ok and (staged.data or {}).get("executed") is True:
        tracker.complete("ändringar skrivna i staging", "Staging-granskningsplan: klar.")
        handler._clear_review_tracking(chat_id, session, plan_id)
        lines = [
            f"Ändringarna för {plan_id} är skrivna i staging ({site_id}, mock). Produktionen är oförändrad."
        ]
        file_lines = format_changed_files(changed_files)
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
    tracker.fail(f"skrevs inte ({error})", f"Staging-granskningsplan: misslyckades ({error}).")
    detail = str(staged.message or "").strip()
    msg = f"Staging skrevs inte ({error}). Ingen ändring utfördes."
    if detail and detail != error:
        msg = f"{msg}\n{detail}"
    return BotReply(text=msg)


def handle_wp_plans_command(actor: str, parts: list[str]) -> BotReply:
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
        _ready, ready_label = review_plan_ready(plans, item)
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


def handle_wp_plan_command(
    handler: Any, chat_id: str | int, actor: str, parts: list[str]
) -> BotReply:
    plan_id = parts[1].strip()
    if not REVIEW_PLAN_ID_RE.fullmatch(plan_id):
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
        list((ws_status.data or {}).get("changed_files") or []) if ws_status.ok else []
    )
    ready, ready_label = review_plan_ready(plans, raw_plan, changed_count=len(changed_files))
    can_stage = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(Permission.CODE_EDIT)
    artifacts = plans.list_artifacts(site_id, plan_id, actor=actor) if can_stage else []
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
        lines.extend(format_changed_files(changed_files))
    else:
        lines.append("Ändrade filer: inga ännu.")

    check_lines = format_check_rows(list(view.get("checks") or []))
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
        prev = handler.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        handler._pin_review_hashes(chat_id, session, actor=actor, plan=raw_plan)
        handler._persist_review_hashes(chat_id, session)
        return bot_reply(
            "\n".join(lines),
            actions=wpreview_stage_actions(
                plan_id,
                review_url=url,
                include_push_draft=isinstance(raw_plan.get("page_draft"), dict),
            ),
        )
    if ready and not can_stage:
        lines.append("Staging kräver Oscar (WP_OPS_ADMIN + CODE_EDIT).")
    return BotReply(text="\n".join(lines))


def handle_wp_context_command(
    handler: Any,
    chat_id: str | int,
    text: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BotReply | None:
    """Minimal user controls for the sticky, actor-scoped WordPress site."""
    match = re.fullmatch(r"/wp(?:@\w+)?(?:\s+(.*))?", text.strip(), re.I)
    if not match:
        return None
    parts = (match.group(1) or "").strip().split()
    action = parts[0].lower() if parts else "help"
    if action == "clear" and len(parts) == 1:
        handler._update_session_atomic(
            chat_id,
            pop_session_keys=("wp_ops_context", "wp_review_hashes", "active_wp_review"),
        )
        return BotReply(text="WordPress-sajtkontexten är rensad.")
    if action == "help" and len(parts) == 1:
        return BotReply(
            text="/wp sites · /wp use <site-id> · /wp plans [site-id] · /wp plan <plan-id> · /wp clear"
        )
    try:
        actor = handler._resolve_actor(chat_id)
    except (TelegramActorDenied, ChannelActorDenied):
        return handler._deny_actor()
    if action == "plans" and len(parts) in {1, 2}:
        return handle_wp_plans_command(actor, parts)
    if action == "plan" and len(parts) == 2:
        return handle_wp_plan_command(handler, chat_id, actor, parts)

    tools = WpAgentTools()
    if action == "sites" and len(parts) == 1:
        tracker = _ProgressTracker(
            on_progress,
            step_id="cmd:wp:sites",
            tool="wp_list_sites",
            label="WordPress-sajter",
            detail="Hämtar registrerade sajter",
            legacy_start="Hämtar WordPress-sajter...",
        )
        output = tools.execute(
            "wp_list_sites",
            {},
            actor=actor,
            channel=handler.channel,
            conversation_id=f"{handler.channel}:{chat_id}",
        )
        if output.data.get("ok") is not True:
            tracker.fail("saknar behörighet", "WordPress-sajter: saknar behörighet.")
            return BotReply(text="Du saknar rättighet att läsa WordPress-sajter.")
        sites = output.data.get("sites", [])
        tracker.complete(f"{len(sites)} sajter", f"WordPress-sajter: {len(sites)} sajter.")
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
        tracker = _ProgressTracker(
            on_progress,
            step_id=f"cmd:wp:use:{site_id}",
            tool="wp_site_status",
            label="WordPress-sajt",
            detail=f"Kontrollerar {site_id}",
            legacy_start=f"Kontrollerar WordPress-sajt {site_id}...",
        )
        output = tools.execute(
            "wp_site_status",
            {"site_id": site_id},
            actor=actor,
            channel=handler.channel,
            conversation_id=f"{handler.channel}:{chat_id}",
        )
        if output.data.get("ok") is not True:
            tracker.fail(
                "okänd sajt eller saknar behörighet",
                "WordPress-sajt: okänd eller saknar behörighet.",
            )
            return BotReply(text="Sajten är okänd eller du saknar rättighet att läsa den.")
        context = context_for_storage(
            actor=actor,
            channel=handler.channel,
            conversation_id=f"{handler.channel}:{chat_id}",
            site_id=output.site_id,
            operation_ids=output.operation_ids,
        )
        if context is None:
            tracker.fail("kunde inte väljas", "WordPress-sajt: kunde inte väljas.")
            return BotReply(text="WordPress-sajten kunde inte väljas.")
        handler._update_session_atomic(chat_id, session_updates={"wp_ops_context": context})
        tracker.complete(f"aktiv sajt: {site_id}", f"Aktiv WordPress-sajt: {site_id}.")
        return BotReply(
            text=f"Aktiv WordPress-sajt: {site_id}. Fråga om status, backup eller uppdateringar."
        )

    return BotReply(
        text="Använd /wp sites, /wp use <site-id>, /wp plans [site-id], /wp plan <plan-id> eller /wp clear."
    )


__all__ = [
    "format_changed_files",
    "format_check_rows",
    "handle_wp_context_command",
    "handle_wp_plan_command",
    "handle_wp_plans_command",
    "handle_wpfile_callback",
    "handle_wpreview_callback",
    "review_plan_ready",
    "wpreview_stage_actions",
]
