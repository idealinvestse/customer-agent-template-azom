"""Cases queue, poll, bulk-close, and detail (HITL approve)."""

from __future__ import annotations

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required, _dashboard_actor
from support import (
    _case_queue_query,
    _dashboard_context,
    _flash,
    _flash_q,
    _is_mock,
    _queue_filter_from_request,
    _redirect_next_or_list,
)

_ALLOWED_STATUSES = frozenset(
    {"open,escalated", "open", "escalated", "replied", "closed", "sending", "all"}
)
_ALLOWED_CASE_ACTIONS = frozenset(
    {
        "reply",
        "reply_next",
        "save_draft",
        "close",
        "close_next",
        "next_only",
        "regenerate",
        "escalate",
        "reopen",
    }
)
_QUEUE_LIMIT = 100


def _known_categories() -> list[str]:
    from ecom_ops.actions.support import SupportCategory

    return [c.value for c in SupportCategory]


def _count_matching(
    svc: object,
    *,
    status: str,
    mailbox_id: str | None,
    category: str | None,
    suggest_only: bool,
) -> int:
    sql = "SELECT COUNT(*) FROM cases WHERE 1=1"
    params: list[object] = []
    if status and status != "all":
        parts = [s.strip() for s in status.split(",") if s.strip()]
        if parts:
            placeholders = ",".join("?" * len(parts))
            sql += f" AND status IN ({placeholders})"
            params.extend(parts)
    if mailbox_id:
        sql += " AND mailbox_id = ?"
        params.append(mailbox_id)
    if category:
        sql += " AND category = ?"
        params.append(category)
    if suggest_only:
        sql += " AND suggest_approve = 1"
    with svc.store._conn() as conn:  # type: ignore[attr-defined]
        row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def register_cases_routes(app: Flask) -> None:
    @app.route("/cases")
    @_auth_required
    def cases_list():
        from ecom_ops.cases.mailboxes import enabled_mailboxes
        from ecom_ops.cases.service import CaseService

        raw_status = (request.args.get("status") or "open,escalated").strip()
        status = raw_status if raw_status in _ALLOWED_STATUSES else "open,escalated"
        mailbox_id = (request.args.get("mailbox") or "").strip() or None
        category = (request.args.get("category") or "").strip() or None
        suggest_raw = (request.args.get("suggest") or "").strip().lower()
        suggest_only = suggest_raw in {"1", "true", "yes", "on"}
        svc = CaseService()
        rows = svc.store.list_cases(
            status=status if status != "all" else None,
            mailbox_id=mailbox_id,
            category=category,
            suggest_approve=True if suggest_only else None,
            limit=_QUEUE_LIMIT + 1,
        )
        truncated = len(rows) > _QUEUE_LIMIT
        rows = rows[:_QUEUE_LIMIT]
        total_matching = (
            _count_matching(
                svc,
                status=status,
                mailbox_id=mailbox_id,
                category=category,
                suggest_only=suggest_only,
            )
            if truncated
            else len(rows)
        )
        return render_template(
            "cases.html",
            **_dashboard_context(
                cases=rows,
                status_filter=status,
                mailbox_filter=mailbox_id or "",
                category_filter=category or "",
                suggest_filter=suggest_only,
                mailboxes=enabled_mailboxes(),
                categories=_known_categories(),
                queue_limit=_QUEUE_LIMIT,
                total_matching=total_matching,
                truncated=truncated,
            ),
        )

    @app.route("/cases/poll", methods=["POST"])
    @_auth_required
    def cases_poll():
        from ecom_ops.cases.service import CaseService

        filt = _queue_filter_from_request()
        q = _case_queue_query(filt)
        result = CaseService().poll(actor=g.actor["name"], use_mock=_is_mock() or None)
        if result.ok:
            msg = f"Skapade {result.created} ärenden"
            detail = str(getattr(result, "message", "") or "")
            if "PARTIAL" in detail.upper():
                err_msg = f"{msg} — PARTIAL: {detail[:200]}"
                _flash(err_msg, "warn")
                return redirect(url_for("cases_list") + f"?{q}&{_flash_q(err=err_msg)}")
            _flash(msg, "ok")
            return redirect(url_for("cases_list") + f"?{q}&{_flash_q(msg)}")
        _flash(result.message, "error")
        return redirect(url_for("cases_list") + f"?{q}&{_flash_q(err=result.message)}")

    @app.route("/cases/bulk-close", methods=["POST"])
    @_auth_required
    def cases_bulk_close():
        from ecom_ops.cases.service import CaseService

        filt = _queue_filter_from_request()
        q = _case_queue_query(filt)
        selected: list[str] = []
        seen: set[str] = set()
        for cid in request.form.getlist("case_id_list"):
            val = cid.strip()
            if val and val not in seen:
                seen.add(val)
                selected.append(val)
        ids_raw = request.form.get("case_ids", "")
        for cid in ids_raw.split(","):
            val = cid.strip()
            if val and val not in seen:
                seen.add(val)
                selected.append(val)
        if not selected:
            _flash("Inga ärenden valda", "warn")
            return redirect(url_for("cases_list") + f"?{q}&{_flash_q(err='Inga ärenden valda')}")
        result = CaseService().bulk_close(
            selected, actor=g.actor["name"], reason="bulk-close-dashboard"
        )
        if result["ok"]:
            msg = f"Stängde {result['closed']} ärenden"
            _flash(msg, "ok")
            return redirect(url_for("cases_list") + f"?{q}&{_flash_q(msg)}")
        _flash(result["message"], "error")
        return redirect(url_for("cases_list") + f"?{q}&{_flash_q(err=result['message'])}")

    @app.route("/cases/<case_id>", methods=["GET", "POST"])
    @_auth_required
    def case_detail(case_id: str):
        from ecom_ops.cases.service import CaseService
        from ecom_ops.rbac import AccessDenied, Permission, require_permission

        svc = CaseService()
        filt = _queue_filter_from_request()
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            if action not in _ALLOWED_CASE_ACTIONS:
                err_msg = "Ogiltig eller saknad åtgärd"
                _flash(err_msg, "error")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=err_msg)}"
                )
            body = request.form.get("body") or ""
            go_next = action in {"reply_next", "close_next", "next_only"}
            if action == "next_only":
                return _redirect_next_or_list(svc, case_id, filt=filt, msg="Nästa")
            if action in {"reply", "reply_next"}:
                if not body.strip():
                    err_msg = "Svarstext saknas — fyll i ett svar innan du skickar."
                    _flash(err_msg, "error")
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q(err=err_msg)}"
                    )
                nxt_before = None
                if go_next or action == "reply_next":
                    nxt_before = svc.next_in_queue(
                        case_id,
                        status=str(filt.get("status") or "open,escalated"),
                        mailbox_id=str(filt.get("mailbox") or "") or None,
                        category=str(filt.get("category") or "") or None,
                        suggest_only=bool(filt.get("suggest_only")),
                    )
                result = svc.approve_and_send(
                    case_id, actor=g.actor["name"], body_override=body
                )
                if result.ok:
                    _flash("Skickat", "ok")
                    if go_next or action == "reply_next":
                        q = _case_queue_query(filt)
                        if nxt_before:
                            return redirect(
                                url_for("case_detail", case_id=nxt_before.id)
                                + f"?{q}&{_flash_q('Skickat')}"
                            )
                        return redirect(
                            url_for("cases_list") + f"?{q}&{_flash_q('Skickat')}"
                        )
                    q = _case_queue_query(filt)
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{q}&{_flash_q('Skickat')}"
                    )
                _flash(result.message, "error")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action == "save_draft":
                result = svc.save_draft(case_id, body, actor=g.actor["name"])
                if result.ok:
                    _flash("Draft sparad", "ok")
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q('Draft sparad')}"
                    )
                _flash(result.message, "error")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action in {"close", "close_next"}:
                nxt_before = None
                if go_next or action == "close_next":
                    nxt_before = svc.next_in_queue(
                        case_id,
                        status=str(filt.get("status") or "open,escalated"),
                        mailbox_id=str(filt.get("mailbox") or "") or None,
                        category=str(filt.get("category") or "") or None,
                        suggest_only=bool(filt.get("suggest_only")),
                    )
                result = svc.close(case_id, actor=g.actor["name"], reason="dashboard")
                if result.ok:
                    _flash("Stängt", "ok")
                    if go_next or action == "close_next":
                        q = _case_queue_query(filt)
                        if nxt_before:
                            return redirect(
                                url_for("case_detail", case_id=nxt_before.id)
                                + f"?{q}&{_flash_q('Stängt')}"
                            )
                        return redirect(
                            url_for("cases_list") + f"?{q}&{_flash_q('Stängt')}"
                        )
                    return redirect(
                        url_for("cases_list")
                        + f"?{_case_queue_query(filt)}&{_flash_q('Stängt')}"
                    )
                _flash(result.message, "error")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action == "regenerate":
                result = svc.regenerate_draft(
                    case_id, actor=g.actor["name"], use_mock=_is_mock() or None
                )
                if result.ok:
                    _flash("Utkast regenererat", "ok")
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q('Utkast regenererat')}"
                    )
                _flash(result.message, "error")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action == "escalate":
                try:
                    require_permission(_dashboard_actor(g.actor), Permission.CASE_REPLY)
                except AccessDenied:
                    return Response("Saknar behörighet", 403)
                case_obj = svc.get(case_id)
                if not case_obj:
                    return Response("Case not found", 404)
                reason = (request.form.get("reason") or "dashboard-escalate").strip()[:200]
                ticket = svc.escalation.escalate_critical(
                    f"Case escalated from dashboard: {case_obj.subject[:80]}",
                    details={
                        "case_id": case_obj.id,
                        "category": case_obj.category,
                        "actor": g.actor["name"],
                        "reason": reason,
                    },
                )
                svc.store.set_escalation(case_id, ticket.id)
                from ecom_ops.audit import log_action

                log_action(
                    actor=g.actor["name"],
                    action="case_escalate",
                    target="case",
                    target_id=case_id,
                    details={"ticket_id": ticket.id, "reason": reason},
                )
                _flash("Ärende eskalerat till Oscar", "ok")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q('Ärende eskalerat till Oscar')}"
                )
            if action == "reopen":
                try:
                    require_permission(_dashboard_actor(g.actor), Permission.CASE_REPLY)
                except AccessDenied:
                    return Response("Saknar behörighet", 403)
                case_obj = svc.get(case_id)
                if not case_obj:
                    return Response("Case not found", 404)
                if case_obj.status == "sending":
                    err_msg = "Kan inte återöppna ett ärende som håller på att skickas."
                    _flash(err_msg, "error")
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q(err=err_msg)}"
                    )
                if case_obj.status in {"closed", "replied"}:
                    svc.store.set_status(case_id, "open")
                    from ecom_ops.audit import log_action

                    log_action(
                        actor=g.actor["name"],
                        action="case_reopen",
                        target="case",
                        target_id=case_id,
                        details={"from_status": case_obj.status},
                    )
                    _flash("Ärende återöppnat", "ok")
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q('Ärende återöppnat')}"
                    )
                _flash("Ärendet är redan aktivt", "info")
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(info='Ärendet är redan aktivt')}"
                )

        case = svc.get(case_id)
        if not case:
            resolved = svc.store.resolve_id_prefix(case_id)
            if resolved:
                q = request.query_string.decode("utf-8", errors="replace")
                target = url_for("case_detail", case_id=resolved.id)
                if q:
                    target = f"{target}?{q}"
                return redirect(target)
            return Response("Case not found", 404)
        msgs = svc.store.messages(case_id)
        order_panel = None
        if case.order_id:
            from ecom_ops.order_context import resolve_order_panel, woo_domain_from_market

            order_panel = resolve_order_panel(
                case.order_id,
                use_mock=_is_mock() or None,
                domain=woo_domain_from_market(case.market),
            )
        return render_template(
            "case_detail.html",
            **_dashboard_context(
                case=case,
                messages=msgs,
                order_panel=order_panel,
                queue_status=filt["status"],
                queue_mailbox=filt.get("mailbox") or "",
                queue_category=filt.get("category") or "",
                queue_suggest=bool(filt.get("suggest_only")),
                case_language=case.language,
                case_market=case.market,
                case_site=case.site,
                classify_method=case.classify_method,
                categories=_known_categories(),
            ),
        )
