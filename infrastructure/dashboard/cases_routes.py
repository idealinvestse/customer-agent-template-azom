"""Cases queue, poll, bulk-close, and detail (HITL approve)."""

from __future__ import annotations

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import (
    _case_queue_query,
    _dashboard_context,
    _flash_q,
    _is_mock,
    _queue_filter_from_request,
    _redirect_next_or_list,
)


def register_cases_routes(app: Flask) -> None:
    @app.route("/cases")
    @_auth_required
    def cases_list():
        from ecom_ops.cases.mailboxes import enabled_mailboxes
        from ecom_ops.cases.service import CaseService

        status = request.args.get("status", "open,escalated")
        mailbox_id = request.args.get("mailbox") or None
        category = request.args.get("category") or None
        suggest_raw = (request.args.get("suggest") or "").strip().lower()
        suggest_only = suggest_raw in {"1", "true", "yes", "on"}
        svc = CaseService()
        rows = svc.store.list_cases(
            status=status if status != "all" else None,
            mailbox_id=mailbox_id or None,
            category=category or None,
            suggest_approve=True if suggest_only else None,
            limit=100,
        )
        rows.sort(key=lambda c: c.created_at or "", reverse=True)
        rows.sort(key=lambda c: 0 if getattr(c, "suggest_approve", False) else 1)
        rows.sort(key=lambda c: 0 if (c.priority or "") == "high" else 1)
        rows.sort(key=lambda c: 0 if c.status == "escalated" else 1)
        return render_template(
            "cases.html",
            **_dashboard_context(
                cases=rows,
                status_filter=status,
                mailbox_filter=mailbox_id or "",
                category_filter=category or "",
                suggest_filter=suggest_only,
                mailboxes=enabled_mailboxes(),
            ),
        )

    @app.route("/cases/poll", methods=["POST"])
    @_auth_required
    def cases_poll():
        from ecom_ops.cases.service import CaseService

        result = CaseService().poll(actor=g.actor["name"], use_mock=_is_mock() or None)
        if result.ok:
            msg = f"Skapade {result.created} ärenden"
            detail = str(getattr(result, "message", "") or "")
            if "PARTIAL" in detail.upper():
                return redirect(
                    url_for("cases_list")
                    + f"?{_flash_q(err=f'{msg} — PARTIAL: {detail[:200]}')}"
                )
            return redirect(url_for("cases_list") + f"?{_flash_q(msg)}")
        return redirect(url_for("cases_list") + f"?{_flash_q(err=result.message)}")

    @app.route("/cases/bulk-close", methods=["POST"])
    @_auth_required
    def cases_bulk_close():
        from ecom_ops.cases.service import CaseService

        ids_raw = request.form.get("case_ids", "")
        case_ids = [c.strip() for c in ids_raw.split(",") if c.strip()]
        if not case_ids:
            return redirect(url_for("cases_list") + "?err=Inga+ärenden+valda")
        result = CaseService().bulk_close(
            case_ids, actor=g.actor["name"], reason="bulk-close-dashboard"
        )
        if result["ok"]:
            return redirect(
                url_for("cases_list")
                + f"?msg=Stängde+{result['closed']}+ärenden"
            )
        return redirect(
            url_for("cases_list")
            + f"?err={result['message']}"
        )

    @app.route("/cases/<case_id>", methods=["GET", "POST"])
    @_auth_required
    def case_detail(case_id: str):
        from ecom_ops.cases.service import CaseService

        svc = CaseService()
        filt = _queue_filter_from_request()
        if request.method == "POST":
            action = request.form.get("action", "reply")
            body = request.form.get("body") or ""
            go_next = action in {"reply_next", "close_next", "next_only"}
            if action == "next_only":
                return _redirect_next_or_list(svc, case_id, filt=filt, msg="Nästa")
            if action in {"reply", "reply_next"}:
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
                    case_id, actor=g.actor["name"], body_override=body or None
                )
                if result.ok:
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
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action == "save_draft":
                result = svc.save_draft(case_id, body, actor=g.actor["name"])
                if result.ok:
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q('Draft sparad')}"
                    )
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
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
                )
            if action == "regenerate":
                result = svc.regenerate_draft(
                    case_id, actor=g.actor["name"], use_mock=_is_mock() or None
                )
                if result.ok:
                    return redirect(
                        url_for("case_detail", case_id=case_id)
                        + f"?{_case_queue_query(filt)}&{_flash_q('Utkast regenererat')}"
                    )
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
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
            ),
        )
