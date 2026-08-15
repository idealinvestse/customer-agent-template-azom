"""Oscar-only admin routes."""

from __future__ import annotations

import sqlite3

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from auth import _oscar_required
from secret_probes import run_all_probes, run_probe
from settings_store import (
    EDITABLE_SECRET_KEYS,
    apply_env_overlays,
    load_settings_view,
    resolve_escalation,
    save_secrets,
    secrets_status,
)
from support import (
    _dashboard_context,
    _data_dir,
    _load_probe_last,
    _merge_probe_last,
    _save_probe_last,
    _tail_jsonl,
)


def register_oscar_routes(app: Flask) -> None:
    @app.route("/oscar")
    @_oscar_required
    def oscar_home():
        esc = _tail_jsonl(_data_dir() / "escalations.jsonl", 100)
        open_esc_rows = [e for e in esc if e.get("status", "open") == "open"]
        secs = secrets_status()
        present = sum(1 for s in secs if s["present"])
        missing = sum(1 for s in secs if not s["present"])
        from ecom_ops.oauth.gmail import GmailOAuthStore

        store = GmailOAuthStore()
        return render_template(
            "oscar.html",
            **_dashboard_context(
                open_escalation_tickets=open_esc_rows,
                open_count=len(open_esc_rows),
                secrets_present=present,
                secrets_missing=missing,
                gmail_connected=store.has_tokens(),
                settings=load_settings_view(),
            ),
        )

    @app.route("/oscar/secrets", methods=["GET", "POST"])
    @_oscar_required
    def oscar_secrets():
        if request.method == "POST":
            updates = {}
            for key in EDITABLE_SECRET_KEYS:
                if key in request.form:
                    updates[key] = request.form.get(key, "")
            saved = save_secrets(updates)
            msg = f"Sparade+{len(saved)}+nycklar" if saved else "Inga+ändringar"
            return redirect(url_for("oscar_secrets") + f"?msg={msg}")
        last = _load_probe_last()
        results = (last or {}).get("results") or [r.to_dict() for r in run_all_probes()]
        if not last:
            live = run_all_probes()
            _save_probe_last(live)
            results = [r.to_dict() for r in live]
        return render_template(
            "oscar_secrets.html",
            **_dashboard_context(
                secrets=secrets_status(),
                editable_keys=EDITABLE_SECRET_KEYS,
                probe_results=results,
            ),
        )

    @app.route("/oscar/secrets/test", methods=["POST"])
    @_oscar_required
    def oscar_secrets_test():
        apply_env_overlays()
        probe = (request.form.get("probe") or "all").strip().lower()
        if probe in {"", "all"}:
            results = run_all_probes()
            _save_probe_last(results)
        else:
            single = run_probe(probe)
            results = [single]
            _merge_probe_last(single)
        ok_n = sum(1 for r in results if r.status == "ok")
        err_n = sum(1 for r in results if r.status in {"error", "missing"})
        msg = f"Test+klart:+{ok_n}+ok,+{err_n}+problem"
        if request.headers.get("Accept") == "application/json" or request.is_json:
            return jsonify(
                {
                    "ok": err_n == 0,
                    "results": [r.to_dict() for r in results],
                }
            )
        return render_template(
            "oscar_secrets.html",
            **_dashboard_context(
                secrets=secrets_status(),
                editable_keys=EDITABLE_SECRET_KEYS,
                probe_results=[r.to_dict() for r in results],
                flash=f"ok:{msg.replace('+', ' ')}",
            ),
        )

    @app.route("/oscar/escalations", methods=["GET", "POST"])
    @_oscar_required
    def oscar_escalations():
        if request.method == "POST":
            ticket_id = request.form.get("ticket_id", "").strip()
            show = request.args.get("show", "open")
            if ticket_id and resolve_escalation(ticket_id):
                return redirect(
                    url_for("oscar_escalations", show=show) + "?msg=Markerad+som+löst"
                )
            return redirect(
                url_for("oscar_escalations", show=show) + "?err=Ticket+hittades+inte"
            )
        show = request.args.get("show", "open")
        rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
        if show != "all":
            rows = [e for e in rows if e.get("status", "open") == "open"]
        return render_template(
            "oscar_escalations.html",
            **_dashboard_context(rows=rows, show_filter=show),
        )

    @app.route("/oscar/gdpr/delete", methods=["POST"])
    @_oscar_required
    def oscar_gdpr_delete():
        email = (request.form.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "message": "email required"}), 400
        from ecom_ops.security import validate_email

        try:
            validate_email(email)
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        from ecom_ops.audit import log_action

        db_path = _data_dir() / "cases.db"
        if not db_path.is_file():
            return jsonify({"ok": False, "message": "cases.db not found"}), 404
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id FROM cases WHERE from_addr = ?", (email,)
            ).fetchall()
            case_ids = [r[0] for r in rows]
            if not case_ids:
                return jsonify({"ok": True, "deleted": 0, "message": "No cases found for this email"})
            placeholders = ",".join("?" * len(case_ids))
            conn.execute(f"DELETE FROM case_messages WHERE case_id IN ({placeholders})", case_ids)
            conn.execute(f"DELETE FROM cases WHERE id IN ({placeholders})", case_ids)
            conn.commit()
            log_action(
                actor=g.actor["name"],
                action="gdpr_delete",
                target="cases",
                target_id=email,
                details={"deleted_count": len(case_ids)},
            )
            return jsonify({"ok": True, "deleted": len(case_ids), "message": f"Deleted {len(case_ids)} cases for {email}"})
        except Exception as exc:
            conn.rollback()
            return jsonify({"ok": False, "message": str(exc)[:200]}), 500
        finally:
            conn.close()

    @app.route("/oscar/gdpr/export", methods=["GET"])
    @_oscar_required
    def oscar_gdpr_export():
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "message": "email required"}), 400
        db_path = _data_dir() / "cases.db"
        if not db_path.is_file():
            return jsonify({"ok": False, "message": "cases.db not found"}), 404
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cases = [dict(r) for r in conn.execute(
                "SELECT * FROM cases WHERE from_addr = ?", (email,)
            ).fetchall()]
            if not cases:
                return jsonify({"ok": True, "email": email, "cases": [], "messages": [], "message": "No data found"})
            case_ids = [c["id"] for c in cases]
            placeholders = ",".join("?" * len(case_ids))
            messages = [dict(r) for r in conn.execute(
                f"SELECT * FROM case_messages WHERE case_id IN ({placeholders})", case_ids
            ).fetchall()]
            from ecom_ops.audit import log_action

            log_action(
                actor=g.actor["name"],
                action="gdpr_export",
                target="cases",
                target_id=email,
                details={"case_count": len(cases)},
            )
            return jsonify({"ok": True, "email": email, "cases": cases, "messages": messages})
        finally:
            conn.close()
