"""Unified CLI for Azom ecom-ops V2."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ecom_ops import __version__
from ecom_ops.actions.mail import MailService
from ecom_ops.actions.order_status import OrderStatusService
from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.actions.ssh_ops import SSHOpsService
from ecom_ops.actions.support import SupportService
from ecom_ops.integrations.mail import client_from_env as mail_client_from_env
from ecom_ops.integrations.woocommerce import client_from_env


def _print(result: Any) -> int:
    data = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(data, dict):
        return 0 if data.get("ok", True) else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecom-ops",
        description=(
            "Azom ecom-ops V2: order-status, product-desc, support, SSH, mail "
            "(dashboard/OAuth/Telegram via separate entrypoints)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ecom-ops {__version__}",
    )
    parser.add_argument("--site", default="azom", help="Customer/site id")
    parser.add_argument(
        "--actor",
        default="agent",
        help="Actor name (jonatan|oscar|agent)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force mock integrations (no external network)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_order = sub.add_parser("order-status", help="Update WooCommerce order status")
    p_order.add_argument("--order-id", required=True)
    p_order.add_argument("--status", required=True)

    p_prod = sub.add_parser("product-desc", help="Generate product description")
    p_prod.add_argument("--product-id")
    p_prod.add_argument("--name")
    p_prod.add_argument("--features", default="")
    p_prod.add_argument("--language", default="sv")
    p_prod.add_argument("--publish", action="store_true")

    p_sup = sub.add_parser("support", help="Handle support message")
    p_sup.add_argument("--message", required=True)
    p_sup.add_argument("--email")
    p_sup.add_argument("--customer-name")
    p_sup.add_argument("--language", default="sv")

    p_ssh = sub.add_parser("ssh", help="Run allowlisted SSH / escalate critical")
    p_ssh.add_argument("--command", required=True, dest="ssh_command")
    p_ssh.add_argument("--host")

    p_health = sub.add_parser("ssh-health", help="Run SSH health checks")
    p_health.add_argument("--host")

    sub.add_parser("version", help="Print package version")
    sub.add_parser("status", help="Print runtime status (config + mock flags)")
    p_kpis = sub.add_parser(
        "kpis",
        help="Support-loop KPIs last N days (time-to-approve, edit distance)",
    )
    p_kpis.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default 7)",
    )
    p_smoke = sub.add_parser(
        "smoke",
        help="Opt-in integration smoke (requires AZOM_LIVE_SMOKE=1 or --live)",
    )
    p_smoke.add_argument(
        "--live",
        action="store_true",
        help="Force smoke even without AZOM_LIVE_SMOKE=1",
    )

    p_mail = sub.add_parser("mail", help="Send / fetch / reply email")
    mail_sub = p_mail.add_subparsers(dest="mail_command", required=True)

    p_mail_send = mail_sub.add_parser("send", help="Send an email")
    p_mail_send.add_argument("--to", required=True, help="Recipient (comma-separated ok)")
    p_mail_send.add_argument("--subject", required=True)
    p_mail_send.add_argument("--body", required=True)
    p_mail_send.add_argument("--cc", default="")
    p_mail_send.add_argument("--html-body", default="")
    p_mail_send.add_argument(
        "--provider",
        help="gmail|outlook|exchange_graph|generic_imap|generic_pop3",
    )

    p_mail_fetch = mail_sub.add_parser("fetch", help="Fetch inbox messages")
    p_mail_fetch.add_argument("--folder", default="INBOX")
    p_mail_fetch.add_argument("--limit", type=int, default=20)
    p_mail_fetch.add_argument(
        "--all",
        action="store_true",
        help="Fetch all messages (not only unread)",
    )
    p_mail_fetch.add_argument(
        "--provider",
        help="gmail|outlook|exchange_graph|generic_imap|generic_pop3",
    )

    p_mail_reply = mail_sub.add_parser("reply", help="Reply to a sender")
    p_mail_reply.add_argument("--to", required=True)
    p_mail_reply.add_argument("--subject", required=True)
    p_mail_reply.add_argument("--body", required=True)
    p_mail_reply.add_argument("--uid", dest="original_uid")
    p_mail_reply.add_argument("--html-body", default="")
    p_mail_reply.add_argument("--provider")

    p_cases = sub.add_parser("cases", help="Support cases from inbound mail")
    cases_sub = p_cases.add_subparsers(dest="cases_command", required=True)

    p_cases_poll = cases_sub.add_parser("poll", help="Fetch mailboxes and create cases")
    p_cases_poll.add_argument("--limit", type=int, default=20)

    p_cases_list = cases_sub.add_parser("list", help="List cases")
    p_cases_list.add_argument("--status", default="open")
    p_cases_list.add_argument("--limit", type=int, default=50)

    p_cases_show = cases_sub.add_parser("show", help="Show one case")
    p_cases_show.add_argument("--id", required=True, dest="case_id")

    p_cases_reply = cases_sub.add_parser(
        "reply", help="Approve draft and send reply"
    )
    p_cases_reply.add_argument("--id", required=True, dest="case_id")
    p_cases_reply.add_argument("--body", help="Override draft body")

    p_cases_close = cases_sub.add_parser("close", help="Close case without reply")
    p_cases_close.add_argument("--id", required=True, dest="case_id")
    p_cases_close.add_argument("--reason", default="")

    p_cases_draft = cases_sub.add_parser("draft", help="Save draft without sending")
    p_cases_draft.add_argument("--id", required=True, dest="case_id")
    p_cases_draft.add_argument("--body", required=True)

    cases_sub.add_parser(
        "regenerate", help="Regenerate draft from inbound (never sends)"
    ).add_argument("--id", required=True, dest="case_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mock:
        import os

        os.environ["AZOM_USE_MOCK"] = "1"

    # Defer Woo client — version/status/mail/cases must not require Woo secrets
    woo = None

    def _woo():
        nonlocal woo
        if woo is None:
            woo = client_from_env(use_mock=args.mock or None)
        return woo

    if args.command == "order-status":
        svc = OrderStatusService(woo=_woo())
        result = svc.update(
            order_id=args.order_id,
            status=args.status,
            site=args.site,
            actor=args.actor,
        )
        return _print(result)

    if args.command == "product-desc":
        svc = ProductDescService(woo=_woo())
        result = svc.generate(
            product_id=args.product_id,
            name=args.name,
            features=args.features,
            language=args.language,
            site=args.site,
            publish=args.publish,
            actor=args.actor,
        )
        return _print(result)

    if args.command == "support":
        svc = SupportService()
        result = svc.handle(
            args.message,
            customer_email=args.email,
            customer_name=args.customer_name,
            language=args.language,
            site=args.site,
            actor=args.actor,
        )
        return _print(result)

    if args.command == "ssh":
        svc = SSHOpsService(host=args.host)
        result = svc.run(args.ssh_command, site=args.site, actor=args.actor)
        return _print(result)

    if args.command == "ssh-health":
        svc = SSHOpsService(host=args.host)
        results = svc.health(site=args.site, actor=args.actor)
        payload = [r.to_dict() for r in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if all(r.ok for r in results) else 1

    if args.command == "version":
        print(json.dumps({"version": __version__, "package": "azom-ecom-ops"}, indent=2))
        return 0

    if args.command == "status":
        import os

        from ecom_ops.budget import budget_status
        from ecom_ops.config import load_app_config
        from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured
        from ecom_ops.ops_status import readiness_from_last_poll

        try:
            cfg = load_app_config()
            budget = budget_status()
            status = {
                "ok": True,
                "version": __version__,
                "mock": os.environ.get("AZOM_USE_MOCK", "").lower()
                in {"1", "true", "yes"},
                "customer": cfg.customer.customer,
                "domains": list(cfg.customer.domains),
                "gmail_oauth_configured": gmail_oauth_configured(),
                "gmail_tokens_stored": GmailOAuthStore().has_tokens(),
                "telegram_configured": bool(
                    os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                ),
                "readiness": readiness_from_last_poll(),
                "budget": budget,
            }
        except Exception as exc:
            status = {"ok": False, "version": __version__, "error": str(exc)}
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("ok") else 1

    if args.command == "smoke":
        from ecom_ops.smoke import run_live_smoke

        result = run_live_smoke(force=bool(getattr(args, "live", False)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", False) else 1

    if args.command == "kpis":
        from ecom_ops.kpis import support_kpis_last_days

        result = support_kpis_last_days(days=int(getattr(args, "days", 7) or 7))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "mail":
        provider = getattr(args, "provider", None)
        mail_client = mail_client_from_env(
            provider=provider, use_mock=args.mock or None
        )
        mail_svc = MailService(client=mail_client)

        if args.mail_command == "send":
            result = mail_svc.send(
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc or None,
                html_body=args.html_body or None,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        if args.mail_command == "fetch":
            result = mail_svc.fetch(
                folder=args.folder,
                unread_only=not args.all,
                limit=args.limit,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        if args.mail_command == "reply":
            result = mail_svc.reply(
                to=args.to,
                subject=args.subject,
                body=args.body,
                original_uid=args.original_uid,
                html_body=args.html_body or None,
                site=args.site,
                actor=args.actor,
            )
            return _print(result)

        parser.error(f"Unknown mail command: {args.mail_command}")
        return 2

    if args.command == "cases":
        from ecom_ops.cases.service import CaseService

        case_svc = CaseService()
        if args.cases_command == "poll":
            result = case_svc.poll(
                limit_per_mailbox=args.limit,
                actor=args.actor,
                use_mock=args.mock or None,
            )
            return _print(result)
        if args.cases_command == "list":
            cases = case_svc.store.list_cases(
                status=args.status or None, limit=args.limit
            )
            payload = {"ok": True, "count": len(cases), "cases": [c.to_dict() for c in cases]}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.cases_command == "show":
            case = case_svc.get(args.case_id)
            if not case:
                print(json.dumps({"ok": False, "message": "not found"}, indent=2))
                return 1
            msgs = [m.to_dict() for m in case_svc.store.messages(args.case_id)]
            print(
                json.dumps(
                    {"ok": True, "case": case.to_dict(), "messages": msgs},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.cases_command == "reply":
            result = case_svc.approve_and_send(
                args.case_id,
                actor=args.actor,
                body_override=args.body,
            )
            return _print(result)
        if args.cases_command == "close":
            result = case_svc.close(
                args.case_id,
                actor=args.actor,
                reason=args.reason or None,
            )
            return _print(result)
        if args.cases_command == "draft":
            result = case_svc.save_draft(
                args.case_id,
                args.body,
                actor=args.actor,
            )
            return _print(result)
        if args.cases_command == "regenerate":
            result = case_svc.regenerate_draft(
                args.case_id,
                actor=args.actor,
                use_mock=args.mock or None,
            )
            return _print(result)
        parser.error(f"Unknown cases command: {args.cases_command}")
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
