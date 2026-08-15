"""Case ingest from mail + approve/send drafts (Cases 2.0)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ecom_ops.actions.mail import MailService
from ecom_ops.actions.support import SupportService
from ecom_ops.cases.results import ACTIVE_CASE_STATUSES, CaseActionResult, IngestResult
from ecom_ops.cases.store import Case, CaseStore
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.mail import MailClient, MailMessage
from ecom_ops.integrations.mail_threading import assemble_outbound_thread_headers
from ecom_ops.order_context import (
    draft_has_order_block,
    resolve_order_context,
)
from ecom_ops.profile import load_profile
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.runtime_profile import null_send_active
from ecom_ops.telemetry import Telemetry, default_telemetry

_ACTIVE = ACTIVE_CASE_STATUSES
REGENERATE_COOLDOWN_SEC = 60
_log = logging.getLogger("ecom_ops.cases")


def _edit_distance_ratio(a: str, b: str) -> float:
    """Normalized Levenshtein distance in [0, 1] (1 = totally different)."""
    s1, s2 = a or "", b or ""
    if s1 == s2:
        return 0.0
    n, m = len(s1), len(s2)
    if n == 0 or m == 0:
        return 1.0
    # Bound work for long drafts
    if n * m > 250_000:
        # Cheap proxy: char set / length delta
        return min(1.0, abs(n - m) / max(n, m) + (0.0 if s1[:80] == s2[:80] else 0.5))
    prev = list(range(m + 1))
    for i, c1 in enumerate(s1, 1):
        cur = [i]
        for j, c2 in enumerate(s2, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (c1 != c2)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[m] / max(n, m)


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        raw = str(iso).replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - created).total_seconds())
    except Exception:
        return None


def _enrich_draft_with_order(
    draft: str | None,
    order_id: str | None,
    *,
    use_mock: bool | None = None,
    order_context: str | None = None,
    domain: str | None = None,
) -> str:
    base = (draft or "").strip()
    if not order_id:
        return base
    if draft_has_order_block(base, order_id):
        return base
    block = (order_context or "").strip()
    if not block:
        block = (
            resolve_order_context(order_id, use_mock=use_mock, domain=domain) or ""
        )
    if not block:
        return base
    if block in base:
        return base
    return f"{block}\n\n{base}"


def _outbound_thread_headers(
    case: Case, store: CaseStore
) -> tuple[str | None, str | None]:
    """Build In-Reply-To / References for an outbound case reply."""
    msgs = store.messages(case.id)
    inbound = [m for m in msgs if m.direction == "inbound"]
    parent: str | None = None
    refs_hdr: str | None = None
    in_reply: str | None = None
    if inbound:
        last = inbound[-1]
        parent = (last.message_id or case.message_id or "").strip() or None
        refs_hdr = last.references_header
        in_reply = last.in_reply_to
    else:
        parent = (case.message_id or "").strip() or None
    return assemble_outbound_thread_headers(
        parent=parent,
        references_header=refs_hdr,
        in_reply_to=in_reply,
    )


class CaseService:
    def __init__(
        self,
        store: CaseStore | None = None,
        *,
        mail: MailService | None = None,
        support: SupportService | None = None,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
        mail_client: MailClient | None = None,
    ) -> None:
        self.store = store or CaseStore()
        self.mail = mail or MailService(client=mail_client)
        self.support = support or SupportService()
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation
        self._injected_mail_client = mail_client
        self._shutdown_requested = False  # P6.6: graceful shutdown flag

    def request_shutdown(self) -> None:
        """Signal the service to stop polling gracefully (P6.6)."""
        self._shutdown_requested = True

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def evaluate_auto_send_eligibility(
        self,
        case: Case | dict[str, Any],
        *,
        auto_sends_today: int = 0,
    ) -> bool:
        """Rails-only checkpoint: eligibility for a future Oscar auto-send experiment.

        Never sends mail. ``poll`` / ingest must not call this to dispatch outbound
        mail — human ``approve_and_send`` remains the live path while
        ``auto_send_enabled`` defaults to false (and even when True until an
        experiment wires a sender).
        """
        from ecom_ops.cases.auto_send import should_auto_send

        if isinstance(case, Case):
            category = case.category
            confidence = float(case.classify_confidence or 0.0)
            order_id = case.order_id
            escalated = case.status == "escalated" or bool(case.escalation_id)
        else:
            category = str(case.get("category") or "")
            confidence = float(case.get("classify_confidence") or 0.0)
            order_id = case.get("order_id")
            escalated = (
                str(case.get("status") or "") == "escalated"
                or bool(case.get("escalation_id"))
            )
        return should_auto_send(
            category=category,
            confidence=confidence,
            order_id=order_id,
            escalated=escalated,
            auto_sends_today=auto_sends_today,
        )

    def _maybe_record_shadow(self, case: Case) -> Case:
        """Under null-send: persist FU9 would-have decision; never sends mail."""
        if not null_send_active():
            return case
        from ecom_ops.cases.auto_send import AutoSendDayCounter, explain_auto_send

        escalated = case.status == "escalated" or bool(case.escalation_id)
        eligible, reason = explain_auto_send(
            category=case.category,
            confidence=float(case.classify_confidence or 0.0),
            order_id=case.order_id,
            escalated=escalated,
            auto_sends_today=AutoSendDayCounter().count_today(),
        )
        deny = None if eligible else reason
        updated = self.store.set_shadow_decision(
            case.id, eligible=eligible, deny_reason=deny
        )
        self.telemetry.record(
            action="case_shadow_decision",
                site=case.site or load_profile().customer,
            case_id=case.id,
            meta={
                "case_id": case.id,
                "shadow_eligible": eligible,
                "shadow_deny_reason": deny or "eligible",
                "mailbox_id": case.mailbox_id,
            },
        )
        return updated or case

    def poll(
        self,
        *,
        limit_per_mailbox: int = 20,
        actor: Actor | str | None = None,
        use_mock: bool | None = None,
    ) -> IngestResult:
        from ecom_ops.cases.ingest import run_poll

        return run_poll(
            self,
            limit_per_mailbox=limit_per_mailbox,
            actor=actor,
            use_mock=use_mock,
        )

    def _maybe_escalate(self, case: Case, support: Any) -> Case:
        if not getattr(support, "escalated", False):
            return case
        if case.escalation_id:
            return case
        ticket_id = getattr(support, "ticket_id", None)
        if not ticket_id:
            ticket = self.escalation.escalate_critical(
                f"Case threaded escalate: {case.subject[:80]}",
                details={"case_id": case.id, "category": case.category},
            )
            ticket_id = ticket.id
        updated = self.store.set_escalation(case.id, ticket_id)
        return updated or case

    def _best_effort_mark_read(self, client: MailClient, msg: MailMessage) -> None:
        uid = (msg.uid or "").strip()
        if not uid:
            return
        try:
            client.mark_read(uid, folder="INBOX")
        except Exception as exc:
            self.telemetry.record(
                action="case_mark_read_error",
                site=load_profile().customer,
                meta={"uid": uid, "error": str(exc)[:200]},
            )

    def list_open(self, *, limit: int = 50) -> list[Case]:
        return self.store.list_cases(status="open,escalated", limit=limit)

    def get(self, case_id: str) -> Case | None:
        return self.store.get(case_id)

    def next_in_queue(
        self,
        after_id: str,
        *,
        status: str = "open,escalated",
        mailbox_id: str | None = None,
        category: str | None = None,
        suggest_only: bool = False,
        limit: int = 100,
    ) -> Case | None:
        """Return the next case after ``after_id`` using list-view sort order.

        Sort: escalated → high priority → suggest_approve → newest first.
        Used by dashboard "Godkänn & nästa" / "Nästa".
        """
        rows = self.store.list_cases(
            status=status if status != "all" else None,
            mailbox_id=mailbox_id or None,
            category=category or None,
            suggest_approve=True if suggest_only else None,
            limit=limit,
        )
        rows.sort(key=lambda c: c.created_at or "", reverse=True)
        rows.sort(key=lambda c: 0 if getattr(c, "suggest_approve", False) else 1)
        rows.sort(key=lambda c: 0 if (c.priority or "") == "high" else 1)
        rows.sort(key=lambda c: 0 if c.status == "escalated" else 1)
        found = False
        for c in rows:
            if found:
                return c
            if c.id == after_id:
                found = True
        return None

    def save_draft(
        self,
        case_id: str,
        body: str,
        *,
        actor: Actor | str | None = None,
    ) -> CaseActionResult:
        from ecom_ops.cases.drafting import save_draft as _save

        return _save(self, case_id, body, actor=actor)

    def regenerate_draft(
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        use_mock: bool | None = None,
    ) -> CaseActionResult:
        from ecom_ops.cases.drafting import regenerate_draft as _regen

        return _regen(self, case_id, actor=actor, use_mock=use_mock)

    def _inbound_text_for_regen(self, case: Case) -> tuple[str, str]:
        msgs = self.store.messages(case.id)
        inbound = [m for m in msgs if (m.direction or "") == "inbound"]
        if inbound:
            last = inbound[-1]
            body = (last.body or "").strip()
            subject = (last.subject or case.subject or "").strip()
            if body:
                return body, subject
        # Fallback: subject + empty (still classifiable)
        return (case.subject or "").strip(), case.subject or ""

    def _patch_case_after_regen(
        self,
        case_id: str,
        *,
        draft: str,
        draft_before_regen: str,
        category: str,
        order_id: str | None,
        classify_confidence: float | None,
        classify_method: str | None,
        suggest_approve: bool,
    ) -> Case | None:
        """Update draft + AI fields without inserting phantom messages."""
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self.store._conn() as conn:
            conn.execute(
                """
                UPDATE cases SET
                    draft_reply = ?,
                    draft_before_regen = ?,
                    draft_regenerated_at = ?,
                    category = ?,
                    order_id = COALESCE(?, order_id),
                    classify_confidence = ?,
                    classify_method = ?,
                    suggest_approve = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    draft,
                    draft_before_regen,
                    now,
                    category,
                    order_id,
                    classify_confidence,
                    classify_method,
                    1 if suggest_approve else 0,
                    now,
                    case_id,
                ),
            )
        return self.store.get(case_id)

    def close(
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        reason: str | None = None,
    ) -> CaseActionResult:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.CASE_REPLY)
            case = self.store.get(case_id)
            if not case:
                return CaseActionResult(ok=False, message="Case not found")
            if case.status == "closed":
                return CaseActionResult(
                    ok=True, message="Already closed", case=case.to_dict()
                )
            updated = self.store.close(case_id)
            self.telemetry.record(
                action="case_closed",
                site=case.site,
                case_id=case_id,
                meta={
                    "case_id": case_id,
                    "actor": actor_obj.name,
                    "reason": (reason or "")[:200],
                },
            )
            from ecom_ops.audit import log_action

            log_action(
                actor=actor_obj.name,
                action="case_close",
                target="case",
                target_id=case_id,
                details={"reason": (reason or "")[:200]},
            )
            return CaseActionResult(
                ok=True,
                message="Case closed",
                case=updated.to_dict() if updated else case.to_dict(),
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Case close denied for {actor_obj.name}",
                details={"error": str(exc), "case_id": case_id},
            )
            return CaseActionResult(
                ok=False,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )

    def bulk_close(
        self,
        case_ids: list[str],
        *,
        actor: Actor | str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Close multiple cases in one call (bulk triage).

        Returns a summary dict: {ok, closed, skipped, errors, results}.
        Each result in ``results`` is a CaseActionResult.to_dict().
        """
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        closed = 0
        skipped = 0
        errors = 0
        results: list[dict[str, Any]] = []
        for cid in case_ids:
            cid = (cid or "").strip()
            if not cid:
                continue
            r = self.close(cid, actor=actor_obj, reason=reason)
            results.append(r.to_dict())
            if r.ok:
                if r.message == "Already closed":
                    skipped += 1
                else:
                    closed += 1
            else:
                errors += 1
        return {
            "ok": errors == 0,
            "closed": closed,
            "skipped": skipped,
            "errors": errors,
            "results": results,
            "message": f"Bulk close: {closed} closed, {skipped} already closed, {errors} errors",
        }

    def approve_and_send(
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        body_override: str | None = None,
    ) -> CaseActionResult:
        from ecom_ops.cases.approve import approve_and_send as _approve

        return _approve(self, case_id, actor=actor, body_override=body_override)
